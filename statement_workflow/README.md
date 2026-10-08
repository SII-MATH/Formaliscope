# Statement 补充数据契约与导入

[字段与填写标准 v2](SCHEMA_V2.md) 是当前生成规范。入口是按当前 harness 安装的 [仓库 Skill](../skills/README.md)，Codex、Claude Code、Kimi Code 三份调度说明与两阶段 prompt 分别维护在 `skills/`；准备和收集脚本共用 `skills/scripts/`。新批次使用 v2；旧 v1 试跑仍可按原契约校验，不自动转换。

## 当前流程

准备任务并引用固定快照 → 配置指定的子 Agent 分组生成并保存纯 Lean 回译 → 同一子 Agent 根据独立预期材料补内部判断 → 脚本校验 → 仅低回译置信度由主 Agent 复核 → 汇总私密补充数据 → 生成公开候选快照／独立导入内部评估 → 显式安装 → 用户审阅。

没有预期材料时，判断为不知道；不能自行从 Lean 构造预期再判符合。第二阶段不能改变已保存的回译正文及其分值。主 Agent 复核也保留原始两个分值和完整机器预期判断，不把机器处理标为人的审阅。

默认预期来自固定快照中绑定该声明的 Blueprint 文案。准备脚本自动保存按精确声明 ID 索引的参考到 `expectation-context.txt`，同时保留所有关联节点、出处及共同关联的声明。`--expectation-context` 可补充用户提供的材料。参考只进入第二阶段；没有该声明的参考或明确补充预期时填不知道。同一节点覆盖多个 Lean 声明时，核对当前声明对应的部分，而不是要求每条声明独自覆盖节点全部内容。

