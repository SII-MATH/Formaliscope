# 多仓库与源码版本

Statement 模式可接入不同 Lean 工程，不要求源码目录名或命名空间为 KIP126。仓库身份、扫描路径、主目标和项目主题由 JSON 配置，源码位置和提交由构建参数确定。示例见 `repository-configs/kip126.json` 和 `repository-configs/cgwhmodelproof.json`；它们只包含接入配置，不包含被审源码或用户记录。

CGWHModelProof 使用 [SII-MATH/CGWHModelProof](https://github.com/SII-MATH/CGWHModelProof) 的 Git 源码。接入时固定选定的提交，用 `--require-clean --expect-commit` 核对源码状态，不以聊天中的源码包或本机导入提交代替上游版本。

## 生成候选

```bash
python3 -m review_app build --statements --require-clean \
  --source /path/to/KIP126 \
  --repository-config repository-configs/kip126.json \
  --expect-commit <KIP126的40位HEAD> --output /tmp/kip126-candidate.json
python3 -m review_app build --statements --require-clean \
  --source /path/to/CGWHModelProof \
  --repository-config repository-configs/cgwhmodelproof.json \
  --expect-commit <CGWH的40位HEAD> --output /tmp/cgwh-candidate.json
```

`id` 是持久的仓库身份，采用小写字母、数字、下划线和短横线，不能因显示名称变化而修改。`name` 是显示名称。`roots` 支持仓库内的相对目录或单个 `.lean` 文件，`.` 表示整个工程；重叠扫描范围只读取一次，排除 `.git`、`.lake` 和 `lakefile.lean`，拒绝越出仓库的路径和源码符号链接。`topics` 是该仓库允许的主题 ID/名称列表，`main_targets` 是明确的声明全名，`url` 是可选的 HTTPS 来源链接。

公共声明 ID 为 `statement::<repository-id>::<Lean全名>`。私有声明追加 `::file=<相对文件>`，保留不同模块中的同名私有定理，引用与符号追溯仅在其所属文件内可见。公开声明名称重复时构建报错，需要确认扫描范围或修正命名空间，不静默覆盖。

旧的不带 `--repository-config` 的 KIP126 构建方式继续支持。只有预览源码包时才使用 `--source-commit` 声明已知版本，并保留 `archive-unverified` 来源标记；不要把来源不明的源码包或本地导入提交当作上游的已验证提交。提取不会执行 Lean；真实编译和回译交付由管线验收负责。

## 组合与安装

```bash
python3 -m review_app bundle-snapshots \
  --snapshot /tmp/kip126-candidate.json --snapshot /tmp/cgwh-candidate.json \
  --output /tmp/collection-candidate.json
python3 -m review_app install-snapshot \
  --file /tmp/collection-candidate.json --data-dir /path/to/review-data
python3 -m review_app preflight --data-dir /path/to/review-data
```

集合是 `formaliscope-review-collection.v1`，内含完整、独立校验的快照；任何子数据集的私有评估、脏源码或未核实源码包都不能借集合绕过正式安装检查。构建和组合只产生新候选文件。`install-snapshot` 在维护锁下切换运行证据；服务重启后加载安装的集合。

数据集身份为 `<repository-id>@<source-commit>`。同一仓库的多个 commit 可以放在同一集合中，`--default <数据集身份>` 可指定初始版本。同一仓库、同一 commit 在一个集合中只能出现一次；同一 commit 的新回译应替换该版本的候选，而不是新增一个冒充源码版本的条目。组合新集合时显式列出仍需保留的旧版本快照。

现有 GitHub `publish-snapshot` 工作流和自动拉取器仍专用于旧 KIP126 单仓库制品；多仓库集合使用上述显式构建与安装流程。拉取器的 `--legacy-kip126-only` 检查在安装锁内阻止单仓库自动更新覆盖已安装的仓库数据集/集合。采用集合时应停用原 snapshot-pull 定时器，避免持续产生拒绝更新的告警；通用的自动数据交付由 KIP-32 后续确认。

页面只提供仓库选择，每个仓库只显示当前版本，并展示当前提交、条目数量和数据生成时间，标明源码包/本地修改。集合默认数据集所在仓库使用该显式选定版本；其他仓库使用数据生成时间最新的已安装版本，时间相同或均缺失时使用集合中最后列出的版本。安装更新集合时应把主仓库默认数据集设为新版本。

切换仓库先保存当前草稿和已选择的审阅结论，保存失败会停留原页并保留输入；成功后重新加载该仓库当前数据集，清除原目录、标签和声明选择。打开历史版本的页面链接时转到同仓库当前版本，页面的符号、完整文件、统计、个人导出和管理员汇总都使用当前数据集参数。历史快照和记录仍保留，历史数据的显式 API 查询不受页面跳转影响；集合的写入请求必须明确指定数据集，服务器不会把旧版本写入自动转到新版本。

## 隔离、迁移与回退

数据库迁移 **10** 为判断、草稿和私有模型运行增加数据集身份，并扩展草稿唯一约束。不同仓库的记录完全隔离；同仓库的新版本默认沿用已安装前身中依据未变的机器结果和人工判断，仍各自保存独立记录。使用 `--no-reuse` 可以保持版本独立。同一数据集内回译改变仍保留旧结论并按内容指纹判断是否有效。跨版本沿用见下文；迁移 **11** 增加判断的来源数据集和原始记录关联。

身份与登录会话仍共用，避免切换仓库需要重建账号。现有操作员管理员保留全局管理权限；可给普通已登记身份授予某一数据集的管理员权限：

```bash
python3 -m review_app dataset-admin --data-dir /path/to/review-data \
  --dataset <repository-id@commit> --reviewer <已有内部身份ID>
# 追加 --revoke 撤销这一个数据集的权限。
```

某数据集的授权不授予其他仓库或其他版本的权限。管理员汇总只统计所选数据集。私有模型评估按冻结基础快照的数据集入库，既不进入公共集合，也不写入人工判断。

升级前先按部署说明备份。把旧 KIP126 快照加入集合时，会在新副本中补仓库身份；原制品不改写。安装时把对应 commit 的旧判断和草稿复制到新作用域，保留原始来源、完成关联和原始记录，重复安装不会重复复制。缺少 commit 的旧记录只依据仍安装的旧快照定位；无法消除的同名歧义保留在原作用域，不随意归属。其他版本和旧私有模型产物保留在数据库中，不自动改写为新的实验。

重新安装旧快照可回到旧界面和原始记录，新作用域中的记录继续保留，重装集合后仍可访问。**回退到仅支持数据库 9 的旧应用必须恢复升级前完整备份**；旧应用不能直接打开迁移 10 的数据库。备份仍包含一个 `snapshot.json` 和一个数据库，集合及其全部版本随同备份。

## 跨版本沿用

更新同一仓库时默认沿用已安装的上一版本，无需填写 `--reuse-from`：

```bash
python3 -m review_app build --statements --require-clean \
  --source /path/to/LeanProject --repository-config /path/to/repository.json \
  --output /tmp/next-reused.json
```

构建只读 `REVIEW_DATA_DIR` 指定的运行目录，未设置时读取项目 `.formaliscope/runtime`；可用 `--review-data-dir /path/to/review-data` 改变读取位置（构建的旧 `--data-dir` 仍只代表候选输出目录）。没有已安装的同仓库版本时按首次接入处理，不创建运行目录或数据库。

优先使用已安装集合中该仓库的默认版本；集合默认属于其他仓库时，使用该仓库最近生成的版本。生成时间必须早于新候选；缺少可比较时间或安装的是已经存在的版本时，不自动推断新的前身，回退旧快照也不会从更新版本继承结果。最近生成时间有歧义时需通过 `--reuse-from` 明确选择。该默认规则基于已安装证据，不推断 Git 分支祖先关系。

如果构建机器没有运行数据，直接安装普通新候选也会默认沿用目标数据目录中该仓库的上一版本。安装在维护锁内先生成独立的临时沿用候选，再校验和切换；输入制品不会改写，安装后的摘要以新候选为准。重复安装同一普通输入会保留已经生成的沿用结果，不清空已有回译。

`build --no-reuse` 生成新数据并在候选中记录关闭默认沿用，安装也尊重该标记。`install-snapshot --no-reuse` 关闭安装时自动生成候选及人工判断继承；输入候选本身已经包含的回译仍属于该制品，若需全新回译应从 `build --no-reuse` 开始。

也可对已经构建的新版本生成一个独立沿用候选：

```bash
python3 -m review_app reuse-snapshot \
  --snapshot /tmp/next-base.json --reuse-from /tmp/previous-enriched.json \
  --output /tmp/next-reused.json
python3 -m review_app bundle-snapshots \
  --snapshot /tmp/previous-enriched.json --snapshot /tmp/next-reused.json \
  --default <repository-id@新commit> --output /tmp/updated-collection.json
python3 -m review_app install-snapshot \
  --file /tmp/updated-collection.json --data-dir /path/to/review-data
```

`--reuse-from` 可以覆盖默认前身，接受单快照或集合；显式指定的集合中同仓库版本不唯一时，使用 `--reuse-dataset <repository-id@旧commit>` 明确选择。新旧快照必须属于同一持久仓库 ID、不同源码 commit。旧 KIP126 非作用域快照先通过 `bundle-snapshots` 转为仓库数据集，再按上述默认或指定前身的流程沿用；不对不同仓库或同名私有声明进行猜测配对。

统一沿用依据包括稳定声明 ID、完整声明源码、整个所属模块、递归导入的本仓库模块、Blueprint 数学文案、主目标配置、依赖锁与工具链/工程配置。构建会冻结扫描范围以外实际导入的本仓库模块，仍仅为所选扫描范围生成条目。外部导入还要求存在冻结的依赖锁和工具链；锁中包含无法冻结的 path 依赖时阻止沿用。依赖锁表示上游固定版本，不证明本机依赖缓存未经修改或 Lean 编译成功。旧快照缺少所需环境信息时，不声称已经确认外部环境未变。

空行和公共声明的文件位置变化可沿用；Lean 缩进、字符串内容、模块变量/假设、导入内容及依赖环境变化不能沿用。私有声明的所属文件属于身份，移动后视为新增。当前没有 elaborator 的完整语义依赖，因此整个模块或递归导入模块中其他声明变化也会保守地阻止沿用；这是减少误继承的边界，不代表确认了 Lean 语义等价。

候选只携带公开机器结果和带摘要保护的沿用清单，不读写人工数据库。分类主题必须仍在新版本主题配置中；已有新回译优先保留。候选生成显示沿用条目数及 changed/added/removed；运行证据仍仅由 `install-snapshot` 激活。

安装在数据目录维护锁内核对精确旧快照和上下文，再为每位审阅人复制旧版本的当前有效判断。只有新旧审阅内容指纹也一致的判断才能继承，目标版本已有判断不会被覆盖。复制保留原始作者、审阅时间、提交与快照摘要，另记来源数据集和记录 ID；页面及个人导出可识别沿用来源。重装不会重复复制，连续更新保留原始审阅来源。草稿、账号权限和私有模型评估不随沿用复制，继续按原版本隔离。

沿用候选的精确旧快照须仍在已安装版本或待安装集合中；推荐像上例一样在集合保留旧版本，以便查看变化、删除条目的原始机器结果和人工历史。带沿用清单的候选无法找到精确前身时首次安装会拒绝；没有前身的普通候选仍可首次安装。已验证沿用版本的重复安装和同版本重新回译可以继续安装。旧版本记录始终保留，回退不改写已有判断。升级至数据库 **11** 前备份；回退到只支持数据库 10 或更早的应用需要恢复升级前完整备份。
