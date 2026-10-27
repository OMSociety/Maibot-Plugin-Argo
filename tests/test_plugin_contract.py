"""plugin.py 的契约测试：失败结果字段、工具描述与缓存语义。"""

import inspect

import plugin


class TestFailContract:
    def test_fail_fills_both_error_and_content(self):
        result = plugin.fail("出问题了")
        assert result == {"success": False, "error": "出问题了", "content": "出问题了"}

    def test_no_failure_dict_is_built_inline(self):
        source = inspect.getsource(plugin)
        # 全文件只允许 fail() 里出现一次失败字典；其余失败分支必须走 fail()。
        assert source.count('"success": False') == 1
        before, _, after = source.partition('"success": False')
        assert "def fail(message: str) -> dict:" in before
        assert after.lstrip().startswith(', "error"')


class TestToolDescription:
    def test_kind_hint_is_generated_from_engine_map(self):
        for kind, spec in plugin.argo_cli.ENGINE_MAP.items():
            assert f"{kind}={spec.purpose}" in plugin._KIND_HINT


class TestCache:
    def _plugin(self) -> plugin.ArgoLookup:
        return plugin.ArgoLookup()

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
