"""argo 命令行封装：同步检索 + 异步作业（提交 / 状态 / 列表 / 取消）。

只依赖标准库，宿主运行与离线冒烟共用同一份实现。
作业协议：提交时只负责把 argv 交给脱离式进程并立刻返回；
完成信号是作业目录里的 ``rc`` 文件（内容为退出码），
状态查询只读文件，不依赖任何长连接。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

DEFAULT_ROOT = Path(r"C:\Users\Administrator\argo")


class KindSpec(NamedTuple):
    """一个来源类别的完整定义。"""

    engine: str
    paid: bool
    purpose: str


# kind -> 引擎/计费/适用场景。工具描述、README 表格都由它派生，不要另抄一份。
ENGINE_MAP: dict[str, KindSpec] = {
    "web": KindSpec("metaso", False, "中文综合，游戏影视对比、热梗、娱乐热点、新闻综述"),
    "news": KindSpec("deepseek_search", True, "时事新闻，带发布日期与权威分级"),
    "academic_cn": KindSpec("metaso_scholar", False, "中文期刊题录（万方/维普）"),
    "fulltext_cn": KindSpec("ncpssd", False, "中文社科全文 PDF（国家哲学社会科学文献中心）"),
    "academic_en": KindSpec("sciverse", False, "外文文献题录与摘要"),
    "doi": KindSpec("unpaywall", False, "传 DOI 查合法免费全文"),
    "book": KindSpec("weread", False, "中文书目"),
    "image": KindSpec("metaso_image", False, "配图与梗图"),
    "code": KindSpec("github", False, "GitHub 仓库与插件"),
    "tech_en": KindSpec("tavily", True, "英文工程资料"),
}

# 允许作为长任务提交的 argo 子命令
JOB_TASKS = ("research", "crawl", "pdf", "paper", "article")

# argo `search --mode` 的合法取值：fast=免费优先、auto=成本感知、deep=质量优先、budget=配额控制。
# `--depth` 是另一组取值（fast/balanced/deep），传错会被 argparse 拒（exit 2）。
SEARCH_MODES = ("fast", "auto", "deep", "budget")
DEFAULT_MODE = "fast"

# DETACHED_PROCESS 让作业脱离宿主控制台；CREATE_NEW_PROCESS_GROUP 便于整组结束。
# CREATE_NO_WINDOW（0x08000000）与 DETACHED_PROCESS 同用时被系统忽略，故不传。
DETACHED_FLAGS = 0x00000008 | 0x00000200 if os.name == "nt" else 0


# ── 基础工具 ────────────────────────────────────────────────────────────


def build_env(env_file: Path | str | None = None) -> dict[str, str]:
    """复制宿主环境并注入 argo 需要的变量。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_file:
        env["ARGO_ENV_FILE"] = str(env_file)
    return env


def default_env_file() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "argo" / "env"


def argo_root(root: str | Path | None = None) -> Path:
    return Path(root) if root else DEFAULT_ROOT


def launcher(root: Path) -> Path:
    return root / "bin" / "argo"


