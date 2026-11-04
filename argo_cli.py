"""argo 冲浪选源、排版与异步作业协议封装。

只依赖标准库，宿主运行与离线单测共用同一份实现。
- ``ENGINE_MAP`` 定义麦麦冲浪/查梗/ACG/游戏/热榜的语义选源映射，工具描述与 README 表格均由其派生；
- 作业协议：长任务交给脱离式进程写 ``rc`` 文件（内容为退出码），
  并在 ``meta.json`` 记录发起的 ``stream_id`` 与唤醒状态 ``notified``。
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
from typing import Any, NamedTuple

DEFAULT_ROOT = Path(os.environ.get("ARGO_ROOT") or (Path.home() / "argo"))
DEFAULT_PYTHON = os.environ.get("ARGO_PYTHON", "")


class KindSpec(NamedTuple):
    """一个语义来源类别的完整定义。"""

    engine: str
    paid: bool
    purpose: str


# kind -> 引擎/计费/适用场景。工具描述、README 表格都由它派生，不要另抄一份。
ENGINE_MAP: dict[str, KindSpec] = {
    "web": KindSpec("metaso", False, "中文综合冲浪（公众号/帖文/百科/攻略综述）"),
    "news": KindSpec("deepseek_search", True, "时事热点与权威通报（带发布日期与权威分级）"),
    "meme": KindSpec("itotii", False, "中文网络热梗、流行黑话与抽象梗溯源"),
    "slang_en": KindSpec(
        "urban_dictionary", False, "英文网络俚语、缩写与欧美迷因（Urban Dictionary）"
    ),
    "anime": KindSpec("bangumi", False, "动画、番剧、漫画、轻小说与 Galgame 资料（Bangumi）"),
    "game": KindSpec("steam", False, "Steam 游戏商店页、作品信息与发售详情"),
    "baike": KindSpec("baidu_baike", False, "中文词条、人物、作品设定与常识（百度百科）"),
    "wiki": KindSpec("zh_wikipedia", False, "跨领域概念、历史事件与客观词条（中文维基百科）"),
    "video": KindSpec("metaso_video", False, "游戏攻略视频、实况解说与二创视频检索"),
    "music": KindSpec("qq_music", False, "ACG 曲目、歌手、专辑与热门歌曲（QQ 音乐）"),
    "hot_bili": KindSpec("bilibili_hot", False, "B 站实时全站热榜（看看 B 站今天在火什么）"),
    "hot_weibo": KindSpec("weibo_hot", False, "微博实时热搜榜（社会娱乐吃瓜热点）"),
    "hot_zhihu": KindSpec("zhihu_hot_app", False, "知乎实时热榜（全网热议话题）"),
    "hot_douyin": KindSpec("douyin_hot", False, "抖音实时热点榜（短视频流行趋势）"),
    "code": KindSpec("github", False, "GitHub 开源项目、Mod、脚本与插件"),
    "web_en": KindSpec("tavily", True, "英文网页、海外游戏资讯与外网冲浪"),
}

# 以图搜图（反搜）允许的引擎与用途说明
REVERSE_IMAGE_ENGINES: dict[str, str] = {
    "saucenao": "二次元插画、动漫图、立绘、同人图找 Pixiv/Twitter 原图出处与画师（首选）",
    "animetrace": "动画截图、番剧画面、Galgame 截图识别具体角色名与所属作品名",
    "google_lens": "三次元照片、梗图原图、电影画面、游戏截图、周边商品与场景通用识图",
}

# 允许作为长任务提交的 argo 子命令
JOB_TASKS = ("research", "crawl", "pdf", "paper", "article")

# argo `search --mode` 的合法取值：fast=免费优先、auto=成本感知、deep=质量优先、budget=配额控制。
SEARCH_MODES = ("fast", "auto", "deep", "budget")
DEFAULT_MODE = "fast"

# DETACHED_PROCESS 让作业脱离宿主控制台；CREATE_NEW_PROCESS_GROUP 便于整组结束。
DETACHED_FLAGS = 0x00000008 | 0x00000200 if os.name == "nt" else 0


# ── 基础工具 ────────────────────────────────────────────────────────────


def build_env(env_file: Path | str | None = None) -> dict[str, str]:
    """复制宿主环境并注入 argo 需要的变量。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_file:
        env["ARGO_ENV_FILE"] = str(env_file)
    for key in ("NO_PROXY", "no_proxy"):
        if key in env and env[key]:
            parts = [p.strip() for p in env[key].split(",") if p.strip() and p.strip() != "[::1]"]
            env[key] = ",".join(parts)
    return env


