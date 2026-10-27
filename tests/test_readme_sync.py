"""README 的来源表必须与 argo_cli.ENGINE_MAP 逐行一致。"""

import re
from pathlib import Path

import argo_cli

README = Path(__file__).resolve().parents[1] / "README.md"
ROW = re.compile(
    r"^\|\s*`(?P<kind>[a-z_]+)`\s*\|\s*`(?P<engine>[a-z_]+)`\s*\|\s*(?P<purpose>.+?)\s*\|$"
)


def test_readme_table_matches_engine_map():
    rows = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            rows[match["kind"]] = (match["engine"], match["purpose"])

    expected = {kind: (spec.engine, spec.purpose) for kind, spec in argo_cli.ENGINE_MAP.items()}
    assert rows == expected


TASK_LINE = re.compile(r"`task` 取 (?P<tasks>[^，]+)")


def test_readme_job_tasks_match():
    tasks = set()
    for line in README.read_text(encoding="utf-8").splitlines():
        match = TASK_LINE.search(line)
        if match:
            tasks = set(re.findall(r"`([a-z_]+)`", match["tasks"]))

    assert tasks == set(argo_cli.JOB_TASKS)
