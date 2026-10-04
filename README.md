# Formaliscope · Statement 审阅台

KIP-12 前端以 Lean 声明为条目，提供目录选择、名称与源码搜索、标签筛选、声明详情、候选依赖图及独立人工审阅。主标题使用 Lean 名称，中文说明由 Agent 补全；回译、角色、主题和优先度遵循 [Statement enrichment v1](statement_workflow/schema/statement-enrichment.v1.schema.json)。人工判断单独保存，Agent 草稿不会自动成为“通过”。

仓库包含网页服务 `review_app/`、声明补充数据的 [契约与导入说明](statement_workflow/README.md)，以及下述仓库 Skill。补全按选定目录或声明分批执行，早期 Blueprint 对照模式继续保留。

当前补全入口是仓库内的 [$formaliscope-enrich](.agents/skills/formaliscope-enrich/SKILL.md)：一组模型无关的子 Agent prompt、低置信度复核 prompt 与机械收集脚本。模型与推理等级统一由 [config.json](.agents/skills/formaliscope-enrich/config.json) 指定，按相关组输出中文回译及现有 enrichment 字段。默认只有自报 `confidence < 0.8` 的条目进入主 Agent 复核；高分和已复核条目合并为候选快照，仍作为机器草稿。无需额外模型服务，置信度及复核记录保存在批次旁文件。

应用仓库只保留 `main` 和 `dev`。日常开发在 `dev`（跟踪 `origin/dev`）进行，合并到 `main` 后由 GitHub Actions 自动测试并发布 `v0.0.1`、`v0.0.2` 等版本。

Release 说明自动包含相对上一正式版本的 Changelog 和完整对比链接。中文版本摘要见 [CHANGELOG.md](CHANGELOG.md)。

需要 Python 3.10+，应用和 Workflow 只使用 Python 标准库。在仓库根目录运行本地演示：

```bash
python3 -m review_app build --statements --source /path/to/KIP126 --output /tmp/statement-candidate.json
python3 -m review_app install-snapshot --file /tmp/statement-candidate.json --data-dir .review
python3 -m review_app preflight --preview --data-dir .review
python3 -m review_app serve --preview --port 8876 --data-dir .review
```

打开 `http://127.0.0.1:8876/`。预览使用姓名和恢复凭证，不发送邮件，只允许本机监听。它是前端演示模式；正式服务默认也使用姓名登记和私人恢复码；管理员由服务器操作员单独创建，见 [身份说明](review_app/IDENTITY.md)。

正式证据应从固定提交的干净 Git 检出生成：

```bash
python3 -m review_app build --statements --require-clean \
  --source /path/to/KIP126 --output /tmp/statement-artifact/snapshot.json
```

当前 KIP126 示例快照包含 6,222 条声明、1,421 个文件。这是源码索引规模，实际审阅对象由目录和标签筛选确定；依赖图基于源码引用候选，尚未包含 Lean elaborator 的完整依赖。

Agent 补充数据通过已冻结快照校验、生成候选快照：

```bash
python3 -m review_app validate-enrichment \
  --snapshot /tmp/statement-artifact/snapshot.json --file /path/to/enrichment.json
python3 -m review_app enrich-snapshot \
  --snapshot /tmp/statement-artifact/snapshot.json --file /path/to/enrichment.json \
  --output /tmp/enriched-snapshot.json
```

构建和 enrichment 只生成新的候选文件，不覆盖旧制品或人工数据库；更新运行证据统一通过 `install-snapshot`。再次构建时选择新的输出路径。正式安装、身份配置、只读预检、备份和回滚见 [部署说明](review_app/DEPLOYMENT.md)，存储职责与一致性见 [存储设计](review_app/STORAGE.md)，制品发布与 VPS 拉取见 [GitHub Actions 部署](review_app/GITHUB_ACTIONS_DEPLOYMENT.md)，模块与行为见 [应用说明](review_app/README.md) 和 [Statement 使用说明](review_app/STATEMENT_REVIEW.md)。

完整验证：

```bash
python3 -m unittest discover -s review_app -t . -p 'test_*.py'
python3 -m unittest discover -s statement_workflow -t . -p 'test_*.py'
for script in review_app/static/*.js; do node --check "$script"; done
for test in review_app/test_*.cjs; do node "$test"; done
```

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。随附 MathJax 的原有许可见 [MATHJAX-LICENSE.txt](review_app/static/MATHJAX-LICENSE.txt)。
