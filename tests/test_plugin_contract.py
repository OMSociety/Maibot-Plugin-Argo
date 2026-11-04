"""plugin.py 与 image_helper.py 的契约测试：失败字典、工具描述、缓存、SSRF 拦截与图片处理。"""

import asyncio
import inspect
from base64 import b64encode
from io import BytesIO

from PIL import Image

import image_helper
import plugin


class TestFailContract:
    def test_fail_fills_both_error_and_content(self):
        result = plugin.fail("出问题了")
        assert result == {"success": False, "error": "出问题了", "content": "出问题了"}

    def test_no_failure_dict_is_built_inline(self):
        source = inspect.getsource(plugin)
        assert source.count('"success": False') == 1
        before, _, after = source.partition('"success": False')
        assert "def fail(" in before
        assert after.lstrip().startswith(', "error"')


class TestToolDescription:
    def test_kind_hint_is_generated_from_engine_map(self):
        for kind, spec in plugin.argo_cli.ENGINE_MAP.items():
            assert f"{kind}={spec.purpose}" in plugin._KIND_HINT

    def test_reverse_hint_is_generated_from_reverse_image_engines(self):
        for eng, desc in plugin.argo_cli.REVERSE_IMAGE_ENGINES.items():
            assert f"{eng}={desc}" in plugin._REVERSE_HINT


class TestCache:
    def _plugin(self) -> plugin.ArgoPlugin:
        return plugin.ArgoPlugin()

    def test_hit_moves_key_to_the_end(self):
        instance = self._plugin()
        instance._remember(("metaso", "a", 5, "fast"), "A")
        instance._remember(("metaso", "b", 5, "fast"), "B")
        instance._touch(("metaso", "a", 5, "fast"))
        assert list(instance._cache) == [("metaso", "b", 5, "fast"), ("metaso", "a", 5, "fast")]

    def test_capacity_evicts_least_recently_used(self):
        instance = self._plugin()
        keys = [("metaso", f"q{i}", 5, "fast") for i in range(plugin._CACHE_MAX)]
        for key in keys:
            instance._remember(key, key[1])
        instance._touch(keys[0])
        instance._remember(("metaso", "new", 5, "fast"), "new")
        assert keys[0] in instance._cache
        assert keys[1] not in instance._cache
        assert len(instance._cache) == plugin._CACHE_MAX


class TestSearchFallback:
    """垂直引擎空手时补一次综合检索，避免群友只看到「没找到」。"""

    class _FakeWorker:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = []

        async def call_tool(self, name, arguments, timeout=0.0):
            self.calls.append((name, arguments))
            return self.replies.pop(0)

    def _instance(self, replies):
        instance = plugin.ArgoPlugin()
        instance._plugin_config_instance = type(instance).get_config_model()()
        fake = self._FakeWorker(replies)
        instance._get_worker = lambda: fake
        return instance, fake

    def test_empty_vertical_result_falls_back_to_web_search(self):
        replies = [
            {"count": 0, "results": []},
            {"count": 1, "results": [{"title": "黑神话：悟空", "url": "https://example.com"}]},
        ]
        instance, fake = self._instance(replies)
        out = asyncio.run(instance.handle_search(query="黑神话 悟空", kind="game", max_results=3))
        assert out["success"] is True
        assert "已回退综合检索" in out["content"]
        engines = [args["engine"] for _, args in fake.calls]
        assert engines == ["steam", "metaso"]
        assert all(args["include_local"] is False for _, args in fake.calls)

    def test_explicit_engine_is_not_rerouted(self):
        replies = [{"count": 0, "results": []}]
        instance, fake = self._instance(replies)
        out = asyncio.run(instance.handle_search(query="黑神话 悟空", engine="steam"))
        assert out["success"] is True
        assert len(fake.calls) == 1


class TestImageHelper:
    def test_is_public_http_url_blocks_local_and_private(self):
        assert image_helper.is_public_http_url("https://i.pximg.net/img/a.jpg") is True
        assert image_helper.is_public_http_url("file:///C:/Windows/win.ini") is False
        assert image_helper.is_public_http_url("http://127.0.0.1:8080/a.jpg") is False
        assert image_helper.is_public_http_url("http://192.168.1.1/a.jpg") is False
        assert image_helper.is_public_http_url("http://localhost/a.jpg") is False

    def test_normalize_and_extract_message_images(self):
        buf = BytesIO()
        Image.new("RGB", (64, 48), color=(120, 180, 240)).save(buf, format="PNG")
        raw_bytes = buf.getvalue()
        b64 = b64encode(raw_bytes).decode("utf-8")

        msg = {
            "raw_message": [
                {"type": "text", "data": "看看这张图"},
                {"type": "image", "binary_data_base64": f"data:image/png;base64,{b64}"},
            ]
        }
        extracted = image_helper.extract_message_images(msg)
        assert len(extracted) == 1
        assert extracted[0][0] == "png"
        assert extracted[0][1] == raw_bytes

        norm = image_helper.normalize_image_bytes(raw_bytes)
        assert norm is not None
        fmt, out_b64, meta = norm
        assert fmt in {"jpeg", "png"}
        assert len(out_b64) > 20
        assert meta["original_width"] == 64
        assert meta["original_height"] == 48
