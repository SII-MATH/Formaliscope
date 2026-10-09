export const meta = {
  name: 'formaliscope-enrich-groups',
  description: '按固定分组流水线生成纯 Lean 回译及独立内部预期判断',
  phases: [
    { title: 'Lean 回译' },
    { title: '内部预期判断' },
  ],
}

if (!args || typeof args !== 'object' || Array.isArray(args)) {
  throw new Error('args 请使用结构化对象')
}
const { repoRoot, skillDir, batchDir, resultDir = batchDir, config, declarationIds, groups, expectationContext } = args
for (const [name, path] of Object.entries({ repoRoot, skillDir, batchDir, resultDir })) {
  if (typeof path !== 'string' || !/^(\/|[A-Za-z]:[\\/])/.test(path)) {
    throw new Error(`${name} 必须是绝对路径`)
  }
  if (/[\\/](?:\.|\.\.)(?:[\\/]|$)/.test(path)) {
    throw new Error(`${name} 必须预先规范化，不能包含 . 或 .. 路径段`)
  }
}
if (expectationContext !== null &&
    (typeof expectationContext !== 'string' || !/^(\/|[A-Za-z]:[\\/])/.test(expectationContext) ||
      /[\\/](?:\.|\.\.)(?:[\\/]|$)/.test(expectationContext))) {
  throw new Error('expectationContext 必须是固定预期材料的规范化绝对路径或 null')
}
if (config?.schema !== 'formaliscope-enrichment-config.v2' ||
    typeof config.worker?.model !== 'string' || !config.worker.model.trim() ||
    !Array.isArray(config.topics)) {
  throw new Error('config 必须来自本批固定的 agent-config.json')
}
const effort = config.worker.reasoning_effort
if (effort !== null && !['low', 'medium', 'high', 'xhigh', 'max'].includes(effort)) {
  throw new Error('effort 请使用本 Workflow 支持的 low/medium/high/xhigh/max 或 null')
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
      keys.has(group.key) || !Array.isArray(group.declarationIds) || group.declarationIds.length !== 1) {
    throw new Error('每组必须有唯一 group-N key 和恰好一条声明')
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
  throw new Error('请补齐分组，使其恰好覆盖本次运行分配的声明集合')
}
const agentCount = groups.length * 2
if (groups.length > 4096 || agentCount > 1000) {
  throw new Error('超过 Workflow 运行时上限；显式拆成多次运行后完整收集，不截断目标')
}

