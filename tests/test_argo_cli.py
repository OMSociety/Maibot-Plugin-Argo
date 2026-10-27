"""argo_cli 的纯逻辑测试：选源、模式收敛、排版、作业协议。"""

import json
import os
from pathlib import Path

import pytest

import argo_cli


class TestResolveEngine:
    def test_default_kind_maps_to_metaso(self):
        assert argo_cli.resolve_engine("web", True, "原神 鸣潮 对比") == ("metaso", "")

    def test_unknown_kind_falls_back_to_web(self):
        assert argo_cli.resolve_engine("不存在", True, "任何词") == ("metaso", "")

    def test_empty_kind_falls_back_to_web(self):
        assert argo_cli.resolve_engine("", True, "任何词") == ("metaso", "")

    def test_paid_engine_uses_engine_when_enabled(self):
        engine, note = argo_cli.resolve_engine("news", True, "今天")
        assert engine == "deepseek_search"
        assert note == ""

    def test_paid_engine_downgrades_when_disabled(self):
        engine, note = argo_cli.resolve_engine("news", False, "今天")
        assert engine == "metaso"
        assert "计费源" in note

    def test_free_engine_ignores_paid_switch(self):
        assert argo_cli.resolve_engine("code", False, "maibot 插件") == ("github", "")

    def test_doi_without_doi_uses_academic_cn(self):
        engine, note = argo_cli.resolve_engine("doi", True, "社会资本 综述")
        assert engine == "metaso_scholar"
        assert "DOI" in note

    def test_doi_with_doi_uses_unpaywall(self):
        engine, note = argo_cli.resolve_engine("doi", True, "10.1000/xyz123")
        assert engine == "unpaywall"
        assert note == ""


class TestNormalizeMode:
    @pytest.mark.parametrize("mode", ["fast", "auto", "deep", "budget", " FAST ", "Deep"])
    def test_known_modes_are_kept(self, mode):
        assert argo_cli.normalize_mode(mode) in argo_cli.SEARCH_MODES

    @pytest.mark.parametrize("mode", ["balanced", "", None, "quick"])
    def test_unknown_modes_fall_back_to_default(self, mode):
        assert argo_cli.normalize_mode(mode) == argo_cli.DEFAULT_MODE

    def test_every_kind_resolves_to_a_known_engine(self):
        for kind in argo_cli.ENGINE_MAP:
            assert argo_cli.resolve_engine(kind, True, "10.1/x")[0]


class TestKindSpec:
    def test_engine_map_is_single_source_of_purpose_text(self):
        assert all(spec.purpose for spec in argo_cli.ENGINE_MAP.values())
        assert set(argo_cli.ENGINE_MAP) == {
            "web",
            "news",
            "academic_cn",
            "fulltext_cn",
            "academic_en",
            "doi",
            "book",
            "image",
            "code",
            "tech_en",
        }


class TestFormatResults:
    def test_empty_results_get_advice(self):
        assert "没有搜到结果" in argo_cli.format_results({"results": []})

    def test_renders_title_source_and_url(self):
        payload = {
            "results": [
                {
                    "title": "标题一",
                    "url": "https://example.org/a",
                    "source": "metaso",
                    "published_at": "2026-10-01T12:00:00Z",
                    "snippet": "摘要一",
                }
            ],
            "count": 1,
            "elapsed_ms": 120,
        }
        text = argo_cli.format_results(payload)
        assert "1. 标题一" in text
        assert "metaso" in text
        assert "2026-10-01" in text
        assert "https://example.org/a" in text
        assert "共 1 条" in text

    def test_note_is_appended(self):
        text = argo_cli.format_results({"results": [{"title": "t"}]}, note="已降级")
        assert "已降级" in text

    def test_truncation_keeps_at_least_one_content_char(self):
        payload = {"results": [{"title": "很长的标题" * 20}]}
        for limit in (1, 2, 10):
            text = argo_cli.format_results(payload, max_chars=limit)
            assert text.endswith("…")
            assert len(text.strip("…")) >= 1

    def test_zero_max_chars_means_no_truncation(self):
        payload = {"results": [{"title": "标题"}]}
        assert argo_cli.format_results(payload, max_chars=0)


class TestJobProtocol:
    def test_bad_task_is_rejected_before_touching_disk(self, tmp_path):
        result = argo_cli.submit_job(tmp_path, "nope", "x")
        assert "不支持的任务" in result["error"]
        assert list(tmp_path.iterdir()) == []

    def test_empty_target_is_rejected(self, tmp_path):
        result = argo_cli.submit_job(tmp_path, "research", "   ")
        assert "目标为空" in result["error"]

    def test_status_of_missing_job_reports_error(self, tmp_path):
        assert "没有这个作业" in argo_cli.job_status(tmp_path, "260101-000000-abcd")["error"]

    def test_status_reads_rc_file(self, tmp_path):
        job_dir = tmp_path / "260101-000000-abcd"
        job_dir.mkdir()
        (job_dir / "meta.json").write_text(json.dumps({"task": "research"}), encoding="utf-8")
        (job_dir / "rc").write_text("0", encoding="utf-8")
        info = argo_cli.job_status(tmp_path, job_dir.name)
        assert info["state"] == "done"
        assert info["rc"] == 0

    def test_status_of_dead_process_without_rc_is_lost(self, tmp_path):
        job_dir = tmp_path / "260101-000000-abcd"
        job_dir.mkdir()
        (job_dir / "meta.json").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        assert argo_cli.job_status(tmp_path, job_dir.name)["state"] == "lost"

    def test_list_is_newest_first(self, tmp_path):
        for name in ("260101-000000-aaaa", "260102-000000-bbbb"):
            (tmp_path / name).mkdir()
        rows = argo_cli.list_jobs(tmp_path)
        assert [row["job_id"] for row in rows] == ["260102-000000-bbbb", "260101-000000-aaaa"]

    def test_prune_removes_only_expired_dirs(self, tmp_path):
        keep = tmp_path / "keep"
        drop = tmp_path / "drop"
        keep.mkdir()
        drop.mkdir()
        (drop / "old.log").write_text("x", encoding="utf-8")
        old = (drop.stat().st_atime - 30 * 86400, drop.stat().st_mtime - 30 * 86400)
        os.utime(drop, old)
        assert argo_cli.prune_jobs(tmp_path, retention_days=7) == 1
        assert keep.exists() and not drop.exists()

    def test_prune_on_missing_root_is_noop(self, tmp_path: Path):
        assert argo_cli.prune_jobs(tmp_path / "nope") == 0
