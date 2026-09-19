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

仓库中的 `deploy/formaliscope-app-pull` 和 `deploy/formaliscope-snapshot-pull`
实现了这套流程。它们只依赖 VPS 上已有的 `curl`、`python3`、`tar`、
`sha256sum` 和 `systemd`，用 `/run/lock/formaliscope-deploy.lock` 防止应用与
快照更新同时改动数据。应用包解压到不可变的
`/opt/formaliscope/releases/<commit>`，校验通过后才原子切换
`/opt/formaliscope/current`；快照更新先创建 SQLite 在线备份，再调用
`install-snapshot`。首次安装快照时不会伪造空备份，服务首次启动负责创建数据库。

对应的 systemd 单元是：

```text
formaliscope-app-pull.timer       每 15 分钟检查应用 release
formaliscope-snapshot-pull.timer  每 15 分钟检查 KIP126 snapshot release
formaliscope-review.service       只监听 127.0.0.1:8765
formaliscope-review-backup.timer  每日创建一致性备份
```

安装这些单元时，把两个可执行拉取器和
`deploy/formaliscope-review-backup` 复制到 `/usr/local/sbin/`，把 `.service`
和 `.timer` 复制到 `/etc/systemd/system/`。应用用户使用
`formaliscope-review`，持久数据位于 `/var/lib/formaliscope`，令牌文件为
`/etc/formaliscope/github-read-token`（`0600`）。首次启用前必须先放入一个只对
`SII-MATH/Formaliscope` 授予 **Contents: read** 的 fine-grained token；不要把现有
开发机 Git 凭据复制到 VPS。

一次性初始化可以按下面的顺序执行（命令在 HK 上以 root 运行）：

```bash
useradd --system --home /nonexistent --shell /usr/sbin/nologin formaliscope-review
install -d -o formaliscope-review -g formaliscope-review -m 0700 \
  /var/lib/formaliscope /var/backups/formaliscope
install -d -o root -g formaliscope-review -m 0750 /etc/formaliscope
install -d -o root -g root -m 0755 /opt/formaliscope/releases
install -o root -g root -m 0755 deploy/formaliscope-app-pull /usr/local/sbin/formaliscope-app-pull
install -o root -g root -m 0755 deploy/formaliscope-snapshot-pull /usr/local/sbin/formaliscope-snapshot-pull
install -o root -g root -m 0755 deploy/formaliscope-review-backup /usr/local/sbin/formaliscope-review-backup
install -o root -g root -m 0644 deploy/formaliscope-*.service deploy/formaliscope-*.timer /etc/systemd/system/
install -o root -g root -m 0600 /secure/formaliscope/github-read-token /etc/formaliscope/github-read-token
```

先执行一次 `formaliscope-app-pull` 和 `formaliscope-snapshot-pull`，确认
`/var/lib/formaliscope/snapshot.json`、`judgments.sqlite3` 和
`/opt/formaliscope/current` 都已经出现，再启用四个 timer/service。这样首发
失败时不会把旧的 Caddy 路由或已有审核数据切走。

当前 HK 机器的 `math.opensii.ai/formaliscope/` 是旧的 Django 服务入口，仍由
现有 SSH 反向隧道提供。部署新审核台时应使用新的公开入口，例如
`/kip126-review/`，并保留旧路径。由于现有站点把 `forward_auth` 放在全局，若新
入口需要只使用邮箱验证码，应在 Caddy 的 `route` 中先匹配新路径、直接反代
`127.0.0.1:8765`，再对其余路径执行现有 `forward_auth`；不要简单把新的
`handle_path` 追加到旧的全局认证之前。Caddy 改动应先 `caddy validate`，再备份并
reload，最后检查旧 `/formaliscope/` 和新入口各一次。

QQ 邮箱生产配置仍需在 VPS 上单独写入 `/etc/formaliscope/review.env` 和
`/etc/formaliscope/smtp-password`。密码或授权码不进 GitHub、workflow 日志、应用
数据库或 release 制品。

GitHub Actions 不接触 `/var/lib/formaliscope`。生产数据库只在 VPS 上读写。

## GitHub 侧安全设置

- 保护 `main`，要求 CI 通过后才能合并。
- 生产发布使用 GitHub Environment，必要时要求人工批准。
- `GITHUB_TOKEN` 仅在发布工作流中使用 `contents: write`。
- KIP126 读取 token 使用单独的 fine-grained token，只给 Contents read。
- 不在 workflow 日志中打印 token、邮件密码或数据库内容。
- 第三方 Action 应固定到审查过的版本；生产部署前先在测试 VPS 演练。

当前仓库只负责构建和发布私有制品；VPS 拉取器需要在目标机器安装后才会真正自动切换服务。
