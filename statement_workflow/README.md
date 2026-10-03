# Statement 审阅 Agent Workflow

现有 Formaliscope 仓库的独立模块，与 `review_app/` 并列。Python 3.10+ 标准库即可运行。前端与工作流通过快照和分析 JSON 交换数据，工作流不依赖页面服务、不写人工审阅数据库、不改 KIP126 源码。

流程：固定快照 → 独立上下文包 → Agent 盲读回译 → 新上下文审计 → 规则分级 → analysis.json。

## 当前选择：手动 Agent 补全与统一 schema

当前先由协作 Agent 按用户指定的目录或条目分批读取源码、补字段、保存结果，不要求部署模型执行器或启动全库任务。自动 Workflow 保留为后续扩展。

交换契约 [statement-enrichment.v1.schema.json](schema/statement-enrichment.v1.schema.json)（JSON Schema 2020-12）保持已确认的 v1：手动 Agent 的补充数据按 declaration_id 与冻结快照关联；现有人工数据库和 analysis.v1 不迁移。已提供校验、新快照生成及前端消费；只有显式安装生成的候选快照才改变线上展示。

| 字段 | 含义与填写方 |
| --- | --- |
| 现有快照的 id / declaration / kind / module_file / lean | 提取器产生的 Lean 名称、种类、位置和源码；Agent 不改写。展示统一以 Lean 短名为主标题，title_zh 为可选副标题。 |
| declaration_id / basis | 关联条目，绑定源码提交、快照摘要和完整声明 SHA-256；context_fingerprint 可为 null，此时没有经验证的语义上下文缓存绑定。 |
| title_zh | Agent 补中文阅读标题；未填写为 null，不拿原始 Lean 名称假充中文。 |
| summary_zh | Agent 的阅读摘要；不等同于语义回译，不能自动放入 readback。 |
| readback | 只据 Lean 上下文生成的中文陈述，含 LaTeX、证据引用和无法解释的对象。Agent 只能填 none 或 draft；没有上下文时保持 none，草稿有缺口时列 unresolved。 |
| classification | 一个候选数学角色和多个候选主题，附中文理由及证据；使用当前标签 ID 的末段。角色允许 unclassified，主题允许空数组。 |
| priority | p0 / p1 / p2 或 null；有分级就必须有理由和证据。null 表示尚未分级，不能自动当作低优先度。 |
| evidence | 此次实际读取的源码片段及文件行号；其他 evidence_ids 只引用本列表。 |
| provenance | agent_manual、实际模型、生成时间、政策版本、上下文完整程度；记录此次是谁、基于什么生成。 |
| 人工审阅状态 | 从当前用户、当前审阅依据的有效判断派生；继续保存于 review_app，不属于 Agent 输出字段。 |

数学角色：model、literature、computed、target、transport、derivation、challenge、infrastructure、unclassified。主题：spectral、adams、sphere、comparison。关注程度统一使用优先度，不增加风险标签。

一次手动补全流程：选范围 → 固定快照与源码 → 仅从 Lean 生成回译 → 按目录/上下文补分类与优先度 → 校验并保存旁文件 → 通过导入适配器展示 → 用户审阅。原文/已有中文标题仅能用于后续对照，不能带入独立回译输入。结构是逐条输出，执行可以按相关主题分批，不需要创建 6,000 个子 Agent。

导入器核对 source_commit / snapshot_digest / source_sha256 与冻结快照一致、declaration_id 唯一、证据 ID 唯一及引用有效、行号范围和实际片段一致。完整 snapshot_digest 绑定所有源码模块，改变上下文后旧旁文件不能重新导入。仅 source_sha256 无法证明依赖未变。本版没有经 Lean 验证的上下文适配器，因此 context_fingerprint 必须为 null，context_completeness 只允许 unknown / partial；complete 在 schema 中保留给后续适配器，目前导入拒绝。

```sh
python3 -m review_app validate-enrichment --snapshot .statement-review/snapshot.json --file /path/to/enrichment.json
python3 -m review_app enrich-snapshot --snapshot .statement-review/snapshot.json --file /path/to/enrichment.json --output /path/to/candidate-snapshot.json
```

两条命令均不改数据库，不覆盖输入，第二条拒绝已有输出路径。候选新快照通过 [部署流程](../review_app/DEPLOYMENT.md) 的备份/安装/重启步骤应用。只有新回译正文改变时，相关内容指纹变化，旧判断继续留在历史；仅中文副标题或标签改变不会把已审条目清空。未补回译时保留现有有效正文和出处，阅读摘要始终独立展示。priority.level=null 对应未分级，不自动贴 P2。现有 Workflow 的 analysis.v1 尚未自动转换为本格式。

本版不由 Agent 输出 verified、人的 verdict 或自动确认数学正确；回译已核验须有单独的人工核验记录。禁止将未回译的阅读摘要升级成回译草稿。

默认不配置模型，也不自动对全库调用。已实现任务导出/结果导入、命令适配器调用、超时、分阶段失败和重试上限、验证、缓存和当前产物导出。每次 run 默认最多处理一条需要执行 Agent 的声明，已完成的条目不会占用本次额度。

审阅范围和任务粒度的最新方向见 [范围修订](design/REVIEW_SCOPE.md)：6,222 是源码索引，正式审阅应围绕 M/A(M)/C(M)/T(M) 的数学边界建立集合，Agent 按相关主题组工作。当前骨架仍按单声明执行，主题组支持尚待实现。

