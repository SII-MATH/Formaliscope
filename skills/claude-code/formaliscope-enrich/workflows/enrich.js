export const meta = {
  name: 'formaliscope-enrich-groups',
  description: '按冻结分组流水线生成纯 Lean 回译及独立内部预期判断',
  phases: [
    { title: 'Lean 回译' },
    { title: '内部预期判断' },
  ],
}

if (!args || typeof args !== 'object' || Array.isArray(args)) {
  throw new Error('args 必须是结构化对象，不能是 JSON 字符串')
}
const { repoRoot, skillDir, batchDir, resultDir = batchDir, config, declarationIds, groups, expectationContext } = args
for (const [name, path] of Object.entries({ repoRoot, skillDir, batchDir, resultDir })) {
  if (typeof path !== 'string' || !/^(\/|[A-Za-z]:[\\/])/.test(path)) {
    throw new Error(`${name} 必须是绝对路径`)
  }
}
if (expectationContext !== null &&
    (typeof expectationContext !== 'string' || !/^(\/|[A-Za-z]:[\\/])/.test(expectationContext))) {
  throw new Error('expectationContext 必须是冻结预期材料的绝对路径或 null')
}
if (config?.schema !== 'formaliscope-enrichment-config.v2' ||
    typeof config.worker?.model !== 'string' || !config.worker.model.trim() ||
    !Array.isArray(config.topics)) {
  throw new Error('config 必须来自本批冻结的 agent-config.json')
}
const effort = config.worker.reasoning_effort
if (effort !== null && !['low', 'medium', 'high', 'xhigh', 'max'].includes(effort)) {
  throw new Error('本 Workflow 不支持所配置的 effort；不得忽略或翻译其他 harness 的等级')
}
if (!Array.isArray(declarationIds) || !declarationIds.length ||
    declarationIds.some(id => typeof id !== 'string' || !id.startsWith('statement::')) ||
    new Set(declarationIds).size !== declarationIds.length) {
  throw new Error('declarationIds 必须是本次运行分配的完整非空精确 ID 集合')
}
if (!Array.isArray(groups) || !groups.length) {
  throw new Error('groups 必须是非空分组列表')
}
const selected = new Set(declarationIds), assigned = new Set(), keys = new Set()
for (const group of groups) {
  if (!group || typeof group.key !== 'string' || !/^group-[1-9][0-9]*$/.test(group.key) ||
      keys.has(group.key) || !Array.isArray(group.declarationIds) || !group.declarationIds.length) {
    throw new Error('每组必须有唯一 group-N key 和非空声明集合')
  }
  keys.add(group.key)
  for (const id of group.declarationIds) {
    if (!selected.has(id) || assigned.has(id)) {
      throw new Error('分组包含未知或重复声明')
    }
    assigned.add(id)
  }
}
if (assigned.size !== selected.size) {
  throw new Error('分组必须恰好覆盖本次运行分配的声明集合，不得丢组')
}
const agentCount = groups.length * (expectationContext === null ? 1 : 2)
if (groups.length > 4096 || agentCount > 1000) {
  throw new Error('超过 Workflow 运行时上限；显式拆成多次运行后完整收集，不截断目标')
}

const join = (root, relative) => `${root.replace(/[\\/]$/, '')}/${relative}`
const snapshotPath = join(batchDir, 'snapshot.json')
const schemaPath = join(repoRoot, 'statement_workflow/SCHEMA_V2.md')
const receiptSchema = {
  type: 'object',
  properties: {
    result_path: { type: 'string' },
    count: { type: 'integer', minimum: 0 },
  },
  required: ['result_path', 'count'],
  additionalProperties: false,
}
const options = (agentType, phase, label) => ({
  agentType, phase, label, schema: receiptSchema, model: config.worker.model,
  ...(effort === null ? {} : { effort }),
})
const checkReceipt = (receipt, path, group) => {
  if (!receipt || receipt.result_path !== path || receipt.count !== group.declarationIds.length) {
    throw new Error(`${group.key} 未返回完整文件回执；停止该组，不启动后续阶段`)
  }
  return receipt
}

log(`调度 ${groups.length} 组、${selected.size} 条声明、${agentCount} 个 agents；文件回执不代表内容已校验或模型已核实`)
const results = await pipeline(
  groups,
  async group => {
    const path = join(resultDir, `${group.key}-readback.json`)
    const input = {
      repo_root: repoRoot,
      schema_path: schemaPath,
      prompt_path: join(skillDir, 'references/worker-prompt.md'),
      snapshot_path: snapshotPath,
      topics: config.topics,
      declaration_ids: group.declarationIds,
      result_path: path,
    }
    const receipt = await agent(
      `执行纯 Lean 回译。先读 prompt_path 和 schema_path，再按以下 JSON 数据完成本组。
只读本阶段分配的材料和必要冻结 Lean 定义，不遍历批次目录或读取其他阶段文件。
只写指定结果，权限 0600。不要自行确认模型身份，返回文件回执。
任务数据：${JSON.stringify(input)}`,
      options('formaliscope-readback', 'Lean 回译', `${group.key}:readback`),
    )
    return checkReceipt(receipt, path, group)
  },
  async (readback, group) => {
    if (!readback) return null
    if (expectationContext === null) {
      return { key: group.key, readback_path: readback.result_path, result_path: readback.result_path, count: readback.count }
    }
    const path = join(resultDir, `${group.key}.json`)
    const input = {
      repo_root: repoRoot,
      schema_path: schemaPath,
      prompt_path: join(skillDir, 'references/expectation-prompt.md'),
      snapshot_path: snapshotPath,
      readback_path: readback.result_path,
      expectation_context_path: expectationContext,
      declaration_ids: group.declarationIds,
      result_path: path,
    }
    const receipt = await agent(
      `执行独立预期判断。先读 prompt_path 和 schema_path，再按以下 JSON 数据完成本组。
第一阶段文件是固定基线，只修改 expectation_assessment；不反写原文件或改动其他字段。
只写指定的新结果，权限 0600。不要自行确认模型身份，返回文件回执。
任务数据：${JSON.stringify(input)}`,
      options('formaliscope-expectation', '内部预期判断', `${group.key}:expectation`),
    )
    checkReceipt(receipt, path, group)
    return { key: group.key, readback_path: readback.result_path, result_path: receipt.result_path, count: receipt.count }
  },
)
const incomplete = groups.filter((group, index) => !results[index]).map(group => group.key)
if (incomplete.length) {
  log(`未完成分组：${incomplete.join(', ')}；禁止当作完整批次收集`)
}
return {
  complete: incomplete.length === 0,
  groups: results.filter(Boolean),
  incomplete_groups: incomplete,
  expected_count: selected.size,
}
