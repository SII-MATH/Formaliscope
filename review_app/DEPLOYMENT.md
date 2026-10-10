# 部署、就绪检查与持久存储

新部署统一使用 `formaliscope` 命名。保留的 `deploy/kip126-review*` 单元供已有安装迁移参考，不在新环境启用。发布前先在测试环境验证；本仓库的准备工作不会自动变更目标服务器。

| 路径 | 内容 | 发布时的处理 |
| --- | --- | --- |
| `/opt/formaliscope/releases/<commit>` | 不可变代码、静态资源、Workflow 与 schema | 新增目录，保留上一版本 |
| `/opt/formaliscope/current` | 当前 release 的符号链接 | 原子切换 |
| `/var/lib/formaliscope` | `snapshot.json`、`judgments.sqlite3`、`auth-pepper` | 使用持久磁盘，不随代码覆盖 |
| `/etc/formaliscope` | Origin、Cookie、只读 GitHub token | 密钥单独管理 |
| `/var/backups/formaliscope` | SQLite 在线备份、对应快照及清单 | 加密复制到异机存储 |

审核服务保持单实例，数据库使用 SQLite WAL。代码更新不删除判断。不要让多个容器通过共享卷同时运行审核服务；需要多写实例时先迁移数据库。

## 首次安装

下面是 Linux/systemd 安装模板，需替换域名和固定源码提交。目标机器需要 Python 3.10+、curl、tar、sha256sum、flock、runuser 和 systemd，无需 npm 或第三方 Python 包。

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin formaliscope-review
sudo install -d -o formaliscope-review -g formaliscope-review -m 0700 \
  /var/lib/formaliscope /var/backups/formaliscope
sudo install -d -o root -g formaliscope-review -m 0750 /etc/formaliscope
sudo install -d -o root -g root -m 0755 /opt/formaliscope/releases
sudo install -o root -g formaliscope-review -m 0640 deploy/review.env.example /etc/formaliscope/review.env
sudo install -o root -g formaliscope-review -m 0640 deploy/backup.env.example /etc/formaliscope/backup.env
```

配置 `REVIEW_AUTH_MODE=name`、HTTPS Origin 和 Cookie 路径，无需邮箱服务或学生名单。管理员由服务账号在候选应用目录执行下列命令创建；凭证文件只读给本人，勿打包或上传：

```bash
sudo -u formaliscope-review python3 -m review_app create-admin \
  --data-dir /var/lib/formaliscope --name '管理员姓名' \
  --output /var/lib/formaliscope/admin-account.txt
```

管理员在登录页使用账号 ID 与初始密码 `12345678`，进入后须先修改密码。schema 13 迁移为已有姓名账号初始化同样的初始密码，保留原记录和权限；升级前备份、停服，不能用旧应用运行迁移后的数据库。恢复码机制已取消；更多身份及旧记录迁移步骤见 [IDENTITY.md](IDENTITY.md)。已有邮箱部署继续使用时必须显式设置 `REVIEW_AUTH_MODE=email`。正式服务不使用 `--preview`。`REVIEW_PUBLIC_ORIGIN=https://review.example.org` 不包含应用路径；前缀入口 `/review/` 使用 `REVIEW_COOKIE_PATH=/review/`。

在开发机或 CI 的固定提交、干净 Git checkout 构建 Statement 快照：

```bash
python3 -m review_app build --statements --require-clean \
  --source /srv/KIP126 --output /tmp/formaliscope-artifact/snapshot.json
```

应用包必须包含 `review_app/`、`statement_workflow/schema/` 及其本地静态资源。只复制 `review_app/` 会丢失 enrichment v1 运行契约。安装应用包到 `/opt/formaliscope/releases/<app-commit>`，先创建 `current` 链接，再以服务账号安装经过校验的候选快照：

```bash
sudo -u formaliscope-review python3 -m review_app install-snapshot \
  --file /srv/staging/snapshot.json --data-dir /var/lib/formaliscope
```

命令应在候选应用目录执行，暂存文件和上层目录应可由服务账号读取。使用自动制品拉取时，按 [GitHub Actions 部署说明](GITHUB_ACTIONS_DEPLOYMENT.md) 先拉应用、再拉快照，最后安装并启用 service/timer，避免第一次启动时没有快照。

## 部署前的只读检查

在候选应用目录，用实际服务身份及实际环境文件运行预检：

```bash
sudo systemd-run --wait --pipe --uid=formaliscope-review \
  --property=WorkingDirectory=/opt/formaliscope/current \
  --property=EnvironmentFile=/etc/formaliscope/review.env \
  /usr/bin/python3 -m review_app preflight --data-dir /var/lib/formaliscope
```

