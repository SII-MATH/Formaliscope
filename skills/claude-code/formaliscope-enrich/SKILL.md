---
name: formaliscope-enrich
description: 在 Claude Code 中用动态 Workflow 按组生成 Formaliscope 中文 Lean 回译、标题、标签、优先度及独立内部预期判断；仅按原始回译置信度复核，输出候选快照和可独立入库的内部评估。不修改 Lean 证明，不安装快照或部署。
argument-hint: <snapshot.json> <目录、文件或声明范围> [独立预期材料]
disable-model-invocation: true
---

# Formaliscope 中文回译与字段补全

此 Skill 是用户手动调用的主会话入口。使用 Claude Code 的 **Workflow** 工具执行确定性的分组流水线；准备、执行状态核实、机械收集与低分复核留在主会话。不要设置 `context: fork`，不要改成逐组 `Agent`/`Task` 加 `resume` 的调度。

## 1. 检查能力、范围与输入边界

定位包含 `review_app/`、`statement_workflow/` 和 `skills/` 的 Formaliscope 仓库根目录，遵守 `AGENTS.md`；下述仓库命令均在该根目录运行。安装位置只用于发现资源，不能据此推算仓库位置。只处理用户指定的 Statement 快照及目录、文件或声明；缺少范围时请用户指定，不默认全库。

调用本 Skill 表示请求使用 Workflow，但不绕过任何工具许可。开始前检查：

- 当前 harness 是 Claude Code，且提供 `Workflow` 与 `workflow-authoring`。先加载 `/workflow-authoring`；不可用时停止说明，不静默回退到别的 harness 或独立 API 执行器。
- `${CLAUDE_SKILL_DIR}/agents/` 是分发资源，**不会自动注册**。按仓库 `skills/README.md` 将两个定义安装到 `.claude/agents/`，确认已加载 `formaliscope-readback` 与 `formaliscope-expectation`。
- 本流程要求支持 `omitClaudeMd: true`（Claude Code v2.1.271 起），不使用继承主历史的 fork，不预加载本 Skill 或其他含预期资料的 skill，不配置持久 memory。`omitClaudeMd` 不排除托管策略等所有来源；还要检查自动注入的其他上下文。若第一阶段会接收预期、既有中文、Blueprint、论文、作者注释或人工判断，停止，不先生成被污染的回译。
- 模型路由 ID 来自 `${CLAUDE_SKILL_DIR}/config.json` 的 `worker.model`，默认 **`luna6`**。用户负责路由；不替换为 Claude 家族别名或供应商 ID，不修改全局模型、凭据或允许列表。Workflow 每个阶段的 `agent()` 都显式传该路由，而不是依赖主会话模型或 Worker 自报身份。
- `worker.reasoning_effort` 默认 null，表示不额外指定 effort。非 null 时只能使用当前 Workflow 和模型确实支持的 `low/medium/high/xhigh/max`，由逐次 `agent()` 的 `effort` 设置；无法确认支持时停止，不翻译 Codex 等级、不忽略或降级。

运行前确认 harness 能接受并核实该路由；运行后还须逐次核对实际执行。模型允许列表或环境可能替换请求模型；仅传入 `model` 并不是实际执行证据。无法核实时不能发布本批结果。

## 2. 冻结私密批次

新批次使用 `formaliscope-enrichment-config.v2` 与仓库 `statement_workflow/SCHEMA_V2.md`。先读取安装配置；用户指定其他配置时使用该文件，准备脚本必须显式传 `--config`：

```bash
python3 skills/scripts/prepare.py \
  --config "${CLAUDE_SKILL_DIR}/config.json" \
  --snapshot /absolute/path/snapshot.json \
  --directory KIP126/Def/ClassicalAdams/Tower \
  --output .statement-enrichment/new-batch
```

可重复 `--directory`、`--file`、`--declaration-id`（精确 ID 或完整 Lean 名称）；有独立预期材料时加 `--expectation-context /absolute/path/project-expectation.txt`。默认阈值 0.8，只在用户要求时用 `--threshold` 修改。

目录参数指定本批要补全的范围，按冻结快照中的 Lean 文件路径选择，并包含子目录。上例使用当前 KIP126 存在的 `KIP126/Def/ClassicalAdams/Tower`，只是示例；实际执行应替换为用户指定的目录、文件或声明。源码版本已经固定后，批次冻结进一步固定本次的条目清单、输入快照、Blueprint 参考和模型配置，保证两阶段及中断续做使用同一组材料。批次还保存机器内部判断、置信度和原始输出，因此放在本地被 Git 忽略的目录；只有公开回译和标签进入审阅页面。

