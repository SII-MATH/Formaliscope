# KIP126 对应关系审核台

从单独指定的 KIP126 源码检出中读取 `blueprint/src/content.tex` 引用的章节，按每条 `\lean{...}` 生成一张人工审核卡片。页面并排展示 Blueprint 原文与定位到的 Lean 源码；审核者选择“对齐 / 部分对齐 / 不对齐 / 暂无法判断”，并可填写理由。未定位到的声明明确标示，机器生成的候选不会自动算作审核通过。

```bash
python3 -m review_app build --source /path/to/KIP126
REVIEW_MAILER=agently REVIEW_ALLOW_ANY_EMAIL=1 python3 -m review_app serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

应用本体仅使用 Python 标准库；`REVIEW_MAILER=agently` 模式还需要本机已授权的 `agently-cli`。运行数据放在本仓库 Git 忽略的 `.review/`：`snapshot.json` 是构建时的只读证据快照，`judgments.sqlite3` 保存人工记录、验证码摘要及会话，`auth-pepper` 是验证码摘要密钥。请一起备份数据库与密钥，不要把它们提交到 Git。源文件变化后重新执行 `build --source ...`，重启服务；旧判断留在原审核人的历史中，但内容指纹不再匹配时会显示“需重审”。登录后点击“导出我的记录”下载带有源码提交与快照摘要的 JSON，可用于人工审查、统计或导入后续流程；导出不会自动修改 KIP126。

## 邮箱登录

当前配置允许任何能收信的邮箱登录。验证码为 8 位数字，10 分钟有效且只能使用一次；每个验证码最多试 5 次。会话有效期为 12 小时，退出后立即失效。审核人邮箱由服务端写入判断记录，页面提交的同名字段不会生效。邮件发送请求按邮箱、来源 IP 和全局限速；目前本机邮箱每日最多可发送 50 封，应用限制为 40 次/日，保留 10 封余量。发送失败不会扣应用限额。邮件服务商自身仍可能拒发或限流。

每个已验证邮箱拥有独立的审核进度、判断历史和导出记录。所有用户查看同一份 Blueprint/Lean 证据快照，但无法通过网页 API 读取或修改其他邮箱的判断。判断仍存储在同一个 SQLite 数据库中，以审核邮箱为键隔离；服务管理员持有数据库文件时可以查看全部记录。部署此版本时，应用会自动将旧版判断表迁移为按邮箱限定请求 ID 的表，并保留原有判断。

本机已绑定 `siimath@agent.qq.com` 时，使用上面的 `REVIEW_MAILER=agently` 命令。服务进程必须能访问同一用户的 `agently-cli` 登录状态与网络。不要在网页或仓库里填写邮箱密码。若改用 SMTP，请在运行环境设置 `REVIEW_MAILER=smtp`、`REVIEW_SMTP_HOST`、`REVIEW_SMTP_PORT`、`REVIEW_SMTP_SECURITY=ssl`（或 `starttls`）、`REVIEW_SMTP_USER`、`REVIEW_SMTP_FROM`，以及 `REVIEW_SMTP_PASSWORD_FILE` 指向仅服务账号可读的密码文件；也可用 `REVIEW_SMTP_PASSWORD` 环境变量。仍需设置 `REVIEW_ALLOW_ANY_EMAIL=1`。默认发件模式为 SMTP，缺少配置时服务拒绝启动。

如果通过 HTTPS 代理公开，例如 `https://example.org/proxy/8765/`，设置 `REVIEW_PUBLIC_ORIGIN=https://example.org` 和 `REVIEW_COOKIE_PATH=/proxy/8765/`，由代理剥掉路径前缀再转发到本地服务。这样 Origin 校验使用公开地址，Cookie 仅发送到本应用路径并带 `Secure` 标记。登录页、资源、API 与跳转均使用相对路径。代理需终止 TLS，并限制对本机服务的直接访问。邮箱验证码适合这个人工审核场景；若以后承载更敏感的数据，应改用更强的登录方式。

## 请求与缓存

- 构建时解析 Blueprint 和 Lean，打开网页时不运行 Lake 或重新扫描源码。
- 清单只传标题、标识、状态；卡片证据按需读取，并用内容指纹做 ETag。浏览器最多保留 12 张卡片，空闲时预读相邻卡片。
- 审核历史与进度始终从 SQLite 读取且不缓存。数据库启用 WAL、短事务和 busy timeout；每次提交带 UUID，网络重试不会重复写入。
- 并发等待时间、审核者之间的差距及优化前后五轮对照见 [PERFORMANCE.md](PERFORMANCE.md)。
- 默认只监听 `127.0.0.1`。远程访问由可信 HTTPS 反向代理转发，部署时须设置公开 Origin 与 Cookie 路径。

当前第一版按 KIP126 Blueprint 做候选来源。FormaliScope 的 Stage 3 论文节点和本应用不共享判断数据，也不应把两套节点 ID 当成同一审核对象。`\leanok` 和“对齐”是不同判断：此页面不验证证明完成情况。

## 验证

```bash
python3 -m unittest review_app.test_review review_app.test_auth
```

数学公式渲染使用从本机 FormaliScope 前端复用的 MathJax 浏览器包（Apache 2.0，许可证在 `static/MATHJAX-LICENSE.txt`）和该项目的 LaTeX 渲染辅助脚本。前端资源均由本地服务提供，无需 CDN。
Lean 源码高亮复用 FormaliScope 的 `lean-renderer.js`，在浏览器内对关键字、注释、字符串、类型和 `sorry` 等着色；源码中的 HTML 特殊字符在生成高亮标记前转义。