只有返回 `ready: true` 且退出码为 0 才继续。预检验证：源码提交、快照 digest/内容指纹、干净来源、Statement 模式、页面引用资源、enrichment schema、HTTPS Origin、Cookie 路径、姓名身份模式与启用的管理员（兼容 email 模式才检查邮件配置）。`archive-unverified` 来源只适用于预览，不能作为新生产快照。已有 Blueprint 安装可显式加 `--legacy-blueprint`；正式 systemd 服务使用该兼容开关，Statement 检查仍然有效。

兼容开关允许保留尚未记录 clean 状态的既有 v1 Blueprint 快照，并在预检中明确报告来源限制；它不允许新 Statement 快照跳过干净来源要求。再次发布新证据时重新构建。

预检只读查询身份表中的管理员配置，不初始化数据库、不创建密钥、不发邮件、不测试代理。仍需在测试部署中完成账号注册、密码登录、首次改密、同名双用户隔离、管理员重置、退出恢复、管理员汇总与重名注册拒绝。

## 启动与 readiness

```bash
sudo install -o root -g root -m 0755 deploy/formaliscope-app-pull \
  deploy/formaliscope-snapshot-pull deploy/formaliscope-review-backup /usr/local/sbin/
sudo install -o root -g root -m 0755 deploy/formaliscope_release.py /usr/local/sbin/formaliscope-release
sudo install -o root -g root -m 0644 deploy/formaliscope-*.service \
  deploy/formaliscope-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now formaliscope-review.service
curl --fail http://127.0.0.1:8765/healthz
```

`formaliscope-app-pull` 读取公开的应用 Release 时可不配置 GitHub 令牌。若 `/etc/formaliscope/github-read-token`（或 `FORMALISCOPE_GITHUB_TOKEN_FILE` 指定的文件）存在，拉取器会使用其中的非空令牌；文件不可读或为空时直接报错，不会悄悄改用匿名请求。私有仓库需要能读取 Release 的令牌。安装拉取器不会自动启用 `formaliscope-app-pull.timer`；是否定时部署由操作员另行决定。旧 KIP126 证据拉取器仍按其独立配置运行，多仓库集合不使用它自动覆盖运行证据。

`/healthz` 无需登录，只报告 `ready`、快照 schema 与数据库 schema，不包含用户或内容。拉取器重启服务后也用该端点检查就绪。再通过公开 HTTPS 路径检查登录页、静态资源、密码登录、更新通知及重名注册拒绝。反向代理必须剥去应用前缀后转发至 `127.0.0.1:8765`，限制外界直接访问监听端口。

Nginx/Caddy 变更先验证配置，再备份和 reload。如果目标站点已有全局认证或旧应用路由，明确新路径所用身份模式，并同时验证旧入口继续可用。此文档不假定既有站点路径或认证规则可直接覆盖。

## 更新顺序

1. 在新 release 目录运行全部应用、Workflow、模块和 schema 导入回归，校验制品 checksum。
2. 记录当前应用提交、快照 digest、数据库 schema 及 `current` 目标；创建在线备份并确认异机副本可读取。
3. 先停止审核服务，再用新代码对当前快照执行 `migrate`，之后才安装新快照。schema 7 会重命名会话列，迁移时旧进程不能继续读写。旧快照用于为早期判断补齐稳定依据，不能先覆盖。
4. 检查 `unchanged/changed/added/removed`，在实际服务身份下运行预检。
5. 原子切换 `current`，重启服务；检查 `/healthz`、公开入口与登录。确认后启用拉取 timer。

```bash
# 使用旧 current 的 CLI 先做备份，不向旧版本传入新参数。
cd /
sudo -u formaliscope-review env PYTHONPATH="$(readlink -f /opt/formaliscope/current)" \
  python3 -m review_app backup --data-dir /var/lib/formaliscope --output /var/backups/formaliscope
sudo systemctl stop formaliscope-review.service
# migrate 应在新候选 release 目录执行。
cd /opt/formaliscope/releases/<new-commit>
sudo -u formaliscope-review python3 -m review_app migrate --data-dir /var/lib/formaliscope
sudo ln -sfn /opt/formaliscope/releases/<new-commit> /opt/formaliscope/current.new
sudo mv -Tf /opt/formaliscope/current.new /opt/formaliscope/current
sudo systemctl restart formaliscope-review.service
```

备份命令应从 `/` 执行，确保 `PYTHONPATH` 指定旧 current；迁移命令由新候选 release 提供模块。不要在其他 checkout 下误运行。发布脚本以 `formaliscope-review` 身份做迁移和快照写入，避免 root 创建的 0600 数据文件使服务无法读取。迁移失败时保留服务停用和备份，确认数据库兼容性后再恢复；不能自动重启只支持旧 schema 的代码。