准备脚本默认从冻结快照提取所选声明绑定的 Blueprint 文案，并按精确声明 ID 保存到 `expectation-context.txt`，记录材料摘要；一个声明的多个节点及节点共同关联的声明全部保留。`--expectation-context` 是补充材料，不会取消 Blueprint 参考。没有任何绑定文案或补充材料时不创建预期文件；混合批次中未绑定的声明仍须判断为不知道。预期文件只能在第一阶段落盘后提供。

脚本冻结 `snapshot.json`、`agent-config.json`、`manifest.json`，可选材料存为 `expectation-context.txt` 并记录 SHA-256。运行 ID、时间、源码提交、快照摘要、模型路由、推理等级、主题、规则版本及阈值均由脚本记录，不让 Agent 填写。批次目录 0700、文件 0600；调度输入、结果及修正文件也采用这些权限。

续做只使用冻结文件。换模型或 effort 时开新批次。Workflow 的 prompt、工具调用与 journal 也可能包含内部数据；只在授权的私密会话内运行，不将批次或会话记录上传到公开服务、网页或 Git。

## 3. 用按组 Workflow 执行两阶段

主会话读取冻结 manifest、config 和快照，按文件或相关数学对象拆组。组内 ID 非空，组间不重叠，恰好覆盖 `manifest.declaration_ids`；共享定义可跨组读取，但不自动成为目标。worker 用只读 JSON 查询按本组 ID 和依赖提取冻结快照中的 Lean 内容，不通读整份快照；不得将既有中文或评估字段带入第一阶段。

读取 `${CLAUDE_SKILL_DIR}/workflows/enrich.js`。通过 `Workflow` 的 `scriptPath` 使用此分发脚本，`args` 传**真实结构化对象**，不能传 JSON 编码字符串。它是 Skill 的支持文件，不需要另装到 `.claude/workflows/`，也不是另一个可直接发现的同名 slash command。

`args` 字段：

| 字段 | 来源 |
| --- | --- |
| `repoRoot` | 已确认的仓库绝对路径 |
| `skillDir` | `${CLAUDE_SKILL_DIR}` 的实际绝对路径 |
| `batchDir` | 本批冻结目录绝对路径 |
| `resultDir` | 可选，默认 batchDir；需重生成时由主会话创建新的 0700 私密结果目录，不复制或更改冻结输入 |
| `config` | 冻结 `agent-config.json` 的完整 JSON 对象，不删主题或改模型 |
| `declarationIds` | 必填，由主会话显式传入本次 Workflow 的完整精确 ID 列表；单次全批运行时取 `manifest.declaration_ids` |
| `groups` | `[{"key":"group-1","declarationIds":["statement::Example.value"]}, ...]`，按实际分组替换 |
| `expectationContext` | manifest 有预期摘要时为本批 `expectation-context.txt` 绝对路径，否则 null |

在私密批次中以 0600 保存完整分组计划及每次运行的 args 以便核对和续做，不把预期材料正文嵌入 args。默认一次 Workflow 完成全部组；规模超过运行时上限时，主会话显式将完整计划拆成若干运行，每次 declarationIds 与 groups 恰好匹配，跨运行保持 key/目标互不重叠，所有运行的目标并集仍必须等于 manifest。运行前核对 config 与 manifest 的 model/effort 一致，预期路径确属本批冻结材料。

脚本先检查完整分组、路径及设置，再用 `pipeline()` 逐组推进，不等待其他组完成第一阶段：

