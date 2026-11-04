"""麦麦图片提取、下载归一化与视觉回传辅助。

职责：
1. 从聊天上下文取图（三通道：``maisaka.context.resolve_image`` ->
   ``message.get_by_id`` -> ``message.get_recent`` 最近附图消息）；
2. 将聊天图片落盘到受控暂存目录（供 ``saucenao`` / ``animetrace`` 本地文件反搜）；
3. 并发下载搜索结果中的图片链接（``image_url`` 或 ``url``），经 Pillow 校验并归一化为
   PNG/JPEG Base64，构造 ``content_items`` 供麦麦视觉查看与 ``reply(attach_pic=...)``
   直接发出。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import uuid
from base64 import b64decode, b64encode
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VISUAL_EDGE = 1600
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def is_public_http_url(url: str) -> bool:
    """校验是否为非回环、非内网的公网 http(s) URL。"""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return False
    try:
        parsed = urlparse(u)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        return False
    if not host or host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    except ValueError:
        pass
    return True


def decode_base64_image(raw_base64: str) -> tuple[str, bytes] | None:
    """解析普通 Base64 或 data:image/...;base64 字符串。"""
    normalized = str(raw_base64 or "").strip()
    if not normalized:
        return None
    image_format = "jpeg"
    match = re.match(
        r"^data:image/(?P<fmt>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
        normalized,
        re.DOTALL,
    )
    if match is not None:
        image_format = match.group("fmt").lower()
        normalized = match.group("data").strip()
    try:
        image_bytes = b64decode(normalized, validate=False)
    except Exception:
        return None
    if len(image_bytes) < 16:
        return None
    return image_format, image_bytes


def _iter_image_reference_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        refs: list[str] = []
        for key in ("binary_data_base64", "base64", "data_url", "data"):
            refs.extend(_iter_image_reference_values(value.get(key)))
        return refs
    if isinstance(value, list):
        refs = []
        for item in value:
            refs.extend(_iter_image_reference_values(item))
        return refs
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]") and not text.startswith("[CQ:"):
        return []
    return [text]


def extract_message_images(message: dict[str, Any]) -> list[tuple[str, bytes]]:
    """从宿主消息字典的 raw_message 中提取全部图片字节。"""
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        return []
    images: list[tuple[str, bytes]] = []
    for component in raw_message:
        if not isinstance(component, dict):
            continue
        comp_type = str(component.get("type") or "").strip().lower()
        if comp_type not in {"image", "emoji"}:
            continue
        for ref in _iter_image_reference_values(component):
            decoded = decode_base64_image(ref)
            if decoded is not None:
                images.append(decoded)
                break
    return images


def normalize_image_bytes(
    image_bytes: bytes,
    *,
    max_edge: int = MAX_VISUAL_EDGE,
    prefer_format: str = "jpeg",
) -> tuple[str, str, dict[str, int]] | None:
    """用 Pillow 校验图片并转为紧凑的 Base64 与尺寸信息。"""
    try:
        with Image.open(BytesIO(image_bytes)) as probe:
            probe.verify()
        with Image.open(BytesIO(image_bytes)) as raw:
            orig_w, orig_h = raw.size
            has_alpha = raw.mode in ("RGBA", "LA") or (
                raw.mode == "P" and "transparency" in raw.info
            )
            work = raw.convert("RGBA" if has_alpha else "RGB")
            if max(work.size) > max_edge:
                work.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            out_w, out_h = work.size
            buf = BytesIO()
            if has_alpha or prefer_format.lower() == "png":
                work.save(buf, format="PNG")
                fmt = "png"
            else:
                work.convert("RGB").save(buf, format="JPEG", quality=88)
                fmt = "jpeg"
            b64 = b64encode(buf.getvalue()).decode("utf-8")
            return (
                fmt,
                b64,
                {
                    "original_width": orig_w,
                    "original_height": orig_h,
                    "output_width": out_w,
                    "output_height": out_h,
                },
            )
    except Exception:
        return None


async def resolve_chat_image_to_file(
    ctx: Any,
    *,
    stream_id: str,
    msg_id: str,
    image_index: int,
    temp_dir: Path,
) -> tuple[Path | None, str | None]:
    """从当前聊天上下文解析目标图片并写入 temp_dir，返回 (文件路径, 错误信息)。"""
    target_stream = (stream_id or "").strip()
    target_msg = (msg_id or "").strip()
    idx = max(0, int(image_index or 0))
    image_bytes: bytes | None = None
    ext = "jpg"

    # 通道 1：指定了 msg_id 时优先走 maisaka.context.resolve_image
    # （支持模型可见的短编号与 tool_result 引用）
    if target_msg and target_stream:
        try:
            resolved = await ctx.maisaka.context.resolve_image(target_stream, target_msg, index=idx)
            if isinstance(resolved, dict) and resolved.get("success"):
                img_obj = resolved.get("image") or {}
                b64 = img_obj.get("binary_data_base64")
                if isinstance(b64, str) and b64.strip():
                    decoded = decode_base64_image(b64)
                    if decoded is not None:
                        ext, image_bytes = decoded
        except Exception as exc:
            logger.debug("maisaka.context.resolve_image 未命中: %s", exc)

    # 通道 2：指定了 msg_id 但通道 1 未命中时，回退 message.get_by_id
    if image_bytes is None and target_msg:
        try:
            msg_dict = await ctx.message.get_by_id(
                target_msg,
                chat_id=target_stream,
                include_binary_data=True,
            )
            if isinstance(msg_dict, dict):
                imgs = extract_message_images(msg_dict)
                if 0 <= idx < len(imgs):
                    ext, image_bytes = imgs[idx]
                elif imgs:
                    return (
                        None,
                        f"消息 {target_msg} 只有 {len(imgs)} 张图片，image_index={idx} 超出范围。",
                    )
        except Exception as exc:
            logger.debug("message.get_by_id 失败: %s", exc)

    # 通道 3：未指定 msg_id（或前两步均未拿到），扫描最近 12 条消息找最新一张图
    if image_bytes is None and target_stream:
        try:
            recent = await ctx.message.get_recent(target_stream, limit=12)
            messages: list[dict[str, Any]] = []
            if isinstance(recent, list):
                messages = [m for m in recent if isinstance(m, dict)]
            elif isinstance(recent, dict):
                raw_list = recent.get("messages") or recent.get("data") or []
                if isinstance(raw_list, list):
                    messages = [m for m in raw_list if isinstance(m, dict)]
            for candidate in reversed(messages):
                cand_id = str(candidate.get("message_id") or candidate.get("msg_id") or "").strip()
                imgs = extract_message_images(candidate)
                if not imgs and cand_id:
                    full_msg = await ctx.message.get_by_id(
                        cand_id,
                        chat_id=target_stream,
                        include_binary_data=True,
                    )
                    if isinstance(full_msg, dict):
                        imgs = extract_message_images(full_msg)
                if imgs:
                    pick_idx = idx if 0 <= idx < len(imgs) else 0
                    ext, image_bytes = imgs[pick_idx]
                    break
        except Exception as exc:
            logger.debug("message.get_recent 扫描图片失败: %s", exc)

    if image_bytes is None:
        if target_msg:
            return None, f"未能在消息 {target_msg} 中找到可读取的图片。"
        return None, "当前会话最近消息中没有找到图片，也没有提供公网 image_url。"

    norm_ext = "png" if ext == "png" else ("webp" if ext == "webp" else "jpg")
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / f"argo_chat_{uuid.uuid4().hex[:10]}.{norm_ext}"
    out_path.write_bytes(image_bytes)
    return out_path, None


async def download_image_as_content_item(
    image_url: str,
    *,
    description: str,
    name_prefix: str = "argo_img",
    proxy: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """下载单张公网图片并封装为 Maisaka 可直接查看的 content_item 字典。"""
    if not is_public_http_url(image_url):
        return None

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if "saucenao.com" in image_url:
        headers["Referer"] = "https://saucenao.com/"
    elif "pximg.net" in image_url or "pixiv.net" in image_url:
        headers["Referer"] = "https://www.pixiv.net/"
    elif "hdslb.com" in image_url or "bilibili.com" in image_url:
        headers["Referer"] = "https://www.bilibili.com/"

    client_kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": httpx.Timeout(timeout, connect=6.0),
        "follow_redirects": True,
    }
    if proxy and proxy.strip() and proxy.strip().lower() != "direct":
        client_kwargs["proxy"] = proxy.strip()

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", image_url) as resp:
                if resp.status_code != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_IMAGE_BYTES:
                        return None
        raw_bytes = bytes(buf)
    except Exception as exc:
        logger.debug("下载预览图片失败 (%s): %s", image_url, exc)
        return None

    normalized = await asyncio.to_thread(normalize_image_bytes, raw_bytes)
    if normalized is None:
        return None
    fmt, b64, meta = normalized
    digest = hashlib.sha256(b64.encode("utf-8")).hexdigest()[:10]
    item_name = f"{name_prefix}_{digest}.{fmt}"
    return {
        "content_type": "image",
        "data": b64,
        "mime_type": f"image/{fmt}",
        "name": item_name,
        "description": description,
        "metadata": meta | {"source_url": image_url},
    }
