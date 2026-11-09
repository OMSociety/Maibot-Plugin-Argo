"""Argo 检索桥接器 —— 面向麦麦的冲浪、查梗、ACG/游戏百科、社区风评与搜图识图插件。

七个工具：
1. ``argo_search``：按语义类别 ``kind``（或显式 ``engine``）查梗、查番剧/游戏/百科、
   看全网实时热榜；
2. ``argo_image_search``：按关键词搜动漫图、梗图、游戏截图与角色图，把图片经
   ``content_items`` 回传给麦麦视觉查看；
3. ``argo_reverse_image``：以图搜图与动漫角色识别（``saucenao`` / ``animetrace`` /
   ``google_lens``），自动从聊天消息取图并回传缩略图供麦麦核验；
4. ``argo_social_search``：跨 Bilibili、Twitter/X、小红书、Reddit、V2EX、Hacker News、
   YouTube 搜玩家社区讨论与实时风评；
5. ``argo_fetch``：打开网页、攻略长贴、微信公众号文章或单条推文读取正文；
6. ``argo_job``：多源长线深扒或整站/长文档异步作业，跑完后自动携带摘要唤醒麦麦；
7. ``argo_send_image``：把选中的结果图按原分辨率带回并发送给群友。
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
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:
    from . import argo_cli, image_helper, worker
except ImportError:
    import argo_cli  # type: ignore[no-redef]
    import image_helper  # type: ignore[no-redef]
    import worker  # type: ignore[no-redef]

_CACHE_TTL = 300.0
_CACHE_MAX = 128
_WATCH_POLL_SECONDS = 2
_WEIXIN_HOST = "mp.weixin.qq.com"
_TWEET_URL_PREFIXES = (
    "https://x.com/",
    "https://twitter.com/",
    "http://x.com/",
    "http://twitter.com/",
)


def fail(message: str) -> dict[str, Any]:
    """失败结果。宿主取 ``error`` 当错误信息、``content`` 当可见文本，两个字段都填。"""
    return {"success": False, "error": message, "content": message}


def _int_arg(
    kwargs: dict[str, Any],
    name: str,
    default: int,
    lo: int,
    hi: int | None,
) -> int:
    """取整数参数并夹到 ``[lo, hi]``；缺失、非法或为空时用 ``default``。"""
    try:
        value = int(kwargs.get(name) or default)
    except (TypeError, ValueError):
        value = default
    value = max(lo, value)
    return min(hi, value) if hi is not None else value


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
        default=str(argo_cli.DEFAULT_ROOT),
        description="argo 仓库目录（须包含 bin/argo 与 scripts/mcp_server.py）",
        json_schema_extra={"label": "argo 目录", "hint": "留空则取 ARGO_ROOT 或用户目录下的 argo"},
    )
    python: str = Field(
        default=argo_cli.DEFAULT_PYTHON,
        description="启动 argo 常驻 Worker 用的 Python 解释器路径（需装有 curl_cffi 等依赖）",
        json_schema_extra={
            "label": "Python 解释器",
            "hint": "留空取 ARGO_PYTHON，再退回当前进程解释器",
        },
    )
    env_file: str = Field(
        default="",
        description="argo 的密钥文件；留空表示用 %APPDATA%\\argo\\env",
        json_schema_extra={"label": "密钥文件", "hint": "留空走默认位置"},
    )
    proxy: str = Field(
        default="",
        description="下载搜图/反搜预览缩略图时使用的 HTTP 代理（填 direct 则直连）",
        json_schema_extra={"label": "图片下载代理"},
    )
    mode: str = Field(
        default=argo_cli.DEFAULT_MODE,
        description=(
            "检索模式：fast（免费优先）/ auto（成本感知）/ deep（质量优先）/ budget（配额控制）"
        ),
        json_schema_extra={"label": "检索模式", "hint": "写错时按 fast 处理"},
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
        default=1800,
        description="回给模型的总字符上限",
        json_schema_extra={"label": "输出上限（字符）"},
    )
    calls_per_minute: int = Field(
        default=10,
        description="同一会话每分钟最多几次检索",
        json_schema_extra={"label": "每分钟上限", "hint": "防止群友刷屏"},
    )
    enable_paid: bool = Field(
        default=True,
        description="是否允许用时事（deepseek_search）/ 英文冲浪（tavily）这两档计费源",
        json_schema_extra={"label": "允许计费源", "hint": "关闭时自动改用中文综合源 metaso"},
    )


class JobsSection(PluginConfigBase):
    """长任务作业目录与主动唤醒策略。"""

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
    job_watch_seconds: int = Field(
        default=720,
        description="提交后台作业后守候完成的秒数，超时即停止轮询",
        json_schema_extra={"label": "守候时长（秒）"},
    )
    proactive_notify: bool = Field(
        default=True,
        description="后台长任务完成后是否自动携带结果摘要唤醒麦麦汇报",
        json_schema_extra={"label": "完成主动唤醒"},
    )


class ArgoConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    argo: ArgoSection = Field(default_factory=ArgoSection)
    limits: LimitsSection = Field(default_factory=LimitsSection)
    jobs: JobsSection = Field(default_factory=JobsSection)


_KIND_HINT = "；".join(f"{kind}={spec.purpose}" for kind, spec in argo_cli.ENGINE_MAP.items())
_REVERSE_HINT = "；".join(f"{eng}={desc}" for eng, desc in argo_cli.REVERSE_IMAGE_ENGINES.items())
_IMAGE_HINT = "；".join(f"{eng}={desc}" for eng, desc in argo_cli.IMAGE_SEARCH_ENGINES.items())


class ArgoPlugin(MaiBotPlugin):
    """Argo 检索桥接器插件主类。"""

    config_model = ArgoConfig

    def __init__(self) -> None:
        super().__init__()
        self._cache: dict[tuple[str, str, int, str], tuple[float, str]] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._data_dirs: dict[str, Path] = {}
        self._worker: worker.ArgoWorker | None = None
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}

    # ── 内部辅助 ────────────────────────────────────────────────────

    def _argo_kwargs(self) -> dict[str, Any]:
        return {
            "root": self.config.argo.root,
            "env_file": self.config.argo.env_file or None,
            "python": self.config.argo.python or argo_cli.default_python(),
        }

    def _data_dir(self, name: str) -> Path:
        cached = self._data_dirs.get(name)
        if cached is None:
            try:
                base = self.ctx.paths.data_dir
            except Exception:
                base = Path(__file__).resolve().parent / "data"
            cached = base / name
            cached.mkdir(parents=True, exist_ok=True)
            self._data_dirs[name] = cached
        return cached

    def _get_worker(self) -> worker.ArgoWorker:
        if self._worker is None:
            env_file = self.config.argo.env_file or str(argo_cli.default_env_file())
            self._worker = worker.ArgoWorker(
                python_path=self.config.argo.python or argo_cli.default_python(),
                argo_dir=self.config.argo.root,
                argo_env_file=env_file,
                temp_image_root=self._data_dir("temp_images"),
                default_timeout=float(self.config.argo.timeout_seconds),
            )
        return self._worker

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

    def _remember(self, key: tuple[str, str, int, str], text: str) -> None:
        self._cache.pop(key, None)
        while len(self._cache) >= _CACHE_MAX:
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = (time.monotonic(), text)

    def _touch(self, key: tuple[str, str, int, str]) -> None:
        self._cache[key] = self._cache.pop(key)

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

    async def _watch_job_and_notify(self, job_id: str, stream_id: str) -> None:
        """后台轮询作业目录的 rc 文件；一旦完成则携带输出摘要主动唤醒麦麦。"""
        job_root = self._data_dir("jobs")
        try:
            watch_seconds = max(1, int(self.config.jobs.job_watch_seconds))
            for _ in range(max(1, watch_seconds // _WATCH_POLL_SECONDS)):
                await asyncio.sleep(_WATCH_POLL_SECONDS)
                info = await asyncio.to_thread(
                    argo_cli.job_status,
                    job_root,
                    job_id,
                    log_lines=int(self.config.jobs.max_log_lines),
                )
                state = info.get("state")
                if state in ("done", "failed", "lost"):
                    if not info.get("notified") and stream_id and self.config.jobs.proactive_notify:
                        await asyncio.to_thread(argo_cli.mark_job_notified, job_root, job_id)
                        tail_lines = info.get("tail") or []
                        summary = "\n".join(tail_lines)[-1200:] if tail_lines else "（无输出内容）"
                        state_cn = "已完成" if state == "done" else f"异常结束({state})"
                        head = f"后台长任务 {job_id}（{info.get('task')}：{info.get('target')}）"
                        intent = (
                            f"{head}{state_cn}。\n"
                            f"结果摘要：\n{summary}\n"
                            "请用你平时聊天的语气自然地把查到的关键信息告诉群友。"
                        )
                        try:
                            await self.ctx.maisaka.proactive.trigger(
                                stream_id,
                                intent,
                                source="omsociety.argo",
                                reason=f"argo_job {job_id} {state}",
                                metadata={"job_id": job_id, "state": state},
                            )
                        except Exception as exc:
                            self.ctx.logger.warning("主动唤醒麦麦失败 (job=%s): %s", job_id, exc)
                    return
        except asyncio.CancelledError:
            return
        finally:
            self._watch_tasks.pop(job_id, None)

    # ── 生命周期 ────────────────────────────────────────────────────

    async def on_load(self) -> None:
        self._data_dir("jobs")
        self._data_dir("temp_images")
        root = argo_cli.argo_root(self.config.argo.root)
        if not argo_cli.launcher(root).exists():
            self.ctx.logger.warning(
                "argo 启动器不存在，检索类工具将不可用：%s", argo_cli.launcher(root)
            )
        mode = (self.config.argo.mode or "").strip().lower()
        if mode not in argo_cli.SEARCH_MODES:
            self.ctx.logger.warning(
                "argo.mode=%r 不是 argo 认的取值（%s），检索按 %s 处理",
                self.config.argo.mode,
                "/".join(argo_cli.SEARCH_MODES),
                argo_cli.DEFAULT_MODE,
            )
        removed = argo_cli.prune_jobs(
            self._data_dir("jobs"), retention_days=self.config.jobs.retention_days
        )
        try:
            await self._get_worker().start()
        except Exception as exc:
            self.ctx.logger.warning("Argo 常驻 Worker 预热失败（将在首次调用时重试）：%s", exc)
        self.ctx.logger.info("Argo 检索桥接器已加载（清理过期作业 %s 个）", removed)

    async def on_unload(self) -> None:
        for task in list(self._watch_tasks.values()):
            task.cancel()
        self._watch_tasks.clear()
        if self._worker is not None:
            await self._worker.close()
            self._worker = None
        self._cache.clear()
        self._hits.clear()
        self.ctx.logger.info("Argo 检索桥接器已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._cache.clear()
            if self._worker is not None:
                self._worker.update_settings(
                    python_path=self.config.argo.python or argo_cli.default_python(),
                    argo_dir=self.config.argo.root,
                    argo_env_file=self.config.argo.env_file or str(argo_cli.default_env_file()),
                    default_timeout=float(self.config.argo.timeout_seconds),
                )
            self.ctx.logger.info(
                "Argo 检索桥接器配置已热更新（scope=%s，版本=%s）", scope, version or "未标注"
            )

    # ── 工具 1：网上冲浪与分类检索 ──────────────────────────────────

    @Tool(
        "argo_search",
        brief_description="网上冲浪、查热梗、查番剧与游戏设定、查百科词条或看全网实时热榜",
        detailed_description=(
            "麦麦上网查资料、懂新梗、查 ACG 与游戏情报、看热搜的主力工具，毫秒级常驻通道返回。\n"
            f"优先按语义选 kind：{_KIND_HINT}。\n"
            "选路指南：\n"
            "- 看不懂的抽象话/新梗/流行黑话 -> kind=meme（英文梗/缩写用 kind=slang_en）；\n"
            "- 动画/番剧/漫画/轻小说/Galgame -> kind=anime；Steam 游戏/发售/评测 -> kind=game；\n"
            "- 人物/作品设定/常识词条 -> kind=baike 或 kind=wiki；攻略视频 -> kind=video；\n"
            "  查歌 -> kind=music；\n"
            "- 问「最近B站/微博/知乎/抖音在火什么」 -> kind=hot_bili / hot_weibo /\n"
            "  hot_zhihu / hot_douyin；\n"
            "- 今天刚出的大事/通报 -> kind=news；日常中文综合冲浪 -> kind=web；\n"
            "  外网英文资讯 -> kind=web_en。\n"
            "若需要直指底层特定引擎，也可填可选参数 engine（填了 engine 会优先于 kind）。\n"
            "判据补充：\n"
            "- kind=news（deepseek_search）涉时事、突发或涉外时，自己按领域选定权威来源，"
            "用 include_domains 限定后重搜；百家号、网易号等聚合洗稿站只当线索，不作事实依据；\n"
            "- kind=web_en（tavily）只查英文与海外内容，中文 query 不要走它——"
            "中文摘要截断、索引不稳，常返回营销号与机翻站；\n"
            "- kind=web（metaso）涉当下的事把年份写进 query，时效衰减弱，旧文会混入。\n"
            "注意：搜图片请调 argo_image_search，以图搜图请调 argo_reverse_image。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="检索关键词；看热榜（hot_*）时随便填如『热榜』即可",
                required=True,
            ),
            ToolParameterInfo(
                name="kind",
                param_type=ToolParamType.STRING,
                description="语义来源类别",
                enum_values=list(argo_cli.ENGINE_MAP.keys()),
                default="web",
                required=False,
            ),
            ToolParameterInfo(
                name="engine",
                param_type=ToolParamType.STRING,
                description="可选：显式指定底层 Argo 引擎名（留空则按 kind 自动映射）",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="返回条数（1–10），默认 5",
                required=False,
                default=5,
            ),
            ToolParameterInfo(
                name="include_domains",
                param_type=ToolParamType.ARRAY,
                description='仅保留这些域名（含子域）的结果，如 ["gov.cn", "news.cn"]',
                required=False,
                default=[],
            ),
            ToolParameterInfo(
                name="exclude_domains",
                param_type=ToolParamType.ARRAY,
                description='排除这些域名（含子域）的结果，如 ["baijiahao.baidu.com"]',
                required=False,
                default=[],
            ),
        ],
        visibility="visible",
    )
    async def handle_search(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")

        query = str(kwargs.get("query") or "").strip()
        if not query:
            return fail("缺少 query：要查什么？")

        kind = str(kwargs.get("kind") or "web").strip().lower()
        explicit_engine = str(kwargs.get("engine") or "").strip().lower()
        if explicit_engine in argo_cli.REVERSE_IMAGE_ENGINES:
            return fail(f"{explicit_engine} 是以图搜图引擎，请改调专用工具 argo_reverse_image。")
        if explicit_engine == "metaso_image":
            return fail("搜图片请改调专用工具 argo_image_search（支持把图片直接带回给你查看）。")

        want = _int_arg(kwargs, "max_results", int(self.config.limits.max_results), 1, 10)
        include_domains = [
            str(d).strip().lower() for d in (kwargs.get("include_domains") or []) if str(d).strip()
        ]
        exclude_domains = [
            str(d).strip().lower() for d in (kwargs.get("exclude_domains") or []) if str(d).strip()
        ]

        stream_id = str(kwargs.get("stream_id") or "")
        if not self._rate_ok(stream_id):
            return fail("查得太快啦，歇几秒钟再试。")

        engine, note = argo_cli.resolve_engine(
            kind,
            self.config.limits.enable_paid,
            explicit_engine=explicit_engine,
        )
        mode = argo_cli.normalize_mode(self.config.argo.mode)
        cache_key = (engine, query, want, mode, tuple(include_domains), tuple(exclude_domains))
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL:
            self._touch(cache_key)
            return {"success": True, "content": cached[1]}

        payload = await self._get_worker().call_tool(
            "argo_search",
            {
                "query": query,
                "engine": engine,
                "max_results": want,
                "mode": mode,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
                "include_local": False,
                "summary": True,
            },
            timeout=float(self.config.argo.timeout_seconds),
        )
        if payload.get("error"):
            # 常驻 Worker 异常时回退一次 CLI
            payload = await asyncio.to_thread(
                argo_cli.search,
                query,
                engine,
                max_results=want,
                mode=mode,
                timeout=int(self.config.argo.timeout_seconds),
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                **self._argo_kwargs(),
            )
        if payload.get("error"):
            return fail(f"检索失败：{payload['error']}")

        # 垂直引擎常因语种或收录范围空手而归（如 Steam 商店按英文商店检索中文名），
        # 走 kind 选路时补一次综合网页检索，避免群友只得到一句「没找到」。
        fallback_note = ""
        if not payload.get("count") and not explicit_engine and engine != "metaso":
            retry = await self._get_worker().call_tool(
                "argo_search",
                {
                    "query": query,
                    "engine": "metaso",
                    "max_results": want,
                    "mode": mode,
                    "include_domains": include_domains,
                    "exclude_domains": exclude_domains,
                    "include_local": False,
                    "summary": True,
                },
                timeout=float(self.config.argo.timeout_seconds),
            )
            if retry.get("count"):
                payload = retry
                fallback_note = f"（{engine} 未命中，已回退综合检索）\n"

        text = argo_cli.format_results(
            payload,
            max_items=want,
            max_chars=int(self.config.limits.max_chars),
            note=note,
        )
        if fallback_note:
            text = fallback_note + text
        self._remember(cache_key, text)
        return {"success": True, "content": text}

    # ── 工具 2：按词搜配图/梗图（带视觉回传） ─────────────────────────

    @Tool(
        "argo_image_search",
        brief_description="按关键词搜索动漫图、角色图、表情包梗图或游戏截图，并把图片带回给麦麦查看和发送",
        detailed_description=(
            "想找某个人物/番剧的图片、搜表情包梗图、看某款游戏或物品长什么样时调用；"
            "回答到具体角色、作品、画风或物品长什么样时，也可以主动搜图自然配上 1–2 张，"
            "不必等群友开口。\n"
            "本工具带回的是轻量候选图（content_items）：先亲眼看过，挑中满意的再调 "
            "argo_send_image 按原分辨率带回并发送。\n"
            f"引擎选路：{_IMAGE_HINT}。按角色名或作品名找 Pixiv 插画时选 pixiv——query 是标签串，"
            "空格分隔多个标签，按标签匹配"
            "（如「東方Project10000users入り フランドール 白タイツ」）；"
            "标签为作品的内容标签：作品名、角色名、题材、服装或 10000users入り 这类收藏夹标签，"
            "角色名写日文名或中文名都能命中。"
            "描述句与表情包、截图这类体裁词用 metaso_image，中文描述即可。"
            "作者名属作者字段：查某位画师的作品时 query 写 画师:<画师名>、"
            "user:<画师 id> 或画师主页链接，"
            "可再接标签收窄（如「画师:ke-ta フランドール」）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description=(
                    "搜图关键词。pixiv 写标签串（空格分隔，如「初音ミク 制服」），"
                    "查画师写「画师:ke-ta」或「user:3104565」；metaso_image 写中文描述。"
                ),
                required=True,
            ),
            ToolParameterInfo(
                name="engine",
                param_type=ToolParamType.STRING,
                description="搜图引擎：metaso_image 搜通用图，pixiv 搜公开插画与漫画",
                enum_values=list(argo_cli.IMAGE_SEARCH_ENGINES.keys()),
                default="metaso_image",
                required=False,
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description=(
                    "最多带回几张候选图供查看（1–8，默认 6）；挑中后用 argo_send_image 原图发出"
                ),
                required=False,
                default=6,
            ),
        ],
        visibility="visible",
    )
    async def handle_image_search(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")

        query = str(kwargs.get("query") or "").strip()
        if not query:
            return fail("缺少 query：要搜什么图片？")

        engine = str(kwargs.get("engine") or "metaso_image").strip().lower()
        if engine not in argo_cli.IMAGE_SEARCH_ENGINES:
            return fail(
                f"不支持的搜图引擎：{engine}（可选：{' / '.join(argo_cli.IMAGE_SEARCH_ENGINES)}）"
            )

        limit = _int_arg(kwargs, "max_results", 6, 1, 8)

        stream_id = str(kwargs.get("stream_id") or "")
        if not self._rate_ok(stream_id):
            return fail("搜图太频繁啦，稍等一下再试。")

        payload = await self._get_worker().call_tool(
            "argo_search",
            {
                "query": query,
                "engine": engine,
                "max_results": max(10, limit * 2),
                "mode": argo_cli.normalize_mode(self.config.argo.mode),
                "include_local": False,
                "summary": False,
            },
            timeout=float(self.config.argo.timeout_seconds),
        )
        if payload.get("error"):
            return fail(f"搜图失败：{payload['error']}")

        raw_results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
        if not raw_results:
            return fail(f"没有搜到与「{query}」相关的图片。")

        content_items: list[dict[str, Any]] = []
        lines: list[str] = []
        proxy = self.config.argo.proxy
        budget = image_helper.MAX_FRAME_BUDGET

        for item in raw_results:
            if len(content_items) >= limit or budget <= 0:
                break
            img_url = str(item.get("image_url") or item.get("url") or "").strip()
            if not img_url:
                continue
            title = str(item.get("title") or query).strip()
            idx = len(content_items) + 1
            desc = f"搜索「{query}」第 {idx} 张图：{title}（来源：{img_url}）"
            c_item = await image_helper.download_image_as_content_item(
                img_url,
                description=desc,
                preview_url=str(item.get("image_url_preview") or "").strip() or None,
                name_prefix=f"imgsearch_{idx}",
                proxy=proxy,
                max_base64_bytes=min(
                    image_helper.CANDIDATE_BASE64_BUDGET,
                    max(budget - image_helper.ENVELOPE_OVERHEAD, 0),
                ),
                max_edge=image_helper.CANDIDATE_MAX_EDGE,
                quality=image_helper.CANDIDATE_QUALITY,
            )
            if c_item is not None:
                content_items.append(c_item)
                budget -= len(c_item["data"]) + image_helper.ENVELOPE_OVERHEAD
                credits = []
                author = str(item.get("author") or "").strip()
                if author:
                    credits.append(f"作者：{author}")
                tags = item.get("tags")
                if isinstance(tags, list):
                    names = [str(t).strip() for t in tags[:3] if str(t).strip()]
                    if names:
                        credits.append("标签：" + " / ".join(names))
                suffix = f"（{'；'.join(credits)}）" if credits else ""
                lines.append(f"{idx}. {title}{suffix}\n   图片直链：{img_url}")

        if not content_items:
            text_fallback = argo_cli.format_results(
                payload, max_items=limit, max_chars=int(self.config.limits.max_chars)
            )
            return {
                "success": True,
                "content": "搜到了候选图片，但缩略图防盗链未能直接拉取，仅返回链接：\n"
                + text_fallback,
            }

        summary_text = (
            f"已搜索「{query}」并带回 {len(content_items)} 张图片到你的视觉上下文"
            "（如果要发给群友，可在 reply 的 attach_pic 填对应 tool_result 编号）：\n"
            + "\n".join(lines)
        )
        return {
            "success": True,
            "content": summary_text,
            "content_items": content_items,
        }

    # ── 工具 3：以图搜图与动漫角色识别（自动取图 + 缩略图回传） ─────

    @Tool(
        "argo_reverse_image",
        brief_description=(
            "以图搜图与动漫角色识别：查插画 Pixiv 原图画师、认番剧截图角色、查梗图或照片出处"
        ),
        detailed_description=(
            "当群友发了一张图问『这是谁』『出自哪部番』『求画师/原图/P站ID』时调用。\n"
            f"必选 engine：{_REVERSE_HINT}。\n"
            "二次元图（插画、动漫截图、角色立绘）按顺序试：saucenao → animetrace，"
            "两者都返回空才用 google_lens。\n"
            "图片获取方式：\n"
            "1. 搜聊天里的图（最常用）：传目标图片所在消息的 msg_id"
            "（留空则自动找当前会话最新一张图），三条引擎都吃得下；\n"
            "2. 搜公网图片链接：直接填 image_url。\n"
            "插件会把匹配结果首条缩略图放入 content_items 供你亲眼核对与用 attach_pic 发出。\n"
            "相似度不足 90% 的匹配只是相似作品线索，不得断言为同一张图、更不得据此指认作者；"
            "结果里的数字是作品编号，不是画师编号。"
        ),
        parameters=[
            ToolParameterInfo(
                name="engine",
                param_type=ToolParamType.STRING,
                description=(
                    "反搜引擎：saucenao（二次元原图/画师首选）/ animetrace（认动漫角色与番名）"
                    "/ google_lens（通用识图：真人照片、梗图原图、商品与场景）"
                ),
                enum_values=list(argo_cli.REVERSE_IMAGE_ENGINES.keys()),
                default="saucenao",
                required=True,
            ),
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description=("可选：聊天消息编号（如 m12）；留空且未填 image_url 时取最新带图消息"),
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="image_index",
                param_type=ToolParamType.INTEGER,
                description="同一条消息里第几张图（从 0 开始，默认 0）",
                required=False,
                default=0,
            ),
            ToolParameterInfo(
                name="image_url",
                param_type=ToolParamType.STRING,
                description="可选：公网图片直链（http/https）；搜聊天图片时留空",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="返回匹配条数（1–8，默认 5）",
                required=False,
                default=5,
            ),
        ],
        visibility="visible",
    )
    async def handle_reverse_image(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")

        engine = str(kwargs.get("engine") or "saucenao").strip().lower()
        if engine not in argo_cli.REVERSE_IMAGE_ENGINES:
            return fail(
                f"不支持的反搜引擎：{engine}（可选：{' / '.join(argo_cli.REVERSE_IMAGE_ENGINES)}）"
            )

        msg_id = str(kwargs.get("msg_id") or "").strip()
        image_index = _int_arg(kwargs, "image_index", 0, 0, None)
        want = _int_arg(kwargs, "max_results", 5, 1, 8)

        raw_url = str(kwargs.get("image_url") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")

        temp_file: Path | None = None
        if raw_url:
            if not image_helper.is_public_http_url(raw_url):
                return fail(
                    "image_url 仅接受公网 http(s):// 图片直链；"
                    "搜聊天图片请留空 image_url 并传 msg_id。"
                )
            query_target = raw_url
        else:
            temp_file, err = await image_helper.resolve_chat_image_to_file(
                self.ctx,
                stream_id=stream_id,
                msg_id=msg_id,
                image_index=image_index,
                temp_dir=self._data_dir("temp_images"),
            )
            if err or temp_file is None:
                return fail(err or "无法读取聊天图片。")
            query_target = str(temp_file)

        try:
            payload = await self._get_worker().call_tool(
                "argo_search",
                {
                    "query": query_target,
                    "engine": engine,
                    "max_results": want,
                    "include_local": False,
                    "summary": False,
                },
                timeout=max(30.0, float(self.config.argo.timeout_seconds)),
            )
        finally:
            if temp_file is not None:
                temp_file.unlink(missing_ok=True)

        if payload.get("error"):
            return fail(f"反搜图失败：{payload['error']}")

        results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
        text = argo_cli.format_results(
            payload,
            max_items=want,
            max_chars=int(self.config.limits.max_chars),
        )

        content_items: list[dict[str, Any]] = []
        for item in results:
            thumb_url = str(item.get("image_url") or "").strip()
            if not thumb_url:
                continue
            title = str(item.get("title") or "反搜匹配预览").strip()
            src_url = str(item.get("url") or "").strip()
            c_item = await image_helper.download_image_as_content_item(
                thumb_url,
                description=f"[{engine}] 匹配预览：{title}（出处：{src_url}）",
                preview_url=str(item.get("image_url_preview") or "").strip() or None,
                name_prefix=f"rev_{engine}",
                proxy=self.config.argo.proxy,
            )
            if c_item is not None:
                content_items.append(c_item)
                break

        out: dict[str, Any] = {"success": True, "content": text}
        if content_items:
            out["content"] = (
                text + "\n（已将首条匹配缩略图带回你的视觉上下文，核对画面是否一致；"
                "出处链接以条目中的 url 为准）"
            )
            out["content_items"] = content_items
        return out

    # ── 工具 4：发送所选图片（按原分辨率带回） ─────────────────────

    @Tool(
        "argo_send_image",
        brief_description="把搜索结果里选中的图片按原分辨率带回，供 reply 发送给群友",
        detailed_description=(
            "只在搜图/反搜结果里看过候选、确认画面后再调用；image_url 填结果条目里的图片直链。\n"
            "图片会进入本工具结果（content_items），随后在 reply 工具里用 attach_pic="
            '"tool_result:<call_id>:1" 发给群友；直接发，不要预告，不要复述发送过程。\n'
            "reply 返回 success:false（图片没发出去）时，换一张没发过的结果图重发；"
            "没有可换的就只补文字，不解释、不道歉。"
        ),
        parameters=[
            ToolParameterInfo(
                name="image_url",
                param_type=ToolParamType.STRING,
                description="图片直链，必填；填搜图/反搜结果条目里的图片直链",
                required=True,
            ),
            ToolParameterInfo(
                name="preview_url",
                param_type=ToolParamType.STRING,
                description="预览直链（可选）；image_url 取不到时改用它",
                required=False,
                default="",
            ),
        ],
    )
    async def handle_send_image(
        self, image_url: str, preview_url: str = "", **kwargs
    ) -> dict[str, Any]:
        image_url = (image_url or "").strip()
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")
        if not image_helper.is_public_http_url(image_url):
            return fail("image_url 需要是公网图片直链")
        preview = (preview_url or "").strip() or None
        if preview and not image_helper.is_public_http_url(preview):
            preview = None
        c_item = await image_helper.download_image_as_content_item(
            image_url,
            description="待发送图片",
            preview_url=preview,
            name_prefix="send_img",
            proxy=self.config.argo.proxy,
            max_base64_bytes=image_helper.MAX_FRAME_BUDGET - image_helper.ENVELOPE_OVERHEAD,
        )
        if not c_item:
            return fail("图片下载失败：该直链暂时取不到，可换一张候选图重试")
        return {
            "success": True,
            "content": (
                "已把这张图片按原分辨率放入本工具结果（content_items）。"
                '发送：reply 工具 attach_pic="tool_result:<call_id>:1"。'
            ),
            "content_items": [c_item],
        }

    # ── 工具 5：玩家社区与社媒舆情检索 ──────────────────────────────

    @Tool(
        "argo_social_search",
        brief_description=(
            "跨 Bilibili、Twitter/X、小红书、Reddit、V2EX、Hacker News、YouTube "
            "搜玩家热议与社区风评"
        ),
        detailed_description=(
            "想看网友/玩家对某部新番、游戏、更新补丁、热点事件的真实评价、吐槽或爆料时调用。\n"
            "platforms 传逗号分隔的平台名，可选：bilibili, twitter, xiaohongshu, reddit, "
            "v2ex, hackernews, youtube。\n"
            "mode=text 返回带作者与原帖链接的帖文列表；"
            "mode=sentiment 额外汇总多平台正负面风评倾向。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="社区搜索关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="platforms",
                param_type=ToolParamType.STRING,
                description="逗号分隔的平台列表，默认 bilibili,twitter",
                required=False,
                default="bilibili,twitter",
            ),
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="text（帖文列表）或 sentiment（附带风评情感汇总）",
                enum_values=["text", "sentiment"],
                default="text",
                required=False,
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="每个平台最多返回几条（1–15，默认 6）",
                required=False,
                default=6,
            ),
        ],
        visibility="visible",
    )
    async def handle_social_search(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")

        query = str(kwargs.get("query") or "").strip()
        if not query:
            return fail("缺少 query：要在社区搜什么？")

        platforms = str(kwargs.get("platforms") or "bilibili,twitter").strip()
        mode = str(kwargs.get("mode") or "text").strip().lower()
        if mode not in {"text", "sentiment"}:
            mode = "text"
        want = _int_arg(kwargs, "max_results", 6, 1, 15)

        stream_id = str(kwargs.get("stream_id") or "")
        if not self._rate_ok(stream_id):
            return fail("查得太快啦，歇几秒钟再试。")

        payload = await self._get_worker().call_tool(
            "argo_social_search",
            {
                "query": query,
                "platforms": platforms,
                "mode": mode,
                "max_results": want,
            },
            timeout=max(25.0, float(self.config.argo.timeout_seconds)),
        )
        if payload.get("error"):
            return fail(f"社区检索失败：{payload['error']}")

        text = argo_cli.format_social_results(
            payload,
            max_items=want * 2,
            max_chars=int(self.config.limits.max_chars),
        )
        return {"success": True, "content": text}

    # ── 工具 6：打开网页/公众号/单条推文读正文 ──────────────────────

    @Tool(
        "argo_fetch",
        brief_description=(
            "打开指定 URL（普通网页、游戏攻略贴、微信公众号文章或单条 X/Twitter 推文）提取正文"
        ),
        detailed_description=(
            "当群友发了一个链接让你看，或者你在 argo_search 里看到某篇攻略/文章想细读时调用。\n"
            "- 自动识别 mp.weixin.qq.com 链接并走公众号专用全文提取；\n"
            "- 自动识别 x.com / twitter.com 单条推文链接并走免登 Syndication 提取全文与互动数；\n"
            "- 普通网页支持填 focus 关键词按相关性聚焦提取（省篇幅），"
            "遇反爬壳页可设 use_browser=true。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="要读取的公网 http(s):// 链接",
                required=True,
            ),
            ToolParameterInfo(
                name="focus",
                param_type=ToolParamType.STRING,
                description="可选：只提取与该关键词相关的段落（BM25 聚焦）",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="use_browser",
                param_type=ToolParamType.BOOLEAN,
                description="普通抓取为空或遇知乎等登录站点时设为 true 启用浏览器通道",
                required=False,
                default=False,
            ),
        ],
        visibility="visible",
    )
    async def handle_fetch(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")

        url = str(kwargs.get("url") or "").strip()
        if not image_helper.is_public_http_url(url):
            return fail("url 仅接受公网 http:// 或 https:// 链接。")

        focus = str(kwargs.get("focus") or "").strip()
        use_browser = bool(kwargs.get("use_browser", False))
        max_chars = max(800, int(self.config.limits.max_chars) * 2)

        lower_url = url.lower()
        # 1. 单条推文直链 -> 走 twitter_syndication
        if lower_url.startswith(_TWEET_URL_PREFIXES) and "/status/" in lower_url:
            payload = await self._get_worker().call_tool(
                "argo_search",
                {
                    "query": url,
                    "engine": "twitter_syndication",
                    "max_results": 1,
                    "include_local": False,
                },
                timeout=float(self.config.argo.timeout_seconds),
            )
            if payload.get("error"):
                return fail(f"推文读取失败：{payload['error']}")
            return {
                "success": True,
                "content": argo_cli.format_results(payload, max_items=1, max_chars=max_chars),
            }

        # 2. 微信公众号文章 -> 走 argo_article
        if _WEIXIN_HOST in lower_url:
            payload = await self._get_worker().call_tool(
                "argo_article",
                {"url": url, "max_chars": max_chars},
                timeout=max(30.0, float(self.config.argo.timeout_seconds)),
            )
            if payload.get("error"):
                return fail(f"公众号文章提取失败：{payload['error']}")
            title = str(payload.get("title") or "公众号文章").strip()
            author = str(payload.get("author") or "").strip()
            pub_time = str(payload.get("publish_time") or "").strip()
            content = str(payload.get("content") or payload.get("raw_text") or "").strip()
            if len(content) > max_chars:
                content = content[:max_chars].rstrip() + "…"
            meta_line = " · ".join(p for p in (author, pub_time) if p)
            header = f"《{title}》" + (f"（{meta_line}）" if meta_line else "")
            return {"success": True, "content": f"{header}\n{content}"}

        # 3. 通用网页 -> 走 argo_fetch
        args: dict[str, Any] = {
            "url": url,
            "mode": "text",
            "max_chars": max_chars,
            "use_browser": use_browser,
        }
        if focus:
            args["focus"] = focus
        payload = await self._get_worker().call_tool(
            "argo_fetch",
            args,
            timeout=max(30.0, float(self.config.argo.timeout_seconds)),
        )
        if payload.get("error"):
            return fail(f"网页读取失败：{payload['error']}")
        title = str(payload.get("title") or url).strip()
        content = str(
            payload.get("content") or payload.get("text") or payload.get("raw_text") or ""
        ).strip()
        if not content:
            return fail("未能从该页面提取到有效正文，可尝试设 use_browser=true 重试。")
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "…"
        return {"success": True, "content": f"【{title}】\n{content}\n来源：{url}"}

    # ── 工具 7：后台长任务（跑完主动携带摘要唤醒麦麦） ──────────────

    @Tool(
        "argo_job",
        brief_description=(
            "提交或管理耗时较长的后台深扒任务（多源深度研究、整站抓取、长 PDF 提取），"
            "跑完自动唤醒你汇报"
        ),
        detailed_description=(
            "需要跨多源深扒一个复杂话题（research）、抓整站（crawl）或解析大 PDF（pdf）时调用。\n"
            "action=submit 提交后立刻返回 job_id，任务在后台独立进程执行；"
            "先告诉群友『已经在后台深扒了，跑完跟你说』——"
            "插件监控到任务完成后会自动携带结果摘要唤醒你汇报，无需你在原地死等。\n"
            "也可随时用 action=status / list / cancel 查看或取消。"
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
                description="任务类型，仅 submit 时必填",
                enum_values=list(argo_cli.JOB_TASKS),
                required=False,
            ),
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="研究主题或目标链接，仅 submit 时必填",
                required=False,
            ),
            ToolParameterInfo(
                name="job_id",
                param_type=ToolParamType.STRING,
                description="作业号，status 与 cancel 时可填（status 留空默认查最新一条）",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def handle_job(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return fail("Argo 检索桥接器已在配置中关闭。")

        action = str(kwargs.get("action") or "status").strip().lower()
        stream_id = str(kwargs.get("stream_id") or "")
        job_root = self._data_dir("jobs")

        if action == "submit":
            task = str(kwargs.get("task") or "").strip()
            target = str(kwargs.get("target") or "").strip()
            meta = await asyncio.to_thread(
                argo_cli.submit_job,
                job_root,
                task,
                target,
                stream_id=stream_id,
                **self._argo_kwargs(),
            )
            if meta.get("error"):
                return fail(f"提交失败：{meta['error']}")
            job_id = str(meta["job_id"])
            if stream_id and self.config.jobs.proactive_notify:
                watch_task = asyncio.create_task(self._watch_job_and_notify(job_id, stream_id))
                self._watch_tasks[job_id] = watch_task
            return {
                "success": True,
                "content": (
                    f"已提交后台作业 {job_id}（{meta['task']}：{meta['target']}）。"
                    "任务正在后台独立进程运行，跑完后会自动携带摘要唤醒你汇报；现在先跟群友说一声即可。"
                ),
            }

        if action == "list":
            rows = await asyncio.to_thread(argo_cli.list_jobs, job_root, limit=10)
            if not rows:
                return {"success": True, "content": "还没有作业记录。"}
            lines = [
                f"{row['job_id']}｜{row['state']}｜{row.get('task') or '?'}："
                f"{(row.get('target') or '')[:40]}"
                for row in rows
            ]
            return {"success": True, "content": "\n".join(lines)}

        job_id = str(kwargs.get("job_id") or "").strip()
        if action == "cancel":
            if not job_id:
                return fail("要取消哪个作业？请给 job_id。")
            watch = self._watch_tasks.pop(job_id, None)
            if watch is not None:
                watch.cancel()
            result = await asyncio.to_thread(argo_cli.cancel_job, job_root, job_id)
            if result.get("error"):
                return fail(result["error"])
            return {"success": True, "content": f"作业 {result['job_id']} 已取消。"}

        if not job_id:
            rows = await asyncio.to_thread(argo_cli.list_jobs, job_root, limit=1)
            if not rows:
                return fail("没有正在跑的作业，也没有给 job_id。")
            job_id = rows[0]["job_id"]

        info = await asyncio.to_thread(
            argo_cli.job_status,
            job_root,
            job_id,
            log_lines=int(self.config.jobs.max_log_lines),
        )
        if info.get("error"):
            return fail(info["error"])

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
    return ArgoPlugin()
