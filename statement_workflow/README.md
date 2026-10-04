# Statement 补充数据契约与导入

[字段与填写标准 v2](SCHEMA_V2.md) 是当前生成规范。入口是按当前 harness 安装的 [仓库 Skill](../skills/README.md)，Codex、Claude Code、Kimi Code 三份调度说明与两阶段 prompt 分别维护在 `skills/`；准备和收集脚本共用 `skills/scripts/`。新批次使用 v2；旧 v1 试跑仍可按原契约校验，不自动转换。

## 当前流程

冻结源码快照、范围及配置 → 配置指定的子 Agent 分组生成并保存纯 Lean 回译 → 同一子 Agent 根据独立预期材料补内部判断 → 脚本校验 → 仅低回译置信度由主 Agent 复核 → 汇总私密补充数据 → 生成公开候选快照／独立导入内部评估 → 显式安装 → 用户审阅。

没有预期材料时，判断为不知道；不能自行从 Lean 构造预期再判符合。第二阶段不能改变已保存的回译正文及其分值。主 Agent 复核也保留原始两个分值和完整机器预期判断，不把机器处理标为人的审阅。

模型路由 ID、推理等级、主题选项从[当前 harness 的配置](../skills/README.md#模型配置)读取，prompt 不固定模型。准备脚本冻结配置和源码，续做沿用副本。调度层必须按配置显式启动子 Agent，并确认实际执行模型；模型不可用或不符时停止并准备新批次，不能静默回退。

复核唯一条件是原始 `readback.confidence < threshold`，默认 0.8，等于阈值直接汇总。预期判断及其分值、角色、优先度、依赖数量、复杂度和抽样都不参与路由。正文 null 单列生成失败，不计成功，不以空正文覆盖现有回译。

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
| `readback.confidence` | 回译忠实于冻结 Lean 的自报分值，0–1。 |
| `classification` | 七类数学角色单选或 null；当前配置主题多选、去重，未知为 []。 |
| `priority` | p0/p1/p2/null，按实际审核目标和作用分级。 |
| `expectation_assessment` | 是／否／不知道及独立自报分值；否与不知道必须有非空具体理由。仅供内部评估。 |

Agent 不填写阅读摘要、未解释对象列表、证据摘录、行号、basis、provenance、时间或实际执行模型。来源校验仍保留：脚本自动记录运行 ID、配置模型、推理等级、生成时间、源码提交、冻结快照摘要、规则版本和预期材料摘要，对每条精确 Lean 源码计算 SHA-256。

收集产物使用 [statement-enrichment.v2.schema.json](schema/statement-enrichment.v2.schema.json)：`run` 保存冻结运行记录；`annotations` 为接受条目；`sources` 是 ID → 源码摘要；`originals` 是 ID → 原始 Worker 条目；`reviews` 是已复核 ID → 实际复核模型和时间。原始结果文件保留，未复核和失败条目另列报告。

## 准备、校验与汇总

在仓库根目录准备一个新的私密批次：

```sh
python3 skills/scripts/prepare.py \
  --config /path/to/selected-agent-config.json \
  --snapshot /path/to/snapshot.json --directory KIP126/Interface/Axiom \
  --output .statement-enrichment/new-batch
```

也可用 `--file`、`--declaration-id`，多次提供时取并集。必须通过 `--config` 指定当前 harness 的模型配置；可选用户指定的 `--threshold` 和 `--expectation-context`。冻结的预期材料只在第二阶段提供给 Worker。批次目录 0700、文件 0600，全部放在被 Git 忽略的 `.statement-enrichment/`；因为含内部评估，不放入公开目录。

按 Skill 完成分组两阶段输出，调度层确认模型后收集：

```sh
python3 skills/scripts/collect.py \
  --snapshot .statement-enrichment/new-batch/snapshot.json \
  --manifest .statement-enrichment/new-batch/manifest.json \
  --result .statement-enrichment/new-batch/group-1.json \
  --executed-model ACTUAL_MODEL \
  --readback-result .statement-enrichment/new-batch/group-1-readback.json \
  --output .statement-enrichment/new-batch/collected
```

`ACTUAL_MODEL` 是调度工具确认的实际模型，必须与本批要求相同。`--result`、`--readback-result` 和 `--review` 可重复。没有预期材料时可省略第一阶段参数；有预期时必须提供，脚本验证目标集合、原回译不变及预期材料摘要。

脚本对所有输入检查精确目标集合、冻结来源、选项、类型、有限分值和条件必填理由，全部通过后才写新的输出目录。它生成 `enrichment.json`、`review-queue.json`、`report.json`，区分直接汇总、已复核、待复核、生成失败。不能以提高原分值代替复核。

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

协议回归采用合成夹具，覆盖冻结范围与主题、实际模型确认、两阶段回译不变、阈值边界、独立双分值、失败回译、原结果保留、私密文件权限和旧批次兼容。真实数学质量仍按实际试跑验收。