const join = (root, relative) => `${root.replace(/[\\/]$/, '')}/${relative}`
const snapshotPath = join(batchDir, 'snapshot.json')
const manifestPath = join(batchDir, 'manifest.json')
const deliveryScript = join(repoRoot, 'skills/scripts/collect.py')
const command = (...argv) => argv.map(value => `'${value.replace(/'/g, "'\\''")}'`).join(' ')
const deliveryCommand = (...argv) => command('python3', deliveryScript, ...argv)
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
    throw new Error(`${group.key} 的文件回执待补齐；该组后续阶段等待完整回执`)
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
      output_schema_path: join(repoRoot, 'statement_workflow/schema/statement-readback-batch.v1.schema.json'),
      delivery_script: deliveryScript,
      manifest_path: manifestPath,
      snapshot_path: snapshotPath,
      topics: config.topics,
      declaration_ids: group.declarationIds,
      draft_path: `${path}.input.json`,
      next_result_path: join(resultDir, `${group.key}.json`),
      result_path: path,
      output_template: {
        schema: 'formaliscope-readback-batch.v1',
        annotations: [{ declaration_id: group.declarationIds[0], title_zh: null,
          readback: { text_zh: null, confidence: null },
          classification: { role: null, topics: [] }, priority: null }],
      },
    }
    input.delivery_command = deliveryCommand('--deliver-readback', '--snapshot', snapshotPath,
      '--manifest', manifestPath, '--input', input.draft_path, '--result', path,
      '--declaration-id', group.declarationIds[0], '--next-result', input.next_result_path)
    const receipt = await agent(
      `执行纯 Lean 回译。先读取 prompt_path、output_schema_path 和 schema_path 的绝对路径，再按以下 JSON 数据完成本组。
所有路径以任务数据为准，不拼接当前工作目录、repo_root 或额外的 .formaliscope 前缀。
以本组 cards 的 ID 与 Lean 字段、必要 modules 定义为回译依据。
只填写第一阶段字段，不生成 expectation_assessment。
按 output_template 的结构填写，confidence 的 null 是待填写标记，必须换成自主判断的 0–1 数值；不交付未填写的模板。
先保存 draft_path，再原样执行 delivery_command 交付至 result_path 并封存摘要；不得直接写正式结果或自报摘要。
交付命令：${input.delivery_command}
固定程序成功后返回其文件回执；失败停止本组，不改写原文件。
任务数据：${JSON.stringify(input)}`,
      options('formaliscope-readback', 'Lean 回译', `${group.key}:readback`),
    )
    return checkReceipt(receipt, path, group)
  },
  async (readback, group) => {
    if (!readback) return null
    const path = join(resultDir, `${group.key}.json`)
    const input = {
      repo_root: repoRoot,
      schema_path: schemaPath,
      prompt_path: join(skillDir, 'references/expectation-prompt.md'),
      output_schema_path: join(repoRoot, 'statement_workflow/schema/statement-expectation-batch.v1.schema.json'),
      delivery_script: deliveryScript,
      manifest_path: manifestPath,
      snapshot_path: snapshotPath,
      readback_path: readback.result_path,
      expectation_context_path: expectationContext,
      declaration_ids: group.declarationIds,
      draft_path: `${path}.input.json`,
      result_path: path,
      output_template: {
        schema: 'formaliscope-expectation-batch.v1',
        annotations: [{ declaration_id: group.declarationIds[0],
          expectation_assessment: { verdict: null, reason_zh: null, confidence: null } }],
      },
    }
    input.check_readback_command = deliveryCommand('--check-readback', '--snapshot', snapshotPath,
      '--manifest', manifestPath, '--readback-result', input.readback_path)
    input.delivery_command = deliveryCommand('--deliver-expectation', '--snapshot', snapshotPath,
      '--manifest', manifestPath, '--readback-result', input.readback_path,
      '--input', input.draft_path, '--result', path)
    const receipt = await agent(
      `执行独立预期判断。先读取 prompt_path、output_schema_path 和 schema_path 的绝对路径，再按以下 JSON 数据完成本组。
所有路径以任务数据为准，不拼接当前工作目录、repo_root 或额外的 .formaliscope 前缀。
先原样执行 check_readback_command 检查基线摘要，成功后只读基线和预期材料。
检查命令：${input.check_readback_command}
基线记录由检查程序自动定位。
仅输出 declaration_id 和 expectation_assessment，不复制或输出正文、标题、分类、优先度及回译分值。
按 output_template 的结构填写；expectation_assessment 必须同时包含 verdict、reason_zh、confidence。verdict 与 confidence 的 null 是待填写标记，必须替换为独立判断，不能改字段名或交付未填写的模板。
expectation_context_path 为 null 时，判断填 undetermined 并说明缺少独立预期材料。
保存 draft_path，再原样执行 delivery_command 排他交付；成功后返回程序文件回执，保留基线和原始输入。
交付命令：${input.delivery_command}
任务数据：${JSON.stringify(input)}`,
      options('formaliscope-expectation', '内部预期判断', `${group.key}:expectation`),
    )
    checkReceipt(receipt, path, group)
    return { key: group.key, readback_path: readback.result_path, result_path: receipt.result_path, count: receipt.count }
  },
)
const incomplete = groups.filter((group, index) => !results[index]).map(group => group.key)
if (incomplete.length) {
  log(`未完成分组：${incomplete.join(', ')}；完整收集等待这些组交付`)
}
return {
  complete: incomplete.length === 0,
  groups: results.filter(Boolean),
  incomplete_groups: incomplete,
  expected_count: selected.size,
}
