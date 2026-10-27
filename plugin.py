"""argo 检索 —— 把本机 argo 的能力交给麦麦。

两个工具：

- ``argo_lookup``：按「问题类型」选一个检索源，几秒返回带出处的条目；
- ``argo_job``：长任务提交即返回，作业在独立进程里跑，稍后再取结果。

设计要点：检索与长任务都走本机 argo 命令行（``C:\\Users\\Administrator\\argo``），
不经网络、不经 MCP；长任务用脱离式进程 + 作业目录（``rc`` 文件即完成信号），
所以工具调用永远不会长时间挂住对话。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)

# ToolParameterInfo / ToolParamType 不在 maibot_sdk 顶层导出（2.16.0 的 __all__ 里没有），
# 它们的定义在 maibot_sdk.types；从顶层导入会直接 ImportError。
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:  # 作为包的一部分加载
    from . import argo_cli
except ImportError:  # 作为单文件目录加载
    import argo_cli  # type: ignore[no-redef]

_CACHE_TTL = 300.0
_CACHE_MAX = 128


class PluginSection(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default="1.0.0",
        description="配置版本号，请勿手动修改",
        json_schema_extra={"label": "配置版本", "hidden": True},
    )


class ArgoSection(PluginConfigBase):
    """本机 argo 的位置与调用参数。"""

    __ui_label__ = "argo"
    __ui_order__ = 1

    root: str = Field(
        default=r"C:\Users\Administrator\argo",
        description="argo 仓库目录（须包含 bin/argo）",
        json_schema_extra={"label": "argo 目录", "hint": "换机器时改这里"},
    )
    python: str = Field(
        default="",
        description="启动 argo 用的解释器路径；留空表示用麦麦自身解释器",
        json_schema_extra={"label": "Python 解释器", "hint": "留空即可，需装有 PyYAML"},
    )
    env_file: str = Field(
        default="",
        description="argo 的密钥文件；留空表示用 %APPDATA%\\argo\\env",
        json_schema_extra={"label": "密钥文件", "hint": "留空走默认位置"},
    )
    mode: str = Field(
        default="fast",
        description="检索模式：fast / balanced / deep",
        json_schema_extra={"label": "检索模式"},
    )
    timeout_seconds: int = Field(
        default=25,
        description="单次检索的秒级上限",
        json_schema_extra={"label": "检索超时（秒）"},
    )


class LimitsSection(PluginConfigBase):
    """用量与输出长度。"""

    __ui_label__ = "用量"
    __ui_order__ = 2

    max_results: int = Field(
        default=5,
        description="默认返回条数（1–10）",
        json_schema_extra={"label": "默认条数"},
    )
    max_chars: int = Field(
        default=1500,
        description="回给模型的总字符上限",
        json_schema_extra={"label": "输出上限（字符）"},
    )
    calls_per_minute: int = Field(
        default=6,
        description="同一会话每分钟最多几次检索",
        json_schema_extra={"label": "每分钟上限", "hint": "防止群友刷屏"},
    )
    enable_paid: bool = Field(
        default=False,
        description="是否允许用时事/英文工程这两档的计费源",
        json_schema_extra={"label": "允许计费源", "hint": "关闭时自动改用中文综合源"},
    )


class JobsSection(PluginConfigBase):
    """长任务作业目录的维护策略。"""

    __ui_label__ = "长任务"
    __ui_order__ = 3

    retention_days: int = Field(
        default=7,
        description="作业目录保留天数",
        json_schema_extra={"label": "保留天数"},
    )
    max_log_lines: int = Field(
        default=20,
        description="查看状态时回显的日志尾部行数",
        json_schema_extra={"label": "日志行数"},
    )


class ArgoLookupConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    argo: ArgoSection = Field(default_factory=ArgoSection)
    limits: LimitsSection = Field(default_factory=LimitsSection)
    jobs: JobsSection = Field(default_factory=JobsSection)


_KIND_HINT = (
    "web=中文综合（游戏影视对比、热梗、娱乐热点、中文新闻综述都走它）；"
    "news=时事新闻（带发布日期与权威分级）；"
    "academic_cn=中文期刊题录（万方/维普）；"
    "fulltext_cn=国家哲学社会科学文献中心（中文社科，能拿全文 PDF）；"
    "academic_en=外文文献题录与摘要；"
    "doi=传 DOI 查合法免费全文；"
    "book=中文书目；image=配图与梗图；code=GitHub 仓库与插件；"
    "tech_en=英文工程资料"
)


class ArgoLookup(MaiBotPlugin):
    """插件主类。"""

    config_model = ArgoLookupConfig

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[tuple[str, str, int], tuple[float, str]] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._jobs_dir: Path | None = None

    # ── 内部辅助 ────────────────────────────────────────────────────

    def _argo_kwargs(self) -> dict[str, Any]:
        return {
            "root": self.config.argo.root,
            "env_file": self.config.argo.env_file or None,
            "python": self.config.argo.python or None,
        }

    def _rate_ok(self, stream_id: str) -> bool:
        limit = max(1, int(self.config.limits.calls_per_minute))
        bucket = self._hits[stream_id or "__global__"]
        now = time.monotonic()
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def _remember(self, key: tuple[str, str, int], text: str) -> None:
        if len(self._cache) >= _CACHE_MAX:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (time.monotonic(), text)

    def _job_root(self) -> Path:
        if self._jobs_dir is None:
            self._jobs_dir = self.ctx.paths.data_dir / "jobs"
            self._jobs_dir.mkdir(parents=True, exist_ok=True)
        return self._jobs_dir

    @staticmethod
    def _elapsed_text(created_at: str | None) -> str:
        if not created_at:
            return ""
        try:
            started = datetime.fromisoformat(created_at)
        except ValueError:
            return ""
        seconds = max(0, int((datetime.now() - started).total_seconds()))
        if seconds < 60:
            return f"{seconds} 秒"
        if seconds < 3600:
            return f"{seconds // 60} 分 {seconds % 60} 秒"
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"

    # ── 生命周期 ────────────────────────────────────────────────────

    async def on_load(self) -> None:
        self._jobs_dir = self.ctx.paths.data_dir / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        root = argo_cli.argo_root(self.config.argo.root)
        if not argo_cli.launcher(root).exists():
            self.ctx.logger.warning("argo 启动器不存在，检索类工具将不可用：%s", argo_cli.launcher(root))
        removed = argo_cli.prune_jobs(self._jobs_dir, retention_days=self.config.jobs.retention_days)
        self.ctx.logger.info("argo 检索插件已加载（清理过期作业 %s 个）", removed)

    async def on_unload(self) -> None:
        # 不杀作业进程：它们是独立进程，宿主重启也应当继续跑完。
        self._cache.clear()
        self._hits.clear()
        self.ctx.logger.info("argo 检索插件已卸载（在跑的作业不受影响）")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._cache.clear()
            self.ctx.logger.info("argo 检索配置已更新（scope=%s，配置版本=%s）", scope, version or "未标注")

    # ── 组件一：按类选源的检索 ──────────────────────────────────────

    @Tool(
        "argo_lookup",
        brief_description="按问题类型选源检索，返回几条带出处与链接的资料",
        detailed_description=(
            "想查资料、找论文、找说法出处、看最近消息时调用。一次只选一个来源类别，几秒返回。\n"
            f"kind 的取值与用途：{_KIND_HINT}。\n"
            "选源判断：群友聊游戏/影视/梗/八卦用 web；问「今天出什么事了」用 news；"
            "问「有没有研究说过」用 academic_cn 或 fulltext_cn；问外文文献用 academic_en；"
            "手上有 DOI 用 doi；找书用 book；要配图用 image；找插件或仓库用 code；"
            "写代码遇到英文技术问题用 tech_en。\n"
            "返回值已经排好版（标题｜来源与日期｜摘要｜链接），直接转述即可，不必复述全部条目。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="检索词；用群友的原话或提炼出的关键词都行",
                required=True,
            ),
            ToolParameterInfo(
                name="kind",
                param_type=ToolParamType.STRING,
                description="来源类别",
                enum_values=list(argo_cli.ENGINE_MAP.keys()),
                default="web",
                required=False,
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="最多几条（1–10），不确定就不填",
                required=False,
                default=5,
            ),
        ],
        visibility="visible",
    )
    async def handle_lookup(self, **kwargs: Any) -> dict:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return {"success": False, "content": "缺少 query：要查什么？"}

        kind = str(kwargs.get("kind") or "web").strip().lower()
        try:
            want = int(kwargs.get("max_results") or self.config.limits.max_results)
        except (TypeError, ValueError):
            want = int(self.config.limits.max_results)
        want = max(1, min(10, want))

        stream_id = str(kwargs.get("stream_id") or "")
        if not self._rate_ok(stream_id):
            return {"success": False, "content": "这个问题问得有点快，等一分钟再查。"}

        engine, note = argo_cli.resolve_engine(kind, self.config.limits.enable_paid, query)
        cache_key = (engine, query, want)
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            return {"success": True, "content": cached[1]}

        payload = await asyncio.to_thread(
            argo_cli.search,
            query,
            engine,
            max_results=want,
            mode=self.config.argo.mode,
            timeout=int(self.config.argo.timeout_seconds),
            **self._argo_kwargs(),
        )
        if payload.get("error"):
            return {"success": False, "content": f"检索失败：{payload['error']}"}

        text = argo_cli.format_results(
            payload,
            max_items=want,
            max_chars=int(self.config.limits.max_chars),
            note=note,
        )
        self._remember(cache_key, text)
        return {"success": True, "content": text}

    # ── 组件二：长任务 ──────────────────────────────────────────────

    @Tool(
        "argo_job",
        brief_description="提交或查看本机长任务（深度研究、站点抓取、PDF 与论文深读）",
        detailed_description=(
            "需要几分钟以上的任务用这个工具，不要用 argo_lookup 硬等。\n"
            "action=submit 提交任务：task 选 research（深度研究，出一份综合报告）、"
            "crawl（抓整站）、pdf（读一个 PDF 链接）、paper（读 arXiv 论文）、"
            "article（读公众号文章）；target 填查询词或链接。提交后立刻返回作业号，"
            "任务在后台独立进程里跑，先告诉群友「已经在查，稍后播报」。\n"
            "action=status 查看进度与日志尾部；action=list 列出最近的作业；"
            "action=cancel 取消。\n"
            "用法示例：先 submit，过一会儿用同一个 job_id 调 status，跑完把结论转述出来。"
        ),
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="submit 提交 / status 查看 / list 列表 / cancel 取消",
                enum_values=["submit", "status", "list", "cancel"],
                default="status",
                required=False,
            ),
            ToolParameterInfo(
                name="task",
                param_type=ToolParamType.STRING,
                description="任务类型，仅 submit 时需要",
                enum_values=list(argo_cli.JOB_TASKS),
                required=False,
            ),
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="查询词或链接，仅 submit 时需要",
                required=False,
            ),
            ToolParameterInfo(
                name="job_id",
                param_type=ToolParamType.STRING,
                description="作业号，status 与 cancel 时填",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def handle_job(self, **kwargs: Any) -> dict:
        action = str(kwargs.get("action") or "status").strip().lower()
        job_root = self._job_root()

        if action == "submit":
            task = str(kwargs.get("task") or "").strip()
            target = str(kwargs.get("target") or "").strip()
            meta = await asyncio.to_thread(
                argo_cli.submit_job,
                job_root,
                task,
                target,
                **self._argo_kwargs(),
            )
            if meta.get("error"):
                return {"success": False, "content": f"提交失败：{meta['error']}"}
            return {
                "success": True,
                "content": (
                    f"已提交作业 {meta['job_id']}（{meta['task']}：{meta['target']}）。"
                    f"它在后台独立进程里跑，先告诉群友已经开始了；"
                    f"过一会儿用 argo_job action=status job_id={meta['job_id']} 取结果。"
                ),
            }

        if action == "list":
            rows = await asyncio.to_thread(argo_cli.list_jobs, job_root, limit=10)
            if not rows:
                return {"success": True, "content": "还没有作业记录。"}
            lines = [
                f"{row['job_id']}｜{row['state']}｜{row.get('task') or '?'}：{(row.get('target') or '')[:40]}"
                for row in rows
            ]
            return {"success": True, "content": "\n".join(lines)}

        job_id = str(kwargs.get("job_id") or "").strip()
        if action == "cancel":
            if not job_id:
                return {"success": False, "content": "要取消哪个作业？请给 job_id。"}
            result = await asyncio.to_thread(argo_cli.cancel_job, job_root, job_id)
            if result.get("error"):
                return {"success": False, "content": result["error"]}
            return {"success": True, "content": f"作业 {result['job_id']} 已取消。"}

        if not job_id:
            rows = await asyncio.to_thread(argo_cli.list_jobs, job_root, limit=1)
            if not rows:
                return {"success": False, "content": "没有正在跑的作业，也没有给 job_id。"}
            job_id = rows[0]["job_id"]

        info = await asyncio.to_thread(
            argo_cli.job_status,
            job_root,
            job_id,
            log_lines=int(self.config.jobs.max_log_lines),
        )
        if info.get("error"):
            return {"success": False, "content": info["error"]}

        state_map = {"running": "运行中", "done": "已完成", "failed": "失败", "lost": "已失联"}
        state = state_map.get(info.get("state") or "", info.get("state") or "未知")
        head = f"作业 {info['job_id']}｜{state}"
        if info.get("task"):
            head += f"｜{info['task']}：{(info.get('target') or '')[:60]}"
        elapsed = self._elapsed_text(info.get("created_at"))
        if elapsed and info.get("state") == "running":
            head += f"｜已跑 {elapsed}"
        if info.get("rc") is not None:
            head += f"｜退出码 {info['rc']}"

        tail = info.get("tail") or []
        body = "\n".join(tail) if tail else "（还没有输出）"
        if len(body) > int(self.config.limits.max_chars):
            body = body[: int(self.config.limits.max_chars)].rstrip() + "…"

        content = f"{head}\n—— 输出尾部 ——\n{body}"
        if info.get("err_tail"):
            content += "\n—— 错误摘要 ——\n" + "\n".join(info["err_tail"])[:400]
        return {"success": True, "content": content}


def create_plugin() -> MaiBotPlugin:
    """宿主靠这个工厂拿插件实例。"""
    return ArgoLookup()
