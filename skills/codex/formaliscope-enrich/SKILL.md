---
name: formaliscope-enrich
description: "在 Codex 中为 Formaliscope 按配置指定的子 Agent 分组生成中文 Lean 回译、标题、标签、优先度及内部预期判断；仅按回译自报置信度复核，输出候选快照和可独立入库的内部评估。用于补全审阅数据。"
---

# Formaliscope 中文回译与字段补全

在当前 Codex 会话中，主 Agent 分组调度，子 Agent 按 [字段标准 v2](../../../statement_workflow/SCHEMA_V2.md) 填字段，脚本负责校验和合并。

## 准备回译任务

遵守项目 `AGENTS.md`，以用户指定的 Statement 快照及目录、文件或声明为范围。范围待明确时，请用户指定本次目标。

读取本工具对应的 `config.json`，或用户指定的配置。`worker.model` 默认 `luna6`，由用户配置模型路由；`worker.reasoning_effort` 指定推理设置，`topics` 提供主题选项。新任务使用 `formaliscope-enrichment-config.v2`，通过 `--config` 显式传入配置。

```sh
python3 skills/scripts/prepare.py \
  --config skills/codex/formaliscope-enrich/config.json \
  --snapshot /absolute/path/snapshot.json \
  --directory KIP126/Def/ClassicalAdams/Tower \
  --output .statement-enrichment/new-batch
```

将示例目录替换为用户选定的范围。`--directory` 包含子目录，也可重复使用 `--file`、`--declaration-id`，各选择取并集。阈值默认 0.8，用户指定时通过 `--threshold` 设置。补充预期材料通过 `--expectation-context` 提供。

准备脚本自动记录声明清单、模型配置、源码提交、快照摘要、运行 ID、时间和规则版本。完整快照按内容 SHA-256 共用在批次父目录的 `.snapshots/`，批次的 `snapshot.json` 为相对链接。共享快照为 0400，批次目录为 0700，普通文件为 0600。续做沿用本批文件；更改模型、材料或范围时准备新任务。移动、备份时一起保留共享快照目录；历史批次的完整快照继续可用。

脚本将所选声明绑定的 Blueprint 文案按精确 ID 保存到 `expectation-context.txt`，保留所有关联节点、出处及共同关联的声明，并记录材料摘要。补充材料和 Blueprint 一起作为第二阶段参考。第一阶段保存完成后提供该文件；缺少对应文案及明确补充预期的声明填 `undetermined`。

## 分组与两阶段填写

按文件或数学对象分组，目标互不重叠且恰好覆盖 `manifest.declaration_ids`。共享定义用于上下文阅读，任务目标以分配的精确 ID 为准。

启动子 Agent 时，将 `worker.model` 传给 `spawn_agent.model`，非 null 的推理等级传给 `reasoning_effort`，设置 `fork_turns="none"`，按工具并发额度执行。根据调度记录核对实际路由和设置；核对结果一致后收集，差异或待确认项作为本批执行限制报告。调整配置时开新任务。

第一阶段使用 [回译 prompt](references/worker-prompt.md)，以独立上下文接收仓库路径、固定快照、主题、本组 ID 和唯一 `group-N-readback.json` 路径。提取 Lean 声明及必要定义作为回译依据，保存正文和自报分值；预期判断暂填 `undetermined`。

有预期材料时，第一阶段落盘后继续同一子 Agent，通过 [预期判断 prompt](references/expectation-prompt.md) 提供保存的基线、预期文件和新的 `group-N.json` 路径。补充 `expectation_assessment`，原样保留基线的其余字段。`manifest.run.expectation_context_digest` 为 null 时，第一阶段文件即为最终结果，预期判断保持 `undetermined`。

每组输出 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，逐条填写 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`。可靠回译尚待完成时，正文填 null。来源、运行及模型记录由脚本和调度层维护。

## 校验与复核

确认实际执行模型后，用全部分组结果收集：

```sh
python3 skills/scripts/collect.py \
  --snapshot .statement-enrichment/new-batch/snapshot.json \
  --manifest .statement-enrichment/new-batch/manifest.json \
  --result .statement-enrichment/new-batch/group-1.json \
  --executed-model ACTUAL_MODEL \
  --readback-result .statement-enrichment/new-batch/group-1-readback.json \
  --output .statement-enrichment/new-batch/collected
```

`ACTUAL_MODEL` 使用调度记录确认的模型路由 ID，并与本批配置一致。`--result`、`--readback-result` 和 `--review` 均可重复。有预期时提供每组第一阶段文件；无预期时将第一阶段文件作为 `--result`，可省略 `--readback-result`。

收集器校验完整目标集合、固定来源、配置主题、字段类型、有限分值和条件理由；有预期时还校验第一阶段回译及原分值、材料摘要。格式错误可由对应 worker 按机械错误修正一次，写入新的私密文件并保持其余内容。沿用完整原始输入收集到新输出目录；未解决的错误按组记录为未完成。

复核条件为 **原始 `readback.confidence < threshold`**。等于阈值直接汇总；正文 null 单列 `failed`。主 Agent 按 [复核 prompt](references/review-prompt.md) 处理 `review-queue.json`，保存独立 0600 的 `review.json`：

```json
{"schema":"formaliscope-enrichment-review.v2","reviews":[]}
```

每项包含精确 `declaration_id`、完整确认或修订的 `annotation`、实际复核 `model` 和带时区 `reviewed_at`。保持原始两个分值和完整 `expectation_assessment`，依据固定 Lean 核对回译。仍待解释的条目保持 pending，原始文件保留。

用全部原始输入加 `--review /absolute/path/review.json` 重新收集到新目录。产物为 `enrichment.json`、`review-queue.json`、`report.json`，记录 `direct`、`reviewed`、`pending`、`failed`、原分值及复核来源。内部评估与原始结果保存在被 Git 忽略的私密目录。

## 候选快照与内部评估

```sh
python3 -m review_app validate-enrichment \
  --snapshot /absolute/path/batch/snapshot.json --file /absolute/path/collected/enrichment.json
python3 -m review_app enrich-snapshot \
  --snapshot /absolute/path/batch/snapshot.json --file /absolute/path/collected/enrichment.json \
  --output /absolute/path/new-candidate-snapshot.json
python3 -m review_app import-agent-assessments \
  --snapshot /absolute/path/batch/snapshot.json --file /absolute/path/collected/enrichment.json \
  --data-dir /absolute/path/private-data-dir
```

前两个命令校验并生成新的公开候选快照，含草稿回译、标题、分类和优先度。第三个命令独立保存机器内部评估，同批幂等、冲突报错。人工审阅、候选安装和部署分别沿项目流程执行。

汇报选中、direct、reviewed、pending、failed 数量及候选、内部结果位置，标明回译为机器草稿。新任务使用 v2；历史 v1 批次沿原契约校验。
