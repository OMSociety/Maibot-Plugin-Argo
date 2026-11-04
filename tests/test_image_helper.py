"""图片解析：只认真图片字节，不吃宿主写进图片段的文字描述。"""

from __future__ import annotations

import base64
import io

from PIL import Image

import image_helper


def _png_base64(size: tuple[int, int] = (4, 4)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


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
