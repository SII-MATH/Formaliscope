# 主 Agent 低置信度复核 prompt

调用者提供仓库根目录、冻结快照、脚本生成的 `review-queue.json` 和一个新的复核结果路径。

只处理队列中的声明。它们进入队列的唯一原因是原始子 Agent 自报置信度低于本批阈值；不额外选择高置信度条目，不追加重要性、复杂度或抽样规则。

根据冻结快照中的 Lean 声明和必要定义，核对中文回译是否遗漏变量或前提、改变量词及逻辑方向、误解定义或扩大结论。以证据为依据，修正有依据的问题；无法解释的部分保留 `unresolved`，不要强行写成完整结论。没有明确指定的参考资料时，不推断论文或作者意图。

逐条确认或修订完整 `statement-enrichment.v1` annotation。保持 `declaration_id` 和 `basis` 与原始结果一致。有回译的正文仍是 `draft`；仍无法回译时可以保留合法的 `none`，不为结束任务虚构正文。不写人的 verdict 或 verified。未改 annotation 时保留原生成来源；改写时在 annotation 的 `provenance` 记录实际改写模型与时间。复核者本身的实际模型和时间另记在以下 review 行中。

保存独立 JSON 文件：

```json
{
  "schema": "formaliscope-enrichment-review.v1",
  "reviews": []
}
```

每个 `reviews` 项包含：

- `declaration_id`：队列里的精确 ID。
- `annotation`：确认或修订后的完整 annotation。
- `model`：实际执行此次复核的模型。
- `reviewed_at`：带时区的实际复核时间。

每条最多一项。尚未完成的条目不写入 reviews，脚本会继续把它列为待复核。不得修改原始子 Agent 文件或原始 confidence；下一次收集仍使用原始分值决定路由。
