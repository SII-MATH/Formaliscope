# 回译任务配置

Agent 开始任务前，起草一份独立 JSON 配置，保存到 `.formaliscope/tasks/configs/YYYYMMDD-HHMMSS-任务名.json`。时间取本地时间，任务名使用简短英文，例如 `20261008-150000-tower.json`；同名任务加序号。

共享默认值在 [default-config.json](default-config.json)。三个工具的 `config.json` 引用共享默认值并提供各自的设置；任务通过 `defaults` 引用当前工具的配置，填写输入及需要覆盖的字段。对象逐项合并，数组整体替换，明确的 null 覆盖默认值；省略字段继承默认配置。

所有配置内的相对路径都相对于 **Formaliscope 仓库根目录**，也可使用绝对路径。路径规则适用于 `defaults`、快照、补充材料、输出和结果文件。

```json
{
  "defaults": "skills/claude-code/formaliscope-enrich/config.json",
  "snapshot": "/absolute/path/snapshot.json",
  "selection": {
    "directories": ["KIP126/Def/ClassicalAdams/Tower"]
  }
}
```

| 字段 | 用途 |
| --- | --- |
| `defaults` | 要继承的配置文件，如当前工具的 `config.json` |
| `snapshot` | 本次输入的 Statement 快照路径 |
| `selection.directories` | Lean 目录列表，包含子目录 |
| `selection.files` | Lean 文件列表 |
| `selection.declaration_ids` | 精确声明 ID 或完整 Lean 名称列表 |
| `output` | 批次目录；省略时取 `.formaliscope/tasks/batches/<任务配置文件名去掉 .json>` |
| `harness` | 当前工具名；各工具配置指定 `claude-code`、`codex` 或 `kimi-code`，共享默认值为 null |
| `model_aliases` | 按 harness 分层的逻辑模型名 → 调度名映射 |
| `worker` | 覆盖逻辑模型名或推理设置 |
| `topics` | 覆盖本次可选主题列表 |
| `threshold` | 覆盖低回译置信度复核阈值 |
| `expectation_context` | 补充预期材料路径；null 表示仅使用可用的 Blueprint 参考 |
| `collection` | 执行后的收集设置 |

三种范围选择取并集。模型、主题、阈值等实际值以合并后的配置为准。

默认逻辑模型为 `luna6`，共享别名表为：

```json
{"model_aliases": {"claude-code": {"luna6": "sonnet"}}}
```

准备时仅查找当前 `harness` 下的 `worker.model`，直接解析一次，不递归追踪别名；没有对应项时使用原名。因此 Claude Code 默认调度 `sonnet`，Codex/Kimi 仍调度 `luna6`。任务可逐项覆盖或新增 harness/模型映射；harness 和模型键、调度名须为非空字符串，每个 harness 的映射须为对象。其他配置字段仍拒绝未知键。若需直通，可将逻辑模型映射为自身。

```sh
python3 skills/scripts/prepare.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
```

准备脚本保存完整合并结果为批次的 `task-config.json`，保留原始逻辑模型、harness 及别名表；供调度使用的 `agent-config.json.worker.model` 与 `manifest.run.model` 冻结为已解析调度名。新 manifest 为 `formaliscope-enrichment-batch.v3`，显式记录 `result_protocol=formaliscope-stage-results.v1` 与 harness；最终收集产物仍为 `statement-enrichment.v2`。输出回执返回批次路径、运行 ID、选中数量和调度名。第一阶段交付封存摘要后才提供该批参考文件进入第二阶段。后续默认值或别名修改不改变已准备批次；换路由须准备新批次。

## 阶段交付与不可变基线

调度层给每组分配精确 ID 和唯一新 draft/result 路径，先确定第二阶段路径（Codex/Kimi 无材料时为 null）。第一阶段只输出 [回译契约](../statement_workflow/schema/statement-readback-batch.v1.schema.json) 的五个字段；第二阶段只输出 [判断契约](../statement_workflow/schema/statement-expectation-batch.v1.schema.json) 的 ID 和判断。未知字段直接拒绝，不让模型复制第一阶段内容。

