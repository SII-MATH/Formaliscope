# KIP126 对应关系审核台

从 `blueprint/src/content.tex` 引用的章节读取节点，按每条 `\lean{...}` 生成一张人工审核卡片。页面并排展示 Blueprint 原文与定位到的 Lean 源码；审核者选择“对齐 / 部分对齐 / 不对齐 / 暂无法判断”，并可填写理由。未定位到的声明明确标示，机器生成的候选不会自动算作审核通过。

```bash
python3 -m review_app build
python3 -m review_app serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

仅使用 Python 标准库。运行数据放在 Git 忽略的 `.review/`：`snapshot.json` 是构建时的只读证据快照，`judgments.sqlite3` 是人工记录。源文件变化后重新执行 `build`，重启服务；旧判断留在历史中，但内容指纹不再匹配时会显示“需重审”。页面“导出记录”下载带有源码提交与快照摘要的 JSON。

## 请求与缓存

- 构建时解析 Blueprint 和 Lean，打开网页时不运行 Lake 或重新扫描源码。
- 清单只传标题、标识、状态；卡片证据按需读取，并用内容指纹做 ETag。浏览器最多保留 12 张卡片，空闲时预读相邻卡片。
- 审核历史与进度始终从 SQLite 读取且不缓存。数据库启用 WAL、短事务和 busy timeout；每次提交带 UUID，网络重试不会重复写入。
- 并发等待时间、审核者之间的差距及优化前后五轮对照见 [PERFORMANCE.md](PERFORMANCE.md)。
- 默认只监听 `127.0.0.1`。它是单机审核原型，不包含身份认证；需要让多人远程使用时，应接入已有可信认证代理，并将审核人绑定到服务端身份后再开放网络入口。

当前第一版按 KIP126 Blueprint 做候选来源。FormaliScope 的 Stage 3 论文节点和本应用不共享判断数据，也不应把两套节点 ID 当成同一审核对象。`\leanok` 和“对齐”是不同判断：此页面不验证证明完成情况。

## 验证

```bash
python3 -m unittest review_app.test_review
```

数学公式渲染使用从本机 FormaliScope 前端复用的 MathJax 浏览器包（Apache 2.0，许可证在 `static/MATHJAX-LICENSE.txt`）和该项目的 LaTeX 渲染辅助脚本。前端资源均由本地服务提供，无需 CDN。
