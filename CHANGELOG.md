# Changelog

每次合并到 `main` 后，GitHub Actions 自动将上一正式版本至本次提交的变更清单和完整对比链接写入 Release 说明。直接提交和通过 PR 合并的修改都会纳入；草稿、预发行、旧 `app-*` 和证据 `snapshot-*` 不作为比较起点。

下列为已整理的中文版本摘要，后续发布的完整变更明细见 [GitHub Releases](https://github.com/SII-MATH/Formaliscope/releases)。发布与服务器部署分别执行。

## [v0.0.3] — 2026-10-04

### 改进

- 应用名称统一为 Formaliscope，使用 F 标志及 favicon。
- 整理存储模块和认证会话，数据库升级至 schema 7，保留已有身份与审阅记录。
- 快照安装与备份共用数据锁；增加 v2 备份校验、数量保留与异机备份配置说明。
- 应用开发分支统一为 `dev`，新增 Apache License 2.0。

### 修复

- 审阅结果自动保存，减少忘记保存或切换条目时丢失输入。
- 返回上一条目时恢复目录、筛选、滚动位置和阅读视图。
- 增加 Lean 名称定义追溯，对无法确认的解析提供候选。
- 修复审阅选项获得焦点时引起的页面跳动。

## [v0.0.2] — 2026-10-03

- 完善 HK-VPS 部署、校验与更新准备，保留不可变应用及证据制品。

## [v0.0.1] — 2026-10-03

- 应用发布从 `v0.0.1` 开始编号，后续自动增加 patch 版本。
- 合并到 `main` 后自动测试、打包并发布 GitHub Release；日常开发使用 `dev`。
- 草稿可续传，重跑同一提交复用版本，已发布附件保持不变。

[v0.0.3]: https://github.com/SII-MATH/Formaliscope/releases/tag/v0.0.3
[v0.0.2]: https://github.com/SII-MATH/Formaliscope/releases/tag/v0.0.2
[v0.0.1]: https://github.com/SII-MATH/Formaliscope/releases/tag/v0.0.1
