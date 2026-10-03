# GitHub Actions 制品发布与 VPS 拉取

仓库使用“GitHub 发布不可变制品，VPS 主动拉取”的流程。Actions 不保存 VPS SSH 私钥，不读取生产数据库。更新 main 会触发应用制品发布，发布结果以 Actions 的实际运行状态为准；目标机器的配置、拉取和验证仍需按下述步骤完成。

## 三条工作流

| Workflow | 触发 | 发布前验证与产物 |
| --- | --- | --- |
| `ci.yml` | PR、main push | 自动发现全部 review_app / statement_workflow 测试，检查全部前端模块和 Node 回归、Python/脚本语法及新旧 systemd 单元 |
| `publish-app.yml` | main push | 重跑同一应用/Workflow/模块/schema 导入测试门槛，`git archive` 打包整个仓库，发布 `app-<commit>`、SHA-256 与应用 manifest |
| `publish-snapshot.yml` | 手动或 `kip126-updated` dispatch | 默认 develop，也可指定固定 ref；从干净 KIP126 检出构建 `build --statements --require-clean`，验证模式、commit、digest，发布 `snapshot-<source-commit>` |

应用包包括 `review_app/`、`statement_workflow/` 与 enrichment schema。生产部署不需要自动 Agent 执行器；用户先按目录手动补数据。自动快照 workflow 发布的是基础源码快照，不自动生成 Agent 回译。需要发布手动 enrichment 时，先对冻结基础快照运行 `validate-enrichment` 和 `enrich-snapshot`，将校验通过的候选产物作为单独的正式变更评审；不能默默替换同一标签的证据。

读取私有 `SII-MATH/KIP126` 需要只授予该仓库 Contents read 的 `KIP126_READ_TOKEN` Secret。发布使用 `GITHUB_TOKEN` 的 Contents write，CI 只有 Contents read。保护 main 并要求 CI 通过；按组织策略启用生产 Environment 审批。禁止在日志、制品或数据库打印 token 和恢复码。

## VPS 的目录和权限

```text
/etc/formaliscope/github-read-token      root:root 0600，只读 release token
/etc/formaliscope/review.env             root:formaliscope-review 0640，姓名身份模式与公开入口
/etc/formaliscope/smtp-password          root:formaliscope-review 0640，独立密码
/opt/formaliscope/releases/<app-commit>   不可变应用目录
/opt/formaliscope/current                当前应用链接
/var/lib/formaliscope                    服务账号持有，0700
/var/backups/formaliscope                服务账号持有，0700
```

目标 Linux 需要 curl、Python 3.10+、tar、sha256sum、flock、runuser 和 systemd。GitHub token 只读取 `SII-MATH/Formaliscope` 的 release；不要复制开发机凭据。正式账号、Origin、Cookie 和管理员配置见 [部署说明](DEPLOYMENT.md)。

首次安装先创建服务账号与目录、写好配置，安装三个 `/usr/local/sbin/formaliscope-*` 脚本。**先运行 app pull，再运行 snapshot pull，最后安装并启用 systemd 单元**；第一次 app pull 没有快照，因此不能先启用审核服务：

```bash
sudo /usr/local/sbin/formaliscope-app-pull
sudo /usr/local/sbin/formaliscope-snapshot-pull
# 按 DEPLOYMENT.md 运行带实际环境文件的只读 preflight
sudo install -m 0644 deploy/formaliscope-*.service deploy/formaliscope-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now formaliscope-review.service formaliscope-review-backup.timer
# 首次启动前按 IDENTITY.md 执行 create-admin；旧邮件部署明确设置 REVIEW_AUTH_MODE=email。
# 人工验证 /healthz、公开入口、姓名登录、恢复码与管理员后启用：
sudo systemctl enable --now formaliscope-app-pull.timer formaliscope-snapshot-pull.timer
```

## 拉取器约束

两个拉取器用 `/run/lock/formaliscope-deploy.lock` 串行化更新。应用选择最新 `app-*`，校验 archive 与 manifest，解包到不可变目录；先备份和迁移，再原子切换 `current`。快照选择最新 `snapshot-*`，核对 SHA-256、快照内部 digest、source commit、干净来源和 review mode；先备份，再原子安装。数据库、备份与快照写入使用服务身份，认证密钥由服务启动创建。

快照拉取器默认要求 Statement。已有 Blueprint 部署需要显式设置 `FORMALISCOPE_REVIEW_MODE=blueprint`；服务预检带 `--legacy-blueprint` 保持旧模式升级兼容。旧 `deploy/kip126-review*` 单元保留，但新安装使用 `formaliscope-*`。不要同时启用两套服务访问同一数据库。

重启后检查 `/healthz`，最多重试 15 次。失败即报错，由运维按 [回滚步骤](DEPLOYMENT.md#备份恢复与回滚) 处理，不自动覆盖用户数据。静态资源、快照和 schema 在启动时读取，更新后需重启。`/login` 返回成功只代表登录页面可达，不能代替 readiness 检查。

新自动 snapshot release 会更新当前证据，因此启用 timer 前确认接收 source 分支、审阅范围和手动 enrichment 保留策略。实际 source commit 和 digest 是审阅依据；GitHub release 时间不能证明数学内容或回译正确。

## 发布前验收

在测试 Linux 环境确认所有 systemd 单元通过验证，正式 preflight ready=true，姓名登录、同名双身份隔离、恢复码与单独创建的管理员有效；演练一次 SQLite 在线备份和恢复、上一 release 回滚，并检查已有公开入口继续可用。默认 timer 每 15 分钟检查应用/快照，每日生成备份。

代码提交、workflow 发布、目标机器配置、公开路由和 timer 启用分别是独立动作。仓库文件准备完成不表示这些外部步骤已经执行。
