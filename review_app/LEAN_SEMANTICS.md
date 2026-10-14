# Lean 语义条目索引

Statement 新构建以 Lean 编译结果中的声明名、源码范围和有效可见性为依据，不再用 Python 正则检出条目或判断 private/public。应用运行时只读取冻结快照，不要求服务器安装 Lean。早期 Blueprint 模式维持原有定位方式。

## 构建

当前导出器支持被审仓库锁定的 `leanprover/lean4:v4.32.2`，源码位于仓库根目录的模块路径，例如 `KIP126/Foo.lean` 对应 `KIP126.Foo`。需要可用的 Elan/Lake 和源码依赖；其他版本或自定义 `srcDir` 应先增加导出器适配，不能退回文本猜测。

常规构建会检查工具链、执行 `lake build` 和所选模块构建，再调用仓库随附的 Lean 导出器：

```sh
python3 -m review_app build --statements --require-clean \
  --source /path/to/LeanProject \
  --repository-config repository-configs/kervaire126.json \
  --output /tmp/statement-candidate.json
```

可以在有 Lean 的构建机器上先生成独立清单，然后在另一台机器消费同一源码检出和清单：

```sh
python3 -m review_app export-lean-index --require-clean \
  --source /path/to/LeanProject \
  --repository-config repository-configs/kervaire126.json \
  --output /tmp/lean-semantic-index.json
python3 -m review_app build --statements --require-clean \
  --source /path/to/LeanProject \
  --repository-config repository-configs/kervaire126.json \
  --semantic-index /tmp/lean-semantic-index.json \
  --output /tmp/statement-candidate.json
```

无仓库配置时仍扫描 `KIP126`、`KIPBase` 中实际存在的目录。清单的扫描范围必须与构建范围一致。源码提交、脏状态、任一本地 Lean 源文件、工具链、Lake 配置、依赖锁或导出器变化，都会使已有清单失效。首次构建若 Lake 生成或修改依赖锁，先确认锁定依赖，再重新导出。输出必须是新的文件，不能覆盖运行数据、原快照、源文件或输入清单。

构建只生成候选；激活仍由 `install-snapshot` 完成。生成并安装新语义快照前应先升级应用，新制品的语义元数据和导入图参与摘要校验，旧应用会拒绝它们。旧快照继续可读、可回滚。此次实现不改变数据库表结构，不自动安装候选或部署服务。

## 审阅资格

可见性按声明的定义模块判断，由 Lean 导出集合与真实私有名称信息共同决定。`module` 默认 private、`public section`、显式 `private/public` 和跨行修饰符由 Lean 处理。`public import` 是重新导出关系；`@[expose]` 控制实现体能否展开，公开但未 expose 的声明仍是 public。`backward.privateInPublic` 不会把私有名称变成默认待审对象。

private 条目保留源码、引用及历史，默认列表、自动首选与目录/全库进度均排除；用户仍可按可见性标签、链接或依赖引用查看。结构字段作为所属条目的字段展示，编译器生成的构造器、投影、递归器和证明辅助声明不各自进入审阅队列。匿名实例使用 Lean 实际生成的声明名。

声明范围来自编译器元数据，以 UTF-8 字节位置切出冻结源码；不依赖“到下一条正则匹配”截断。文件导入关系同样来自编译器，并冻结扫描范围外的本地传递依赖。条目之间的依赖图和点击名称定位仍是源码引用候选，尚不是 elaborator 的完整语义依赖图。

## 旧审阅记录

具名 public 条目继续使用原有完整 Lean 名称 ID；配置仓库的 private 条目仍追加所属文件。编译器的 `_private` 内部名只用于证据映射，不作为新的用户身份。无仓库配置的历史唯一私有名保留 ID，同名私有项必须按文件区分。匿名实例使用 Lean 实际名称并追加所属文件，从首次检出就固定身份，避免新增模块或改变扫描范围时因重名而改变 ID；可见性仍按编译器保留，public 实例分别纳入默认审阅和进度。具名 public 冲突仍报错，不猜测或合并。

内容指纹算法维持原有版本。条目 ID、源码和审阅文案未变时，同一数据集中的有效判断继续显示；跨版本仍按现有上下文与来源检查保守沿用。源码范围、名称或审阅依据修正后，旧判断保留在历史，新依据进入待审，不能把旧判断直接转为通过。旧制品、账号、人工记录和数据库均不改写。

历史快照继续可以读取、安装和回滚，不需要重新执行 Lean。没有可见性字段的历史快照保留独立的冻结源码兼容解码；该路径只用于读取旧证据，不参与新构建。新清单缺失、过期、覆盖不完整或范围不明确时构建报错，不默认为 public。

## 验证

普通 Python 回归使用固定历史快照或显式语义清单，验证快照、服务和记录生命周期。`review_app.test_lean_exporter` 使用真实 Lean 工具链编译独立的小型工程，验证声明检出与可见性；CI 设置 `FORMALISCOPE_REQUIRE_LEAN_TESTS=1`，工具链不可用时失败。仅本地未安装 Lean 时允许跳过这组编译器回归。

```sh
FORMALISCOPE_REQUIRE_LEAN_TESTS=1 python3 -m unittest review_app.test_lean_exporter
python3 -m unittest review_app.test_lean_semantics review_app.test_semantic_compatibility
```
