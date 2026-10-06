# Statement 审阅应用

应用读取独立的 KIP126 源码检出并生成只读快照。Statement 模式索引 `KIP126/` 与 `KIPBase/` 中的完整声明，按目录、标签和搜索组织内容。Lean 名称作为主标题；中文副标题、独立回译和分类按 enrichment v2 补充。人工审阅使用“通过 / 没看懂 / 不通过”，与 Agent 内部预期判断分开保存。

也可通过仓库配置接入其他 Lean 工程并组合多个源码版本；详见 [多仓库与版本](REPOSITORIES.md)。旧 KIP126 构建与快照继续兼容。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `build.py` / `statements.py` | Blueprint 与 Statement 提取、源码版本、内容指纹和快照校验 |
| `repositories.py` / `dataset_storage.py` | 仓库配置、独立数据集集合、旧记录复制与回退保留 |
| `enrichment.py` / `enrichment_v2.py` | 字段校验、自动源码绑定、内部字段隔离、生成公开候选快照 |
| `agent_assessments.py` | 显式事务导入私有模型判断，保存原始分值、运行版本与历史 |
| `database.py` / `judgments.py` | 数据库版本迁移、人工判断、当前有效记录和管理员汇总 |
| `session_store.py` / `name_auth.py` / `auth.py` / `preview.py` | 共享会话、正式姓名身份、兼容邮箱验证码与本机预览 |
| `server.py` | HTTP 路由、资源、会话、授权和 API 适配 |
| `storage.py` / `data_lock.py` / `preflight.py` | 安装与备份共用锁、在线备份和保留策略、无写入部署预检 |
| `snapshot_artifacts.py` | 排他写入候选证据，不覆盖已有制品或运行数据 |
| `static/statement.js` | 详情、列表、筛选与页面交互 |
| `static/statement-api.js` / `statement-identity.js` / `statement-graph.js` | API 请求、身份交互和候选依赖图 |
| `static/statement-save.js` / `statement-navigation.js` | 串行自动保存、失败重试、返回与阅读状态恢复 |
| `symbols.py` / `static/statement-symbols.js` | 快照内名称与局部绑定定位、定义追溯交互 |
| `static/directory-tree.js` / `review-labels.js` | 目录树与统一标签、状态筛选规则 |
| `static/latex-renderer.js` / `lean-renderer.js` | 本地数学公式渲染与安全转义后的 Lean 高亮 |
| `../skills/` | 配置模型、分组补全、低置信度复核与批次结果收集 |
| `../statement_workflow/schema/` | v2 Agent 和收集产物契约，保留 v1 历史校验；其他执行器属于早期实验入口 |

## 构建与本地演示

```bash
python3 -m review_app build --statements --source /path/to/KIP126 --output /tmp/statement-candidate.json
python3 -m review_app install-snapshot --file /tmp/statement-candidate.json --data-dir .review
python3 -m review_app preflight --preview --data-dir .review
python3 -m review_app serve --preview --port 8876 --data-dir .review
```

预览只监听 loopback，不发送验证码。姓名和恢复凭证隔离演示记录。只有 Statement 快照能使用预览身份；这个模式不能公开到生产。`--source-commit` 仅用于本机归档预览，它不能证明归档与提交一致，生产预检拒绝新生成的 `archive-unverified` 来源。

生产从干净 Git 检出生成 `build --statements --require-clean --output <新文件>`。早期 Blueprint 模式仍可用 `build --source /path/to/KIP126 --output <新文件>` 构建；它读取 `blueprint/src/content.tex` 的章节和 `\lean{...}`。已有 Blueprint 部署可显式运行 `preflight --legacy-blueprint`，不会把旧节点 ID 与 Statement ID 混用。构建只产候选包；激活统一走 `install-snapshot`。兼容的 `build --data-dir` 仅用于空候选目录，不能覆盖已有快照或运行数据库。

## Agent 补充数据接入

