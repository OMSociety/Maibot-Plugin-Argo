# argo 检索（麦麦插件）

把本机 [argo](https://github.com/taxueseek/argo) 的检索与长任务能力交给 MaiBot（麦麦），
面向群聊场景：查资料、找论文、找梗的出处、看时事，以及跑几分钟以上的深度任务。

插件本身不联网、不调 MCP，只在本机起一个 `python <argo>/bin/argo …` 子进程。

## 两个工具

### `argo_lookup`：按问题类型选源，几秒返回

| kind | argo 引擎 | 适合 |
|---|---|---|
| `web` | `metaso` | 中文综合，游戏影视对比、热梗、娱乐热点、新闻综述 |
| `news` | `deepseek_search` | 时事新闻，带发布日期与权威分级 |
| `academic_cn` | `metaso_scholar` | 中文期刊题录（万方/维普） |
| `fulltext_cn` | `ncpssd` | 中文社科全文 PDF（国家哲学社会科学文献中心） |
| `academic_en` | `sciverse` | 外文文献题录与摘要 |
| `doi` | `unpaywall` | 传 DOI 查合法免费全文 |
| `book` | `weread` | 中文书目 |
| `image` | `metaso_image` | 配图与梗图 |
| `code` | `github` | GitHub 仓库与插件 |
| `tech_en` | `tavily` | 英文工程资料 |

`news` 与 `tech_en` 走计费源；`limits.enable_paid` 关闭时自动降级到 `metaso`（中文综合）。

返回文本已排版为「标题｜来源·日期｜摘要｜链接」，整体受 `max_chars` 截断。

### `argo_job`：长任务提交即返回

- `action=submit`：`task` 取 `research` / `crawl` / `pdf` / `paper` / `article`，`target` 填查询词或链接。
  提交后立刻拿到作业号，任务在**独立进程**里跑。
- `action=status`：给 `job_id` 看状态与日志尾部；不给就取最近一条。
- `action=list` / `action=cancel`：列表与取消。

作业协议：作业目录里出现 `rc` 文件即表示结束，内容就是退出码；状态查询只读文件，
所以工具调用永远不会挂住对话，宿主重启也不影响已经在跑的任务。

## 安装

1. 把本目录放到 `MaiBot/plugins/omsociety_argo-lookup/`。
2. 确认 `config.toml` 里的 `argo.root` 指向本机 argo（默认 `C:\Users\Administrator\argo`）。
3. 重启 MaiBot（或热重载插件）。

依赖：宿主 Python 需有 PyYAML（argo 的唯一必需第三方依赖）。argo 的密钥放在
`%APPDATA%\argo\env`，插件会通过 `ARGO_ENV_FILE` 传给子进程。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `argo.root` | `C:\Users\Administrator\argo` | 仓库目录 |
| `argo.python` | 空 | 留空用麦麦自身解释器 |
| `argo.env_file` | 空 | 留空用 `%APPDATA%\argo\env` |
| `argo.mode` | `fast` | 检索模式：`fast` / `auto` / `deep` / `budget`，写错按 `fast` |
| `argo.timeout_seconds` | `25` | 单次检索上限 |
| `limits.max_results` | `5` | 默认条数（1–10） |
| `limits.max_chars` | `1500` | 回给模型的字符上限 |
| `limits.calls_per_minute` | `6` | 单会话每分钟上限，防刷屏 |
| `limits.enable_paid` | `false` | 关闭时 `news`/`tech_en` 自动改用中文综合源 |
| `jobs.retention_days` | `7` | 作业目录保留天数 |
| `jobs.max_log_lines` | `20` | 状态回显的日志行数 |

## 已知边界

- 只支持 Windows 本机；`DETACHED_PROCESS` 等 creationflags 是 Windows 语义。
- 付费引擎（`deepseek_search`、`tavily`）默认关闭，需要时在配置里打开。
- 长任务不主动回群：跑完要由对话里的下一轮去 `argo_job status` 取。主动回群需要发送能力与消息目标记录，属于后续版本。
- 作业进程独立于宿主，卸载插件不会杀掉正在跑的任务（作业目录里可查）。
