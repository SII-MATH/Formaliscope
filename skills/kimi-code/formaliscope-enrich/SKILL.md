---
name: formaliscope-enrich
description: "在 Kimi Code 中用原生 AgentSwarm 按配置指定的模型并行生成中文 Lean 回译、标题、标签、优先度及内部预期判断；仅按回译自报置信度复核，输出候选快照和可独立入库的内部评估。"
type: prompt
whenToUse: "用户要求为 Formaliscope 的明确 Statement 范围补全中文回译、审阅字段或内部预期评估时。"
---

# Formaliscope 中文回译与字段补全

在当前 Kimi Code 会话中，主 Agent 用原生 `AgentSwarm` 调度专用 worker，按仓库 `statement_workflow/SCHEMA_V2.md` 填字段，脚本负责校验和合并。

## 准备回译任务

开始任务前，按用户要求起草独立配置文件，命名为 `.statement-enrichment/task-configs/YYYYMMDD-HHMMSS-任务名.json`，时间使用本地时间，任务名用简短英文。

用 `defaults` 引用本工具的配置，填写本次输入快照和范围；额外要求写为覆盖项，其余继承默认配置。字段和合并规则见 [任务配置说明](../../../skills/CONFIG.md)。例如 `20261008-150000-tower.json`：

```json
{
  "defaults": "skills/kimi-code/formaliscope-enrich/config.json",
  "snapshot": "/absolute/path/snapshot.json",
  "selection": {"directories": ["KIP126/Def/ClassicalAdams/Tower"]}
}
```

按实际任务替换路径和范围，然后准备：

```sh
python3 skills/scripts/prepare.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

根据准备脚本的回执读取本批 `manifest.json` 和 `agent-config.json`，按所选声明和合并后的配置执行。

第二阶段使用脚本准备的 `expectation-context.txt`，以 Blueprint 文案和用户补充材料为参考；缺少对应参考时填 `undetermined`。

从本批 `agent-config.json` 读取合并后的设置，确认当前 provider、模型和调度机制可应用配置的推理设置。配置差异和待确认项作为执行限制报告，调整配置时开新任务。

## 原生 Swarm 与两阶段填写

按文件或数学对象分组，每组目标互不重叠且恰好覆盖 `manifest.declaration_ids`，共享定义作为阅读上下文。使用独立上下文的 `formaliscope-enrich-worker`，每组分配稳定 `group_id`、精确 ID 和唯一第一阶段、第二阶段输出路径。第一阶段上下文由阶段说明、字段标准、Lean 输入和主题组成，核对自动注入内容符合阶段范围。

模型由 Kimi 调度层绑定。有模型池和 `model` 参数时，选择与本批路由对应的池 alias；`primary` 使用调用者已核实的路由。无池时沿用调用者路由；配置强制 secondary model 时按该绑定执行。将启动请求、实际绑定与执行记录逐组核对，确认本批模型和设置。

### 第一阶段

两组及以上使用 `AgentSwarm`，单组使用 `Agent`。模板引用 `${KIMI_SKILL_DIR}/references/worker-prompt.md`，提供仓库路径、本批快照、主题配置和本组任务；items 包含 `group_id`、精确 ID 和唯一结果路径。按当前工具的参数约定调用，等待聚合报告。

按工具返回的真实身份记录每组 agent ID 与结果文件的对应关系，核对声明集合及文件就绪情况。

### 第二阶段

有预期材料且第一阶段就绪时，通过 `AgentSwarm.resume_agent_ids` 续做对应 worker；单组可用 `Agent` resume。任务引用 `${KIMI_SKILL_DIR}/references/expectation-prompt.md`，提供已保存的第一阶段文件、本批参考和新的结果路径。补充内部判断并原样保留回译及分值。

没有预期材料时，第一阶段文件即为最终结果，预期判断保持 `undetermined`。

每组输出 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，逐条填写 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`。可靠回译尚待完成时，正文填 null。来源、运行及模型记录由脚本和调度层维护。

## 校验与复核

执行完成后，根据文件回执和调度记录，将各组结果、第一阶段文件及已确认的实际模型写入任务配置的 `collection`；用同一任务配置收集：

```sh
python3 skills/scripts/collect.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

收集路径和复核文件均在 `collection` 中配置，格式见 [任务配置说明](../../../skills/CONFIG.md)。

根据收集报告处理待复核或失败条目，保存原始结果。

复核条件为 **原始 `readback.confidence < threshold`**。等于阈值直接汇总；正文 null 单列 `failed`。主 Agent 按 [复核 prompt](references/review-prompt.md) 处理 `review-queue.json`，保存独立的 `review.json`：

```json
{"schema":"formaliscope-enrichment-review.v2","reviews":[]}
```

每项包含精确 `declaration_id`、完整确认或修订的 `annotation`、实际复核 `model` 和带时区 `reviewed_at`。保持原始两个分值和完整 `expectation_assessment`，依据固定 Lean 核对回译。仍待解释的条目保持 pending，原始文件保留。

将复核文件路径写入 `collection.reviews`，设置新的 `collection.output`，用同一任务配置重新收集。产物为 `enrichment.json`、`review-queue.json`、`report.json`，记录 `direct`、`reviewed`、`pending`、`failed`、原分值及复核来源。内部评估与原始结果保存在被 Git 忽略的私密目录。

格式修正通过真实 agent ID 续做对应 worker，提供机械错误及新的结果路径，保持原始语义、分值和已保存的第一阶段基线。

## 交付

用收集产物生成公开候选快照，按项目流程保存内部评估。具体命令见 [数据导入说明](../../../statement_workflow/README.md#公开候选与私密数据库分别导入)。

汇报选中、已汇总、待复核和失败数量，以及候选和内部结果位置，标明回译为机器草稿。