def looks_like_doi(query: str) -> bool:
    q = (query or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if q.startswith(prefix):
            q = q[len(prefix) :].strip()
    return q.startswith("10.") and "/" in q


def resolve_engine(kind: str, enable_paid: bool, query: str) -> tuple[str, str]:
    """返回 (引擎, 降级说明)；降级说明为空表示按用户所要求的那类源检索。"""
    kind = (kind or "web").strip().lower()
    spec = ENGINE_MAP.get(kind, ENGINE_MAP["web"])
    if kind == "doi" and not looks_like_doi(query):
        return "metaso_scholar", "传入的不是 DOI，改用中文期刊题录"
    if spec.paid and not enable_paid:
        return "metaso", f"{kind} 这一档用的是计费源，已按配置改用中文综合"
    return spec.engine, ""


def normalize_mode(mode: str | None) -> str:
    """把 ``--mode`` 收敛到 argo 认的取值；不认识的一律按 fast。

    fast 是免费优先档，配置写错时宁可少花钱也不要让整次检索因 argparse 退出码 2 而失败。
    """
    value = (mode or "").strip().lower()
    return value if value in SEARCH_MODES else DEFAULT_MODE


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _new_job_id() -> str:
    return datetime.now().strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


# ── 同步检索 ────────────────────────────────────────────────────────────


def search(
    query: str,
    engine: str,
    *,
    max_results: int = 5,
    mode: str = DEFAULT_MODE,
    timeout: int = 25,
    root: str | Path | None = None,
    env_file: str | Path | None = None,
    python: str | None = None,
) -> dict:
    """调用 ``argo search --json``，返回其 JSON 载荷或 ``{"error": ...}``。"""
    base = argo_root(root)
    argv = [
        python or sys.executable,
        str(launcher(base)),
        "search",
        query,
        "--json",
        "-e",
        engine,
        "-n",
        str(max(1, int(max_results))),
        "--mode",
        normalize_mode(mode),
        "--timeout",
        str(max(5, int(timeout))),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10, int(timeout)),
            env=build_env(env_file or default_env_file()),
            cwd=str(base),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"检索超时（>{timeout}s）"}
    except FileNotFoundError:
        return {"error": f"找不到 argo 启动器：{launcher(base)}"}
    except OSError as exc:
        return {"error": f"无法启动 argo：{exc}"}

    raw = (proc.stdout or "").strip()
    if not raw:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {"error": f"argo 没有返回内容（exit={proc.returncode}）{detail[0]}"[:240]}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "argo 返回的不是合法 JSON（可能被日志或警告污染）"[:240]}
    if isinstance(payload, dict) and not payload.get("results"):
        payload.setdefault("count", 0)
    return payload


def _pick(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def format_results(
    payload: dict,
    *,
    max_items: int = 5,
    max_chars: int = 1500,
    note: str = "",
) -> str:
    """把 argo 的 JSON 载荷排成给模型看的短文本。"""
    results = payload.get("results") or []
    if not results:
        return "没有搜到结果。换个更具体的说法，或者换一类来源再试。"[:max_chars]

    lines: list[str] = []
    for index, item in enumerate(results[:max_items], start=1):
        if not isinstance(item, dict):
            continue
        title = _pick(item, "title", "name") or "（无标题）"
        url = _pick(item, "url", "link")
        source = _pick(item, "source", "engine", "upstream") or "未知来源"
        date = _pick(item, "published_at", "date", "publish_date")
        snippet = _pick(item, "snippet", "summary", "description", "content")
        if len(snippet) > 120:
            snippet = snippet[:120].rstrip() + "…"
        head = f"{index}. {title}"
        if source or date:
            head += f"\n   来源：{source}" + (f" · {date[:10]}" if date else "")
        body = f"\n   {snippet}" if snippet else ""
        tail = f"\n   {url}" if url else ""
        lines.append(head + body + tail)

    text = "\n".join(lines)
    count = payload.get("count") or len(results)
    elapsed = payload.get("elapsed_ms")
    footer = (
        f"（共 {count} 条"
        + (f"，{int(elapsed)} ms" if isinstance(elapsed, (int, float)) else "")
        + "）"
    )
    if note:
        footer += f"；{note}"
    text = text + "\n" + footer
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max(1, max_chars - 1)].rstrip() + "…"
    return text


# ── 异步作业 ────────────────────────────────────────────────────────────


