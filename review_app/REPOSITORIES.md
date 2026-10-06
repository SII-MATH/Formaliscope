# 多仓库与源码版本

Statement 模式可接入不同 Lean 工程，不要求源码目录名或命名空间为 KIP126。仓库身份、扫描路径、主目标和项目主题由 JSON 配置，源码位置和提交由构建参数确定。示例见 `repository-configs/kip126.json` 和 `repository-configs/cgwhmodelproof.json`；它们只包含接入配置，不包含被审源码或用户记录。

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

页面的“仓库与版本”选择器显示仓库、提交、条目数量和数据生成时间，并标明源码包/本地修改。切换先保存当前草稿和已选择的审阅结论，保存失败会停留原页并保留输入；成功后重新加载该数据集，清除原目录、标签和声明选择。链接的 `dataset` 参数固定仓库版本，符号、完整文件、统计、个人导出和管理员汇总都使用该参数。集合的写入请求必须明确指定数据集。

## 隔离、迁移与回退

数据库迁移 **10** 为判断、草稿和私有模型运行增加数据集身份，并扩展草稿唯一约束。即使源码和内容指纹完全相同，不同仓库或不同源码版本也独立记录判断、草稿和历史；同一数据集内回译改变仍保留旧结论并按内容指纹判断是否有效。跨版本比较或结论迁移由 KIP-28 另行实现。

身份与登录会话仍共用，避免切换仓库需要重建账号。现有操作员管理员保留全局管理权限；可给普通已登记身份授予某一数据集的管理员权限：

```bash
python3 -m review_app dataset-admin --data-dir /path/to/review-data \
  --dataset <repository-id@commit> --reviewer <已有内部身份ID>
# 追加 --revoke 撤销这一个数据集的权限。
```

某数据集的授权不授予其他仓库或其他版本的权限。管理员汇总只统计所选数据集。私有模型评估按冻结基础快照的数据集入库，既不进入公共集合，也不写入人工判断。

升级前先按部署说明备份。把旧 KIP126 快照加入集合时，会在新副本中补仓库身份；原制品不改写。安装时把对应 commit 的旧判断和草稿复制到新作用域，保留原始来源、完成关联和原始记录，重复安装不会重复复制。缺少 commit 的旧记录只依据仍安装的旧快照定位；无法消除的同名歧义保留在原作用域，不随意归属。其他版本和旧私有模型产物保留在数据库中，不自动改写为新的实验。

重新安装旧快照可回到旧界面和原始记录，新作用域中的记录继续保留，重装集合后仍可访问。**回退到仅支持数据库 9 的旧应用必须恢复升级前完整备份**；旧应用不能直接打开迁移 10 的数据库。备份仍包含一个 `snapshot.json` 和一个数据库，集合及其全部版本随同备份。
