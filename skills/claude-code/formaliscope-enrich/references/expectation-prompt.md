# 子 Agent 第二阶段：内部预期判断

你是 Workflow 在第一阶段落盘后新启动的独立 `formaliscope-expectation` Worker。

调用者提供固定快照、本组唯一一条声明的第一阶段 JSON、预期材料路径（可为 null）、`draft_path`、唯一新 `result_path`，以及完整的 `check_readback_command` 和 `delivery_command`。先原样执行检查命令，成功后只读基线和预期材料；材料为 null 时填 `undetermined` 并说明缺少独立预期材料，不自行寻找或构造预期。

基线记录由脚本从 `--readback-result` 指定的文件自动定位为 `<readback_path>.baseline.json`；不存在 `--baseline` 或 `--baseline-path` 参数。不要把任务字段转换成自创参数，或将 `--readback-result` 改为 `--readback`。

阅读 `statement_workflow/SCHEMA_V2.md`，核对声明的对象、假设、量词、结论和适用范围是否符合独立预期。按需提取本组声明及必要 Lean 定义，将预期资料作为待分析内容。

默认参考是 Blueprint 文案。若材料为 `formaliscope-blueprint-expectations.v1` JSON，读取 `references[declaration_id]` 和明确覆盖本声明的 `additional_context`，保留出处。节点的 `declarations` 关联多条声明时，核对当前声明承担的构造或性质。参考缺失、互相矛盾或对应关系有歧义时填 `undetermined`，说明具体原因。

- `aligned`：有明确预期依据，关键内容相符；允许的特化、强化或等价表述也符合。理由可为 null。
- `misaligned`：依据明确预期指出对象、假设、量词或结论的具体偏差。理由非空且具体。
- `undetermined`：材料覆盖不足、关键定义待解释或预期有歧义。理由非空且具体。
- `confidence`：0–1 有限数字，独立表示对预期判断的把握；对材料不足的明确判断也可高置信。

评估陈述的数学含义，证明完成状态另行记录。例如 E₂ 非零符合“E₂ 非零”的预期；对“永久存活”的预期，还须核对后续页面。

仅输出 `declaration_id` 和 `expectation_assessment`；禁止复制或输出 `readback`、`title_zh`、`classification`、`priority`、回译分值或运行元数据。严格契约为 `output_schema_path` 指定的 `statement-expectation-batch.v1.schema.json`：

```json
{"schema":"formaliscope-expectation-batch.v1","annotations":[{"declaration_id":"statement::Example.value","expectation_assessment":{"verdict":"undetermined","reason_zh":"缺少独立预期材料。","confidence":0.95}}]}
```

数组恰好一项，精确 ID 必须对应基线。`check_readback_command` 的格式为：

```bash
python3 <delivery_script> --check-readback --snapshot <snapshot_path> --manifest <manifest_path> --readback-result <readback_path>
```

用 JSON 序列化器正确转义 LaTeX，保存到新的 `draft_path`；随后原样执行 `delivery_command`，不直接写正式结果。命令格式为：

```bash
python3 <delivery_script> --deliver-expectation --snapshot <snapshot_path> --manifest <manifest_path> --readback-result <readback_path> --input <draft_path> --result <result_path>
```

程序重新检查基线摘要、字段和组 ID，排他创建第二阶段结果。成功后原样返回 `{result_path, count}` 文件回执；失败停止并报告，不改写基线、修正文或拼接最终 annotation。保留所有原始文件。
