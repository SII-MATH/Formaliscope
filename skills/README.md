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

Claude Code（要求提供动态 Workflow，并支持专用 agent 的 `omitClaudeMd`，后者需 v2.1.271 或更新版本）：

```bash
test ! -e .claude/skills/formaliscope-enrich && test ! -L .claude/skills/formaliscope-enrich && \
  test ! -e .claude/agents/formaliscope-readback.md && test ! -L .claude/agents/formaliscope-readback.md && \
  test ! -e .claude/agents/formaliscope-expectation.md && test ! -L .claude/agents/formaliscope-expectation.md && \
  mkdir -p .claude/skills .claude/agents && \
  cp -R skills/claude-code/formaliscope-enrich .claude/skills/formaliscope-enrich && \
  cp skills/claude-code/formaliscope-enrich/agents/formaliscope-readback.md .claude/agents/formaliscope-readback.md && \
  cp skills/claude-code/formaliscope-enrich/agents/formaliscope-expectation.md .claude/agents/formaliscope-expectation.md
```

Claude 版的 `agents/` 是分发源，必须另行注册到 `.claude/agents/`，不能仅复制 Skill 后假定 agents 自动可用。`workflows/enrich.js` 是 Skill 的支持文件，通过 `Workflow(scriptPath=..., args=...)` 调用，不需要复制到 `.claude/workflows/`。入口只由用户手动调用，不自动展开多 Agent 运行，也不配置任何免确认权限。

安装后检查两个专用 agent 已加载；如果会话启动时 `.claude/agents/` 不存在，或仓库是之后添加的额外目录，重启会话以发现定义。已有 agent 目录中的新增/修改通常会自动加载。不要通过继承主历史的 fork 或预加载含预期资料的 Skill 替代隔离定义。

Kimi Code：

```sh
test ! -e .kimi-code/skills/formaliscope-enrich && test ! -L .kimi-code/skills/formaliscope-enrich && \
  test ! -e .kimi-code/agents/formaliscope-enrich && test ! -L .kimi-code/agents/formaliscope-enrich && \
  mkdir -p .kimi-code/skills .kimi-code/agents/formaliscope-enrich && \
  cp -R skills/kimi-code/formaliscope-enrich .kimi-code/skills/formaliscope-enrich && \
  cp skills/kimi-code/formaliscope-enrich/agents/formaliscope-enrich-worker.md .kimi-code/agents/formaliscope-enrich/
```

Kimi Code 不会自动发现 Skill 源目录中的 `agents/`；专用 worker 必须另外复制到项目级 `.kimi-code/agents/formaliscope-enrich/`。安装命令会先同时检查 Skill 和 agent 目标（包括符号链接），任一已存在就停止，不覆盖。安装后在**新会话**确认 Skill 可调用且 `formaliscope-enrich-worker` 已注册；agent 新增或修改后也要重新复制并在新会话确认。更新或卸载时，分别同步或移除本 Skill 的 `.kimi-code/skills/formaliscope-enrich/` 和 `.kimi-code/agents/formaliscope-enrich/` 副本；保留需要的本地修改，不清空其他 Skill、agent 或 harness 配置。

Kimi Code 多组任务使用原生 AgentSwarm：第一阶段为各组启动独立 worker，保存每个 Agent ID；有预期材料时，第二阶段通过对应 ID resume 同一 worker，并仅补充预期判断。只有一组时例外，直接调用该 worker，不启动 AgentSwarm。按 Skill 的两阶段隔离要求传递资料，不改变 `luna6` 模型路由或本 Skill 的其他处理、校验与复核功能。

安装后确认当前 harness 的 Skill 列表中出现了对应版本；必要时重新加载 Skill 或重启会话。安装只是让 harness 发现说明，不执行回译、不导入数据库，也不安装运行快照。Skill 自身没有自动安装机制。

