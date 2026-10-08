# 本机异机备份

HK-VPS 每日备份继续由服务器上的 `formaliscope-review-backup.timer` 生成。本机负责拉取已经完成的备份，保存到项目的 `.formaliscope/backups/hk-vps/`。定时执行调用与手动执行相同的脚本，不需要 VPS 主动连接本机。

## 私有目录

```text
Formaliscope/
  .formaliscope/
    backups/
      hk-vps/
        config.json
        status.json
        <服务器备份时间>.tar.gz.fernet
      keys/
        hk-vps.fernet
```

整个 `.formaliscope/backups/` 已被 Git 忽略。目录权限为 0700，配置、密钥和归档为 0600。密钥放在归档目录之外，不上传到 VPS，也不进入归档；首次同步自动生成随机密钥，无需用户密码。已有归档但密钥丢失时拒绝生成新密钥，以免把旧归档无法恢复的情况掩盖。迁移本机存储时应同时妥善保管原密钥。

## 配置与运行

工具需要 Python 3.10+ 和独立加密依赖；网页服务的运行依赖不变：

```sh
python3 -m pip install -r deploy/requirements-offsite-backup.txt
```

按 [配置模板](../deploy/offsite-backup.config.example.json) 设置 SSH 别名、服务器备份目录、本机目录及密钥路径，把本机配置保存为 `.formaliscope/backups/hk-vps/config.json`。SSH 必须已可免交互登录，主机密钥必须预先可信；工具不接受新的主机密钥、不索取密码、不把凭据写入配置。

```sh
python3 deploy/formaliscope_offsite_backup.py sync --config .formaliscope/backups/hk-vps/config.json
```

同步只选择服务器最新已经完成的 `YYYYMMDDTHHMMSSZ` 目录，接受 v2 格式。服务器用当前正式应用只读校验，传输固定三个文件：数据库、对应快照和 manifest；不复制活动数据库、认证密钥、明文恢复码或任意目录。

本机校验文件摘要、SQLite 完整性、快照指纹和来源目录，使用 cryptography 的 Fernet 进行认证加密，随后实际解密到私有临时目录再次校验。全部成功后才发布新归档；重复拉取只验证不覆盖。Fernet 需要完整内容放入内存，工具设置容量上限；当前约 41 MB 的快照在范围内，数据规模超过限制时应改用流式加密方案。

模板的 `keep` 为 30。只有同步成功才清理本任务可验证且来源匹配的旧归档，本次选中的副本始终保留，其余按时间选择最近的备份。网络、校验或加密失败不会清理已有备份。损坏文件、未知格式和符号链接保留供人工判断。状态文件只保存运行摘要，不包含姓名、判断内容、密钥或恢复码。

## 定时执行

当前本机使用 Codex 的定时任务每天北京时间 11:00 执行一次同步，此时 VPS 的每日备份通常已完成。正常运行不通知，只在失败时提醒。使用本地文件的任务需要电脑开机、联网且 Codex 应用运行；离线期间不会获得新的异机副本，恢复联网后可手动执行同一同步命令补齐最新备份。

定时任务只调用已验证的同步工具，不修改应用代码、快照或 VPS 部署。同步工具自带互斥锁，避免手动运行与定时运行并发操作同一个归档目录。

## 校验与恢复

只读校验指定加密归档：

```sh
python3 deploy/formaliscope_offsite_backup.py verify \
  --config .formaliscope/backups/hk-vps/config.json \
  --archive .formaliscope/backups/hk-vps/<时间>.tar.gz.fernet
```

恢复到全新的目录；工具拒绝覆盖已有目录，拒绝异常归档路径、重复条目、链接和额外文件：

```sh
python3 deploy/formaliscope_offsite_backup.py restore \
  --config .formaliscope/backups/hk-vps/config.json \
  --archive .formaliscope/backups/hk-vps/<时间>.tar.gz.fernet \
  --output /path/to/new-private-restore-directory
```

恢复命令只解密、校验并生成数据库、快照和 manifest，不启动服务、不迁移数据库。正式恢复还需使用兼容应用版本，按照 [部署说明](DEPLOYMENT.md) 保留现场、设置权限、创建新的本机认证密钥、预检和验证登录。恢复后旧会话失效，用户用原恢复码重新登录。

## 回归

```sh
python3 -m unittest review_app.test_offsite_backup review_app.test_storage review_app.test_backup_cli
```

测试使用合成数据，覆盖加密与解密、损坏或错误密钥、安全提取、完整性检查、重复拉取、失败保留和来源限定清理。正式备份的演练只在私有临时目录进行，不连接本地预览或写入 VPS 运行数据库。
