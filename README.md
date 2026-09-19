# KIP126 对应关系审核台

这是独立于 [KIP126](https://github.com/SII-MATH/KIP126) 的人工审核应用。它读取另一份 KIP126 源码检出，生成 Blueprint 陈述与 Lean 声明的审核卡片；审核结论保存在本仓库本机的 SQLite 数据库中，不写入 KIP126。

需要 Python 3.10+。在本仓库根目录运行：

```bash
python3 -m review_app build --source /path/to/KIP126
REVIEW_MAILER=agently REVIEW_ALLOW_ANY_EMAIL=1 python3 -m review_app serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

`--source` 指向含有 `blueprint/src/content.tex` 和 `KIP126/` 的 Git 检出。本地开发默认把数据放在 Git 忽略的 `.review/`。生产部署应给 `build` 和 `serve` 传入相同的 `--data-dir /var/lib/kip126-review`，让数据库独立于源码发布目录；也可设置 `REVIEW_DATA_DIR`。更新 KIP126 源码后，重新运行 `build` 并重启服务，不会删除判断记录。目录、systemd 服务与备份恢复步骤见 [部署文档](review_app/DEPLOYMENT.md)。

审核者用邮箱验证码登录；所有能收信的邮箱均可登录。验证码从已在本机 `agently-cli` 授权的 `siimath@agent.qq.com` 邮箱发送。每个邮箱的审核进度、历史和导出记录独立，服务端只接受登录邮箱名下的判断。也可改用 SMTP；配置、限额及数据回流见 [应用文档](review_app/README.md)。服务默认只监听本机；远程访问需可信 HTTPS 反向代理。前后性能测量见 [性能报告](review_app/PERFORMANCE.md)。

```bash
python3 -m unittest review_app.test_review review_app.test_auth review_app.test_storage
```