模型路由 ID、推理等级、主题选项从[当前 harness 的配置](../skills/README.md#模型配置)读取，prompt 不固定模型。准备脚本记录配置和范围、引用共享只读快照，续做使用本批文件。调度层必须按配置显式启动子 Agent，并确认实际执行模型；模型不可用或不符时停止并准备新批次，不能静默回退。

复核唯一条件是原始 `readback.confidence < threshold`，阈值取合并后的任务配置，等于阈值直接汇总。预期判断及其分值、角色、优先度、依赖数量、复杂度和抽样都不参与路由。正文 null 单列生成失败，不计成功，不以空正文覆盖现有回译。

## 审阅范围

6,222 是示例源码快照的索引数量，不是自动任务或人工必审数量。只处理用户选定的目录、文件或声明。按文件或相关数学对象分组，目标互不重叠且完整覆盖本批范围；按需读取的定义和实例不自动成为补全目标。

每条声明以自身数学内容为准，不能从相邻声明补出它没有的假设或结论。源码引用候选图可能遗漏结构字段或实例，也可能混入证明引用；图上没有引用不代表没有数学依赖。

## v2 Agent 字段与运行记录

Agent 输出 [statement-agent-batch.v2.schema.json](schema/statement-agent-batch.v2.schema.json)，每组格式为 `{"schema":"formaliscope-agent-batch.v2","annotations":[]}`。字段标准详见 [SCHEMA_V2.md](SCHEMA_V2.md)。

| 字段 | 含义 |
| --- | --- |
| `declaration_id` | 调度分配的精确 ID，原样返回。 |
| `title_zh` | 简短、忠实的中文阅读标题，无法可靠命名时为 null。 |
| `readback.text_zh` | 完整中文及 LaTeX 回译；未生成时为 null。 |
| `readback.confidence` | 回译忠实于固定 Lean 的自报分值，0–1。 |
| `classification` | 七类数学角色单选或 null；当前配置主题多选、去重，未知为 []。 |
| `priority` | p0/p1/p2/null，按实际审核目标和作用分级。 |
| `expectation_assessment` | 是／否／不知道及独立自报分值；否与不知道必须有非空具体理由。仅供内部评估。 |

Agent 不填写阅读摘要、未解释对象列表、证据摘录、行号、basis、provenance、时间或实际执行模型。来源校验仍保留：脚本自动记录运行 ID、配置模型、推理等级、生成时间、源码提交、固定快照摘要、规则版本和预期材料摘要，对每条精确 Lean 源码计算 SHA-256。

收集产物使用 [statement-enrichment.v2.schema.json](schema/statement-enrichment.v2.schema.json)：`run` 保存固定运行记录；`annotations` 为接受条目；`sources` 是 ID → 源码摘要；`originals` 是 ID → 原始 Worker 条目；`reviews` 是已复核 ID → 实际复核模型和时间。原始结果文件保留，未复核和失败条目另列报告。

## 准备、校验与汇总

Agent 开始任务前，起草 `.statement-enrichment/task-configs/YYYYMMDD-HHMMSS-任务名.json`，引用当前工具的配置，填写快照、范围和用户要求的覆盖项。字段和合并规则见 [任务配置说明](../skills/CONFIG.md)，默认值集中在 [共享配置](../skills/default-config.json)。

```sh
python3 skills/scripts/prepare.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

准备结果记录实际批次路径、完整合并设置和声明清单。预期材料在第一阶段落盘后提供给 Worker。任务配置、批次和结果均保存在被 Git 忽略的私密目录。

脚本将完整快照按内容 SHA-256 共用保存在批次父目录的 `.snapshots/` 中，已有内容会校验后复用；批次的 `snapshot.json` 是指向它的相对链接，不再每批复制全库。共享快照为只读 0400，目录为 0700；批次保存 `agent-config.json`、`manifest.json` 和可选 `expectation-context.txt`（含 SHA-256），普通文件为 0600。运行 ID、时间、源码提交、快照摘要、模型路由、推理等级、主题、规则版本及阈值由脚本记录，Agent 不填写。任务和 Agent 结果也保持私密权限。续做使用本批入口，不重新读取最初的输入路径；换模型、材料或范围时准备新任务。移动或备份任务时同时保留同级 `.snapshots/`，不要单独移动链接或删除仍被任务引用的快照。旧批次中的完整 `snapshot.json` 仍可直接使用。

按 Skill 完成分组两阶段输出后，将文件回执和调度记录核实的模型写入任务配置的 `collection`，然后收集：

```sh
python3 skills/scripts/collect.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

有预期时配置各组 `readback_results`；无预期时 `results` 指向第一阶段文件。复核后填入 `collection.reviews` 并设置新的输出目录重新收集。收集依据准备时保存的快照、manifest 和模型配置，原始输入或默认配置的后续变化不改变本批依据。

脚本对所有输入检查精确目标集合、固定来源、选项、类型、有限分值和条件必填理由，全部通过后才写新的输出目录。它生成 `enrichment.json`、`review-queue.json`、`report.json`，区分直接汇总、已复核、待复核、生成失败。不能以提高原分值代替复核。

## 公开候选与私密数据库分别导入

```sh
python3 -m review_app validate-enrichment \
  --snapshot /path/to/frozen-snapshot.json --file /path/to/collected/enrichment.json
python3 -m review_app enrich-snapshot \
  --snapshot /path/to/frozen-snapshot.json --file /path/to/collected/enrichment.json \
  --output /path/to/new-candidate-snapshot.json
python3 -m review_app import-agent-assessments \
  --snapshot /path/to/frozen-snapshot.json --file /path/to/collected/enrichment.json \
  --data-dir /path/to/private-data-dir
```

前两个命令不写数据库，候选生成拒绝已有输出。候选快照只保留公开机器草稿字段：中文标题、回译、分类、优先度和项目主题选项。内部预期结果、理由、两个置信度及原始运行资料不进入快照、审阅卡片 API、初始卡片或审阅者导出。

第三个命令显式把模型评估记录写入现有 SQLite 的独立表，保留批次、来源、原始回译、预期材料和规则版本；不写人工 judgments，不改变人的状态、进度或有效判断。重复导入同一批次应保持幂等，有冲突则拒绝，不能静默覆盖历史。数据库表结构仍通过有编号的迁移更新。

内部入库、候选生成和安装分别执行。安装仍遵守 [部署流程](../review_app/DEPLOYMENT.md)，Skill 不自动安装或部署。

回译正文变化才改变相应人工审阅依据，旧判断保留在历史；中文标题或标签变化不清空人工结果。机器回译是 draft，主 Agent 复核不等于人工已审阅。

## 旧批次与实验接口

收集器保留 `formaliscope-enrichment-batch.v1` 和旧结果格式的原校验路径；v1 仍按 [旧契约](schema/statement-enrichment.v1.schema.json) 要求来源、摘要及证据。仅历史批次使用，不让新 Worker 填回已删除字段。没有新字段的旧产物不能冒充新的内部评估。

`engine.py`、`__main__.py`、`workflow.json`、`prompts/` 是早期 `python3 -m statement_workflow` 实验接口，协议见 [旧执行器契约](AGENT.md)。当前 Skill 不调用它；逐条 readback/audit 及旧分级不作为 v2 要求。

## 验证

```sh
python3 -m unittest discover -s statement_workflow -t . -p 'test_*.py'
python3 -m unittest review_app.test_enrichment
```

协议回归采用合成夹具，覆盖固定范围与主题、实际模型确认、两阶段回译不变、阈值边界、独立双分值、失败回译、原结果保留、私密文件权限和旧批次兼容。真实数学质量仍按实际试跑验收。
