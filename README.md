# Formaliscope · Statement 审阅台

KIP-12 前端以 Lean 声明为条目，提供目录选择、名称与源码搜索、标签筛选、声明详情、候选依赖图及独立人工审阅。主标题使用 Lean 名称，中文说明由 Agent 补全；回译、角色、主题和优先度遵循 [字段标准 v2](statement_workflow/SCHEMA_V2.md)。人工判断单独保存，Agent 草稿不会自动成为“通过”。

v2 已接入 Skill、校验器与数据库迁移 9：Agent 填写标题、完整回译、角色、主题、优先度及内部预期判断，不填写摘要、未解释对象、证据或来源记录。两个自报置信度分开保存；内部判断不进入公共快照、审阅接口或人工进度。

仓库包含网页服务 `review_app/`、声明补充数据的 [契约与导入说明](statement_workflow/README.md)，以及下述仓库 Skill。补全按选定目录或声明分批执行，早期 Blueprint 对照模式继续保留。

多仓库接入通过独立配置扫描根目录、主目标和主题，再把各仓库/版本的冻结快照组合成候选集合。页面可切换仓库，每个仓库只显示当前版本；历史快照与审阅记录保留，判断、草稿、导出及管理员汇总按数据集隔离。数据库迁移 10 保留旧 KIP126 记录及快照回退能力，操作步骤见 [多仓库与版本说明](review_app/REPOSITORIES.md)。

同一仓库更新时默认沿用上下文未变条目的机器回译、分类、标签和优先度，安装时继承匹配的人工判断，保留审阅人、时间和原始证据来源。构建从运行数据目录读取上一版本；在别处生成的普通候选也会在安装时生成独立沿用候选。`--no-reuse` 关闭沿用，`--reuse-from <旧快照>` 可覆盖自动选取的前身。迁移 11 记录判断的跨版本来源。模块上下文、递归导入、依赖锁或工具链变化时保守地重新处理；步骤与限制见[跨版本沿用](review_app/REPOSITORIES.md#跨版本沿用)。

全局管理员的后台提供完整用户目录、同名账号区分、参与审阅情况、逐用户已提交结果和密码重置；仓库版本管理员继续只查看其获授权的汇总，详见[身份与后台说明](review_app/IDENTITY.md#用户后台)。

回译 Skill 源文件按 Codex、Claude Code、Kimi Code 分别维护在 `skills/`，先按[安装说明](skills/README.md)安装当前 harness 的版本。Agent 每次先起草独立[任务配置](skills/CONFIG.md)，覆盖[共享默认配置](skills/default-config.json)，准备与收集命令只传 `--config`。先按固定 Lean 分组回译，再用独立预期材料补内部判断；没有材料时填“不知道”。原始 `readback.confidence` 低于配置阈值的成功回译进入主 Agent 复核，无法回译单列失败。脚本自动记录来源、运行与原始分值。

应用仓库只保留 `main` 和 `dev`。日常开发在 `dev`（跟踪 `origin/dev`）进行，合并到 `main` 后由 GitHub Actions 自动测试并发布 `v0.0.1`、`v0.0.2` 等版本。

Release 说明自动包含相对上一正式版本的 Changelog 和完整对比链接。中文版本摘要见 [CHANGELOG.md](CHANGELOG.md)。

本地数据统一保存在被 Git 忽略的 `.formaliscope/`，目录职责、清理规则和旧目录迁移见 [本地存储说明](review_app/LOCAL_STORAGE.md)。

需要 Python 3.10+，应用和 Workflow 只使用 Python 标准库。在仓库根目录运行本地演示：

```bash
python3 -m review_app build --statements --source /path/to/KIP126 --output .formaliscope/snapshots/candidates/local-preview.json
python3 -m review_app install-snapshot --file .formaliscope/snapshots/candidates/local-preview.json --data-dir .formaliscope/runtime
python3 -m review_app preflight --preview --data-dir .formaliscope/runtime
python3 -m review_app serve --preview --port 8876 --data-dir .formaliscope/runtime
```

打开 `http://127.0.0.1:8876/`。预览使用姓名和恢复凭证，不发送邮件，只允许本机监听。正式服务默认使用账号密码登录，保留私人恢复码作备用入口；已有账号迁移后初始密码为 `12345678`，首次登录须修改。管理员由服务器操作员单独创建，见 [身份说明](review_app/IDENTITY.md)。

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
python3 -m review_app import-agent-assessments \
  --snapshot /tmp/statement-artifact/snapshot.json --file /path/to/enrichment.json \
  --data-dir /path/to/review-data
```

前两个命令不写数据库，候选快照仅含公开回译与标签。最后一个命令是 v2 内部评估的显式入库：使用该批次的冻结基础快照，支持幂等重试与版本历史，不安装快照或写入人工判断。应先升级目标应用；当前数据库使用迁移 11，旧版本不能直接打开。旧 v1 补充文件仍可校验和生成候选，但不能作为 v2 内部评估导入。

构建和 enrichment 只生成新的候选文件，不覆盖旧制品或人工数据库；更新运行证据统一通过 `install-snapshot`。再次构建时选择新的输出路径。正式安装、身份配置、只读预检、备份和回滚见 [部署说明](review_app/DEPLOYMENT.md)，存储职责与一致性见 [存储设计](review_app/STORAGE.md)，制品发布与 VPS 拉取见 [GitHub Actions 部署](review_app/GITHUB_ACTIONS_DEPLOYMENT.md)，模块与行为见 [应用说明](review_app/README.md) 和 [Statement 使用说明](review_app/STATEMENT_REVIEW.md)。

HK-VPS 的完整备份可加密拉取到本机项目下的 `.formaliscope/backups/hk-vps/`。目录、密钥和本机配置均被 Git 忽略；同步、校验与恢复步骤见 [本机异机备份](review_app/OFFSITE_BACKUP.md)。只有该工具另需 `deploy/requirements-offsite-backup.txt` 中的加密依赖，网页服务和回译工具仍使用 Python 标准库。

完整验证：

```bash
python3 -m pip install -r deploy/requirements-offsite-backup.txt
python3 -m unittest discover -s review_app -t . -p 'test_*.py'
python3 -m unittest discover -s statement_workflow -t . -p 'test_*.py'
for script in review_app/static/*.js; do node --check "$script"; done
for test in review_app/test_*.cjs; do node "$test"; done
```

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。随附 MathJax 的原有许可见 [MATHJAX-LICENSE.txt](review_app/static/MATHJAX-LICENSE.txt)。
