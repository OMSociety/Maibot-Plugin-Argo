# Changelog

本项目的更改记录在此文件。

All notable changes to this project are documented in this file.

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `argo_image_search` 新增 `engine` 参数：`pixiv` 检索插画、同人图、漫画与角色立绘，`metaso_image` 检索通用配图与表情包（默认）。
- `argo_image_search` 的 `pixiv` 引擎支持按画师浏览：`query` 传 `user:<画师 id>`、`画师:<画师名>` 或画师主页链接，返回该画师的作品列表。
