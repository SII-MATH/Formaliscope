# HK-VPS 首次上线方案

入口使用 `https://math.opensii.ai/formaliscope/`。网页和 API 由 HK-VPS 上同一个
Python 服务提供，监听 `127.0.0.1:8765`，Caddy 剥去 `/formaliscope` 前缀后转发。
浏览器负责交互；审核证据、身份和人工判断保存在服务器。域名根入口和其他项目
继续使用原有 Math portal 认证。

## 准备与切换

准备阶段只使用 `/opt/formaliscope/staging/<timestamp>/`，包括候选代码、快照、
环境文件、代理配置和隔离测试数据。保留生产 `current`、数据目录、环境文件和
systemd 状态。在已经安装但停用 service 的旧机器上，**不要运行 app-pull 或
snapshot-pull 来做暂存**：现有拉取器会写生产数据、切换应用并重启已安装单元。

首次发布固定应用 commit 和证据 commit/digest。快照来自经过验证的干净源码，
不能把演示用 `archive-unverified` 数据改标为生产来源。快照约 41 MiB，独立于
应用 Release；不提交快照、用户数据库或恢复码到本仓库。

候选环境：

```ini
REVIEW_AUTH_MODE=name
REVIEW_PUBLIC_ORIGIN=https://math.opensii.ai
REVIEW_COOKIE_PATH=/formaliscope/
REVIEW_TRUST_PROXY_IP=1
```

Caddy 的现有全站 `forward_auth` 应添加匹配条件，排除 **仅** `/formaliscope`
和 `/formaliscope/*`；其他请求仍经过原认证。候选应用路由如下：

```caddyfile
@math_portal_required {
    not path /formaliscope /formaliscope/*
}
forward_auth @math_portal_required 127.0.0.1:18093 {
    uri /verify
    copy_headers X-Math-Portal-User
}

@formaliscope_without_slash path /formaliscope
redir @formaliscope_without_slash /formaliscope/ permanent
handle_path /formaliscope/* {
    request_body {
        max_size 64KB
    }
    reverse_proxy 127.0.0.1:8765 {
        header_up X-Real-IP {remote_host}
        header_up -Authorization
        header_up -X-Math-Portal-User
    }
}
```

这段需要合入原站点配置，不能用它替换整个站点。先运行 Caddy validate/adapt，
比较其他站点和 Math 路由，再在独立 loopback 端口验证该前缀、登录、静态资源、
Origin 检查和代理头覆盖。正式后端端口不对公网开放。

## 隔离验收

以 `formaliscope-review` 身份在独立目录中复制旧库、在线备份、迁移、安装候选
快照并创建临时测试管理员。严格执行不带 `--legacy-blueprint` 的生产 preflight。
验证同名用户分别保存判断、普通用户不能访问管理汇总、退出恢复、恢复码轮换、
Secure Cookie 和前缀路径；在线备份恢复后，判断与角色保留、旧会话失效、恢复码
仍有效。测试身份、明文恢复码和测试库不得进入生产数据或 GitHub 制品。

本机代理测试不会证明正式 HTTPS 路径已经上线。实际切换后仍需验证 HTTPS
登录页、静态模块、`/formaliscope/healthz` 和其他项目入口。

## 正式切换顺序

1. 发布已验证应用版本，下载校验不可变 Release 附件；本次候选修复在 `dev`，
   合并到 `main` 后才会自动发布下一补丁版本。
2. 重新核对服务器配置摘要、原 `current`、服务状态及磁盘空间；若准备后发生
   变化，重新审查，不能覆盖维护人员的新配置。
3. 创建旧数据库、快照、配置和代码指针备份。SQLite 使用在线备份 API。
   另保存适合旧版本回滚的原库副本，先在隔离环境确认完整性与兼容性。
4. 在新 release 目录先对旧快照执行 `migrate`，再 `install-snapshot`。所有数据
   写入用服务账号，文件权限 0600、数据目录 0700。
5. 单独创建正式管理员，将恢复码私下交付指定人员；不打印在日志、不放网页目录。
6. 安装候选环境和服务单元，严格预检通过后原子切换代码，启动服务并检查本机
   `/healthz`。备份 timer 保持启用，应用和证据更新 timer 暂时保持关闭。
7. 保存原站点配置，验证整份 Caddy 配置，再 reload；验收公开入口和原有项目。
8. 记录实际应用版本、证据提交、digest、备份位置及验收结果。

## 回滚

先撤回候选 Caddy 站点配置并 reload，恢复旧入口。停止本次审核服务，保留
当前数据以便抢救上线后判断。代码回滚前必须确认数据库 schema 兼容；不兼容时
在新的空目录恢复原库和对应快照，不能自动覆盖上线后的数据库。原版本代码链接、
环境文件和 systemd 单元一起恢复。应用/证据自动更新 timer 不启用。

同机备份只用于快速回滚，不等于异机备份。需要另选异机存储并加密复制；当前
准备流程没有假定或配置任何云账号。
