---
name: formaliscope-enrich
description: "在 Codex 中为 Formaliscope 按配置指定的子 Agent 分组生成中文 Lean 回译、标题、标签、优先度及内部预期判断；仅按回译自报置信度复核，输出候选快照和可独立入库的内部评估。用于补全审阅数据。"
---

# Formaliscope 中文回译与字段补全

在当前 Codex 会话中，主 Agent 分组调度，子 Agent 按 [字段标准 v2](../../../statement_workflow/SCHEMA_V2.md) 填字段，脚本负责校验和合并。

## 准备回译任务

开始任务前，按用户要求起草独立配置文件，命名为 `.formaliscope/tasks/configs/YYYYMMDD-HHMMSS-任务名.json`，时间使用本地时间，任务名用简短英文。

用 `defaults` 引用本工具的配置，填写本次输入快照和范围；额外要求写为覆盖项，其余继承默认配置。字段和合并规则见 [任务配置说明](../../../skills/CONFIG.md)。例如 `20261008-150000-tower.json`：

```json
{
  "defaults": "skills/codex/formaliscope-enrich/config.json",
  "snapshot": "/absolute/path/snapshot.json",
  "selection": {"directories": ["KIP126/Def/ClassicalAdams/Tower"]}
}
```

按实际任务替换路径和范围，然后准备：

```sh
python3 skills/scripts/prepare.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
```

根据准备脚本的回执读取本批 `manifest.json` 和 `agent-config.json`，按所选声明和合并后的配置执行。

第二阶段使用脚本准备的 `expectation-context.txt`，以 Blueprint 文案和用户补充材料为参考；缺少对应参考时填 `undetermined`。

## 分组与两阶段填写

按文件或数学对象分组，目标互不重叠且恰好覆盖 `manifest.declaration_ids`。共享定义用于上下文阅读，任务目标以分配的精确 ID 为准。

启动子 Agent 时，将 `worker.model` 传给 `spawn_agent.model`，非 null 的推理等级传给 `reasoning_effort`，设置 `fork_turns="none"`，按工具并发额度执行。根据调度记录核对实际路由和设置；核对结果一致后收集，差异或待确认项作为本批执行限制报告。调整配置时开新任务。

第一阶段使用 [回译 prompt](references/worker-prompt.md)，以独立上下文接收仓库路径、固定快照、主题、本组 ID 和唯一 `group-N-readback.json` 路径。提取 Lean 声明及必要定义作为回译依据，保存正文和自报分值；预期判断暂填 `undetermined`。

有预期材料时，第一阶段落盘后继续同一子 Agent，通过 [预期判断 prompt](references/expectation-prompt.md) 提供保存的基线、预期文件和新的 `group-N.json` 路径。补充 `expectation_assessment`，原样保留基线的其余字段。没有预期材料时，第一阶段文件即为最终结果，预期判断保持 `undetermined`。

每组输出 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，逐条填写 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`。可靠回译尚待完成时，正文填 null。来源、运行及模型记录由脚本和调度层维护。

## 校验与复核

执行完成后，根据文件回执和调度记录，将各组结果、第一阶段文件及已确认的实际模型写入任务配置的 `collection`；用同一任务配置收集：

```sh
python3 skills/scripts/collect.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
```

收集路径和复核文件均在 `collection` 中配置，格式见 [任务配置说明](../../../skills/CONFIG.md)。

根据收集报告处理待复核或失败条目，保存原始结果。

复核条件为 **原始 `readback.confidence < threshold`**。等于阈值直接汇总；正文 null 单列 `failed`。主 Agent 按 [复核 prompt](references/review-prompt.md) 处理 `review-queue.json`，保存独立的 `review.json`：

```json
{"schema":"formaliscope-enrichment-review.v2","reviews":[]}
```

每项包含精确 `declaration_id`、完整确认或修订的 `annotation`、实际复核 `model` 和带时区 `reviewed_at`。保持原始两个分值和完整 `expectation_assessment`，依据固定 Lean 核对回译。仍待解释的条目保持 pending，原始文件保留。

将复核文件路径写入 `collection.reviews`，设置新的 `collection.output`，用同一任务配置重新收集。产物为 `enrichment.json`、`review-queue.json`、`report.json`，记录 `direct`、`reviewed`、`pending`、`failed`、原分值及复核来源。内部评估与原始结果保存在被 Git 忽略的私密目录。

## 交付

用收集产物生成公开候选快照，按项目流程保存内部评估。具体命令见 [数据导入说明](../../../statement_workflow/README.md#公开候选与私密数据库分别导入)。

汇报选中、已汇总、待复核和失败数量，以及候选和内部结果位置，标明回译为机器草稿。
