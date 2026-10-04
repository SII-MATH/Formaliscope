# Statement 审阅应用

应用读取独立的 KIP126 源码检出并生成只读快照。Statement 模式索引 `KIP126/` 与 `KIPBase/` 中的完整声明，按目录、标签和搜索组织内容。Lean 名称作为主标题；中文副标题、阅读摘要、独立回译和候选分类按 enrichment v1 交换契约补充。人工审阅使用“通过 / 没看懂 / 不通过”，与 Agent 输出分开保存。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `build.py` / `statements.py` | Blueprint 与 Statement 提取、源码版本、内容指纹和快照校验 |
| `enrichment.py` | 手动 Agent 旁文件校验、源码依据绑定、生成候选补充快照 |
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
| `../.agents/skills/formaliscope-enrich/` | 配置模型、分组补全、低置信度复核与批次结果收集 |
| `../statement_workflow/schema/` | enrichment v1 数据契约；同目录其他执行器文件属于兼容实验入口 |

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

[仓库 Skill](../.agents/skills/formaliscope-enrich/SKILL.md) 是当前补全入口，按 [模型配置](../.agents/skills/formaliscope-enrich/config.json) 调用子 Agent，并依据自报置信度安排主 Agent 复核。`collect.py` 生成下述命令消费的 `enrichment.json`；操作细节见 Skill，字段约定见 [补充数据说明](../statement_workflow/README.md)。

[enrichment v1 schema](../statement_workflow/schema/statement-enrichment.v1.schema.json) 是冻结的数据契约。先固定基础快照，再按选定目录逐批填写。缺失内容使用 null、none 或空数组；读不到的对象保留 unresolved，Agent 只能生成回译草稿。

```bash
python3 -m review_app validate-enrichment --snapshot .review/snapshot.json --file /path/to/enrichment.json
python3 -m review_app enrich-snapshot --snapshot .review/snapshot.json \
  --file /path/to/enrichment.json --output /tmp/enriched-snapshot.json
python3 -m review_app install-snapshot --file /tmp/enriched-snapshot.json --data-dir .review
```

旁文件必须关联当前声明 ID、源码提交、基础快照摘要、源码 SHA-256 及实际引用的证据。补充快照安装后重启服务才能读取新资源；旧人工判断保留为历史，判断依据变化的条目重新进入未审阅。导入不会写人工 verdict，也不会替换源码。

上例默认使用 Git checkout 构建的快照。本机若从 `--source-commit` 归档生成演示，安装补充产物须显式加 `install-snapshot --allow-dirty-source`；仅用于开发预览，生产仍需从干净 Git 检出重新构建。

## 正式身份与授权

正式 `serve` 默认使用姓名登记：浏览器保持身份 30 天，系统生成的私人恢复码用于换设备或退出后找回记录。学生不需要邮箱、密码、邀请码或预先名单。同名者各有随机身份 ID，改名不改变记录归属，API 从会话确定 owner。

管理员由操作员执行 `create-admin --name ... --output ...` 单独创建。恢复码只保存摘要，初次创建与轮换时才显示明文；恢复码文件不得放入发布包。部署与旧邮箱记录迁移见 [IDENTITY.md](IDENTITY.md)。旧邮件安装须显式选择 `REVIEW_AUTH_MODE=email` 才继续使用原有邮件配置和邮箱白名单。

`snapshot.json` 是共同证据；`judgments.sqlite3` 保存人工记录、姓名身份、角色及恢复摘要；`auth-pepper` 在数据库外保存认证摘要密钥。运行数据默认位于 `.review/`，生产位于 `/var/lib/formaliscope`。备份清除会话但保留身份，恢复后用原恢复码重新登录。

## 请求、渲染与部署检查

打开网页不会运行 Lake 或重新扫描源码。清单和详情分开获取；详情有内容指纹 ETag，人工进度与历史不缓存。数据库启用 WAL、短事务和 busy timeout，提交携带 UUID 以避免网络重试重复写入。

选择审阅结论立即自动保存；备注停止输入 650 毫秒后保存。切换条目、审核范围或身份前等待当前修改保存，失败时保留输入并提供重试。成功提交直接更新当前记录和列表进度。工具栏“返回”与浏览器前进/后退恢复此前的筛选、视图、完整文件展开状态和阅读位置。

Lean 代码中的名称可点击追溯定义。定位仅使用当前快照：唯一匹配跳转至声明，局部参数和绑定定位到源码，多个候选让用户选择；外部依赖或无法确定的名称给出说明。这是保守的源码索引，不执行 Lean elaboration，复杂模式绑定仍可能无法定位。

前端数学使用本地 MathJax 包（Apache 2.0，见 `static/MATHJAX-LICENSE.txt`）；Lean 高亮在转义 HTML 字符后执行。资源不依赖 CDN。静态资源由服务启动时读取，因此更新代码或快照后需要重启。

公开服务设置 HTTPS `REVIEW_PUBLIC_ORIGIN`，路径前缀设置 `REVIEW_COOKIE_PATH`，由代理剥去前缀再转发 loopback 服务。部署前运行 `python3 -m review_app preflight --data-dir /var/lib/formaliscope`；返回 ready=false 时命令失败，且不初始化数据库、不创建密钥、不发邮件。启动后 `GET /healthz` 无需登录，只报告就绪状态及 schema 版本，不返回条目、用户或路径。

完整测试命令见 [根 README](../README.md)，存储职责见 [STORAGE.md](STORAGE.md)，生产 readiness、备份和回滚见 [DEPLOYMENT.md](DEPLOYMENT.md)。现有 [性能报告](PERFORMANCE.md) 基于早期 Blueprint 规模，新 Statement 规模需要部署演练时重新测量。
