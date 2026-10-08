---
name: formaliscope-enrich
description: "在 Kimi Code 中用原生 AgentSwarm 按配置指定的模型并行生成中文 Lean 回译、标题、标签、优先度及内部预期判断；仅按回译自报置信度复核，输出候选快照和可独立入库的内部评估。"
type: prompt
whenToUse: "用户要求为 Formaliscope 的明确 Statement 范围补全中文回译、审阅字段或内部预期评估时。"
---

# Formaliscope 中文回译与字段补全

在当前 Kimi Code 会话中，主 Agent 用原生 `AgentSwarm` 调度专用 worker，按仓库 `statement_workflow/SCHEMA_V2.md` 填字段，脚本负责校验和合并。

## 准备回译任务

开始任务前，按用户要求起草独立配置文件，命名为 `.statement-enrichment/task-configs/YYYYMMDD-HHMMSS-任务名.json`，时间使用本地时间，任务名用简短英文。配置目录采用 0700，文件采用 0600。

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

准备脚本自动记录声明清单、模型配置、源码提交、快照摘要、运行 ID、时间和规则版本。完整快照按内容 SHA-256 共用在批次父目录的 `.snapshots/`，批次的 `snapshot.json` 为相对链接。共享快照为 0400，批次目录为 0700，普通文件为 0600。续做沿用本批文件；更改模型、材料或范围时准备新任务。移动、备份时一起保留共享快照目录；历史批次的完整快照继续可用。

脚本将所选声明绑定的 Blueprint 文案按精确 ID 保存到 `expectation-context.txt`，保留所有关联节点、出处及共同关联的声明，并记录材料摘要。补充材料和 Blueprint 一起作为第二阶段参考。第一阶段保存完成后提供该文件；缺少对应文案及明确补充预期的声明填 `undetermined`。

从本批 `agent-config.json` 读取合并后的设置，确认当前 provider、模型和调度机制可应用配置的推理设置。配置差异和待确认项作为执行限制报告，调整配置时开新任务。

## 原生 Swarm 与两阶段填写

按文件或数学对象分组，每组目标互不重叠且恰好覆盖 `manifest.declaration_ids`，共享定义作为阅读上下文。使用独立上下文的 `formaliscope-enrich-worker`，每组分配稳定 `group_id`、精确 ID 和唯一第一阶段、第二阶段输出路径。第一阶段上下文由阶段说明、字段标准、Lean 输入和主题组成，核对自动注入内容符合阶段范围。

模型由 Kimi 调度层绑定。有模型池和 `model` 参数时，选择与本批路由对应的池 alias；`primary` 使用调用者已核实的路由。无池时沿用调用者路由；配置强制 secondary model 时按该绑定执行。将启动请求、实际绑定与执行记录逐组核对，确认本批模型和设置。

### 第一阶段

两组及以上使用 `AgentSwarm`。每个 `item` 序列化本组的 `group_id`、精确 ID 和结果路径，模板通过 `{{item}}` 引入。调用前将示例路径和池 alias 替换为实际值；工具提供 `model` 参数时传入该字段。

```json
{
  "description": "分组生成纯 Lean 回译",
  "subagent_type": "formaliscope-enrich-worker",
  "model": "已确认绑定本批路由的池 alias",
  "prompt_template": "执行第一阶段。仓库：/absolute/repo；阶段说明：/absolute/skill/references/worker-prompt.md；固定快照：/absolute/batch/snapshot.json；主题配置：/absolute/batch/agent-config.json。以本组 Lean 输入及必要定义回译，将结果写入分配路径。组任务：{{item}}。返回 group_id、结果绝对路径及条目数。",
  "items": [
    "{\"group_id\":\"group-1\",\"declaration_ids\":[\"精确ID-1\"],\"output\":\"/absolute/batch/group-1-readback.json\"}",
    "{\"group_id\":\"group-2\",\"declaration_ids\":[\"精确ID-2\"],\"output\":\"/absolute/batch/group-2-readback.json\"}"
  ]
}
```

模板中的阶段说明使用 `${KIMI_SKILL_DIR}/references/worker-prompt.md`。worker 按本组 ID 提取快照中的 Lean 字段和必要定义，预期判断暂填 `undetermined`。

每次 Swarm 最多 128 项，更多组拆成多次调用，并发由运行时调度。将 `AgentSwarm` 作为该响应的单独工具调用，前台等待聚合报告。只有一组时使用 `Agent`，提供完整 prompt、同一专用类型和模型设置。

按工具返回的真实身份记录 `group_id → agent_id → 第一阶段文件 → 实际路由` 映射。阶段交接核对落盘文件、JSON、精确 ID 集合与 0600 权限。第一阶段的机械格式修正在提供预期前完成；失败或待交付组单列，保留已完成结果。

### 第二阶段

有预期材料且该组第一阶段就绪时，通过 `AgentSwarm.resume_agent_ids` 续做同一 worker。map 的键为真实 `agent_id`，值为完整第二阶段任务；resume 沿用原模型和上下文：

```json
{
  "description": "续做内部预期判断",
  "resume_agent_ids": {
    "真实agent-id-1": "执行第二阶段。group_id：group-1；仓库：/absolute/repo；阶段说明：/absolute/skill/references/expectation-prompt.md；快照：/absolute/batch/snapshot.json；基线：/absolute/batch/group-1-readback.json；参考：/absolute/batch/expectation-context.txt；新输出：/absolute/batch/group-1.json。补充 expectation_assessment，原样保留其余字段。返回 group_id、结果绝对路径及条目数。"
  }
}
```

阶段说明使用 `${KIMI_SKILL_DIR}/references/expectation-prompt.md`。resume map 也按最多 128 项拆批；单组可用 `Agent(resume=真实agent_id, prompt=完整任务)`。第二阶段读取已保存的第一阶段基线和本批参考，补充内部判断并保留原始回译及分值。

`manifest.run.expectation_context_digest` 为 null 时，第一阶段文件即最终结果，预期判断保持 `undetermined`。

每组输出 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，逐条填写 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`。可靠回译尚待完成时，正文填 null。来源、运行及模型记录由脚本和调度层维护。

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

格式修正通过真实 agent ID 续做对应 worker，提供机械错误及新的结果路径，保持原始语义、分值和已保存的第一阶段基线。

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
