# 主 Agent：低回译置信度复核

由当前 Codex 主 Agent 执行，只复核收集器队列。模型记录取当前会话实际模型。

调用者提供仓库根目录、冻结快照、脚本生成的 `review-queue.json`、主题配置和新的复核结果路径。

只处理队列声明。唯一触发原因是原始 `readback.confidence < threshold`；不选择高分条目，不追加重要性、分类、预期判断、判断分值或抽样规则。正文 null 的生成失败另行记录，不以虚构正文结束任务。

根据冻结 Lean 声明和必要定义，核对回译是否遗漏对象、假设、量词及结构约束，改变逻辑方向或扩大结论。确认或修正有依据的问题。不要使用预期材料或人的结果反向改写忠实回译。

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

`annotation` 为完整确认/修订的 worker 条目。保持 ID、原始 `readback.confidence` 和整个 `expectation_assessment` 不变，不提高原分值、不重新做内部预期判断。若仍无法可靠回译，不把该条加入 reviews，继续待复核。复核不是人的审阅，不输出 verified。

只写分配的新复核文件，0600 权限，不修改原始结果。实际复核模型及时间单独记录；worker 不填写 provenance。收集器仍依据原始分值决定路由。
