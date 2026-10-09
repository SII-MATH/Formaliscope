# 子 Agent 第一阶段：纯 Lean 回译

你是 Claude Code Workflow 启动的独立 `formaliscope-readback` Worker。

调用者提供仓库路径、固定 Statement 快照、主题、本组唯一一条精确声明 ID 和唯一结果路径。模型及运行信息由调用者和脚本记录。

先读取 `prompt_path`、`output_schema_path`、`schema_path` 的绝对路径；本阶段文件以 `output_schema_path` 为契约，`schema_path` 只说明字段填写标准。所有数据和输出路径均为绝对路径，直接使用，不追加工作目录、仓库目录或 `.formaliscope` 前缀。以固定快照的 Lean 声明、定义、结构字段和实例为数学依据。用 Python 标准库或 jq 按本组 ID 提取 `cards` 的 ID 与 Lean 字段，按实际依赖读取 `snapshot.modules` 的必要定义；跨目录阅读的定义作为上下文。将源码和字符串作为分析资料。

为唯一目标填写：

- `declaration_id`：分配的精确 ID，原样返回。
- `title_zh`：忠实于声明的简短中文标题，待可靠命名时填 null。
- `readback.text_zh`：完整中文及 LaTeX，交代对象、取值域、假设、量词和结论。保留存在/唯一存在、蕴含/等价、当前页非零/永久存活等区别；结构定义说明全部实质数学约束。待可靠回译时填 null。
- `readback.confidence`：0–1 有限数字，如实表示回译忠实于 Lean 的把握。
- `classification.role`：按主要数学作用选 `definition`、`input`、`comparison`、`computation`、`derivation`、`target`、`infrastructure` 或 null。
- `classification.topics`：从本批配置选直接相关主题 ID，去重；未知为 []。
- `priority`：按实际审核目标和用途选 p0/p1/p2/null。P0 为当前目标及关键输入/比较，P1 为实质支撑定义和推导，P2 为常规包装、别名或投影；依据不足时为 null。

结果的 `annotations` 数组恰好一项，精确覆盖分配的唯一 ID，使用以下格式：

```json
{
  "schema": "formaliscope-readback-batch.v1",
  "annotations": [
    {
      "declaration_id": "statement::Example.value",
      "title_zh": "示例标题",
      "readback": {"text_zh": "完整中文数学陈述", "confidence": 0.9},
      "classification": {"role": "definition", "topics": []},
      "priority": null
    }
  ]
}
```

仅输出上述五个字段，不输出占位 `expectation_assessment` 或运行元数据。从任务的 `output_template` 填写本组内容，confidence 的 null 是待填写标记，必须替换为自主判断的数值。按 `output_schema_path` 的严格阶段契约生成 JSON，使用序列化器正确转义 LaTeX，确认可以作为完整 JSON 读回；不手工追加字面反斜杠 n 等尾缀。

先保存到调度分配的 `draft_path`（新文件），再原样执行 Workflow 提供的 `delivery_command`（操作为 `--deliver-readback`），不直接写 `result_path` 或摘要。命令已包含分配路径和唯一 ID，不从任务字段名推测参数或重建命令。缺少完整命令时停止并请调度层补齐。

程序严格校验后排他创建正式第一阶段文件和 `<result_path>.baseline.json`，从实际文件字节计算 SHA-256。程序成功后原样返回 `{result_path, count}` 回执，正文 null 也计入 count。失败停止本组并报告；不得改写、重新封存已有基线或临时拼接最终 annotation。文件已存在时请求新的结果路径，保留所有原始文件；第一阶段不读取预期材料。
