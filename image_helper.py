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

MAX_IMAGE_BYTES = 16 * 1024 * 1024
# 宿主插件进程按 16MB 分帧收发（4 字节长度前缀 + JSON），工具结果里的图片全走这一帧；
# 留出 1MB 给信封、文案与其他字段。
MAX_FRAME_BUDGET = 15 * 1024 * 1024
MAX_VISUAL_EDGE = 1600
# 结果图统一重编码一次：分辨率不变，只换一份本地生成的字节。
JPEG_QUALITY = 95
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 图床与图站普遍校验防盗链；按图源域名给出对应站点的 Referer。
_REFERER_BY_HOST: tuple[tuple[str, str], ...] = (
    ("saucenao.com", "https://saucenao.com/"),
    ("pximg.net", "https://www.pixiv.net/"),
    ("pixiv.net", "https://www.pixiv.net/"),
    ("donmai.us", "https://danbooru.donmai.us/"),
    ("hdslb.com", "https://www.bilibili.com/"),
    ("bilibili.com", "https://www.bilibili.com/"),
    ("twimg.com", "https://x.com/"),
    ("x.com", "https://x.com/"),
    ("fanbox.cc", "https://www.pixiv.net/"),
    ("fantia.jp", "https://fantia.jp/"),
    ("artstation.com", "https://www.artstation.com/"),
    ("deviantart.net", "https://www.deviantart.com/"),
    ("deviantart.com", "https://www.deviantart.com/"),
    ("steamstatic.com", "https://store.steampowered.com/"),
    ("steamcommunity.com", "https://steamcommunity.com/"),
    ("sinaimg.cn", "https://weibo.com/"),
    ("zhimg.com", "https://www.zhihu.com/"),
    ("doubanio.com", "https://www.douban.com/"),
    ("yande.re", "https://yande.re/"),
    ("konachan", "https://konachan.com/"),
)


def _referer_for(image_url: str) -> str:
    """按图源域名挑 Referer，没有匹配时返回空串。"""
    lowered = (image_url or "").strip().lower()
    for host, referer in _REFERER_BY_HOST:
        if host in lowered:
            return referer
    return ""


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


_B64_CHARS_RE = re.compile(r"^[A-Za-z0-9+/\s=]+$")
_IMAGE_MAGIC = (b"\xff\xd8", b"\x89PNG", b"GIF8", b"BM")


def looks_like_image(data: bytes) -> bool:
    """按文件头判断是否 JPEG/PNG/GIF/BMP/WebP。"""
    if len(data) < 12:
        return False
    if data.startswith(_IMAGE_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _sniff_format(data: bytes) -> str:
    """按文件头给出格式名，用于决定落盘后缀。"""
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpeg"


def decode_base64_image(raw_base64: str) -> tuple[str, bytes] | None:
    """解析普通 Base64 或 data:image/...;base64 字符串。

    只接受纯 Base64 字符集：宿主在未请求二进制时会把图片段写成文字描述，
    宽松解码会把描述里的 ASCII 片段当成数据，产出一段非图片字节。
    """
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
    compact = "".join(normalized.split())
    if not compact or not _B64_CHARS_RE.match(compact):
        return None
    if len(compact) % 4 != 0:
        compact += "=" * (-len(compact) % 4)
    try:
        image_bytes = b64decode(compact, validate=True)
    except Exception:
        return None
    if not looks_like_image(image_bytes):
        return None
    return _sniff_format(image_bytes) or image_format, image_bytes


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
    max_edge: int | None = None,
    quality: int = JPEG_QUALITY,
    prefer_format: str = "jpeg",
) -> tuple[str, str, dict[str, int]] | None:
    """用 Pillow 校验图片、按需缩放后重新编码为 Base64 与尺寸信息。

    默认不缩放（``max_edge=None``），只重新编码：宿主要发出去的字节由本地生成，
    不会与公网上流传的同一份字节完全一致。
    """
    try:
        with Image.open(BytesIO(image_bytes)) as probe:
            probe.verify()
        with Image.open(BytesIO(image_bytes)) as raw:
            orig_w, orig_h = raw.size
            has_alpha = raw.mode in ("RGBA", "LA") or (
                raw.mode == "P" and "transparency" in raw.info
            )
            work = raw.convert("RGBA" if has_alpha else "RGB")
            if max_edge is not None and max(work.size) > max_edge:
                work.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            out_w, out_h = work.size
            buf = BytesIO()
            if has_alpha or prefer_format.lower() == "png":
                work.save(buf, format="PNG")
                fmt = "png"
            else:
                work.convert("RGB").save(buf, format="JPEG", quality=quality)
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


