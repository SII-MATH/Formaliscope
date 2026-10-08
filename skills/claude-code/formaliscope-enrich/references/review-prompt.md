# 主 Agent：低回译置信度复核

由当前 Claude Code 主 Agent 在完整收集后执行，复核模型从实际会话或调度记录确认。

调用者提供仓库路径、固定快照、收集器的 `review-queue.json`、主题和新的复核路径。队列按原始 `readback.confidence < threshold` 生成；正文 null 单列生成失败。

阅读 `statement_workflow/SCHEMA_V2.md`，依据固定 Lean 和必要定义核对对象、假设、量词、结构约束及逻辑方向，确认或修正有依据的问题。处理队列中可可靠解释的条目，其他条目保留待复核状态。

保存 `formaliscope-enrichment-review.v2`，每条最多一项：

```json
{
  "schema": "formaliscope-enrichment-review.v2",
  "reviews": [
    {
      "declaration_id": "statement::Example.value",
      "annotation": {},
      "model": "实际复核模型",
      "reviewed_at": "带时区的实际复核时间"
    }
  ]
}
```

`annotation` 是完整确认或修订的 worker 条目，保持 ID、原始两个分值和完整 `expectation_assessment`。以 0600 保存到分配的新文件，保留原始结果。实际复核模型和时间单独记录，收集器依据原分值决定路由。汇报已复核和待复核数量，标明产物为机器草稿。
