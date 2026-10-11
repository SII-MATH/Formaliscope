---
name: formaliscope-enrich
description: "在 Codex 中为 Formaliscope 按配置指定的子 Agent 分组生成中英双语 Lean 回译、标题、标签、优先度及内部预期判断；仅按回译自报置信度复核，输出候选快照和可独立入库的内部评估。用于补全审阅数据。"
---

# Formaliscope 中英双语回译与字段补全

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

第二阶段使用脚本准备的 `expectation-context.txt`，以 Blueprint 文案和用户补充材料为参考；缺少对应参考时填 `undetermined`。按 manifest 的 `expectation_declaration_ids` 与本组 ID 的交集判断是否有参考；交集为空时 `next_result_path=null`，跳过第二阶段。

## 分组与两阶段填写

按文件或数学对象分组，目标互不重叠且恰好覆盖 `manifest.declaration_ids`。共享定义用于上下文阅读，任务目标以分配的精确 ID 为准。

启动子 Agent 时，将 `worker.model` 传给 `spawn_agent.model`，非 null 的推理等级传给 `reasoning_effort`，设置 `fork_turns="none"`，按工具并发额度执行。根据调度记录核对实际路由和设置；核对结果一致后收集，差异或待确认项作为本批执行限制报告。调整配置时开新任务。

第一阶段使用 [回译 prompt](references/worker-prompt.md)，以独立上下文接收仓库路径、固定快照、主题、本组 ID、manifest、唯一 draft/result 路径及 `next_result_path`（无材料时 null）。仅输出 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`，不接收预期材料、不填占位判断。Worker 调用固定 `collect.py --deliver-readback`，由程序校验、排他交付第一阶段文件并封存 `.baseline.json` 摘要。

有预期材料时，交付成功后继续同一子 Agent，按 [预期判断 prompt](references/expectation-prompt.md) 先调用 `--check-readback`，再提供只读基线、预期文件和已分配的 `group-N.json`。仅输出 `declaration_id`、`expectation_assessment`，通过 `--deliver-expectation` 排他交付，不复制或输出第一阶段字段。没有材料时保留跳过第二阶段规则，`collection.results=[]`，始终填写 `readback_results`；收集程序生成 `undetermined`，理由说明跳过，判断分值固定 1.0，仅表示确定缺少材料，不是模型数学判断。

两个阶段分别使用严格 `formaliscope-readback-batch.v1`、`formaliscope-expectation-batch.v1`，每组精确覆盖分配的 ID，每条一次。正文不能可靠生成时为 null。收集器检查第一阶段字节摘要、组 ID/条数和第二阶段分配路径，再按 ID 确定性合并，保持最终 `statement-enrichment.v2`。主 Agent 不临时拼接或改正文让校验通过；失败停止并保留原始文件。摘要仅检测篡改，不是权限隔离，不覆盖已有产物。历史 v1/v2 按显式 manifest 保留原契约，不猜测或转换。

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