## 目录

```text
statement_workflow/
  workflow.json             流程参数及 Agent 执行入口
  AGENT.md                  Agent 输入、输出和上下文隔离契约
  engine.py                 任务、缓存、验证、分级和产物
  __main__.py               命令入口
  prompts/readback.txt      盲读回译规则
  prompts/audit.txt         覆盖与意图对照规则
  design/                   完整管线设计与后续分级政策草案
  test_workflow.py           协议及恢复回归测试
```

## 用现有 Agent 执行导出的任务

在仓库根目录运行，snapshot.json 使用当前 Statement 构建产物：

```sh
python3 -m statement_workflow prepare --snapshot .statement-review/snapshot.json --output .statement-workflow/pilot
python3 -m statement_workflow run --output .statement-workflow/pilot --export-only
```

未指定声明时只选择主目标和它的直接候选引用。可重复传 `--declaration <完整 Lean 名称或 statement ID>` 精确选择条目。无主目标时必须指定声明。

批次的 `tasks/<id>/readback.job.json` 是 Agent 要接收的完整任务。由一个新的 Agent 会话处理，保存符合任务 instructions 的 JSON 结果，再导入：

```sh
python3 -m statement_workflow import-result --output .statement-workflow/pilot --stage readback --result /path/to/readback-result.json
```

导入成功会生成同目录的 `audit.job.json`，交给另一个全新上下文的 Agent 会话：

```sh
python3 -m statement_workflow import-result --output .statement-workflow/pilot --stage audit --result /path/to/audit-result.json
```

两个阶段结果有效后，在条目目录和批次根目录生成 `analysis.json`。批次根目录只导出当前任务和当前执行器版本的完整结果；消费端应读这里，避免误读条目目录中保留的旧历史文件。

可选 `prepare --references /path/to/references.json`：映射 statement ID 到 `{source_sha256,text,source}`。指纹必须绑定当前声明完整源码，内容和出处不能为空。reference 只进入 audit，不进入 readback。

## 接入自动 Agent 执行器

复制 workflow.json 为本地配置，设置：

```json
{
  "agent_command": ["/absolute/path/to/python3", "/absolute/path/to/agent_adapter.py"],
  "agent_revision": "provider-model-prompt-settings.v1"
}
```

上面是应替换的两个字段，其余配置字段保留，不是完整配置。适配器需按 AGENT.md 实现 stdin/stdout 协议，内部启动全新的 Agent 会话，保证 stdout 只有 JSON。命令作为 argv 执行，不经过 shell；运行目录是 statement_workflow。适配器可在原有 Agent 工具之上包装，不要求另建服务或仓库。

```sh
python3 -m statement_workflow --config /path/to/local-workflow.json run --output .statement-workflow/pilot --limit 1
```

重复运行会恢复缺失或失败阶段；单阶段最多尝试三次，之后继续其他条目，不反复消耗。更新执行器实现、模型或设置时必须更新 agent_revision，它会使旧阶段缓存失效。reference/上下文/prompt 改变也会使相关任务 ID 改变；流程策略变化要求准备新批次。

prepare 不覆盖已有批次。要分析新源码提交，准备另一个批次目录。目前不同批次间尚未共用生成缓存，避免把设计中更细的缓存优化误认为已经实现。

## 当前实现边界

- 输入是现有 Statement 源码快照；现有候选引用图可能漏字段投影、实例或包含证明引用。每个上下文明确标记 unknown，不冒充 Lean 编译环境。
- expected_toolchain 默认固定为当前 KIP126 的 Lean 4.32.2，纳入任务指纹。这是工作流配置的预期版本，当前适配器不调用 Lean 验证它；正式提取器还需记录实际工具链及验证结果。
- 去除注释，剥离 theorem/lemma 的证明体；定义保留实际实现和字符串。不能可靠剥离的陈述直接拒绝，避免把原始意图带入盲读任务。此定位方法仍不是 Lean parser。
- 骨架分级只给 P0 主目标、P1 直接候选、P2 其他选中条目。源码上下文未核验时危险度为 unknown；有证据的 Agent 疑点为 R2，仍需人复核。模型自称确认不会自动升级为 R3。
- `design/triage-policy-proposed.json` 是下一阶段完整政策，尚未由当前分级引擎完整实现。R1/R3、目标传递影响路径、实际字段实例化与 Lean 核验适配器仍待实现。
- 没有模型或 Agent 适配器默认实现，本次没有进行真实回译调用。测试使用明确标记的进程协议夹具，不代表数学效果。
- 分析产物已可独立生成；自动接入前端展示和人工重审指纹尚待实现，当前网页仍使用原有预览数据。

## 验证

```sh
python3 -m unittest statement_workflow.test_workflow
```

测试覆盖任务隔离、上下文变化、失败后恢复、已缓存条目不占额度、执行器版本失效、旧结果不进入当前导出、证据与身份验证、缺参考时拒绝符合原意结论、源码绑定参考和人工结果隔离。

Prove2Me 的公开流程将逐项回译交给独立子 Agent，再写回 readback/readback_model，供人逐项比较；没有规定必须逐条串行执行。参见 [Captain read-backs](https://github.com/prove2me/prove2me_workspace/blob/main/references/mission_captain.md#read-backs-independent-testimony-for-the-audit) 与 [Auditor](https://github.com/prove2me/prove2me_workspace/blob/main/references/mission_auditor.md)。这里只借鉴任务独立性，不依赖其平台。