## 备份、恢复与回滚

服务运行时不要只 `cp judgments.sqlite3`，提交数据可能仍在 WAL 中。每日 timer 与以下命令使用 SQLite 在线备份 API：

```bash
sudo /usr/local/sbin/formaliscope-review-backup
```

时间戳目录包含一致数据库、对应快照与 manifest，并完成 `PRAGMA integrity_check`。备份与快照安装共用数据目录的 `.data.lock`；快照只读取一次，清单记录该副本及数据库的 SHA-256 和大小。备份移除验证码、会话、限流与预览凭证，保留姓名身份、角色、密码哈希与合并档案；恢复后用原密码重新登录。`auth-pepper` 不在应用备份中，恢复时可以重新生成。私人账号文件不在备份或应用包中。

每日 helper 默认保留最近 30 份已验证的 v2 备份。可在 `/etc/formaliscope/backup.env` 设置 `REVIEW_BACKUP_KEEP`；该文件由 backup service 读取，直接运行 helper 时需显式导出环境变量。保留数量必须为正整数；只有新备份成功完成后才清理。旧 v1 备份、其他数据目录的备份、符号链接、不完整目录和未知文件不自动删除，升级后的历史备份需单独评估。直接运行 `python3 -m review_app backup --data-dir ... --output ...` 默认不清理；加 `--keep 30` 才启用数量保留。helper 与旧应用组合时自动退回旧版备份命令，保留全部历史。

新 v2 备份在传输前和异机解密后可只读核对：

```bash
python3 -m review_app verify-backup --directory /var/backups/formaliscope/<timestamp>
```

该命令不要求原数据目录存在，不创建身份或密钥。旧 v1 没有文件摘要与目录范围，仍按既有 SQLite 完整性和快照校验步骤恢复，不进入自动保留清理。

第一版异机目的地为用户 Mac 上的 `.formaliscope/backups/hk-vps/`。本机同步工具只拉取完成的 v2 备份，校验并加密保存，在解密复验成功后执行本机数量保留，不修改 VPS 的备份保留或运行数据。旧 v1 备份不自动转换或复制；若最新服务器备份格式无法验证，同步失败且保留已有本机归档。配置、定时运行及空目录恢复演练见 [本机异机备份](OFFSITE_BACKUP.md)，职责见 [存储设计](STORAGE.md)。

发现 readiness 或登录失败时，先暂停 `formaliscope-app-pull.timer` 和 `formaliscope-snapshot-pull.timer`，停止服务，保存失败现场。区分两类恢复：

- **只回滚代码或快照**：确认旧代码支持当前数据库 schema；将 `current` 原子指回上一 release，或使用 `install-snapshot` 安装上一份已验证快照。保留当前数据库，使发布后的人工判断不会丢失；不匹配的判断仍留在历史中。
- **恢复整个数据集**：数据库不兼容或数据受损时，先保存当前数据目录，再在空目录恢复选定备份的 `judgments.sqlite3` 和 `snapshot.json`。这会回到备份时间点，必须明确发布后新增记录如何处理，不能自动覆盖。

恢复文件所有者为 `formaliscope-review`，数据目录 0700、数据文件 0600。恢复备份时创建新的 `auth-pepper` 或由批准的密钥恢复策略处理，所有旧会话失效。运行预检、迁移兼容检查、启动并检查 `/healthz` 和登录，确认后再恢复 timer。脚本 readiness 失败会报错，不会自动用旧数据库覆盖新记录。

## 容量与待部署确认

当前 KIP126 示例快照约 41 MB，含 6,222 条声明与 1,421 个文件；早期“255 张卡片、约 0.5 MB”的容量估算不适用于 Statement 模式。若 1,000 人各保存一次全库判断，量级约 622 万条；历史重判会继续增加记录。

上线前按实际审阅人数、摘要长度、备份频率与保留期测量数据库、备份和磁盘余量，重新做列表/依赖图/并发写入演练。监控磁盘、写入延迟、数据库锁等待、备份生成及完整性检查；需要多写实例时再规划 PostgreSQL。

[版本模型](VERSIONING_AND_RELEASE.md) 定义已有判断与快照的兼容性；[GitHub Actions 部署](GITHUB_ACTIONS_DEPLOYMENT.md) 定义制品发布。姓名登录与恢复、公开代理路由、systemd 单元验证和备份恢复演练均需在目标 Linux 环境完成后才算上线准备验证完毕。
