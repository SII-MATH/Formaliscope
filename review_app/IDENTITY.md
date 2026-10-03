# 姓名身份与恢复码

默认 `REVIEW_AUTH_MODE=name`，学生首次只填写姓名。服务器生成随机内部身份；姓名仅用于显示，同名者分别保存判断、进度、历史和导出。姓名未经学校核验，不应当作经过验证的学籍身份。

浏览器通过 HttpOnly / SameSite=Strict Cookie 保持登录 30 天，正式 HTTPS 入口同时设置 Secure。学生不需要设置或提交常用密码。恢复码由系统生成 256 位随机值，服务器只保存 SHA-256 摘要；明文只在创建或重新生成时显示，支持复制、下载，不自动放进浏览器长期存储。

恢复码是私人登录凭证。换设备、退出或清除 Cookie 后，在登录页选择“使用恢复码”，即可继续原身份。再次填写姓名会新建身份，不能按姓名合并。如果登录仍在，可以从账户菜单重新生成恢复码：旧码失效，其他设备的会话退出，当前浏览器保留登录。恢复码丢失且所有设备都退出后，系统无法凭姓名确认原身份；须由管理员线下核对后处理，当前版本没有自助按姓名找回或身份合并。

## 首次部署与管理员

配置示例在 `deploy/review.env.example`。公开入口必须使用 HTTPS；服务仍仅监听 loopback，经 Nginx/Caddy 对外提供。开发时可用 `REVIEW_PUBLIC_ORIGIN=http://127.0.0.1:8890`，HTTP 仅允许本机。

网络限流默认使用直接连接的 IP，不信任请求中的代理头。本机 Caddy 部署应设置 `REVIEW_TRUST_PROXY_IP=1`，并在该应用的 `reverse_proxy` 内配置 `header_up X-Real-IP {remote_host}`，强制覆盖浏览器传入的值；否则所有学生会共同占用本机代理 IP 的额度。后端必须保持仅监听 loopback，不能直接暴露到公网，也不能让其他未经授权的本机代理转发到它。只有明确开启且直接连接来自 loopback 时，服务才使用唯一、合法的 `X-Real-IP`；缺失、重复、IP 列表或无效值均回退直接连接的 IP，始终忽略 `X-Forwarded-For`。该设置仅影响姓名注册、恢复和旧邮箱验证码的网络限流，写操作仍检查 Origin。

管理员由服务器操作员本地创建，第一位注册学生、输入“管理员”或提交角色字段都不会获得管理员权限。在候选应用目录执行：

```bash
sudo -u formaliscope-review python3 -m review_app create-admin \
  --data-dir /var/lib/formaliscope --name '管理员姓名' \
  --output /var/lib/formaliscope/admin-recovery.txt
```

输出只包含身份 ID、姓名和文件路径，不打印恢复码。凭证文件以 0600 排他创建；已有文件不会覆盖。操作员私下保存或交付给指定管理员，再从服务器移走明文文件；不要提交 Git、上传发布包或放进网页目录。管理员在普通登录页使用该恢复码进入，账户菜单会出现“管理汇总”。生产预检只读检查数据库中存在启用的管理员身份，缺少时拒绝启动。

## 已有安装升级

先备份、使用新代码执行 `migrate`，再改配置。数据库 v6 添加身份与恢复摘要表，保留全部旧判断与姓名。已有邮箱认证安装若暂不迁移，须明确设置 `REVIEW_AUTH_MODE=email` 并保留邮件、邮箱准入和管理员邮箱配置；原 OTP 路径仍兼容，name 模式禁用 OTP 与预览登录 API。

要把一个旧身份接到新入口，由操作员核对后指定**准确的已有内部 reviewer 键**（不可仅凭姓名）：

```bash
sudo -u formaliscope-review python3 -m review_app bind-recovery \
  --data-dir /var/lib/formaliscope --reviewer 'existing@example.org' \
  --name '原审阅者姓名' --output /var/lib/formaliscope/existing-recovery.txt
```

命令保留原 owner 键，不移动或合并判断；只允许绑定已存在的身份，一次绑定后不能重复。旧管理员如需保留管理权限，操作员明确加 `--admin`，不会继承邮箱白名单或预览首位管理员权限。没有对应记录的学生正常注册即可。`--preview` 仍是显式本机演示，不用于生产。

在线备份保留姓名身份、管理员标志及恢复码摘要，清除会话、验证码与限流记录。恢复备份后原恢复码继续可用，需要重新登录；`auth-pepper` 可重新生成，不影响恢复码摘要。备份属于私有用户数据，按部署说明保护。

## 接口与边界

- `GET /api/config`：公开认证模式；`GET /api/auth/me`：当前 user_id、显示姓名和管理员角色，不返回恢复码。
- `POST /api/auth/register`：只接收 display_name，返回仅此次显示的 recovery_code 和会话 Cookie。
- `POST /api/auth/recover`：使用 recovery_code 恢复原身份；不会把相同姓名视作同一人。
- `POST /api/auth/recovery`：当前身份重新生成恢复码，撤销旧码和其他会话。
- `POST /api/profile`：改显示姓名；`POST /api/auth/logout`：退出当前浏览器。

写操作检查 Origin 和 JSON；注册与恢复有限流，凭证不出现在 URL、访问日志或身份查询结果里。仅支持单个服务进程。由于入口不验证名单，知道网址的人可创建普通审阅身份；此版本用于小规模团队的记录隔离。管理员汇总由服务器角色检查保护，普通用户只能读写自己的人工记录。

部署验证包括同名双用户隔离、改名、退出恢复、管理员访问、恢复码轮换和备份恢复。完整安装步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。
