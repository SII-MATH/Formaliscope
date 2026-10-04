---
name: formaliscope-enrich
description: "在 Formaliscope 中按指定目录或声明调用 Luna 子 Agent，生成中文 Lean 回译、阅读标题、标签与优先度；仅按 Luna 自报置信度安排主 Agent 复核，输出可校验的补充数据与候选快照。用于补全审阅数据，不用于修改 Lean 证明或部署服务。"
---

# Formaliscope 中文回译与字段补全

在现有 Codex 会话内执行。调度由主 Agent 发起，回译与字段填写交给 `gpt-6-luna` 子 Agent；机械校验和合并交给脚本。第一版无需模型 API、独立服务或全库执行器。

## 选择范围与准备批次

定位包含 `review_app/` 的仓库根目录，遵守其 `AGENTS.md`。以用户指定的冻结 Statement `snapshot.json` 为输入；未提供时可查找已有候选快照。范围缺失时请用户指定目录或声明，不默认处理所有索引。

按卡片的 `lean.file` 匹配目录或文件，或按精确 `declaration_id` / Lean 名称选取。按文件或相关主题拆成大小合适的组。各组声明集合互不重叠，合起来恰好覆盖本次范围；上下文定义可以跨组查阅。

在被 Git 忽略的 `.statement-enrichment/<批次>/` 保存任务和结果，不把运行产物提交到仓库。写 `manifest.json`：

```json
{
  "schema": "formaliscope-enrichment-batch.v1",
  "source_commit": "冻结快照中的 source_commit",
  "snapshot_digest": "冻结快照中的 digest",
  "threshold": 0.8,
  "declaration_ids": ["statement::完整.Lean.名称"]
}
```

这里的字符串需替换为实际值。阈值默认 `0.8`，只在用户指定时修改。

## Luna 分组生成

读取 [Luna prompt](references/luna-prompt.md)。向每个子 Agent 提供这个 prompt、仓库绝对路径、冻结快照绝对路径、该组精确声明 ID 和唯一结果文件路径。

显式指定 `model="gpt-6-luna"`，采用独立上下文（可用 `fork_turns="none"`），按当前工具的并发额度分批启动。不要把主会话历史、已有中文草稿或人的判断带入任务。工具无法指定 Luna 时说明限制，不用其他模型冒充。支持推理等级时可用 `high`。

子 Agent 可查阅冻结快照中的 Lean 定义和实例，并记录实际证据；独立上下文不代表文件系统隔离。每组只写自己的结果文件。没有用户要求时不做论文或作者意图对齐。

结果外层为 `formaliscope-luna-batch.v1`，内部直接使用既有 `statement-enrichment.v1`；逐条的 `confidence` 存在外层映射，不更改应用 schema。

## 校验、路由与按需复核

在仓库根目录调用：

```sh
python3 .agents/skills/formaliscope-enrich/scripts/collect.py \
  --snapshot /absolute/path/snapshot.json \
  --manifest .statement-enrichment/batch/manifest.json \
  --result .statement-enrichment/batch/group-1.json \
  --result .statement-enrichment/batch/group-2.json \
  --output .statement-enrichment/batch/collected
```

`--result` 可重复。脚本检查完整声明集合、源码和证据、字段及有限数值分值，生成 `enrichment.json`、`review-queue.json`、`report.json`。格式或来源错误是机械失败，可让原子 Agent 修正；默认最多修正一次，仍失败记录未完成，不静默丢条目。

语义复核的唯一触发条件是 **原始 Luna `confidence < threshold`**；`confidence >= threshold` 直接汇总，默认阈值下 `0.8` 本身直接汇总。不得添加条目角色、重要程度、依赖数量、复杂度、`unresolved` 或随机抽检等触发条件。优先度仍是填写给审阅台的字段，不参与复核路由。

队列非空时，主 Agent 读取 [复核 prompt](references/review-prompt.md)，只复核队列条目，保存独立 `review.json`。保留原始 Luna 结果与分值；不得靠提高分值代替复核。

再用同一组原始 `--result` 加 `--review /absolute/path/review.json`，写入另一个全新的 `--output` 目录。脚本合并高置信度及已复核条目，未复核条目继续留在队列。`report.json` 区分直接汇总、主 Agent 已复核、待复核；主 Agent 的模型与时间单独记录。

## 生成候选快照

对最终输出复用现有命令：

```sh
python3 -m review_app validate-enrichment \
  --snapshot /absolute/path/snapshot.json --file /absolute/path/collected/enrichment.json
python3 -m review_app enrich-snapshot \
  --snapshot /absolute/path/snapshot.json --file /absolute/path/collected/enrichment.json \
  --output /absolute/path/new-candidate-snapshot.json
```

汇报选中、直接汇总、已复核、待复核和失败数量，以及候选文件位置。所有机器回译仍是 `draft`，不写人的 `verdict`，不把主 Agent 复核标成“人工已审阅”。候选快照安装与正式部署沿用仓库既有流程，本 Skill 不自动安装。
