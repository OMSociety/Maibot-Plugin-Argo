"""plugin.py 与 image_helper.py 的契约测试：失败字典、工具描述、缓存、SSRF 拦截与图片处理。"""

import ast
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

    def test_only_fail_builds_failure_dict(self):
        tree = ast.parse(inspect.getsource(plugin))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }

        def is_failure_dict(node: ast.Dict) -> bool:
            return any(
                isinstance(key, ast.Constant)
                and key.value == "success"
                and isinstance(value, ast.Constant)
                and value.value is False
                for key, value in zip(node.keys, node.values, strict=False)
            )

        builders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict) or not is_failure_dict(node):
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            builders.append(owner.name if owner is not None else None)
        assert builders == ["fail"]


class TestJobWatchConfig:
    def test_default_watch_window_is_twelve_minutes(self):
        assert plugin.JobsSection().job_watch_seconds == 720

    def test_watch_window_can_be_overridden(self):
        config = plugin.ArgoConfig(jobs=plugin.JobsSection(job_watch_seconds=30))
        assert config.jobs.job_watch_seconds == 30


class TestToolDescription:
    def test_kind_hint_is_generated_from_engine_map(self):
        for kind, spec in plugin.argo_cli.ENGINE_MAP.items():
            assert f"{kind}={spec.purpose}" in plugin._KIND_HINT

    def test_reverse_hint_is_generated_from_reverse_image_engines(self):
        for eng, desc in plugin.argo_cli.REVERSE_IMAGE_ENGINES.items():
            assert f"{eng}={desc}" in plugin._REVERSE_HINT

    def test_image_hint_is_generated_from_image_search_engines(self):
        for eng, desc in plugin.argo_cli.IMAGE_SEARCH_ENGINES.items():
            assert f"{eng}={desc}" in plugin._IMAGE_HINT


class TestImageSearchEngine:
    class _FakeWorker:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments, timeout=0.0):
            self.calls.append((name, arguments))
            return {"count": 0, "results": []}

    def _instance(self):
        instance = plugin.ArgoPlugin()
        instance._plugin_config_instance = type(instance).get_config_model()()
        fake = self._FakeWorker()
        instance._get_worker = lambda: fake
        return instance, fake

    def test_pixiv_engine_is_forwarded(self):
        instance, fake = self._instance()
        out = asyncio.run(instance.handle_image_search(query="初音ミク", engine="pixiv"))
        assert out["success"] is False
        assert fake.calls[0][1]["engine"] == "pixiv"

    def test_default_engine_remains_metaso_image(self):
        instance, fake = self._instance()
        asyncio.run(instance.handle_image_search(query="风景"))
        assert fake.calls[0][1]["engine"] == "metaso_image"
        assert fake.calls[0][1]["mode"] == plugin.argo_cli.DEFAULT_MODE

    def test_unknown_engine_is_rejected_before_worker_call(self):
        instance, fake = self._instance()
        out = asyncio.run(instance.handle_image_search(query="初音ミク", engine="saucenao"))
        assert out["success"] is False
        assert "不支持的搜图引擎" in out["error"]
        assert fake.calls == []


class TestSendImage:
    def _instance(self):
        instance = plugin.ArgoPlugin()
        instance._plugin_config_instance = type(instance).get_config_model()()
        return instance

    def test_non_public_url_is_rejected(self):
        instance = self._instance()
        out = asyncio.run(instance.handle_send_image(image_url="http://127.0.0.1/a.jpg"))
        assert out["success"] is False
        assert "公网" in out["error"]

    def test_selected_image_comes_back_as_content_item(self, monkeypatch):
        instance = self._instance()
        calls = {}

        async def fake_download(image_url, **kwargs):
            calls["image_url"] = image_url
            calls["kwargs"] = kwargs
            return {"content_type": "image", "mime_type": "image/jpeg", "data": "AAAA"}

        monkeypatch.setattr(plugin.image_helper, "download_image_as_content_item", fake_download)
        out = asyncio.run(
            instance.handle_send_image(
                image_url="https://i.pximg.net/img-original/a.jpg",
                preview_url="https://i.pximg.net/c/600x1200/a.jpg",
            )
        )
        assert out["success"] is True
        assert len(out["content_items"]) == 1
        assert "attach_pic" in out["content"]
        assert calls["image_url"] == "https://i.pximg.net/img-original/a.jpg"
        assert calls["kwargs"]["preview_url"] == "https://i.pximg.net/c/600x1200/a.jpg"


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
