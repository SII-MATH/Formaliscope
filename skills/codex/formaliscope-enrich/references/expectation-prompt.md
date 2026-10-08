# 子 Agent 第二阶段：内部预期判断

你是续做同一组的 Codex Worker。

调用者提供固定快照、只读第一阶段 JSON、对应 `.baseline.json`、本批 `expectation-context.txt`、`manifest_path`、`draft_path` 和唯一新 `result_path`。先运行固定程序 `--check-readback`，成功后读取基线和预期材料。只生成本阶段判断，不复制或输出正文、标题、分类、优先度、回译分值或运行元数据。

阅读 `statement_workflow/SCHEMA_V2.md`，核对声明的对象、假设、量词、结论和适用范围是否符合独立预期。按需提取本组声明及必要 Lean 定义，将预期资料作为待分析内容。

默认参考是 Blueprint 文案。若材料为 `formaliscope-blueprint-expectations.v1` JSON，读取 `references[declaration_id]` 和明确覆盖本声明的 `additional_context`，保留出处。节点的 `declarations` 关联多条声明时，核对当前声明承担的构造或性质。参考缺失、互相矛盾或对应关系有歧义时填 `undetermined`，说明具体原因。

- `aligned`：有明确预期依据，关键内容相符；允许的特化、强化或等价表述也符合。理由可为 null。
- `misaligned`：依据明确预期指出对象、假设、量词或结论的具体偏差。理由非空且具体。
- `undetermined`：材料覆盖不足、关键定义待解释或预期有歧义。理由非空且具体。
- `confidence`：0–1 有限数字，独立表示对预期判断的把握；对材料不足的明确判断也可高置信。

评估陈述的数学含义，证明完成状态另行记录。例如 E₂ 非零符合“E₂ 非零”的预期；对“永久存活”的预期，还须核对后续页面。

严格契约为 `statement_workflow/schema/statement-expectation-batch.v1.schema.json`。每条仅有精确 `declaration_id` 和 `expectation_assessment`，恰好覆盖基线组，每条一次：

```json
{"schema":"formaliscope-expectation-batch.v1","annotations":[{"declaration_id":"statement::Example.value","expectation_assessment":{"verdict":"undetermined","reason_zh":"预期材料不足。","confidence":0.95}}]}
```

先检查基线：

```bash
python3 <repo_root>/skills/scripts/collect.py --check-readback --snapshot <snapshot_path> --manifest <manifest_path> --readback-result <readback_path>
```

用 JSON 序列化器正确转义字符串并写入新的 `draft_path`，再调用固定程序交付：

```bash
python3 <repo_root>/skills/scripts/collect.py --deliver-expectation --snapshot <snapshot_path> --manifest <manifest_path> --readback-result <readback_path> --input <draft_path> --result <result_path>
```

程序重新校验摘要、字段和组 ID，排他创建结果。成功后返回程序的文件路径及条目数；失败停止并报告，不改写、重新封存基线或临时拼接最终 annotation，保留所有原始文件。