以单声明组为例，先保存第一阶段草稿，再调用固定程序；多声明组重复 `--declaration-id`：

```bash
python3 skills/scripts/collect.py --deliver-readback --snapshot <batch>/snapshot.json --manifest <batch>/manifest.json --input <new-draft.json> --result <group-1-readback.json> --declaration-id <精确ID> --next-result <group-1.json>
```

程序校验后排他创建第一阶段文件及同路径追加 `.baseline.json` 的记录，包含文件字节 SHA-256、manifest SHA-256、run、来源、组 ID 和第二阶段分配路径。无材料且按 harness 跳过时省略 `--next-result`。这一步不读取预期材料内容。摘要必须由程序计算，不采用模型自报；已有正式文件或记录不覆盖、不重新封存。

第二阶段先运行 `collect.py --check-readback --snapshot ... --manifest ... --readback-result ...`，成功后只读基线和固定预期材料，再调用 `--deliver-expectation --snapshot ... --manifest ... --readback-result ... --input <new-draft.json> --result <allocated-path>`。程序再次检查摘要、字段和组集合并排他写出。Claude Workflow 无直接文件系统接口，阶段 Agent 负责调用固定交付命令；文件回执不是执行模型或内容可信的证明，收集时仍独立重查。

摘要只是篡改检测，不是文件系统写权限隔离；能同时改写结果和摘要记录的进程不在此机制的防护范围。未新增权限配置。原始阶段文件、草稿和基线记录保留，错误不能靠主 Agent 临时拼接、改正文或更新摘要绕过。需要重做时分配全新组输出路径。

执行结束后，依据各组文件回执与实际调度记录补充同一任务文件的 `collection`：

```json
{
  "results": [".formaliscope/tasks/batches/20261008-150000-tower/group-1.json"],
  "readback_results": [".formaliscope/tasks/batches/20261008-150000-tower/group-1-readback.json"],
  "executed_model": "实际调度记录确认的路由 ID"
}
```

上面的对象写入任务配置的 `collection` 字段。新协议始终填写全部组的 `readback_results`，收集器按固定命名读取对应 `.baseline.json`。Claude Code 每条声明固定两个独立 Agent，无论有无材料都填写第二阶段 `results`，各文件恰好一项。Codex/Kimi 有材料时也填写第二阶段 `results`；无材料时为 `results=[]`，由程序生成内部 `undetermined`、跳过理由及固定判断分值 1.0（仅表示缺少材料的确定性，不是模型数学判断）。

收集器要求两阶段精确 ID、每组条数、分配路径和总目标完全匹配，核对原始第一阶段摘要，按 ID 使用第一阶段全部字段与第二阶段判断构造完整 annotation；非法字段、缺失、重复、冲突或摘要变化均报错，所有输入通过才写新的收集目录。

`executed_model` 仅在核实实际路由后填写已确认的调度名，并须等于冻结的 `manifest.run.model`。分别核对并私密保留请求模型、启动元数据和响应记录模型；它们可能采用不同命名，必须确认路由关系，不能仅从别名、请求值或 Agent 自报推断实际执行成功。记录不符或无法确认时停止，不填该字段或放宽收集校验。通过同一配置收集：

```sh
python3 skills/scripts/collect.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
```

收集器使用已准备批次的快照和模型记录，默认输出到该批次的 `collected/`。复核后，将复核文件路径列表写入 `collection.reviews`，在 `collection.output` 指定新的私密输出目录，再次收集。历史 manifest v1/v2 和原命令参数保持明确兼容，历史完整 `formaliscope-agent-batch.v2` 仍走旧路径，不要求补封摘要；新 v3 不接受历史完整结果或按字段猜测格式，不转换或改写历史批次。
