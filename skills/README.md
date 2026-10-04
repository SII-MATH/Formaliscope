# Formaliscope Skills

Skill 源文件统一在 `skills/` 下维护，按 harness 分目录。目前只提供 Codex、Claude Code 和 Kimi Code 的中文回译与字段补全 Skill。三份入口、模型配置和阶段提示词分别维护，使用同一套 Statement 字段契约和机械校验脚本。

```text
skills/
├── codex/formaliscope-enrich/
├── claude-code/formaliscope-enrich/
├── kimi-code/formaliscope-enrich/
└── scripts/
    ├── prepare.py
    └── collect.py
```

## 按当前 harness 安装

`skills/` 是源目录，不是自动发现目录。需要使用回译 Skill 时，先确认**当前执行会话的 harness**，只安装对应版本到本项目的发现目录。不要依据模型品牌选择版本：Claude Code 使用 Kimi 模型时，仍安装 Claude Code 版。

| 当前 harness | 仓库源目录 | 项目安装目录 | 手动调用 |
| --- | --- | --- | --- |
| Codex | [codex/formaliscope-enrich](codex/formaliscope-enrich/SKILL.md) | `.agents/skills/formaliscope-enrich/` | `$formaliscope-enrich` |
| Claude Code | [claude-code/formaliscope-enrich](claude-code/formaliscope-enrich/SKILL.md) | `.claude/skills/formaliscope-enrich/` | `/formaliscope-enrich` |
| Kimi Code | [kimi-code/formaliscope-enrich](kimi-code/formaliscope-enrich/SKILL.md) | `.kimi-code/skills/formaliscope-enrich/` | `/skill:formaliscope-enrich` |

在 Formaliscope 仓库根目录，仅执行当前 harness 对应的一组命令。目标存在（包括符号链接）时命令会停止，先检查已安装内容，不覆盖本地修改。

Codex：

```sh
mkdir -p .agents/skills
test ! -e .agents/skills/formaliscope-enrich && test ! -L .agents/skills/formaliscope-enrich && \
  cp -R skills/codex/formaliscope-enrich .agents/skills/formaliscope-enrich
```

Claude Code：

```sh
mkdir -p .claude/skills
test ! -e .claude/skills/formaliscope-enrich && test ! -L .claude/skills/formaliscope-enrich && \
  cp -R skills/claude-code/formaliscope-enrich .claude/skills/formaliscope-enrich
```

Kimi Code：

```sh
mkdir -p .kimi-code/skills
test ! -e .kimi-code/skills/formaliscope-enrich && test ! -L .kimi-code/skills/formaliscope-enrich && \
  cp -R skills/kimi-code/formaliscope-enrich .kimi-code/skills/formaliscope-enrich
```

安装后确认当前 harness 的 Skill 列表中出现了对应版本；必要时重新加载 Skill 或重启会话。安装只是让 harness 发现说明，不执行回译、不导入数据库，也不安装运行快照。Skill 自身没有自动安装机制。

安装副本被 Git 忽略，源码修改只写回 `skills/<harness>/formaliscope-enrich/`。更新源码后，先比较安装副本与源码，保存需要保留的本地修改，再移除本 Skill 的旧副本并重新安装。不要清空其他 Skill 或 harness 配置。

同一 checkout 切换 harness 时，先检查并移除上一个 harness 的本 Skill 安装副本，再安装当前版本。Kimi Code 也扫描 `.agents/skills/`，因此同时保留 Codex 和 Kimi 的同名安装可能造成误选。用户级或其他来源若也有同名 Skill，同样需要确认实际加载的路径；不能依赖不同工具的同名优先级。

## 模型配置

三份 `config.json` 的 `worker.model` 均为 **`luna6`**，由用户处理模型路由。保持这个 ID，不自行替换成 GPT、Claude 或 Kimi 的供应商型号，也不修改全局路由或凭据。运行记录中的 model 表示 harness 确认使用的模型路由 ID；调度层仍须核对实际执行是否使用了该路由，不把配置值直接当成执行证据。

Codex 默认 `worker.reasoning_effort=high`；Claude Code 和 Kimi Code 默认为 null，表示不额外要求等级。非 null 的等级必须能被当前 harness 和模型实际应用。Claude 的 effort、Kimi 的 thinking 和 Codex 的 reasoning effort 不默认视为等价，不能静默忽略或降级。

每个 harness 独立维护配置；主题和字段规则保持一致。所有准备命令必须显式传入 `--config`，不从安装目录推断默认模型。用户指定其他配置时使用该配置。批次一旦冻结，续做沿用本批文件；换模型或设置时开新批次。

## 执行与维护

安装包含本 harness 的入口、配置与三份阶段提示词。脚本留在仓库 `skills/scripts/`，安装目录不需要复制脚本。定位包含 `review_app/`、`statement_workflow/` 和 `skills/` 的仓库根目录，在该目录执行命令；不从全局安装路径推算仓库位置。本流程推荐使用上面的项目级安装。

例如 Codex 准备批次；其他 harness 使用各自的配置路径：

```sh
python3 skills/scripts/prepare.py \
  --config skills/codex/formaliscope-enrich/config.json \
  --snapshot /absolute/path/snapshot.json \
  --directory KIP126/Interface/Axiom \
  --output .statement-enrichment/new-batch
```

随后按安装的 Skill 分组调用子 Agent，先保存纯 Lean 回译，再提供独立预期材料。收集器只按原始回译分值安排复核；生成的候选和内部评估沿既有命令分别处理。完整数据契约、收集和导入命令见 [Statement 工作流](../statement_workflow/README.md) 与 [字段标准 v2](../statement_workflow/SCHEMA_V2.md)。

改字段规范或阶段约束时，检查三个版本的入口和提示词；改调度工具、模型选择或上下文管理时，只修改相应 harness。三份版本都不得把机器结果标为人工已审阅，或自动安装快照、部署服务。

发现目录与工具依据：[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Claude Code 子 Agent](https://code.claude.com/docs/en/sub-agents)、[Kimi Code Skills](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/skills.html)、[Kimi Code 工具](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html)。安装和调用时以当前版本实际提供的工具 schema 为准。
