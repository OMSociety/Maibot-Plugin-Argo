"""图片解析：只认真图片字节，不吃宿主写进图片段的文字描述。"""

from __future__ import annotations

import asyncio
import base64
import io

from PIL import Image

import image_helper

ORIGINAL_URL = "https://i.pximg.net/img-original/img/2026/11/07/a_p0.jpg"
PREVIEW_URL = "https://i.pximg.net/c/600x1200_90/img-master/img/2026/11/07/a_p0_master1200.jpg"


def _png_base64(size: tuple[int, int] = (4, 4)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _jpeg_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _gif_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 200, 30)).save(buf, format="GIF")
    return buf.getvalue()


def test_text_description_is_not_decoded_as_image():
    description = "[图片：画面以红黑色为主调，背景铺满“LONG MARCH”（长征）字样。中央为一名少女。]"
    assert image_helper.decode_base64_image(description) is None


def test_plain_ascii_text_is_not_decoded_as_image():
    assert image_helper.decode_base64_image("hello world LONG MARCH") is None


def test_real_png_base64_is_decoded():
    decoded = image_helper.decode_base64_image(_png_base64())
    assert decoded is not None
    fmt, data = decoded
    assert fmt == "png"
    assert image_helper.looks_like_image(data)


def test_data_url_prefix_is_stripped():
    decoded = image_helper.decode_base64_image(f"data:image/png;base64,{_png_base64()}")
    assert decoded is not None
    assert decoded[0] == "png"


def test_unwrap_message_handles_capability_envelope():
    inner = {"message_id": "1", "raw_message": []}
    assert image_helper._unwrap_message({"success": True, "message": inner}) is inner
    assert image_helper._unwrap_message({"success": False, "error": "x"}) is None
    assert image_helper._unwrap_message(inner) is inner
    assert image_helper._unwrap_message(None) is None


def test_looks_like_image_rejects_short_or_unknown_bytes():
    assert image_helper.looks_like_image(b"LONGMARCHLONG") is False
    assert image_helper.looks_like_image(b"\xff\xd8\xff\xe0" + b"0" * 16) is True


def test_referer_follows_image_host():
    assert image_helper._referer_for("https://cdn.donmai.us/orig/ab/cd.png") == (
        "https://danbooru.donmai.us/"
    )
    assert image_helper._referer_for("https://i.pximg.net/img/x.png") == "https://www.pixiv.net/"
    assert image_helper._referer_for("https://pbs.twimg.com/media/x.jpg") == "https://x.com/"
    assert image_helper._referer_for("https://example.com/a.jpg") == ""


def test_download_reencodes_without_resizing(monkeypatch):
    """结果图统一重编码一次：分辨率不变，交给宿主的不是原图字节。"""
    raw = _jpeg_bytes((8, 8))

    async def fake_fetch(url, *, proxy=None, timeout=10.0):
        return raw

    monkeypatch.setattr(image_helper, "fetch_image_bytes", fake_fetch)
    item = asyncio.run(
        image_helper.download_image_as_content_item(ORIGINAL_URL, description="测试")
    )
    assert item is not None
    assert item["mime_type"] == "image/jpeg"
    assert item["metadata"]["reencoded"] is True
    assert item["metadata"]["output_width"] == 8
    assert item["metadata"]["output_height"] == 8
    encoded = base64.b64decode(item["data"])
    assert encoded != raw
    with Image.open(io.BytesIO(encoded)) as decoded:
        assert decoded.size == (8, 8)


def test_download_falls_back_to_preview_url(monkeypatch):
    raw = _jpeg_bytes()
    seen: list[str] = []

    async def fake_fetch(url, *, proxy=None, timeout=10.0):
        seen.append(url)
        return raw if url == PREVIEW_URL else None

    monkeypatch.setattr(image_helper, "fetch_image_bytes", fake_fetch)
    item = asyncio.run(
        image_helper.download_image_as_content_item(
            ORIGINAL_URL, description="测试", preview_url=PREVIEW_URL
        )
    )
    assert item is not None
    assert seen == [ORIGINAL_URL, PREVIEW_URL]
    assert item["metadata"]["source_url"] == PREVIEW_URL


def test_download_shrinks_when_exceeding_frame_budget(monkeypatch):
    """超出分帧预算的图逐档缩小重编码，不会让整帧超限报错。"""
    import random

    noise = Image.new("RGB", (2400, 2400))
    rng = random.Random(7)
    noise.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(2400 * 2400)]
    )
    buf = io.BytesIO()
    noise.save(buf, format="JPEG", quality=95)
    raw = buf.getvalue()
    assert len(raw) * 4 // 3 > 1_000_000

    async def fake_fetch(url, *, proxy=None, timeout=10.0):
        return raw

    monkeypatch.setattr(image_helper, "fetch_image_bytes", fake_fetch)
    item = asyncio.run(
        image_helper.download_image_as_content_item(
            ORIGINAL_URL, description="测试", max_base64_bytes=1_000_000
        )
    )
    assert item is not None
    assert len(item["data"]) <= 1_000_000
    assert item["metadata"]["reencoded"] is True


def test_download_reencodes_unsupported_container(monkeypatch):
    raw = _gif_bytes()

    async def fake_fetch(url, *, proxy=None, timeout=10.0):
        return raw

    monkeypatch.setattr(image_helper, "fetch_image_bytes", fake_fetch)
    item = asyncio.run(
        image_helper.download_image_as_content_item(ORIGINAL_URL, description="测试")
    )
    assert item is not None
    assert item["mime_type"] in {"image/jpeg", "image/png"}
    assert item["metadata"]["reencoded"] is True
