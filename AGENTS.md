# AGENTS.md

给改动本仓库的 Agent（或第一次接手的人）看的操作说明。这个仓库是一个 MaiBot 插件目录，
不是独立可运行的包：全部行为都在宿主进程里发生。

## 定位与运行面

- 插件让群聊里的模型通过两个工具调用本机 argo CLI：`argo_lookup`（秒级检索）、`argo_job`（长任务）。
- 宿主：MaiBot 1.3.15（OneKey 部署），`maibot-plugin-sdk` 2.16.0；插件目录被放进
  `D:\ArtificialIntelligence\Maibot\MaiBot\plugins\omsociety_argo-lookup\` 后由宿主加载。
- 运行平台：Windows。`creationflags`、`taskkill` 都是 Windows 语义。
- argo：`C:\Users\Administrator\argo`，入口 `bin/argo`（Python 脚本，依赖 PyYAML），
  密钥文件 `%APPDATA%\argo\env`，由插件通过 `ARGO_ENV_FILE` 传给子进程。
- 运行期数据：`D:\ArtificialIntelligence\Maibot\MaiBot\data\plugins\omsociety.argo-lookup\`，
  作业目录在其下 `jobs\`，**不入库**。

## 文件职责

| 文件 | 职责 | 改动边界 |
|---|---|---|
| `plugin.py` | 宿主接入层：配置模型、两个 `@Tool`、缓存、限流、结果排版 | 只做宿主交互与编排，不直接拼 argv |
| `argo_cli.py` | 与 argo 的全部交互：选源、调用、作业协议、清理 | 只用标准库，可脱离宿主单测 |
| `job_runner.py` | 长任务执行器，被独立进程拉起，只写作业目录 | 只用标准库；不 import 宿主模块 |
| `tests/` | pytest 用例；`test_readme_sync.py` 校验 README 表格与 `ENGINE_MAP` 一致 | 改行为必须同步加/改用例 |
| `_manifest.json` | 插件清单：id、版本、宿主与 SDK 版本闸、能力声明 | 改版本才动 `version` |
| `README.md` | 面向使用者的安装、工具与配置说明 | 表格由 `ENGINE_MAP` 派生，须逐字一致 |

## 架构边界

- **唯一数据源**：`argo_cli.ENGINE_MAP` 定义 kind → 引擎、是否计费、适用场景。
  工具描述里的 kind 提示与 README 的来源表都从它生成/校验，不要在别处另抄一份。
- **完成信号只有 `rc` 文件**：`job_runner.py` 写 `stdout.log` / `stderr.log` / `rc`；
  `job_status` 只读这些文件。不要改成交付管道或长连接，脱离式进程的 stdout 是 `DEVNULL`。
- **事件循环不能被阻塞**：所有 `subprocess` 调用必须经由 `asyncio.to_thread`；
  检索超时上限受宿主 60 秒 RPC 超时约束，`argo.timeout_seconds` 不要调到 30 秒以上。
- **失败结果统一走 `plugin.fail()`**：返回 `success=False` 时 `error` 与 `content` 都要有值，
  宿主取 `error` 当错误信息。用例会检查文件里只有 `fail()` 一处失败字典。
- **作业与宿主解耦**：`on_unload` 不杀作业进程；卸载、重载、重启都不影响已在跑的任务。

## 改哪类东西必须做什么

| 改动 | 必须同步 |
|---|---|
| `ENGINE_MAP`（增删 kind、换引擎、改用途） | `README.md` 来源表逐行一致（`tests/test_readme_sync.py` 会失败）；重跑 pytest |
| 配置字段或默认值 | 对应 `*Section` 的 `description`（WebUI 文案）；`README.md` 配置表；结构语义变了才 bump `config_version` |
| 工具签名、参数、描述 | `parameters` 用 `ToolParameterInfo`；描述面向模型，写「什么时候用」而不是「实现了什么」 |
| 作业协议（`meta.json` / `rc` / 日志名） | `argo_cli.submit_job`、`job_runner.py`、`job_status`、`list_jobs` 四处一起改 |
| 长任务类型（`argo_cli.JOB_TASKS`） | 必须是 argo 侧真实子命令同名；`argo_job` 的 `task` 枚举由它生成，`README.md` 任务列表须一致（`tests/test_readme_sync.py` 会失败） |
| `_manifest.json` 的 `version` 或 `capabilities` | 见「发版」一节；改完用清单校验器过一遍 |
| 任何 Python 改动 | 跑下面的验收命令，全过再提交 |

## 验收命令（在仓库根目录执行）

```powershell
Set-Location "D:\ArtificialIntelligence\Maibot\MaiBot\plugins\omsociety_argo-lookup"
$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"

