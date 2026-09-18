# KIP126 对应关系审核台

这是独立于 [KIP126](https://github.com/SII-MATH/KIP126) 的人工审核应用。它读取另一份 KIP126 源码检出，生成 Blueprint 陈述与 Lean 声明的审核卡片；审核结论保存在本仓库本机的 SQLite 数据库中，不写入 KIP126。

需要 Python 3.10+。在本仓库根目录运行：

```bash
python3 -m review_app build --source /path/to/KIP126
REVIEW_MAILER=agently REVIEW_ALLOW_ANY_EMAIL=1 python3 -m review_app serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

`--source` 指向含有 `blueprint/src/content.tex` 和 `KIP126/` 的 Git 检出。更新 KIP126 源码后，重新运行 `build` 并重启服务。生成的 `.review/snapshot.json` 和人工判断数据库 `.review/judgments.sqlite3` 留在本应用目录，已被 Git 忽略；重建快照不会删除判断记录。审核记录也可以从页面导出。

审核者用邮箱验证码登录；所有能收信的邮箱均可登录。验证码从已在本机 `agently-cli` 授权的 `siimath@agent.qq.com` 邮箱发送，审核记录由服务端绑定到登录邮箱。也可改用 SMTP；配置、限额及数据回流见 [应用文档](review_app/README.md)。服务默认只监听本机；远程访问需可信 HTTPS 反向代理。前后性能测量见 [性能报告](review_app/PERFORMANCE.md)。

```bash
python3 -m unittest review_app.test_review review_app.test_auth
```
