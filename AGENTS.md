# AGENTS.md

给改动本仓库的 Agent 看的操作说明。本仓库是 MaiBot 插件 `omsociety.argo`（`Maibot-Plugin-Argo`）。

## 定位与运行面

- **代码仓库**：`D:\WorkSpace\Github\Maibot-Plugin-Argo`
- **宿主加载路径（Windows Junction 软链）**：`D:\ArtificialIntelligence\Maibot\MaiBot\plugins\omsociety_argo` -> `D:\WorkSpace\Github\Maibot-Plugin-Argo`
- **宿主环境**：MaiBot 1.3.15（OneKey 部署），`maibot-plugin-sdk` 2.16.0；宿主解释器位于 `D:\ArtificialIntelligence\Maibot\MaiBot\.onekey\environments\288559bb-a676-412d-a862-3f44677925c2\python.exe`。
- **底层 Argo 引擎**：`C:\Users\Administrator\argo`（分支 `local/runtime`），由系统 Python 3.13（`C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe`）驱动；密钥文件位于 `C:\Users\Administrator\AppData\Roaming\argo\env`。
- **运行期数据目录**：`D:\ArtificialIntelligence\Maibot\MaiBot\data\plugins\omsociety.argo\`（含后台作业目录 `jobs\` 与聊天图片暂存目录 `temp_images\`，均不入库）。

## 文件职责

| 文件 | 职责 | 改动边界 |
|---|---|---|
| `plugin.py` | 宿主接入层：`ArgoConfig` 配置模型、`ArgoPlugin` 生命周期与 6 个 `@Tool`（`argo_search`、`argo_image_search`、`argo_reverse_image`、`argo_social_search`、`argo_fetch`、`argo_job`） | 失败返回统一走 `fail()`（全文件仅 `fail()` 一处构造 `"success": False`） |
| `worker.py` | 常驻 `mcp_server.py` stdio NDJSON 守护进程管理器（预热、请求编号对账、超时/断管自动重启、注入 `ARGO_IMAGE_LOCAL_ROOTS`、剥离 `NO_PROXY` 中的 `[::1]`） | 严禁在宿主进程内直接 `import` Argo 脚本 |
| `image_helper.py` | 聊天图片三通道提取（`maisaka.context.resolve_image` -> `message.get_by_id` -> `message.get_recent`）、公网 URL SSRF 校验、缩略图下载与 Pillow 归一化、`content_items` 构造 | 本地反搜暂存文件用完即删；严禁把聊天本地图片上传至第三方公共图床 |
| `argo_cli.py` | 唯一数据源 `ENGINE_MAP`、`REVERSE_IMAGE_ENGINES`、`JOB_TASKS`、结果排版与独立进程作业管理（`submit_job` / `job_status` / `mark_job_notified` / `cancel_job` / `prune_jobs`） | 仅依赖标准库，可脱离宿主单测 |
| `job_runner.py` | 脱离式后台长任务执行器，写 `stdout.log` / `stderr.log` / `rc` | 仅依赖标准库，不 import 宿主模块 |
| `tests/` | pytest 离线单测；`test_readme_sync.py` 强校验 `README.md` 表格与 `argo_cli.ENGINE_MAP`、`JOB_TASKS` 逐行一致 | 改行为或改选源表必须同步更新单测 |

## 架构与修改铁律

1. **唯一数据源**：`argo_cli.ENGINE_MAP` 定义 `kind` -> 引擎、是否计费、适用场景。工具描述里的 `_KIND_HINT` 与 `README.md` 的来源表均由它派生/校验，严禁手工另抄一份。
2. **本机文件与内网物理隔离**：`worker.py` 调用 `argo_search` 一律传 `include_local=False` 并过滤 `source == "local_files"`；`argo_fetch` 与 `image_helper.is_public_http_url` 拦截非 `http(s)://`、回环地址与内网保留 IP。
3. **搜图与反搜视觉闭环**：`argo_image_search` 与 `argo_reverse_image` 下载图片后通过 `content_items: [{"content_type": "image", ...}]` 回传给麦麦视觉上下文，供麦麦核对画面并用 `reply(attach_pic="tool_result:<call_id>:1")` 发出。
4. **长任务完成主动唤醒**：`job_runner.py` 写出 `rc` 文件后，`plugin.py` 的 `_watch_job_and_notify` 协程读取尾部摘要并调用 `self.ctx.maisaka.proactive.trigger` 唤醒麦麦，随后通过 `mark_job_notified` 写回 `meta.json` 防止重复唤醒。
5. **版本与 CHANGELOG 纪律**：未获明确发版指令前，`_manifest.json` 保持 `1.0.0`，`CHANGELOG.md` 仅保留标准双语头部。

## 验收命令（在仓库根目录执行）

```powershell
Set-Location "D:\WorkSpace\Github\Maibot-Plugin-Argo"
$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"

& $py -m ruff format --check .
& $py -m ruff check .
$env:PYTHONPATH = $PWD.Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
& $py -m pytest -q
```

宿主侧导入与清单校验：

```powershell
$hostPy = "D:\ArtificialIntelligence\Maibot\MaiBot\.onekey\environments\288559bb-a676-412d-a862-3f44677925c2\python.exe"
$env:PYTHONPATH = "D:\WorkSpace\Github\Maibot-Plugin-Argo"
& $hostPy -c "import plugin; p = plugin.create_plugin(); print(type(p).__name__, len(plugin.argo_cli.ENGINE_MAP))"
& $py "C:\Users\Administrator\.dsh\skills\maibot-plugin-creator\scripts\check_plugin.py" "D:\WorkSpace\Github\Maibot-Plugin-Argo"
```

通过标准：`ruff format --check` 无差异、`ruff check` 0 warning、`pytest` 全绿、`check_plugin.py` 0 error。