def default_env_file() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "argo" / "env"


def default_python() -> str:
    if os.path.isfile(DEFAULT_PYTHON):
        return DEFAULT_PYTHON
    return sys.executable


def argo_root(root: str | Path | None = None) -> Path:
    return Path(root) if root else DEFAULT_ROOT


def launcher(root: Path) -> Path:
    return root / "bin" / "argo"


def resolve_engine(
    kind: str,
    enable_paid: bool,
    query: str = "",
    *,
    explicit_engine: str = "",
) -> tuple[str, str]:
    """返回 (引擎名, 降级说明)。若传了 explicit_engine 则优先采用。"""
    del query
    exp = (explicit_engine or "").strip().lower()
    if exp and exp != "auto":
        for k, spec in ENGINE_MAP.items():
            if spec.engine == exp and spec.paid and not enable_paid:
                return "metaso", f"{exp}（{k}）是计费源，已按配置改用中文综合源 metaso"
        return exp, ""

    norm_kind = (kind or "web").strip().lower()
    spec = ENGINE_MAP.get(norm_kind, ENGINE_MAP["web"])
    if spec.paid and not enable_paid:
        return "metaso", f"{norm_kind} 这一档用的是计费源，已按配置改用中文综合"
    return spec.engine, ""


def normalize_mode(mode: str | None) -> str:
    """把 ``--mode`` 收敛到 argo 认的取值；不认识的一律按 fast。"""
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