& $py -m ruff format --check .
& $py -m ruff check .
$env:PYTHONPATH = $PWD.Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
& $py -m pytest -q
```

通过标准：`ruff format --check` 无输出差异、`ruff check` 打印 `All checks passed!`、
`pytest` 打印 `40 passed`（数字随用例增减，失败数为 0）。

单跑一组用例：`& $py -m pytest -q tests/test_argo_cli.py -k mode`。
用例不联网、不调用 argo、不需要宿主进程；`tests/conftest.py` 只做两件事：把插件目录加进
`sys.path`，把 `tmp_path` 改到仓库内 `.pytest_tmp/`（本机 `%TEMP%` 无读权限）。两处缓存目录都不入库。

宿主侧导入冒烟（换了 SDK 版本、改了 `@Tool` 或配置模型后必跑）：

```powershell
$hostPy = Join-Path (Get-ChildItem "D:\ArtificialIntelligence\Maibot\MaiBot\.onekey\environments" -Directory |
    Select-Object -First 1).FullName "python.exe"
$env:PYTHONPATH = $PWD.Path
& $hostPy -c "import plugin; plugin.create_plugin(); print(list(plugin.argo_cli.ENGINE_MAP))"
```

通过标准：打印 10 个 kind，无 Traceback。宿主解释器没有 pytest/ruff 是正常的，别往里装。

真实检索冒烟（改了选源、`--mode` 处理或结果排版后跑）：

```powershell
& $py -c "import argo_cli; r = argo_cli.search('MaiBot 插件', 'metaso', max_results=2); print(r.get('count'), r.get('error'))"
```

通过标准：打印 `2 None`。argo 侧命令写错时是 `exit 2`（argparse 报错），
`normalize_mode` 负责把配置里的非法取值收敛成 `fast`，别绕过它直接传原始配置值。

清单校验：用 `maibot-plugin-creator` 技能里的 `scripts/check_plugin.py` 检查本目录，
要求 0 error。1 条 `capability-empty` 警告是预期结果——本插件不注册任何能力。

## 禁止的操作

- 不 import 宿主的 `src.*` 内部模块：宿主升级就会断，插件只能用 `maibot_sdk` 公开 API。
- 不在事件循环线程里同步 `subprocess.run`：会把整条消息处理链路卡住，必须 `asyncio.to_thread`。
- 不把 `jobs/`、`config.toml`、`.pytest_tmp/` 提交进仓库：前者是运行期产物，`config.toml` 由宿主生成。
- 不手工复制 `ENGINE_MAP` 的内容到 README 或工具描述里：会漂移，`test_readme_sync.py` 就是为了卡这个。
- 不用 `os.kill` 杀 Windows 作业：走 `taskkill /T /F`，否则子进程树残留。

## 已知限制（改动前先确认是否要一并修）

- 长任务跑完不会主动回群，必须等下一轮对话里 `argo_job action=status` 取结果。
- `prune_jobs` 只在 `on_load` 执行：宿主长期不重启时，过期作业目录不会被清理。
- `state=lost` 只表示「没有 `rc` 且进程不在」，无法区分崩溃与被外部杀掉。
- `_pid_alive` 以 pid 判断存活，Windows 复用 pid 时会误报 `running`（概率低）。
- `action=status` 不给 `job_id` 时取最近一条作业，可能不是想查的那条。
- 检索缓存键是 `(engine, query, want, mode)`；运行期改 `enable_paid` 不会让旧缓存失效，
  只有配置更新（`on_config_update`）或宿主重载才整体清空。
- 计费源关闭时的降级不报错，只在结果末尾附一句说明。

## 发版

- 仓库：`https://github.com/OMSociety/Maibot-Plugin-Argo-Lookup`，`gh` 与 `git` 命令都在仓库根执行。
- 版本号在 `_manifest.json` 的 `version`；`1.0.0` 的 CHANGELOG 与 Release 正文按
  `github-dev` §3.2 的首发特例留空。
- tag 与 Release 必须指向最终提交；改完代码再打 tag。
- 重发同一版本的顺序：提交并推送 `main` → `gh release delete v1.0.0 --yes` →
  `git push origin :refs/tags/v1.0.0` → `git tag -f -a v1.0.0 -m "v1.0.0"` →
  `git push origin main v1.0.0` → `gh release create v1.0.0 --notes ""`。
- 本仓库没有 CI：上面的验收命令就是「通过」的定义，提交前必须本地跑完。

## 提交约定

- 提交信息用中文，格式 `<type>: <摘要>`，`type` 取 `feat` / `fix` / `docs` / `test` / `chore`。
- 一个提交只做一件事；格式化产生的改动单独提交，不要混进功能改动。
- 改 `_manifest.json` 会被宿主监听到并重启全部插件的 supervisor；提交前提醒用户，
  正在跑的作业是独立进程，不受影响。
