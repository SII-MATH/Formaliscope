---
name: formaliscope-enrich
description: 在 Claude Code 中用动态 Workflow 按组生成 Formaliscope 中文 Lean 回译、标题、标签、优先度及独立内部预期判断；仅按原始回译置信度复核，输出候选快照和可独立入库的内部评估。
argument-hint: <snapshot.json> <目录、文件或声明范围> [独立预期材料]
disable-model-invocation: true
---

# Formaliscope 中文回译与字段补全

此 Skill 是用户手动调用的主会话入口。主会话使用 Claude Code 的 **Workflow** 执行分组流水线，负责准备、核实执行状态、收集和低分复核。

## 准备回译任务

先加载 `/workflow-authoring`。两个专用 agent 使用 `omitClaudeMd: true` 及各自的独立上下文；第一阶段的上下文由阶段说明、字段标准、Lean 输入及主题组成。运行前核对自动注入材料，使其符合该阶段的输入范围。

开始任务前，按用户要求起草独立配置文件，命名为 `.statement-enrichment/task-configs/YYYYMMDD-HHMMSS-任务名.json`，时间使用本地时间，任务名用简短英文。配置目录采用 0700，文件采用 0600。

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

准备脚本自动记录声明清单、模型配置、源码提交、快照摘要、运行 ID、时间和规则版本。完整快照按内容 SHA-256 共用在批次父目录的 `.snapshots/`，批次的 `snapshot.json` 为相对链接。共享快照为 0400，批次目录为 0700，普通文件为 0600。续做沿用本批文件；更改模型、材料或范围时准备新任务。移动、备份时一起保留共享快照目录；历史批次的完整快照继续可用。

脚本将所选声明绑定的 Blueprint 文案按精确 ID 保存到 `expectation-context.txt`，保留所有关联节点、出处及共同关联的声明，并记录材料摘要。补充材料和 Blueprint 一起作为第二阶段参考。第一阶段保存完成后提供该文件；缺少对应文案及明确补充预期的声明填 `undetermined`。

从本批 `agent-config.json` 读取合并后的设置。Workflow 每次 `agent()` 显式传入 `worker.model`，按配置和当前工具支持的等级设置 `effort`。实际路由及设置取调度记录，核对一致后进入收集；配置差异和待确认项记录为执行限制。

## 按组执行 Workflow

主会话按文件或数学对象分组，每组非空、组间互不重叠且恰好覆盖 `manifest.declaration_ids`。worker 按本组 ID 提取 `cards` 的 ID 与 Lean 字段，按实际依赖读取 `modules` 的必要定义。

通过 `Workflow(scriptPath=..., args=...)` 调用 `${CLAUDE_SKILL_DIR}/workflows/enrich.js`，`args` 使用结构化对象：

| 字段 | 内容 |
| --- | --- |
| `repoRoot` | Formaliscope 项目绝对路径 |
| `skillDir` | `${CLAUDE_SKILL_DIR}` 的绝对路径 |
| `batchDir` | 本批目录绝对路径 |
| `resultDir` | 可选，默认 batchDir；重生成时使用新的 0700 私密目录 |
| `config` | 本批 `agent-config.json` 的完整对象 |
| `declarationIds` | 本次运行的完整精确 ID 列表；全批运行取 manifest 的目标 |
| `groups` | `[{"key":"group-1","declarationIds":["statement::Example.value"]}, ...]` |
| `expectationContext` | manifest 有预期摘要时取本批参考文件绝对路径，否则 null |

以 0600 保存分组计划与每次运行的 args。预期正文通过第二阶段的参考文件提供。规模超过运行时上限时拆成多次 Workflow；各次 ID 与 groups 对应，跨运行 key 和目标互不重叠，最终覆盖完整 manifest。

`pipeline()` 按组推进两阶段：

1. `formaliscope-readback` 读取 [回译 prompt](references/worker-prompt.md)，接收字段标准、固定快照、主题、本组 ID 和唯一第一阶段结果路径。以 Lean 声明和必要定义回译，预期判断暂为 `undetermined`。
2. 该组第一阶段落盘并返回完整回执后，启动新的独立 `formaliscope-expectation`。它读取 [预期判断 prompt](references/expectation-prompt.md)、第一阶段基线及本批参考文件，补充 `expectation_assessment`，原样保留其余字段，写新结果。
3. 无预期材料时，以第一阶段文件为最终结果，预期判断保持 `undetermined`。

每组输出 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，逐条填写 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`。可靠回译尚待完成时，正文填 null。来源、运行及模型记录由脚本和调度层维护。

两个阶段均写 0600 文件，Workflow 返回 `{result_path, count}` 回执。等待完成通知，保存 runId、脚本路径和回执；全部计划运行返回 `complete=true`、`incomplete_groups=[]`，且目标并集覆盖 manifest 后收集。失败、缺文件或漏组逐组报告，正文 null 的条目交给收集器列为 failed。

同一会话续做时，先结束旧实例，再用原脚本、完整 args 和 `resumeFromRunId` 恢复。核对缓存结果的落盘文件及调度记录后复用。重生成使用新的 `resultDir` 和新运行，沿用固定 `batchDir`，每次 agent 写入唯一的新结果路径。

## 校验与复核

执行完成后，根据文件回执和调度记录，将各组结果、第一阶段文件及已确认的实际模型写入任务配置的 `collection`；用同一任务配置收集：

```sh
python3 skills/scripts/collect.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

收集路径和复核文件均在 `collection` 中配置，格式见 [任务配置说明](../../../skills/CONFIG.md)。

收集器校验完整目标集合、固定来源、配置主题、字段类型、有限分值和条件理由；有预期时还校验第一阶段回译及原分值、材料摘要。格式错误可由对应 worker 按机械错误修正一次，写入新的私密文件并保持其余内容。沿用完整原始输入收集到新输出目录；未解决的错误按组记录为未完成。

复核条件为 **原始 `readback.confidence < threshold`**。等于阈值直接汇总；正文 null 单列 `failed`。主 Agent 按 [复核 prompt](references/review-prompt.md) 处理 `review-queue.json`，保存独立 0600 的 `review.json`：

```json
{"schema":"formaliscope-enrichment-review.v2","reviews":[]}
```

每项包含精确 `declaration_id`、完整确认或修订的 `annotation`、实际复核 `model` 和带时区 `reviewed_at`。保持原始两个分值和完整 `expectation_assessment`，依据固定 Lean 核对回译。仍待解释的条目保持 pending，原始文件保留。

将复核文件路径写入 `collection.reviews`，设置新的 `collection.output`，用同一任务配置重新收集。产物为 `enrichment.json`、`review-queue.json`、`report.json`，记录 `direct`、`reviewed`、`pending`、`failed`、原分值及复核来源。内部评估与原始结果保存在被 Git 忽略的私密目录。

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