安装副本被 Git 忽略，源码修改只写回 `skills/<harness>/formaliscope-enrich/`。更新源码后，先比较安装副本与源码，保存需要保留的本地修改，再移除本 Skill 的旧副本并重新安装。Claude 版还须同时比较、备份并更新本 Skill 的两个 `.claude/agents/formaliscope-*.md` 注册副本；Kimi 版还须比较、备份并更新 `.kimi-code/agents/formaliscope-enrich/` 中的专用 worker 副本。卸载时只移除本 Skill 对应的安装副本；不要清空其他 Skill、agent 或 harness 配置。

同一 checkout 切换 harness 时，先检查并移除上一个 harness 的本 Skill 安装副本，再安装当前版本。Kimi Code 也扫描 `.agents/skills/`，因此同时保留 Codex 和 Kimi 的同名安装可能造成误选。用户级或其他来源若也有同名 Skill，同样需要确认实际加载的路径；不能依赖不同工具的同名优先级。

## 模型配置

三份 `config.json` 的 `worker.model` 均为 **`luna6`**，由用户处理模型路由。保持这个 ID，不自行替换成 GPT、Claude 或 Kimi 的供应商型号，也不修改全局路由或凭据。运行记录中的 model 表示 harness 确认使用的模型路由 ID；调度层仍须核对实际执行是否使用了该路由，不把配置值直接当成执行证据。

Codex 默认 `worker.reasoning_effort=high`；Claude Code 和 Kimi Code 默认为 null，表示不额外要求等级。非 null 的等级必须能被当前 harness 和模型实际应用。Claude 的 effort、Kimi 的 thinking 和 Codex 的 reasoning effort 不默认视为等价，不能静默忽略或降级。

每个 harness 独立维护配置；主题和字段规则保持一致。所有准备命令必须显式传入 `--config`，不从安装目录推断默认模型。用户指定其他配置时使用该配置。批次一旦冻结，续做沿用本批文件；换模型或设置时开新批次。

## 执行与维护

安装包含本 harness 的入口、配置与三份阶段提示词；Claude 版另含专用 agents 定义和分组 Workflow 脚本，Kimi 版另含单独注册的 swarm worker 定义。脚本留在仓库 `skills/scripts/`，安装目录不需要复制共享准备/收集脚本。定位包含 `review_app/`、`statement_workflow/` 和 `skills/` 的仓库根目录，在该目录执行命令；不从全局安装路径推算仓库位置。Claude 版通过 `${CLAUDE_SKILL_DIR}` 定位安装资源，不据此推算仓库。Kimi 版通过 `${KIMI_SKILL_DIR}` 定位配置与阶段提示词，同样不据此推算仓库。本流程推荐使用上面的项目级安装。

例如 Codex 准备批次；其他 harness 使用各自的配置路径：

```sh
python3 skills/scripts/prepare.py \
  --config skills/codex/formaliscope-enrich/config.json \
  --snapshot /absolute/path/snapshot.json \
  --directory KIP126/Interface/Axiom \
  --output .statement-enrichment/new-batch
```

随后按安装的 Skill 分组调用子 Agent，先保存纯 Lean 回译，再提供独立预期材料。Claude 版由主会话核对冻结范围与配置，Workflow `pipeline()` 逐组执行独立回译与预期 agents，无预期时跳过第二阶段；结构化回执只传文件路径与条目数，必须全组完成且实际模型路由核实后才收集。共享收集器仍只按原始回译分值安排主 Agent 复核；生成的候选和内部评估沿既有命令分别处理。完整数据契约、收集和导入命令见 [Statement 工作流](../statement_workflow/README.md) 与 [字段标准 v2](../statement_workflow/SCHEMA_V2.md)。

改字段规范或阶段约束时，检查三个版本的入口和提示词；改调度工具、模型选择或上下文管理时，只修改相应 harness。三份版本都不得把机器结果标为人工已审阅，或自动安装快照、部署服务。

发现目录与工具依据：[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Claude Code 子 Agent](https://code.claude.com/docs/en/sub-agents)、[Claude Code 动态 Workflow](https://code.claude.com/docs/en/workflows)、[Kimi Code Skills](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/skills.html)、[Kimi Code agents](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents.html)、[Kimi Code 工具](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html)。安装和调用时以当前版本实际提供的工具 schema 为准。
