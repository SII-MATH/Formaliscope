# Formaliscope Skills

Skill 源文件统一在 `skills/` 下维护，按 harness 分目录。目前只提供 Codex、Claude Code 和 Kimi Code 的中文回译与字段补全 Skill。三份入口、模型配置和阶段提示词分别维护，使用同一套 Statement 字段契约和机械校验脚本。

```text
skills/
├── codex/formaliscope-enrich/
├── claude-code/formaliscope-enrich/
├── kimi-code/formaliscope-enrich/
└── scripts/
    ├── config.py
    ├── prepare.py
    └── collect.py
```

## 按当前 harness 安装

必须安装在 **Formaliscope 仓库目录内**，使用下表列出的项目级安装目录。下表和安装命令中的路径都相对于 Formaliscope 仓库根目录；从该目录启动执行会话，并运行后续准备、收集命令。不使用用户级或全局安装。

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

Kimi Code 多组任务使用原生 AgentSwarm：第一阶段为各组启动独立 worker，保存每个 Agent ID；有预期材料时，第二阶段通过对应 ID resume 同一 worker，并仅补充预期判断。只有一组时例外，直接调用该 worker。按 Skill 的两阶段要求传递资料，模型和其他设置取本批合并后的配置。

安装后确认当前 harness 的 Skill 列表中出现了对应版本；必要时重新加载 Skill 或重启会话。安装只是让 harness 发现说明，不执行回译、不导入数据库，也不安装运行快照。Skill 自身没有自动安装机制。

安装副本被 Git 忽略，源码修改只写回 `skills/<harness>/formaliscope-enrich/`。更新源码后，先比较安装副本与源码，保存需要保留的本地修改，再移除本 Skill 的旧副本并重新安装。Claude 版还须同时比较、备份并更新本 Skill 的两个 `.claude/agents/formaliscope-*.md` 注册副本；Kimi 版还须比较、备份并更新 `.kimi-code/agents/formaliscope-enrich/` 中的专用 worker 副本。卸载时只移除本 Skill 对应的安装副本；不要清空其他 Skill、agent 或 harness 配置。

同一 checkout 切换 harness 时，先检查并移除上一个 harness 的本 Skill 安装副本，再安装当前版本。Kimi Code 也扫描 `.agents/skills/`，因此同时保留 Codex 和 Kimi 的同名安装可能造成误选。已有用户级或其他来源的同名 Skill 时，先处理冲突，确保会话加载本项目安装的版本。

## 模型配置

共享默认配置在 [default-config.json](default-config.json)，包含逻辑模型、按 harness 区分的别名、主题、复核阈值和任务设置。三个工具的 `config.json` 指定 harness 并覆盖各自的推理设置。默认逻辑模型仍为 `luna6`；准备时，Claude Code 的别名将其解析为 `sonnet`，Codex/Kimi 不受该映射影响。已解析调度名冻结在 `agent-config.json` 和 manifest，原始名称及映射保留在 `task-config.json`。别名只决定请求路由，实际执行还须核对启动元数据和响应记录，不能把配置当作执行证明。

每次任务开始前，Agent 起草 `.formaliscope/tasks/configs/YYYYMMDD-HHMMSS-任务名.json`，引用当前工具的配置，填写快照和范围，按用户要求覆盖设置。对象字段逐项合并，数组整体替换，明确的 null 覆盖原值。省略项继承默认配置，字段和路径约定见 [任务配置说明](CONFIG.md)。

## 执行与维护

安装包含本 harness 的入口、配置与三份阶段提示词；Claude 版另含专用 agents 定义和分组 Workflow 脚本，Kimi 版另含单独注册的 swarm worker 定义。共享准备/收集脚本留在仓库 `skills/scripts/`。Claude 版通过 `${CLAUDE_SKILL_DIR}` 读取安装资源，Kimi 版通过 `${KIMI_SKILL_DIR}` 读取安装资源。安装与路径约定统一由本说明规定，Skill 按项目已正确安装的前提执行。

准备脚本自动记录任务依据。项目内的任务共用 `.formaliscope/cache/snapshots/`，批次内 `snapshot.json` 为相对链接。目录职责和迁移方式见 [本地存储](../review_app/LOCAL_STORAGE.md)。

起草任务配置后，准备和收集均只传配置路径：

```sh
python3 skills/scripts/prepare.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
python3 skills/scripts/collect.py --config .formaliscope/tasks/configs/20261008-150000-tower.json
```

配置示例、执行后填写的 `collection` 字段和复核后的重新收集见 [任务配置说明](CONFIG.md)。

随后按安装的 Skill 调用子 Agent，先保存纯 Lean 回译，再提供独立预期材料。新批次冻结 manifest v3 和阶段协议：第一阶段仅输出 declaration_id、title_zh、readback、classification、priority，第二阶段仅输出 declaration_id、expectation_assessment。两个阶段分别写新草稿，由固定 `collect.py --deliver-readback`／`--deliver-expectation` 严格校验并排他交付正式结果；第一阶段交付同时记录实际文件字节摘要到 `.baseline.json`，第二阶段先运行 `--check-readback`，收集时再次独立检查并按 ID 确定性合并，模型不复制第一阶段字段。

Claude 版每条声明一组，Workflow `pipeline()` 在每组回译交付且回执有效后，启动新的独立预期 Agent；无预期材料也执行第二阶段，传 null 并明确判断为 `undetermined`。每条声明固定两次调用及两个正式结果文件，不同组独立推进。Workflow 无直接文件系统接口，阶段 Agent 调用固定交付程序；结构化回执只传文件路径与条目数，必须全组完成且实际模型路由核实后才收集。Codex/Kimi 保留各自分组和续做方式，无材料时跳过第二阶段，提供全部 `readback_results` 和空 `results`，由收集程序生成明确的 undetermined 记录。

摘要仅提供篡改检测，不是权限隔离，不能抵御同时改写结果与记录的进程。原始阶段文件保留，失败不能靠主 Agent 改正文、重新封存或临时拼接绕过。最终 `statement-enrichment.v2`、低分复核及公开／私密隔离语义不变；历史 manifest v1/v2 明确走原契约，不猜测或转换。完整交付命令、数据契约和导入流程见 [任务配置说明](CONFIG.md)、[Statement 工作流](../statement_workflow/README.md) 与 [字段标准 v2](../statement_workflow/SCHEMA_V2.md)。

改字段规范或阶段约束时，检查三个版本的入口和提示词；改调度工具、模型选择或上下文管理时，只修改相应 harness。三份版本都不得把机器结果标为人工已审阅，或自动安装快照、部署服务。

发现目录与工具依据：[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Claude Code 子 Agent](https://code.claude.com/docs/en/sub-agents)、[Claude Code 动态 Workflow](https://code.claude.com/docs/en/workflows)、[Kimi Code Skills](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/skills.html)、[Kimi Code agents](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents.html)、[Kimi Code 工具](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/tools.html)。安装和调用时以当前版本实际提供的工具 schema 为准。
