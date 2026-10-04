# Statement 补充数据契约与导入

`statement_workflow/schema/` 保存 Formaliscope 的声明补充数据契约。当前生成入口是 [仓库 Skill `$formaliscope-enrich`](../.agents/skills/formaliscope-enrich/SKILL.md)，操作步骤、子 Agent prompt、复核 prompt 和收集脚本统一在该 Skill 中维护。

[字段与填写标准 v2](SCHEMA_V2.md) 已于 2026-10-04 确认定稿，包含精简 Agent 字段、七类角色、项目主题配置、双置信度和仅供内部使用的预期判断。v2 尚未接入运行实现；下文描述的 Skill、JSON Schema、校验和导入命令仍使用 v1，旧试跑批次也继续按 v1 校验。

## 当前流程

固定源码快照与选定范围 → 按配置调用子 Agent 分组生成中文回译及字段 → 脚本校验 → 低置信度主 Agent 复核 → 合并 enrichment → 生成候选快照 → 显式安装 → 用户审阅。

模型和推理等级从 [config.json](../.agents/skills/formaliscope-enrich/config.json) 读取，prompt 不固定模型。每批保存配置副本，续做时沿用该副本。子 Agent 只依据冻结快照中的 Lean 代码回译，不使用已有中文正文、Blueprint 或人的判断来推断含义。

语义复核仅由子 Agent 自报的 `confidence < threshold` 触发，默认阈值为 `0.8`；分值等于阈值时直接汇总。角色、优先度、依赖数量、复杂度、`unresolved` 和抽样均不参与路由。格式、源码和证据校验仍覆盖每条结果。

置信度存在 `formaliscope-agent-batch.v1` 外层，主 Agent 复核记录另存 `formaliscope-enrichment-review.v1`，不改变应用的 enrichment v1。收集脚本兼容旧的 `formaliscope-luna-batch.v1` 输入，新输出统一使用模型无关的格式名。原始结果、分值和实际模型来源保留；主 Agent 复核仍是机器处理，不等于人工已审阅。

## 审阅范围

6,222 是示例源码快照的索引数量，不是自动生成任务或人工必审数量。只为用户选定的目录、文件或声明产出 annotation；按需查阅的定义和实例用于上下文，不自动成为补全或复核目标。

按文件或相关数学主题分组，各组目标去重，结果逐声明保存。共享上下文时仍以每条声明本身为准，不能从邻近声明补出它没有的前提或结论。源码引用候选图可能遗漏字段、实例或混入证明引用；没有定位到引用不能解释为没有语义依赖。

## 补充字段

[statement-enrichment.v1.schema.json](schema/statement-enrichment.v1.schema.json) 是 JSON Schema 2020-12 交换契约。Agent 不改写提取的 Lean 名称、种类、位置和源码，展示仍以 Lean 短名为主标题。

| 字段 | 含义 |
| --- | --- |
| `declaration_id` / `basis` | 关联条目，绑定源码提交、基础快照摘要和完整声明 SHA-256；当前 `context_fingerprint` 必须为 null。 |
| `title_zh` | 中文阅读副标题；未填写为 null，不拿 Lean 名称假充中文。 |
| `summary_zh` | 中文阅读摘要，不能自动作为语义回译。 |
| `readback` | 中文数学陈述、LaTeX、证据和未解释对象；只能为 none 或 draft。 |
| `classification` | 候选数学角色和主题，附中文理由及证据；角色允许 unclassified，主题允许空数组。 |
| `priority` | p0 / p1 / p2 或 null；非空时必须有理由和证据，null 不代表低优先度。 |
| `evidence` | 实际读取的源码片段、文件及行号；其他 evidence_ids 只引用本条证据列表。 |
| `provenance` | 实际生成模型、时间、政策版本及上下文完整程度；method 为 agent_manual。 |
| 人工审阅状态 | 保存在 review_app 的个人数据库，由当前身份与版本匹配判断派生，不属于 Agent 输出。 |

数学角色：model、literature、computed、target、transport、derivation、challenge、infrastructure、unclassified。主题：spectral、adams、sphere、comparison。关注程度使用优先度，不增加风险标签。

## 校验与候选快照

Skill 的 `collect.py` 校验目标集合、分值、源码及证据，生成 `enrichment.json`、`review-queue.json` 和 `report.json`。高置信度和已完成主 Agent 复核的条目进入 enrichment；未复核的低分条目保留在队列。批次产物放在被 Git 忽略的 `.statement-enrichment/`。

应用导入器核对 source_commit / snapshot_digest / source_sha256、声明与证据 ID 的唯一性、引用有效性，以及证据原文和行号与冻结源码一致。基础快照摘要绑定所有源码模块，改变上下文后旧旁文件不能重新导入。

在仓库根目录运行：

```sh
python3 -m review_app validate-enrichment --snapshot /path/to/snapshot.json --file /path/to/enrichment.json
python3 -m review_app enrich-snapshot --snapshot /path/to/snapshot.json --file /path/to/enrichment.json --output /path/to/new-candidate-snapshot.json
```

两个命令不改数据库、不覆盖输入，第二个拒绝已有输出路径。候选快照按 [部署流程](../review_app/DEPLOYMENT.md) 备份、安装并重启服务后生效。Skill 不自动安装或部署。

对同一基础快照进行 enrichment 时，回译正文变化才改变相应的审阅依据指纹，旧判断留在历史。中文副标题或标签变化不会清空已审条目；未补回译时保留现有正文及出处，阅读摘要独立展示。更新 Lean 源码时按新快照的内容指纹判断有效性。

## 当前边界

- 当前上下文来自源码候选，尚未接入 Lean 语义导出器；导入要求 `context_fingerprint=null`，上下文完整度只接受 unknown / partial，拒绝 complete。
- Agent 不输出 verified 或人的 verdict，也不把阅读摘要升级成回译草稿；无法解释的对象保留 unresolved。
- 批次由 Codex 会话发起，网页服务负责消费候选快照，不在服务器后台自动生成回译。
- 协议与导入回归使用合成夹具；真实数学标注及页面展示仍需按实际批次验收。

## 兼容实验接口

`engine.py`、`__main__.py`、`workflow.json` 和 `prompts/` 保留早期 `python3 -m statement_workflow` 实验接口，其协议见 [旧执行器契约](AGENT.md)。它按单声明执行 readback / audit，支持任务导出、结果导入、缓存和重试；当前 Skill 不调用这套接口。

旧接口默认 `agent_command=null`，没有模型适配器；输出 `statement-analysis.v1`，尚未转换为应用消费的 enrichment。其双阶段审计和旧分级不作为当前 Skill 的要求。

## 验证

```sh
python3 -m unittest discover -s statement_workflow -t . -p 'test_*.py'
python3 -m unittest review_app.test_enrichment
```

测试覆盖批次完整性、阈值边界、实际模型来源、非法分值、证据绑定、原分值保留、候选快照与人工记录隔离，以及旧实验接口兼容性。
