<div align="center">

# Argo 检索桥接器（Maibot-Plugin-Argo）

面向 MaiBot（麦麦）的网上冲浪、热梗溯源、ACG 与游戏百科、玩家社区风评、正向找图与以图搜图工具箱

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](_manifest.json)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![MaiBot](https://img.shields.io/badge/MaiBot-%3E%3D1.3.0-orange.svg)](https://github.com/MaiM-with-u/MaiBot)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[核心特性](#核心特性) · [六个工具](#六个工具) · [安装与配置](#安装与配置) · [更新日志](#更新日志) · [许可证](#许可证)

</div>

---

## 核心特性

| 维度 | 设计说明 |
| :--- | :--- |
| **常驻 NDJSON 守护进程** | 插件启动时拉起单个常驻 `mcp_server.py` 子进程并完成预热，每次调用走毫秒级 stdio NDJSON 通信，超时或异常自动重启自愈 |
| **麦麦冲浪与 ACG 语义选路** | 内置 16 类娱乐与冲浪语义分类（`meme` 热梗、`slang_en` 英文俚语、`anime` 番剧、`game` Steam 游戏、`baike` 百科、`hot_bili` 等全网实时热榜），同时支持直传底层 `engine` |
| **正搜与反搜视觉闭环** | 搜配图（`argo_image_search`）与以图搜图（`argo_reverse_image`）均自动下载缩略图并通过 `content_items` 回传给麦麦视觉上下文，麦麦亲眼核对画面后可在 `reply(attach_pic=...)` 直接把图发给群友 |
| **聊天图片三通道自动提取** | 反搜聊天图片时依次通过 `maisaka.context.resolve_image`、`message.get_by_id` 与 `message.get_recent` 自动提取目标或最近一张聊天图片，落盘至受 `ARGO_IMAGE_LOCAL_ROOTS` 保护的专属暂存目录 |
| **后台长任务完成主动唤醒** | `argo_job` 提交独立后台进程后，内置协程自动守候 `rc` 完成信号并调用 `maisaka.proactive.trigger` 携带结果摘要主动唤醒麦麦向群友汇报 |

---

## 六个工具

### 1. `argo_search`：按语义类别冲浪、查梗、查 ACG 与看热榜

| kind | argo 引擎 | 适合 |
|---|---|---|
| `web` | `metaso` | 中文综合冲浪（公众号/帖文/百科/攻略综述） |
| `news` | `deepseek_search` | 时事热点与权威通报（带发布日期与权威分级） |
| `meme` | `itotii` | 中文网络热梗、流行黑话与抽象梗溯源 |
| `slang_en` | `urban_dictionary` | 英文网络俚语、缩写与欧美迷因（Urban Dictionary） |
| `anime` | `bangumi` | 动画、番剧、漫画、轻小说与 Galgame 资料（Bangumi） |
| `game` | `steam` | Steam 游戏商店页、作品信息与发售详情 |
| `baike` | `baidu_baike` | 中文词条、人物、作品设定与常识（百度百科） |
| `wiki` | `zh_wikipedia` | 跨领域概念、历史事件与客观词条（中文维基百科） |
| `video` | `metaso_video` | 游戏攻略视频、实况解说与二创视频检索 |
| `music` | `qq_music` | ACG 曲目、歌手、专辑与热门歌曲（QQ 音乐） |
| `hot_bili` | `bilibili_hot` | B 站实时全站热榜（看看 B 站今天在火什么） |
| `hot_weibo` | `weibo_hot` | 微博实时热搜榜（社会娱乐吃瓜热点） |
| `hot_zhihu` | `zhihu_hot_app` | 知乎实时热榜（全网热议话题） |
| `hot_douyin` | `douyin_hot` | 抖音实时热点榜（短视频流行趋势） |
| `code` | `github` | GitHub 开源项目、Mod、脚本与插件 |
| `web_en` | `tavily` | 英文网页、海外游戏资讯与外网冲浪 |

> **提示**：除语义 `kind` 外，还可传入可选参数 `engine` 直指任意底层 Argo 引擎；`news` 与 `web_en` 走计费源，`limits.enable_paid` 关闭时自动改用中文综合源 `metaso`。

### 2. `argo_image_search`：按关键词搜动漫图、表情包梗图与游戏截图

- `engine` 可选 `metaso_image`（通用配图、梗图与游戏截图，默认）或 `pixiv`（Pixiv 公开插画、同人图、漫画与角色立绘）。
- `pixiv` 按标签检索：`query` 写标签串，空格分隔多个标签（如 `初音ミク 制服`、`東方Project10000users入り フランドール 白タイツ`）；作品名、角色名、题材、服装与收藏夹标签都可用，角色名的日文名与中文名都能命中。
- Pixiv 支持按画师浏览：`query` 写 `user:<画师 id>`、`画师:<画师名>` 或 `https://www.pixiv.net/users/<id>`，返回该画师的作品列表，后面可接标签收窄（如 `画师:ke-ta フランドール`）；匿名模式下该模式会明确提示需要登录凭证。
- 插件自动下载前 `max_results`（1–4，默认 2）张可用图片，经 Pillow 校验归一化后写入 `content_items`；Pixiv 预览图保持作品宽高比，作品页作为出处链接。
- 麦麦可在视觉上下文中直接查看搜到的图片，并在调用 `reply` 时通过 `attach_pic="tool_result:<call_id>:1"` 将选中的图片发到群聊。

### 3. `argo_reverse_image`：以图搜图与动漫角色识别

- `engine` 三选一：
  - `saucenao`：二次元插画、动漫图、立绘、同人图找 Pixiv/Twitter 原图出处与画师（首选）；
  - `animetrace`：动画截图、番剧画面、Galgame 截图识别具体角色名与所属作品名；
  - `google_lens`：三次元照片、梗图原图、电影画面、游戏截图、周边商品与场景通用识图。
- 三条引擎都支持聊天图片直传与公网图片直链。
- 二次元图按 `saucenao` → `animetrace` → `google_lens` 的顺序试：前两条都返回空才动用 `google_lens`。
- 搜聊天图片时传入目标消息 `msg_id`（留空则自动扫描当前会话最近一条带图消息），插件自动提取图片并在匹配成功后把首条缩略图放入 `content_items` 供麦麦视觉核对。

### 4. `argo_social_search`：跨平台玩家社区与社媒舆情

- `platforms` 支持逗号分隔组合：`bilibili,twitter,xiaohongshu,reddit,v2ex,hackernews,youtube`（默认 `bilibili,twitter`）。
- `mode` 支持 `text`（带作者与原帖链接的帖文列表）与 `sentiment`（附带正负面风评倾向汇总）。

### 5. `argo_fetch`：网页、攻略长贴、微信公众号与单条推文全文读取

- 自动识别 `mp.weixin.qq.com` 链接并路由至公众号专用提取器；
- 自动识别 `x.com` / `twitter.com` 的 `/status/` 推文直链并走免登 Syndication 提取全文与互动数据；
- 普通网页支持 `focus` 关键词相关性裁剪与 `use_browser=true` 反爬浏览器渲染。

### 6. `argo_job`：长任务后台执行与完成主动唤醒

- `action=submit`：`task` 取 `research` / `crawl` / `pdf` / `paper` / `article`，`target` 填研究主题或链接。提交后立即返回作业号，在独立后台进程运行。
- 当 `jobs.proactive_notify` 开启时，后台守候协程检测到 `rc` 完成信号后自动提取输出尾部摘要，调用 `maisaka.proactive.trigger` 唤醒麦麦向群友播报结论。
- `action=status` / `action=list` / `action=cancel`：随时查看进度、列出最近作业或终止运行中的作业。

---

## 安装与配置

1. 将本仓库放置或软链接到 `MaiBot/plugins/omsociety_argo/`。
2. 确认本机已安装 [argo](https://github.com/taxueseek/argo)：默认取环境变量 `ARGO_ROOT`，没有则用用户目录下的 `argo`；密钥文件默认 `%APPDATA%\argo\env`。
3. 启动或重载 MaiBot。

| 键 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 是否启用插件 |
| `argo.root` | `%USERPROFILE%\argo` | Argo 仓库根目录（可用 `ARGO_ROOT` 覆盖） |
| `argo.python` | 当前进程解释器 | 启动常驻 Worker 与作业使用的 Python 解释器，须装有 argo 的运行依赖（可用 `ARGO_PYTHON` 覆盖） |
| `argo.env_file` | 空 | 留空默认读取 `%APPDATA%\argo\env` |
| `argo.proxy` | 空 | 下载搜图与反搜预览图片时使用的 HTTP 代理（留空直连，也可填 `direct`） |
| `argo.mode` | `fast` | 检索模式：`fast` / `auto` / `deep` / `budget` |
| `argo.timeout_seconds` | `25` | 单次同步检索超时秒数 |
| `limits.max_results` | `5` | 默认返回条数（1–10） |
| `limits.max_chars` | `1800` | 返回给模型的文本字符上限 |
| `limits.calls_per_minute` | `10` | 同一会话每分钟最大调用次数 |
| `limits.enable_paid` | `true` | 是否允许调用 `news`（`deepseek_search`）与 `web_en`（`tavily`）计费源 |
| `jobs.retention_days` | `7` | 作业目录保留天数 |
| `jobs.max_log_lines` | `20` | 查看状态或唤醒摘要提取的日志尾部行数 |
| `jobs.proactive_notify` | `true` | 后台长任务完成后是否自动携带结果摘要唤醒麦麦汇报 |

---

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源，作者 [OMSociety](https://github.com/OMSociety)。