1. **纯 Lean 回译**：`formaliscope-readback` 读取 [第一阶段 prompt](references/worker-prompt.md)，只收到字段标准、冻结快照、主题、本组 ID 和唯一 `group-N-readback.json` 路径；不收到预期材料、材料路径、其他组结果或主会话历史。只填 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`、`expectation_assessment`；后者暂为 `undetermined`，理由注明尚未提供独立预期。无法回译时正文 null，不虚构。
2. **独立预期判断**：该组第一阶段落盘并返回完整回执后，才启动新的 `formaliscope-expectation`，读取 [第二阶段 prompt](references/expectation-prompt.md)、固定基线和冻结预期材料，写新的 `group-N.json`。只改 `expectation_assessment`，其余字段全部不变。使用独立上下文而非 resume；基线文件承接组内状态。
3. **无预期材料**：不启动第二阶段，第一阶段文件就是最终结果，判断保持 `undetermined`。不从 Lean 自行构造预期再宣布符合。

两个阶段都写 `{"schema":"formaliscope-agent-batch.v2","annotations":[...]}`，权限 0600。Workflow 的结构化输出仅为 `{result_path, count}` 回执，完整数学内容不在主会话反复传递；回执既不是机械校验结果，也不是模型证明。

等待 Workflow 完成通知，不轮询。保存返回的 runId、脚本路径和回执；只有所有计划运行都返回 `complete=true`、无 `incomplete_groups`，并且全部回执目标并集完整覆盖 manifest 才能进入完整收集。跳过、API 错误、缺文件、计数不符或漏组必须显式报告，不能过滤之后冒充成功。正文 null 的合法条目仍保留，让收集器单列 failed。

从 Workflow 进度详情/调度记录等 harness 状态确认**每次回译与预期 agent** 的实际模型和设置，检查模型替换警告；不是通过要求 Worker 自报身份来确认。实际路由不符或无法确认时停止该批，说明限制；更改要求需新批次。

同一会话中可用原脚本、完整 args 与 `resumeFromRunId` 续做；先停止仍在运行的旧实例。缓存回执只表示原调用完成，仍要核对落盘文件和执行记录。续做可能重跑失败点之后的 agents；不得覆盖已保存的原始回译；缓存完成结果可在主会话核实文件与执行记录后复用，重新执行的 agent 遇到已有目标文件则停止。需要重生成时由主会话创建新的 resultDir、保存新的 args 并启动新运行，冻结 batchDir 不变；不要把改变 args 的运行视为原 runId 的无损续做，不并发写同一个文件。超出运行时上限时显式拆成多次 Workflow，最终仍完整覆盖同一 manifest，不截断目标。

## 4. 完整校验、唯一复核条件与汇总

主会话确认实际路由后，以全部组的回执组成以下命令：

```bash
python3 skills/scripts/collect.py \
  --snapshot .statement-enrichment/new-batch/snapshot.json \
  --manifest .statement-enrichment/new-batch/manifest.json \
  --result .statement-enrichment/new-batch/group-1.json \
  --executed-model ACTUAL_MODEL \
  --readback-result .statement-enrichment/new-batch/group-1-readback.json \
  --output .statement-enrichment/new-batch/collected
```

`ACTUAL_MODEL` 是 harness 核实的路由 ID，默认本批为 `luna6`，不推断底层供应商。`--result` 和 `--readback-result` 均按组重复；无预期材料时 `--result` 指向第一阶段文件，可以省略 `--readback-result`。

这里需要全组屏障，因为收集器校验完整目标集合、冻结来源、字段类型、角色、配置主题、有限分值及条件理由；带预期时还检查第一阶段完整覆盖、回译和原分值不变以及预期摘要。格式错误最多让对应阶段 Worker 修正一次，沿用实际模型和设置、保持阶段输入边界，修正结果写新私密文件；不要丢弃条目或重新做语义评估来规避校验。沿用全部原始输入重新收集到新输出目录。

自动语义复核的唯一条件是 **原始 `readback.confidence < threshold`**。等于阈值直接汇总；预期判断及其分值、角色、优先度、重要性或抽样不增加条件。正文 null 是生成失败，单列 `failed`，不进入语义复核队列。

队列非空时由当前主 Agent 按 [复核 prompt](references/review-prompt.md) 只处理 `review-queue.json`，写独立 0600 的 `review.json`：

```json
{"schema":"formaliscope-enrichment-review.v2","reviews":[]}
```

每项包含 `declaration_id`、完整确认/修订的 `annotation`、harness 核实的实际复核 `model`、带时区的实际 `reviewed_at`。保留原始两个分值及完整 `expectation_assessment`；只核对忠实回译，不借预期材料改回译，不重做内部评估。仍无法回译的条目不加入 reviews，继续 pending。不改原始文件。

用全部原始输入加 `--review /absolute/path/review.json` 写到全新输出目录。收集器生成 `enrichment.json`、`review-queue.json`、`report.json`，区分 `direct`、`reviewed`、`pending`、`failed`，保存原始分值及复核来源。内容包含内部评估，不公开、不进 Git。

## 5. 候选快照与独立内部评估

```bash
python3 -m review_app validate-enrichment \
  --snapshot /absolute/path/batch/snapshot.json --file /absolute/path/collected/enrichment.json
```

```bash
python3 -m review_app enrich-snapshot \
  --snapshot /absolute/path/batch/snapshot.json --file /absolute/path/collected/enrichment.json \
  --output /absolute/path/new-candidate-snapshot.json
```

```bash
python3 -m review_app import-agent-assessments \
  --snapshot /absolute/path/batch/snapshot.json --file /absolute/path/collected/enrichment.json \
  --data-dir /absolute/path/private-data-dir
```

前两个命令不写数据库。候选仅保留公开的草稿回译、标题、分类和优先度，内部预期结果及两个分值不进入审阅接口或审阅者导出。第三个命令独立入库，不写人的判断，同批幂等、冲突拒绝。运行、来源及原始回译由脚本绑定；内部入库不意味着候选已安装。

汇报选中、direct、reviewed、pending、failed 数量及候选/内部结果位置。所有回译仍为机器草稿，不标 verified 或人工已审阅。**不自动安装快照或部署。** 历史 v1 批次沿用收集器原 v1 校验路径，不自动转 v2；新准备和 prompt 使用 v2。
