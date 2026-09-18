# KIP126 对应关系审核台

这是独立于 [KIP126](https://github.com/SII-MATH/KIP126) 的人工审核应用。它读取另一份 KIP126 源码检出，生成 Blueprint 陈述与 Lean 声明的审核卡片；审核结论保存在本仓库本机的 SQLite 数据库中，不写入 KIP126。

需要 Python 3.10+。在本仓库根目录运行：

```bash
python3 -m review_app build --source /path/to/KIP126
python3 -m review_app serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

`--source` 指向含有 `blueprint/src/content.tex` 和 `KIP126/` 的 Git 检出。更新 KIP126 源码后，重新运行 `build` 并重启服务。生成的 `.review/snapshot.json` 和人工判断数据库 `.review/judgments.sqlite3` 留在本应用目录，已被 Git 忽略；重建快照不会删除判断记录。审核记录也可以从页面导出。

默认仅监听本机。多人远程使用前需要可信认证代理；服务当前没有内建账号系统。实现和缓存说明见 [应用文档](review_app/README.md)，前后性能测量见 [性能报告](review_app/PERFORMANCE.md)。

```bash
python3 -m unittest review_app.test_review
```
