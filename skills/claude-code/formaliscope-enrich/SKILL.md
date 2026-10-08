---
name: formaliscope-enrich
description: 在 Claude Code 中用动态 Workflow 按组生成 Formaliscope 中文 Lean 回译、标题、标签、优先度及独立内部预期判断；仅按原始回译置信度复核，输出候选快照和可独立入库的内部评估。
argument-hint: <snapshot.json> <目录、文件或声明范围> [独立预期材料]
disable-model-invocation: true
---

# Formaliscope 中文回译与字段补全

此 Skill 是用户手动调用的主会话入口。主会话使用 Claude Code 的 **Workflow** 执行分组流水线，负责准备、核实执行状态、收集和低分复核。

## 准备回译任务

先加载 `/workflow-authoring`，使用专用的独立回译和预期 Agent。第一阶段接收阶段说明、字段标准、Lean 输入和主题。

开始任务前，按用户要求起草独立配置文件，命名为 `.statement-enrichment/task-configs/YYYYMMDD-HHMMSS-任务名.json`，时间使用本地时间，任务名用简短英文。

用 `defaults` 引用本工具的配置，填写本次输入快照和范围；额外要求写为覆盖项，其余继承默认配置。字段和合并规则见 [任务配置说明](../../../skills/CONFIG.md)。例如 `20261008-150000-tower.json`：

```json
{
  "defaults": "skills/claude-code/formaliscope-enrich/config.json",
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

从本批 `agent-config.json` 读取合并后的设置。Workflow 每次 `agent()` 显式传入 `worker.model`，按配置和当前工具支持的等级设置 `effort`。实际路由及设置取调度记录，核对一致后进入收集；配置差异和待确认项记录为执行限制。

## 按组执行 Workflow

主会话按文件或数学对象分组，每组非空、组间互不重叠且恰好覆盖 `manifest.declaration_ids`。worker 按本组 ID 提取 `cards` 的 ID 与 Lean 字段，按实际依赖读取 `modules` 的必要定义。

读取 `${CLAUDE_SKILL_DIR}/workflows/enrich.js`，按脚本的参数约定，通过 `Workflow(scriptPath=..., args=...)` 传入任务配置和分组计划。

`pipeline()` 按组推进两阶段：

1. `formaliscope-readback` 读取 [回译 prompt](references/worker-prompt.md)，接收字段标准、固定快照、主题、本组 ID 和唯一第一阶段结果路径。以 Lean 声明和必要定义回译，预期判断暂为 `undetermined`。
2. 该组第一阶段落盘并返回完整回执后，启动新的独立 `formaliscope-expectation`。它读取 [预期判断 prompt](references/expectation-prompt.md)、第一阶段基线及本批参考文件，补充 `expectation_assessment`，原样保留其余字段，写新结果。
3. 无预期材料时，以第一阶段文件为最终结果，预期判断保持 `undetermined`。

每组输出 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，逐条填写 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`。可靠回译尚待完成时，正文填 null。来源、运行及模型记录由脚本和调度层维护。

等待 Workflow 完成通知，核对全部分组结果和实际执行模型后收集。失败或待交付组逐组报告。

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

## 交付

用收集产物生成公开候选快照，按项目流程保存内部评估。具体命令见 [数据导入说明](../../../statement_workflow/README.md#公开候选与私密数据库分别导入)。

汇报选中、已汇总、待复核和失败数量，以及候选和内部结果位置，标明回译为机器草稿。