[仓库 Skill](../skills/README.md) 是当前补全入口，按 [模型配置](../skills/README.md#模型配置) 调用子 Agent，并依据自报置信度安排主 Agent 复核。`collect.py` 生成下述命令消费的 `enrichment.json`；操作细节见 Skill，字段约定见 [补充数据说明](../statement_workflow/README.md)。

[字段标准 v2](../statement_workflow/SCHEMA_V2.md) 是当前填写依据。Agent 只填标题、完整回译、单选角色、多选项目主题、优先度、内部预期判断及两个置信度。否／不知道必填理由；没有独立预期材料时填不知道。模型、时间和源码版本由调度层记录，不填摘要、unresolved 或证据。新主题由批次配置冻结，并进入公开快照和筛选配置。

```bash
python3 -m review_app validate-enrichment --snapshot .review/snapshot.json --file /path/to/enrichment.json
python3 -m review_app enrich-snapshot --snapshot .review/snapshot.json \
  --file /path/to/enrichment.json --output /tmp/enriched-snapshot.json
python3 -m review_app import-agent-assessments --snapshot /path/to/frozen-base/snapshot.json \
  --file /path/to/enrichment.json --data-dir /path/to/review-data
python3 -m review_app install-snapshot --file /tmp/enriched-snapshot.json --data-dir .review
```

校验检查声明 ID、源码提交、基础快照摘要、自动计算的源码 SHA-256、分类配置、原始两个分值及复核来源。候选快照只保留公开回译和标签，不含内部判断、理由或置信度。内部入库使用批次的冻结基础快照，不要求它已安装；数据库迁移 9 与幂等事务保留机器评估历史，不写人工 verdict。目标应用须先升级，生成候选不迁移或修改数据库。v1 历史结果仍按原契约校验，不自动转换为 v2。

补充快照安装后重启服务才能读取新资源；旧人工判断保留为历史，回译依据变化的条目重新进入未审阅。只改变内部判断或标签不会影响人工审阅依据。

上例默认使用 Git checkout 构建的快照。本机若从 `--source-commit` 归档生成演示，安装补充产物须显式加 `install-snapshot --allow-dirty-source`；仅用于开发预览，生产仍需从干净 Git 检出重新构建。

## 正式身份与授权

正式 `serve` 默认使用姓名登记：浏览器保持身份 30 天，系统生成的私人恢复码用于换设备或退出后找回记录。学生不需要邮箱、密码、邀请码或预先名单。同名者各有随机身份 ID，改名不改变记录归属，API 从会话确定 owner。

管理员由操作员执行 `create-admin --name ... --output ...` 单独创建。恢复码只保存摘要，初次创建与轮换时才显示明文；恢复码文件不得放入发布包。部署与旧邮箱记录迁移见 [IDENTITY.md](IDENTITY.md)。旧邮件安装须显式选择 `REVIEW_AUTH_MODE=email` 才继续使用原有邮件配置和邮箱白名单。

`snapshot.json` 是共同证据；`judgments.sqlite3` 保存人工记录、姓名身份、角色及恢复摘要，并以独立表保存内部模型评估；`auth-pepper` 在数据库外保存认证摘要密钥。运行数据默认位于 `.review/`，生产位于 `/var/lib/formaliscope`。备份清除会话但保留身份，恢复后用原恢复码重新登录。

## 请求、渲染与部署检查

打开网页不会运行 Lake 或重新扫描源码。清单和详情分开获取；详情有内容指纹 ETag，人工进度与历史不缓存。数据库启用 WAL、短事务和 busy timeout，提交携带 UUID 以避免网络重试重复写入。

选择审阅结论立即保存草稿；备注停止输入 650 毫秒后更新同一份草稿。草稿可在刷新、返回条目或恢复身份后继续编辑，不计入历史与已审进度。点击“完成审阅”（Ctrl/⌘+Enter），或切换条目、审核范围、身份及浏览器前进/后退时，先等待草稿保存，再将已选结论的完整意见记为一个正式版本；未选择结论时只保留草稿。失败时保留输入并提供重试，多标签页冲突拒绝覆盖较新草稿。正式完成后更新当前记录和列表进度。历史仅展开时读取，每页 25 条；可分别导出最新有效结果与完整历史，均不包含未完成草稿。工具栏“返回”与浏览器前进/后退恢复此前的筛选、视图、完整文件展开状态和阅读位置。

Lean 代码中的名称可点击追溯定义。定位仅使用当前快照：唯一匹配跳转至声明，局部参数和绑定定位到源码，多个候选让用户选择；外部依赖或无法确定的名称给出说明。这是保守的源码索引，不执行 Lean elaboration，复杂模式绑定仍可能无法定位。

前端数学使用本地 MathJax 包（Apache 2.0，见 `static/MATHJAX-LICENSE.txt`）；Lean 高亮在转义 HTML 字符后执行。资源不依赖 CDN。静态资源由服务启动时读取，因此更新代码或快照后需要重启。

公开服务设置 HTTPS `REVIEW_PUBLIC_ORIGIN`，路径前缀设置 `REVIEW_COOKIE_PATH`，由代理剥去前缀再转发 loopback 服务。部署前运行 `python3 -m review_app preflight --data-dir /var/lib/formaliscope`；返回 ready=false 时命令失败，且不初始化数据库、不创建密钥、不发邮件。启动后 `GET /healthz` 无需登录，只报告就绪状态及 schema 版本，不返回条目、用户或路径。

完整测试命令见 [根 README](../README.md)，存储职责见 [STORAGE.md](STORAGE.md)，生产 readiness、备份和回滚见 [DEPLOYMENT.md](DEPLOYMENT.md)。6,222 条声明的受控浏览器测量、优化前后指标及复测方法见 [Statement 加载性能报告](PERFORMANCE_STATEMENT.md)；早期 Blueprint 基线保留在 [历史性能报告](PERFORMANCE.md)。
