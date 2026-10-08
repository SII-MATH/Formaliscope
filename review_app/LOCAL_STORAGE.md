# 本地存储

项目内的数据统一保存在 `.formaliscope/`，整个目录被 Git 忽略，不进入应用发布包。
`.agents/`、`.claude/`、`.kimi-code/` 是工具发现目录；`.github/` 是仓库配置，分别按工具约定维护。

```text
.formaliscope/
├── runtime/                   # 已安装快照、数据库、认证密钥
├── snapshots/
│   ├── candidates/            # 构建时指定的新候选输出
│   ├── repositories/<仓库>/<提交>/  # 单仓库候选，以快照摘要命名
│   └── collections/<摘要>/    # 多仓库候选及构建核对记录
├── tasks/
│   ├── configs/               # YYYYMMDD-HHMMSS-任务名.json
│   ├── batches/               # 回译、预期判断、收集与复核结果
│   ├── drafts/                # 本地预览回译草稿
│   └── archive/               # 历史任务与快照更新记录
├── cache/
│   ├── snapshots/             # 按内容摘要共享的任务输入
│   ├── checks/                # 临时验证产物
│   └── tools/                 # 可重新生成的开发工具环境
├── logs/                      # 服务日志、PID 和性能测量
└── backups/                   # 数据、密钥、安装副本与迁移记录
```

## 路径与生命周期

应用默认从 `.formaliscope/runtime/` 读取数据。`REVIEW_DATA_DIR` 和显式 `--data-dir`
继续支持独立位置；生产仍使用 `/var/lib/formaliscope`，本地布局调整不改变服务器配置。
运行目录中的 `snapshot.json` 是唯一已安装证据。候选放入 `snapshots/`，确认后通过
`install-snapshot` 切换；移动文件本身不激活候选，也不修改数据库结构。

每个回译任务使用 `tasks/configs/` 下的独立配置，默认输出到
`tasks/batches/<配置文件名去掉 .json>/`。项目内任务输入共享 `cache/snapshots/`，
结果仍保留各自独立目录。显式输出到本地存储根目录之外时，输入缓存放在批次父目录，
便于独立保存该批次。

运行数据、候选、任务结果和备份按需长期保留。验证产物、性能日志和工具环境在核对完成后
可以清理；任务引用的共享快照须等相关任务归档或删除后再清理。当前不自动删除本地文件。
异机备份沿用同步工具的保留策略，密钥与加密归档分开保存，详见 [异机备份](OFFSITE_BACKUP.md)。

## 迁移旧目录

旧 `.review/` 对应 `runtime/`；旧 `.statement-review/` 中的候选按仓库、提交和摘要归档到
`snapshots/`，快照更新过程归档到 `tasks/archive/`。旧 `.statement-enrichment/` 按任务、
验证缓存和安装备份拆分，旧 `.performance-results/`、`.review-backups/` 分别对应
`logs/performance/`、`backups/`。

迁移前备份运行数据并停止使用旧目录的本机进程；搬移完整数据库目录，保留认证密钥，
核对文件摘要与 SQLite 完整性。更新任务配置、快照链接、备份配置和定时任务中的路径，
再从新位置启动。历史结果保留原内容，路径对应关系记录在 `backups/migrations/`。
旧目录没有自动迁移或隐式回退；其他检出的已有用户应按此流程迁移，或继续显式指定旧目录。
