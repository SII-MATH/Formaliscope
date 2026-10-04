# Statement 审阅前端预览

KIP-12 的前端功能预览固定于 KIP126 `develop` 提交 `38554617a4465bbce021b88977af660b90d83697`，覆盖 KIP126 和 KIPBase 的 6,222 条源码声明。原有 Blueprint 对应关系审核入口和四态历史仍保留；Statement 快照使用新的三态界面和独立条目 ID。

## 生成与运行

需要 Python 3.10+，应用只使用标准库：

```sh
python3 -m review_app build --statements --source /path/to/KIP126 --output /tmp/statement-candidate.json
python3 -m review_app install-snapshot --file /tmp/statement-candidate.json --data-dir .statement-review
python3 -m review_app serve --preview --port 8876 --data-dir .statement-review
```

精确源码归档可额外指定 `--source-commit <完整提交 SHA>`；操作者负责确保归档与提交一致。正式构建应从干净 Git 检出使用 `--require-clean`。

`--annotations review_app/preview_annotations.json` 接入三份回译草稿。草稿绑定声明的源码 SHA-256，源码变化后拒绝沿用。其余自然语言来自 Blueprint 原文或明确标注的阅读摘要。

## 功能

字段补全通过 [仓库 Skill](../.agents/skills/formaliscope-enrich/SKILL.md) 按用户指定范围分批执行，模型由 [配置文件](../.agents/skills/formaliscope-enrich/config.json) 选择，仅低置信度结果交主 Agent 复核。[统一补充 schema](../statement_workflow/schema/statement-enrichment.v1.schema.json) 定义中文副标题、阅读摘要、回译草稿、角色/主题、优先度、证据与生成记录；缺失值允许保留为空，人工审阅状态继续来自个人数据库。已接入旁文件校验、候选快照生成及前端消费；字段细节见 [补充字段](../statement_workflow/README.md#补充字段)。

- 完整条目索引、检索、分页、审阅优先度/名称排序；主定理及直接引用可筛选，KIPBase 可通过目录或基础设施角色筛选。
- 源码目录树逐层展开并显示递归声明数；选择整个目录或单个 Lean 文件，在所选范围内继续搜索和筛选。当前目录以 URL 参数保存，切换视图和重新打开链接保留范围；跨目录引用跳转显示范围外提示。
- 目录/文件进度按完整选择范围计算，搜索和状态筛选不改变分母；全库显示“索引标注情况”，说明 6,222 条不等于全部必审。可导出范围 JSON（固定源码提交、快照指纹、目录和声明清单）；当前工作流尚未自动导入此清单。
- 自然语言在上、完整声明源码在下；公式、Lean 高亮及完整源文件上下文。
- Structure 多行字段展开；引用图点击跳转，视图切换保持定位，支持平移、缩放、居中与一层/两层浏览。大邻域限制绘制节点数，详情保留全部已定位引用。
- 通过、没看懂、不通过三态，选填意见；SQLite 持久化、重试幂等、内容变化后重审。
- 首次姓名设置；同名身份独立。预览身份可通过本浏览器保存的随机恢复凭证切换回来。
- 独立管理员汇总、筛选、检索、分页、导出；普通用户 API 只能读取本人记录。
- 桌面与 390 像素手机布局。
- Label demo：23 个标签，分数学角色、主题、优先度、本人状态和回译状态五组；同组并集、跨组交集，计数结合目录和检索，支持逐项移除、清空和链接恢复。列表显示角色/优先度，详情显示全部标签及来源说明；面板保留零计数标签供查看配色。
- demo 的角色/主题为目录和名称规则候选，优先度来自既有索引规则；前端不再提供独立的语义风险标签。人工状态来自本人版本匹配记录，回译草稿只按快照中的实际记录展示。
- 版本信息只在“选择目录”的折叠详情中提供。顶栏与侧栏不重复显示提交编号，条目数量由列表区域展示。
- 去重：当前版本无有效判断统一为“未审阅”，旧判断仍保存在历史中；左侧状态按钮和状态标签共用一份筛选条件。移除与计算角色近乎重合的“计算与证书”主题及混入阶段的“上下文待补”；移除重复的目录优先核对、基础设施快捷筛选和目录规则排序。删除前端语义风险标签，关注程度统一使用优先度；数学功能与主题仍分别筛选。旧标签链接映射到合并后的标签。
- 入口按功能归拢：左侧“审核范围”“搜索与筛选”，右侧“条目详情”“依赖关系”。标签只保留一个筛选入口，分“内容分类”“审阅与回译”两页；更多筛选/排序、全部条目标签及来源默认折叠。进度置于列表底部，身份操作收进审阅者菜单。手机默认收起查找面板，点“展开查找”再使用目录、检索和标签。

## 身份与部署

姓名预览必须显式传 `--preview`，只监听 loopback、不发送邮件、不用于公开部署。第一个预览身份有管理员权限。恢复凭证只用于该本机预览，清理浏览器存储后会失去恢复入口。

正式运行默认姓名登记与恢复码登录，不需要邮件或学生密码。同名者分别保存记录；管理员通过本地 `create-admin` 创建，提交姓名或页面参数不能授予权限。数据库 v6 添加身份和恢复摘要，既有判断不重写；旧邮箱模式和迁移详见 [IDENTITY.md](IDENTITY.md)。

## 数据边界

图是确定性源码引用候选图，包含声明与证明中的已定位名称引用，尚非 Lean elaborator 提取的精确依赖图。排序规则为最终目标、直接引用、开发期假设、计算/文献输入、解码协议、接口与基础设施。目录角色只是浏览辅助。

Structure 目前展开字段类型，尚未执行实例/记录的语义代入或 Lean 编译核验。源码提取是定位器；完整源文件可用于核对上下文。三份回译草稿尚待数学核验，其余阅读摘要不代表完成自动回译。

正式安装与升级按 [部署说明](DEPLOYMENT.md) 执行。回译生成和候选数据安装分别进行，生成完成不会自动改变运行站点。

## 审核范围与补全

6,222 是示例快照的完整源码索引，任务范围由用户选定的目录、文件或声明决定。页面的目录、标签、搜索与依赖图用于定位对象，浏览或跳转到某条声明不会自动启动 Agent。

Skill 按文件或相关主题拆分任务，逐条保存输出；必要的定义和实例作为上下文，不自动加入补全目标。源码候选图可能漏字段访问和实例，不能用“零个已定位引用”推断没有语义依赖。

当前链路为配置子 Agent → 中文回译与字段填写 → 收集脚本校验 → 按自报置信度复核 → 候选快照 → 显式安装。数据契约、来源校验和当前边界见 [补充数据说明](../statement_workflow/README.md)，实际操作与 prompt 统一见 Skill。

## 验证

```sh
python3 -m unittest discover -s review_app -t .
python3 -m unittest discover -s statement_workflow -t .
node review_app/test_labels.cjs
node review_app/test_statement_modules.cjs
```

回归测试覆盖版本指纹、重试、记录隔离、管理员权限、同名身份及恢复、KIPBase/完整源码、过期草稿与正式模式禁用预览入口。浏览器验证了双视图、节点跳转、字段、完整文件、公式、保存、身份切换恢复及汇总。

预览中“功能预览”身份的记录是明确标注的功能测试，不是数学审阅结论。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| statements.py / build.py | 定位源码、构建快照、内容指纹；不调用 Agent |
| enrichment.py | 校验 v1 旁文件及实际源码证据，生成独立候选快照；不改人工记录 |
| database.py / judgments.py | SQLite 迁移、版本匹配、幂等提交、个人导出和管理读模型 |
| name_auth.py / auth.py / preview.py | 正式姓名身份、兼容邮箱与显式本机预览 |
| server.py | HTTP 路由、会话鉴权、静态资源、只读 /healthz |
| preflight.py / storage.py | 只读部署检查、快照安装、备份 |
| statement-api.js / statement-identity.js | 请求与证据缓存、登录/恢复/注销 |
| statement-graph.js / directory-tree.js / review-labels.js | 依赖图、目录与标签规则 |
| lean-renderer.js / latex-renderer.js | Lean 高亮与 LaTeX 展示 |
| statement.js | 页面状态、筛选和详情的协调 |
| ../.agents/skills/formaliscope-enrich/ | 模型配置、分组补全、低置信度复核与结果收集 |
| ../statement_workflow/schema/ | enrichment v1 数据契约；旧实验执行器保留兼容 |

正式检查使用 `python3 -m review_app preflight --data-dir /var/lib/formaliscope`；
本地预览检查加 `--preview`。检查不发送邮件、不创建会话、密钥或数据库。
生产启动由 systemd 在服务前执行 preflight；实际姓名登录、代理和恢复演练仍须在目标环境完成。
