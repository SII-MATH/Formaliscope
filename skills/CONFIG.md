# 回译任务配置

Agent 开始任务前，起草一份独立 JSON 配置，保存到 `.statement-enrichment/task-configs/YYYYMMDD-HHMMSS-任务名.json`。时间取本地时间，任务名使用简短英文，例如 `20261008-150000-tower.json`；同名任务加序号。

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
| `output` | 批次目录；省略时取 `.statement-enrichment/<任务配置文件名去掉 .json>` |
| `worker` | 覆盖模型路由或推理设置 |
| `topics` | 覆盖本次可选主题列表 |
| `threshold` | 覆盖低回译置信度复核阈值 |
| `expectation_context` | 补充预期材料路径；null 表示仅使用可用的 Blueprint 参考 |
| `collection` | 执行后的收集设置 |

三种范围选择取并集。模型、主题、阈值等实际值以合并后的配置为准。

```sh
python3 skills/scripts/prepare.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

准备脚本保存完整合并结果为批次的 `task-config.json`，供调度使用的模型和主题为 `agent-config.json`。输出回执返回批次路径、运行 ID 和选中数量。第一阶段完成后提供该批参考文件进入第二阶段。

执行结束后，依据各组文件回执与实际调度记录补充同一任务文件的 `collection`：

```json
{
  "results": [".statement-enrichment/20261008-150000-tower/group-1.json"],
  "readback_results": [".statement-enrichment/20261008-150000-tower/group-1-readback.json"],
  "executed_model": "实际调度记录确认的路由 ID"
}
```

上面的对象写入任务配置的 `collection` 字段。有预期时提供各组 `readback_results`，无预期时 `results` 指向第一阶段文件。`executed_model` 在核实执行后填写。通过同一配置收集：

```sh
python3 skills/scripts/collect.py --config .statement-enrichment/task-configs/20261008-150000-tower.json
```

收集器使用已准备批次的快照和模型记录，默认输出到该批次的 `collected/`。复核后，将复核文件路径列表写入 `collection.reviews`，在 `collection.output` 指定新的私密输出目录，再次收集。旧批次和原命令参数保持兼容。
