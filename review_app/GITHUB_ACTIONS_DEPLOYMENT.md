# GitHub Actions 与 VPS 拉取部署

本项目采用“GitHub 发布制品，VPS 主动拉取”的部署方式。GitHub Actions 不保存 VPS SSH 私钥，也不读取生产数据库。

## 已加入的工作流

### `ci.yml`

在 Pull Request 和 `main` push 上运行：

- Python 审核、登录、存储和迁移测试
- Python/JavaScript 语法检查
- systemd 单元检查

### `publish-app.yml`

每次合并到 `main` 后：

1. 重新运行测试。
2. 用当前 commit 生成不可变应用包。
3. 生成 SHA-256 清单。
4. 发布私有 GitHub Release，标签为 `app-<commit>`。

### `publish-snapshot.yml`

支持手动运行，也支持 KIP126 仓库通过 `repository_dispatch` 触发：

1. 使用指定的 KIP126 commit 检出源码。
2. 在干净 checkout 上运行 `build --require-clean`。
3. 发布 `snapshot.json`、校验和和变更统计。
4. 创建标签为 `snapshot-<kip126-commit>` 的私有 Release。

要让这个工作流读取私有 KIP126，需要在 Formaliscope 仓库添加一个只读的 `KIP126_READ_TOKEN` Secret。该 token 只授予 SII-MATH/KIP126 的 Contents read 权限。

## VPS 拉取器的约定

VPS 上的定时任务使用只读 GitHub token 查询 `SII-MATH/Formaliscope` 的 Release：

```text
/etc/formaliscope/github-read-token       只读 token，权限 0600
/opt/formaliscope/releases/<app-commit>  应用 release
/opt/formaliscope/current                 当前应用符号链接
/var/lib/formaliscope/snapshot.json       当前证据快照
/var/lib/formaliscope/judgments.sqlite3   审核数据库
```

应用拉取器选择最新的 `app-*` release；快照拉取器选择最新的 `snapshot-*` release。下载后必须先校验 release manifest 和 SHA-256，再执行：

1. 备份 SQLite。
2. 执行数据库迁移。
3. 安装快照并读取 `unchanged/changed/added/removed`。
4. 原子切换 `current`。
5. 重启 systemd 服务。
6. 请求本机登录页进行健康检查。

GitHub Actions 不接触 `/var/lib/formaliscope`。生产数据库只在 VPS 上读写。

## GitHub 侧安全设置

- 保护 `main`，要求 CI 通过后才能合并。
- 生产发布使用 GitHub Environment，必要时要求人工批准。
- `GITHUB_TOKEN` 仅在发布工作流中使用 `contents: write`。
- KIP126 读取 token 使用单独的 fine-grained token，只给 Contents read。
- 不在 workflow 日志中打印 token、邮件密码或数据库内容。
- 第三方 Action 应固定到审查过的版本；生产部署前先在测试 VPS 演练。

当前仓库只负责构建和发布私有制品；VPS 拉取器需要在目标机器安装后才会真正自动切换服务。