def submit_job(
    job_root: str | Path,
    task: str,
    target: str,
    *,
    root: str | Path | None = None,
    env_file: str | Path | None = None,
    python: str | None = None,
) -> dict:
    """把一条 argo 子命令交给脱离式进程，立刻返回作业元信息。"""
    task = (task or "").strip().lower()
    if task not in JOB_TASKS:
        return {"error": f"不支持的任务：{task}（可用：{'/'.join(JOB_TASKS)}）"}
    if not (target or "").strip():
        return {"error": "任务目标为空"}

    base = argo_root(root)
    job_dir = Path(job_root) / _new_job_id()
    job_dir.mkdir(parents=True, exist_ok=True)

    argv = [python or sys.executable, str(launcher(base)), task, target.strip()]
    (job_dir / "argv.json").write_text(
        json.dumps(argv, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    runner = str(Path(__file__).with_name("job_runner.py"))
    try:
        # 运行器自己的 stdout/stderr 一律丢弃：它只往作业目录里写文件，
        # 不持有任何可被 SSH/会话关闭牵连的管道。
        proc = subprocess.Popen(
            [python or sys.executable, runner, str(job_dir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(base),
            env=build_env(env_file or default_env_file()),
            creationflags=DETACHED_FLAGS,
        )
    except OSError as exc:
        return {"error": f"提交失败：{exc}"}

    meta = {
        "job_id": job_dir.name,
        "task": task,
        "target": target.strip(),
        "argv": argv,
        "pid": proc.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "state": "running",
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def _tail(path: Path, lines: int, limit_bytes: int = 262144) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit_bytes))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = [row.rstrip() for row in chunk.splitlines() if row.strip()]
    return rows[-max(1, int(lines)) :]


def job_status(job_root: str | Path, job_id: str, *, log_lines: int = 20) -> dict:
    job_dir = Path(job_root) / (job_id or "").strip()
    if not job_dir.is_dir():
        return {"error": f"没有这个作业：{job_id}"}
    meta_path = job_dir / "meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}

    rc_path = job_dir / "rc"
    if rc_path.exists():
        try:
            code = int(rc_path.read_text(encoding="utf-8").strip() or "1")
        except (OSError, ValueError):
            code = 1
        state = "done" if code == 0 else "failed"
    else:
        code = None
        state = "running" if _pid_alive(meta.get("pid")) else "lost"

    return {
        "job_id": job_dir.name,
        "state": state,
        "rc": code,
        "task": meta.get("task"),
        "target": meta.get("target"),
        "created_at": meta.get("created_at"),
        "tail": _tail(job_dir / "stdout.log", log_lines),
        "err_tail": _tail(job_dir / "stderr.log", 3) if state == "failed" else [],
    }


def list_jobs(job_root: str | Path, *, limit: int = 10) -> list[dict]:
    base = Path(job_root)
    if not base.is_dir():
        return []
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda p: p.name, reverse=True)
    rows: list[dict] = []
    for job_dir in dirs[: max(1, int(limit))]:
        meta: dict = {}
        meta_path = job_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        rc_path = job_dir / "rc"
        if rc_path.exists():
            try:
                code = int(rc_path.read_text(encoding="utf-8").strip() or "1")
            except (OSError, ValueError):
                code = 1
            state = "done" if code == 0 else "failed"
        else:
            state = "running" if _pid_alive(meta.get("pid")) else "lost"
        rows.append(
            {
                "job_id": job_dir.name,
                "state": state,
                "task": meta.get("task"),
                "target": meta.get("target"),
                "created_at": meta.get("created_at"),
            }
        )
    return rows


def cancel_job(job_root: str | Path, job_id: str) -> dict:
    job_dir = Path(job_root) / (job_id or "").strip()
    if not job_dir.is_dir():
        return {"error": f"没有这个作业：{job_id}"}
    meta: dict = {}
    meta_path = job_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    pid = meta.get("pid")
    if not _pid_alive(pid):
        return {"error": "该作业已不在运行", "job_id": job_dir.name}
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(os.getpgid(int(pid)), 15)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"取消失败：{exc}"}
    return {"job_id": job_dir.name, "state": "cancelled"}


def prune_jobs(job_root: str | Path, *, retention_days: int = 7) -> int:
    """清理过期作业目录，返回删除数量。"""
    base = Path(job_root)
    if not base.is_dir():
        return 0
    cutoff = datetime.now().timestamp() - max(1, int(retention_days)) * 86400
    removed = 0
    for job_dir in base.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            if job_dir.stat().st_mtime < cutoff:
                for child in job_dir.iterdir():
                    child.unlink(missing_ok=True)
                job_dir.rmdir()
                removed += 1
        except OSError:
            continue
    return removed