# ── CLI 同步检索（供离线冒烟与 Worker 回退使用） ─────────────────────────


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
) -> dict[str, Any]:
    """调用 ``argo search --json``，返回其 JSON 载荷或 ``{"error": ...}``。"""
    base = argo_root(root)
    argv = [
        python or default_python(),
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


def _pick(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def format_results(
    payload: dict[str, Any],
    *,
    max_items: int = 5,
    max_chars: int = 1800,
    note: str = "",
) -> str:
    """把 argo 的 JSON 载荷排成给模型看的紧凑文本。"""
    results = [
        r
        for r in (payload.get("results") or [])
        if isinstance(r, dict) and r.get("source") != "local_files"
    ]
    if not results:
        errs = payload.get("errors")
        if isinstance(errs, list) and errs:
            return f"检索未返回结果（{'; '.join(str(e) for e in errs[:2])}）。"[:max_chars]
        return "没有搜到结果。换个更具体的说法，或者换一类来源再试。"[:max_chars]

    lines: list[str] = []
    for index, item in enumerate(results[:max_items], start=1):
        title = _pick(item, "title", "name") or "（无标题）"
        url = _pick(item, "url", "link")
        source = _pick(item, "source", "engine", "upstream", "platform") or "未知来源"
        date = _pick(item, "published_at", "date", "publish_date")
        snippet = _pick(item, "snippet", "summary", "description", "content", "text")
        if len(snippet) > 140:
            snippet = snippet[:140].rstrip() + "…"
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


def format_social_results(
    payload: dict[str, Any],
    *,
    max_items: int = 8,
    max_chars: int = 2000,
) -> str:
    """把 argo_social_search 的结果排成带平台、作者与原帖链接的文本。"""
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    sentiment = payload.get("sentiment_summary")
    lines: list[str] = []
    if isinstance(sentiment, dict) and sentiment:
        summary_str = _pick(sentiment, "summary", "overview", "verdict")
        if summary_str:
            lines.append(f"【舆情速览】{summary_str}")

    for index, item in enumerate(results[:max_items], start=1):
        platform = _pick(item, "platform", "source", "engine") or "社媒"
        author = _pick(item, "author", "user", "username")
        title = _pick(item, "title")
        text = _pick(item, "text", "snippet", "content", "description")
        url = _pick(item, "url", "link")
        date = _pick(item, "published_at", "date", "created_at")

        header = f"{index}. [{platform}]"
        if author:
            header += f" @{author}"
        if title and title != text[: len(title)]:
            header += f"：{title}"
        if date:
            header += f" ({date[:10]})"

        if len(text) > 160:
            text = text[:160].rstrip() + "…"
        entry = header
        if text:
            entry += f"\n   {text}"
        if url:
            entry += f"\n   {url}"
        lines.append(entry)

    if not lines:
        errs = payload.get("errors")
        if isinstance(errs, list) and errs:
            return f"社媒未搜到结果（{'; '.join(str(e) for e in errs[:2])}）。"[:max_chars]
        return "没有搜到相关社区帖文。可以换个关键词或调整平台再试。"[:max_chars]

    out = "\n".join(lines)
    if max_chars > 0 and len(out) > max_chars:
        out = out[: max(1, max_chars - 1)].rstrip() + "…"
    return out


# ── 异步作业 ────────────────────────────────────────────────────────────


def submit_job(
    job_root: str | Path,
    task: str,
    target: str,
    *,
    stream_id: str = "",
    root: str | Path | None = None,
    env_file: str | Path | None = None,
    python: str | None = None,
) -> dict[str, Any]:
    """把一条 argo 子命令交给脱离式进程，立刻返回作业元信息。"""
    task = (task or "").strip().lower()
    if task not in JOB_TASKS:
        return {"error": f"不支持的任务：{task}（可用：{'/'.join(JOB_TASKS)}）"}
    if not (target or "").strip():
        return {"error": "任务目标为空"}

    base = argo_root(root)
    job_dir = Path(job_root) / _new_job_id()
    job_dir.mkdir(parents=True, exist_ok=True)

    py_exe = python or default_python()
    argv = [py_exe, str(launcher(base)), task, target.strip()]
    (job_dir / "argv.json").write_text(
        json.dumps(argv, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    runner = str(Path(__file__).with_name("job_runner.py"))
    try:
        proc = subprocess.Popen(
            [py_exe, runner, str(job_dir)],
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
        "stream_id": (stream_id or "").strip(),
        "notified": False,
        "argv": argv,
        "pid": proc.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "state": "running",
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def mark_job_notified(job_root: str | Path, job_id: str) -> None:
    """将作业标记为已主动唤醒通知，避免重复唤醒。"""
    meta_path = Path(job_root) / (job_id or "").strip() / "meta.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(meta, dict):
            meta["notified"] = True
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


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


def job_status(job_root: str | Path, job_id: str, *, log_lines: int = 20) -> dict[str, Any]:
    job_dir = Path(job_root) / (job_id or "").strip()
    if not job_dir.is_dir():
        return {"error": f"没有这个作业：{job_id}"}
    meta_path = job_dir / "meta.json"
    meta: dict[str, Any] = {}
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
        "stream_id": meta.get("stream_id") or "",
        "notified": bool(meta.get("notified", False)),
        "created_at": meta.get("created_at"),
        "tail": _tail(job_dir / "stdout.log", log_lines),
        "err_tail": _tail(job_dir / "stderr.log", 3) if state == "failed" else [],
    }


def list_jobs(job_root: str | Path, *, limit: int = 10) -> list[dict[str, Any]]:
    base = Path(job_root)
    if not base.is_dir():
        return []
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda p: p.name, reverse=True)
    rows: list[dict[str, Any]] = []
    for job_dir in dirs[: max(1, int(limit))]:
        meta: dict[str, Any] = {}
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
                "stream_id": meta.get("stream_id") or "",
                "notified": bool(meta.get("notified", False)),
                "created_at": meta.get("created_at"),
            }
        )
    return rows


def cancel_job(job_root: str | Path, job_id: str) -> dict[str, Any]:
    job_dir = Path(job_root) / (job_id or "").strip()
    if not job_dir.is_dir():
        return {"error": f"没有这个作业：{job_id}"}
    meta: dict[str, Any] = {}
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
