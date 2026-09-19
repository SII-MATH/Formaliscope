# 部署与持久存储

生产环境将代码、运行数据和密钥分开：

| 路径 | 内容 | 是否持久化 |
|---|---|---|
| `/opt/kip126-review` | 本仓库源码与静态资源 | 由版本发布替换 |
| `/var/lib/kip126-review` | `snapshot.json`、`judgments.sqlite3`、`auth-pepper` | 必须使用持久磁盘 |
| `/etc/kip126-review` | 邮件和公开入口配置、SMTP 密码文件 | 必须备份到密钥系统 |
| `/var/backups/kip126-review` | 经过 SQLite 在线备份 API 生成的备份 | 应再复制到异机存储 |

代码升级不能删除或覆盖 `/var/lib/kip126-review`。数据库只由一个应用实例访问；反向代理可以有多个，但审核服务保持单实例。如果需要多个审核服务实例，应先迁移到 PostgreSQL，不能让多个容器通过共享卷直接打开同一个 SQLite 文件。

## 初始化

以下命令中的系统用户、源码安装方式和反向代理可按目标机器调整：

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin kip126-review
sudo install -d -o kip126-review -g kip126-review -m 0700 /var/lib/kip126-review
sudo install -d -o root -g kip126-review -m 0750 /etc/kip126-review
sudo install -d -o kip126-review -g kip126-review -m 0700 /var/backups/kip126-review
sudo install -o root -g kip126-review -m 0640 deploy/review.env.example /etc/kip126-review/review.env
sudo install -o root -g kip126-review -m 0640 /secure/source/smtp-password /etc/kip126-review/smtp-password
```

在一份 KIP126 源码检出上生成证据快照。它写入持久数据目录，不写入应用源码目录：

```bash
sudo -u kip126-review python3 -m review_app build \
  --source /srv/KIP126 \
  --data-dir /var/lib/kip126-review
```

首次启动会在数据目录创建数据库和 `auth-pepper`。生产机推荐 SMTP；本地 `agently-cli` 的 OAuth 状态不会自动复制到另一台机器。密码放在独立的 0640 文件中，不写入环境示例、源码或数据库。

把 `deploy/*.service` 和 `deploy/*.timer` 安装到 `/etc/systemd/system/`，然后启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kip126-review.service
sudo systemctl enable --now kip126-review-backup.timer
```

服务只监听 `127.0.0.1:8765`。由 Nginx、Caddy 或现有入口提供 HTTPS，并把公开地址写入 `REVIEW_PUBLIC_ORIGIN`。如果公开在路径前缀下，同时把前缀写入 `REVIEW_COOKIE_PATH`。

## 备份与恢复

不要在服务运行时用普通 `cp` 只复制 `judgments.sqlite3`，因为已提交数据可能仍在 WAL 文件中。使用应用命令调用 SQLite 在线备份 API：

```bash
sudo -u kip126-review python3 -m review_app backup \
  --data-dir /var/lib/kip126-review \
  --output /var/backups/kip126-review
```

每个时间戳目录包含一致的数据库、对应的证据快照和清单，并运行 `PRAGMA integrity_check`。验证码、会话和限流记录会从备份中删除，所以恢复后所有人需要重新登录。备份不包含 SMTP 密码或 `auth-pepper`。应由机器现有的备份系统把这些目录加密复制到另一台机器或对象存储，并设置保留周期和容量告警。

恢复步骤：停止服务，把选定备份中的 `judgments.sqlite3` 和 `snapshot.json` 复制到空的数据目录，所有者设为 `kip126-review`、权限设为 0600，然后启动服务。服务会生成新的 `auth-pepper`，所有旧会话自然失效。更新 KIP126 证据时只重新执行 `build` 并重启服务；数据库中的旧判断保留，并通过内容指纹标为需要重审。

## 容量规划

当前证据快照约 0.5 MB，只有 255 张审核卡。即使 1,000 人全部审核，也只有约 25.5 万条判断。生产初期给数据和备份各预留数 GB 已很宽裕；更有意义的告警是磁盘使用率、备份是否按时生成、`PRAGMA integrity_check` 结果以及写入延迟。数据库接近单盘容量、需要多个写服务实例或持续出现锁等待时，再迁移到 PostgreSQL。