def _unwrap_message(response: Any) -> dict[str, Any] | None:
    """把 message.get_by_id 的信封拆成消息字典。"""
    if not isinstance(response, dict):
        return None
    if response.get("success") is False:
        return None
    inner = response.get("message")
    if isinstance(inner, dict):
        return inner
    if "raw_message" in response:
        return response
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
    source = ""

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
                        source = "context.resolve_image"
        except Exception as exc:
            logger.debug("maisaka.context.resolve_image 未命中: %s", exc)

    # 通道 2：指定了 msg_id 但通道 1 未命中时，回退 message.get_by_id
    if image_bytes is None and target_msg:
        try:
            msg_dict = _unwrap_message(
                await ctx.message.get_by_id(
                    target_msg,
                    chat_id=target_stream,
                    include_binary_data=True,
                )
            )
            if msg_dict is not None:
                imgs = extract_message_images(msg_dict)
                if 0 <= idx < len(imgs):
                    ext, image_bytes = imgs[idx]
                    source = "message.get_by_id"
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
                    full_msg = _unwrap_message(
                        await ctx.message.get_by_id(
                            cand_id,
                            chat_id=target_stream,
                            include_binary_data=True,
                        )
                    )
                    if full_msg is not None:
                        imgs = extract_message_images(full_msg)
                if imgs:
                    pick_idx = idx if 0 <= idx < len(imgs) else 0
                    ext, image_bytes = imgs[pick_idx]
                    source = f"message.get_recent:{cand_id}"
                    break
        except Exception as exc:
            logger.debug("message.get_recent 扫描图片失败: %s", exc)

    if image_bytes is None:
        if target_msg:
            return None, f"未能在消息 {target_msg} 中找到可读取的图片。"
        return None, "当前会话最近消息中没有找到图片，也没有提供公网 image_url。"

    norm_ext = ext if ext in ("png", "webp", "gif", "bmp") else "jpg"
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / f"argo_chat_{uuid.uuid4().hex[:10]}.{norm_ext}"
    out_path.write_bytes(image_bytes)
    logger.info(
        "反搜取图 msg_id=%s channel=%s bytes=%d sha256=%s",
        target_msg or "-",
        source or "unknown",
        len(image_bytes),
        hashlib.sha256(image_bytes).hexdigest()[:16],
    )
    return out_path, None


async def fetch_image_bytes(
    image_url: str,
    *,
    proxy: str | None = None,
    timeout: float = 10.0,
) -> bytes | None:
    """带防盗链与体积上限地取回一张公网图片的原始字节。"""
    if not is_public_http_url(image_url):
        return None

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    referer = _referer_for(image_url)
    if referer:
        headers["Referer"] = referer

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
        return bytes(buf)
    except Exception as exc:
        logger.debug("下载图片失败 (%s): %s", image_url, exc)
        return None


def build_content_item(
    raw_bytes: bytes,
    *,
    description: str,
    name_prefix: str,
    source_url: str,
) -> dict[str, Any] | None:
    """把图片字节重编码后封装成 content_item：分辨率不变，字节由本地生成。"""
    normalized = normalize_image_bytes(raw_bytes)
    if normalized is None:
        return None
    fmt, b64, meta = normalized
    meta = meta | {"reencoded": True}

    digest = hashlib.sha256(b64.encode("utf-8")).hexdigest()[:10]
    return {
        "content_type": "image",
        "data": b64,
        "mime_type": f"image/{fmt}",
        "name": f"{name_prefix}_{digest}.{fmt}",
        "description": description,
        "metadata": meta | {"source_url": source_url},
    }


def shrink_to_budget(
    raw_bytes: bytes,
    *,
    description: str,
    name_prefix: str,
    source_url: str,
    max_base64_bytes: int,
) -> dict[str, Any] | None:
    """逐档缩小重编码，直到 Base64 长度落进分帧预算。"""
    for max_edge in (MAX_VISUAL_EDGE, 1200, 900, 640):
        normalized = normalize_image_bytes(raw_bytes, max_edge=max_edge)
        if normalized is None:
            return None
        fmt, b64, meta = normalized
        if len(b64) <= max_base64_bytes:
            digest = hashlib.sha256(b64.encode("utf-8")).hexdigest()[:10]
            return {
                "content_type": "image",
                "data": b64,
                "mime_type": f"image/{fmt}",
                "name": f"{name_prefix}_{digest}.{fmt}",
                "description": description,
                "metadata": meta | {"reencoded": True, "source_url": source_url},
            }
    return None


async def download_image_as_content_item(
    image_url: str,
    *,
    description: str,
    preview_url: str | None = None,
    name_prefix: str = "argo_img",
    proxy: str | None = None,
    timeout: float = 10.0,
    max_base64_bytes: int = MAX_FRAME_BUDGET,
) -> dict[str, Any] | None:
    """下载单张公网图片并封装为 Maisaka 可直接查看的 content_item 字典。

    图片统一按原分辨率重编码一次，宿主拿到的是本地生成的字节；
    原图取不到时改用 ``preview_url``（引擎给的预览直链）；
    两者都超出 ``max_base64_bytes`` 时才逐档缩小重编码，保证有图可发。
    """
    candidates = [image_url]
    if preview_url and preview_url != image_url:
        candidates.append(preview_url)

    oversized: tuple[bytes, str] | None = None
    for candidate in candidates:
        raw_bytes = await fetch_image_bytes(candidate, proxy=proxy, timeout=timeout)
        if raw_bytes is None:
            continue
        item = await asyncio.to_thread(
            build_content_item,
            raw_bytes,
            description=description,
            name_prefix=name_prefix,
            source_url=candidate,
        )
        if item is None:
            continue
        if len(item["data"]) <= max_base64_bytes:
            return item
        if oversized is None:
            oversized = (raw_bytes, candidate)

    if oversized is None:
        return None
    raw_bytes, used_url = oversized
    return await asyncio.to_thread(
        shrink_to_budget,
        raw_bytes,
        description=description,
        name_prefix=name_prefix,
        source_url=used_url,
        max_base64_bytes=max_base64_bytes,
    )
