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

先确认 Formaliscope 仓库根目录，其中同时存在 `skills/scripts/collect.py` 和 `statement_workflow/`；下列准备及收集命令在该根目录执行。`${CLAUDE_SKILL_DIR}` 只定位安装资源，批次目录只定位任务产物，二者都不是仓库根目录。配置内的相对路径相对于仓库根目录；交给阶段 Agent 的路径一律为已规范化的绝对路径，不再拼接当前工作目录或 `.formaliscope` 前缀。

开始任务前，按用户要求起草独立配置文件，命名为 `.formaliscope/tasks/configs/YYYYMMDD-HHMMSS-任务名.json`，时间使用本地时间，任务名用简短英文。

用 `defaults` 引用本工具的配置，填写本次输入快照和范围；额外要求写为覆盖项，其余继承默认配置。字段和合并规则读取仓库根目录下的 `skills/CONFIG.md`。例如 `20261008-150000-tower.json`：

```json
{
  "defaults": "skills/claude-code/formaliscope-enrich/config.json",
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

从本批 `agent-config.json` 读取已解析的调度设置。准备脚本按 harness 解析模型别名，默认 `luna6` 在 Claude Code 中为 `sonnet`；原始名称及映射保留在 `task-config.json`。Workflow 每次 `agent()` 显式传入冻结的 `worker.model`，按配置和当前工具支持的等级设置 `effort`。核对每个 Agent 的请求模型、启动元数据和响应记录模型，确认实际路由及设置后才收集；别名和 Agent 自报不作为执行证明。记录原始证据及待确认项，模型不符或路由无法确认时停止，不静默接受回退。

## 按组执行 Workflow

主会话按声明分组，每组恰好一条精确声明 ID，组间互不重叠且恰好覆盖 `manifest.declaration_ids`。每条声明对应一组、组内两个独立 Agent 先后执行。worker 按本组 ID 提取 `cards` 的 ID 与 Lean 字段，按实际依赖读取 `modules` 的必要定义；上下文定义不增加本组目标。

读取当前安装的 `${CLAUDE_SKILL_DIR}/workflows/enrich.js`，按脚本的参数约定，通过 `Workflow(scriptPath=..., args=...)` 传入任务配置和分组计划。`repoRoot` 为上述仓库根目录，`skillDir` 为安装资源目录，`batchDir` 为 prepare 回执中的批次目录，`resultDir` 为分配的新结果目录。拆批或调整并行度时通过外层调度传入不同分组，不复制重写阶段提示词或交付命令，也不沿用更新前生成的 Workflow 副本。

Workflow 为阶段 Agent 提供完整且已转义的 `delivery_command`，第二阶段另有 `check_readback_command`；原样执行，不从任务字段推测命令行参数。基线记录由检查程序自动定位。阶段任务缺少完整命令时由调度层重新生成，不能让 Worker 自行补命令。`output_schema_path` 是本阶段文件契约，`schema_path` 是共享字段标准，不能用最终收集格式替代阶段格式。

更新 Skill 或变更调度脚本后，先从本批目标中选一组验证两阶段交付及实际回执，成功文件直接计入本批收集，然后执行其余目标。遇到非法参数、路径或字段错误时先修正并重新验证这一组，再扩大调度；保持完整目标集合和失败原件。已启动的 Workflow 不会因安装更新自动更换任务或补回结果。

`pipeline()` 按组推进两阶段：

1. `formaliscope-readback` 读取 [回译 prompt](references/worker-prompt.md)，接收字段标准、第一阶段 schema、固定快照、主题、本组 ID、manifest、固定交付脚本及唯一 draft/result/next_result 路径。仅生成 `declaration_id`、`title_zh`、`readback`、`classification`、`priority`，不接收预期材料、不填占位判断。调用固定 `collect.py --deliver-readback`，程序校验、排他写出正式第一阶段文件并保存 `.baseline.json` 摘要后返回回执。
2. 该组交付成功并返回完整回执后，启动新的独立 `formaliscope-expectation`。它先调用 `--check-readback`，再读取 [预期判断 prompt](references/expectation-prompt.md)、只读基线及本批参考。仅生成 `declaration_id`、`expectation_assessment`，不得输出第一阶段字段。通过 `--deliver-expectation` 排他交付新文件；无材料也启动此 Agent，材料路径为 null，判断 `undetermined` 并说明原因。

不同组独立推进，不等待其他组完成第一阶段。每组两次调用、两个独立结果文件；超过单次 Workflow 上限时显式拆批并完整收集，不截断目标。

两个阶段分别输出 `formaliscope-readback-batch.v1`、`formaliscope-expectation-batch.v1`，`annotations` 都恰好一项，拒绝未知字段。可靠回译尚待完成时正文为 null。调度层分配唯一新路径，正式文件由固定程序排他创建；已有文件不覆盖。来源、运行及模型记录由脚本和调度层维护。

收集器按精确 ID 将第一阶段全部字段与第二阶段判断确定性合并成最终 `statement-enrichment.v2`。第一阶段实际字节摘要绑定本批 manifest、来源、组 ID 及第二阶段路径，收集时重查。主 Agent 不临时拼接或改正文使收集通过；失败保留原始文件并报告。摘要只能检测篡改，不是文件系统权限隔离；Workflow 无直接文件系统接口，交付命令由阶段 Agent 调用，其回执不替代收集时校验。历史 v1/v2 按显式 manifest 原契约处理，不猜测或转换格式。

等待 Workflow 完成通知，核对全部分组结果和实际执行模型后收集。失败或待交付组逐组报告。

## 校验与复核

执行完成后，根据文件回执和已核实的执行路由记录，将各组最终结果、第一阶段文件及已确认的调度名写入任务配置的 `collection`。无论有无预期材料，始终填写两阶段路径；未经核实不填写 `executed_model`。用同一任务配置收集：

```sh
python3 skills/scripts/collect.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
```

收集路径和复核文件均在 `collection` 中配置，格式见仓库根目录下的 `skills/CONFIG.md`。

根据收集报告处理待复核或失败条目，保存原始结果。

复核条件为 **原始 `readback.confidence < threshold`**。等于阈值直接汇总；正文 null 单列 `failed`。主 Agent 按 [复核 prompt](references/review-prompt.md) 处理 `review-queue.json`，保存独立的 `review.json`：

```json
{"schema":"formaliscope-enrichment-review.v2","reviews":[]}
```

每项包含精确 `declaration_id`、完整确认或修订的 `annotation`、实际复核 `model` 和带时区 `reviewed_at`。保持原始两个分值和完整 `expectation_assessment`，依据固定 Lean 核对回译。仍待解释的条目保持 pending，原始文件保留。

将复核文件路径写入 `collection.reviews`，设置新的 `collection.output`，用同一任务配置重新收集。产物为 `enrichment.json`、`review-queue.json`、`report.json`，记录 `direct`、`reviewed`、`pending`、`failed`、原分值及复核来源。内部评估与原始结果保存在被 Git 忽略的私密目录。

## 交付

用收集产物生成公开候选快照，按项目流程保存内部评估。具体命令读取仓库根目录下 `statement_workflow/README.md` 的“公开候选与私密数据库分别导入”一节。

汇报选中、已汇总、待复核和失败数量，以及候选和内部结果位置，标明回译为机器草稿。
