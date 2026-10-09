"""Validate Workflow inputs, stage isolation and failure handling with simulated agents."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from skills.scripts.config import load_config, resolve_worker_model
from skills.scripts.prepare import prepare
from review_app.statements import compile_statements


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/claude-code/formaliscope-enrich'
JSC = Path('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc')


class ClaudeWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which('node')
        if node:
            cls.runtime = [node, '-e']
            cls.print_expression = 'console.log'
        elif JSC.is_file():
            cls.runtime = [str(JSC), '-e']
            cls.print_expression = 'print'
        else:
            raise unittest.SkipTest('Workflow execution tests need Node.js or JavaScriptCore')
        cls.source = (SKILL / 'workflows/enrich.js').read_text(encoding='utf-8')

    def setUp(self):
        settings = load_config(SKILL / 'config.json')
        config = {key: deepcopy(settings[key]) for key in ('schema', 'worker', 'topics')}
        config['worker']['model'] = resolve_worker_model(settings)
        self.args = {
            'repoRoot': '/fixture/repo',
            'skillDir': '/fixture/repo/.claude/skills/formaliscope-enrich',
            'batchDir': '/fixture/repo/.formaliscope/tasks/batches/test',
            'config': config,
            'declarationIds': ['statement::Example.a', 'statement::Example.b', 'statement::Example.c'],
            'groups': [
                {'key': 'group-1', 'declarationIds': ['statement::Example.a']},
                {'key': 'group-2', 'declarationIds': ['statement::Example.b']},
                {'key': 'group-3', 'declarationIds': ['statement::Example.c']},
            ],
            'expectationContext': '/fixture/repo/.formaliscope/tasks/batches/test/expectation-context.txt',
        }

    def run_workflow(self, args=..., mode='normal'):
        harness = r'''
const calls = [], logs = [];
let releaseReadback;
const slowReadback = new Promise(resolve => { releaseReadback = resolve; });
const mockAgent = async (prompt, opts) => {
  const input = JSON.parse(prompt.split('任务数据：')[1]);
  const key = opts.label.split(':')[0];
  calls.push({prompt, opts, input});
  if (MODE === 'pipeline-progress') {
    if (key === 'group-1' && opts.agentType === 'formaliscope-readback') await slowReadback;
    if (key === 'group-2' && opts.agentType === 'formaliscope-expectation') releaseReadback();
  }
  if (key === 'group-1') {
    if (MODE === 'skip-readback' && opts.agentType === 'formaliscope-readback') return null;
    if (MODE === 'fail-readback' && opts.agentType === 'formaliscope-readback') throw new Error('fixture API error');
    if (MODE === 'skip-expectation' && opts.agentType === 'formaliscope-expectation') return null;
  }
  return {
    result_path: MODE === 'wrong-path' && key === 'group-1' ? '/wrong.json' : input.result_path,
    count: MODE === 'wrong-count' && key === 'group-1' ? 0 : input.declaration_ids.length,
  };
};
const mockPipeline = async (items, ...stages) => Promise.all(items.map(async (item, index) => {
  let result = item;
  for (const stage of stages) {
    try { result = await stage(result, item, index); }
    catch (_) { return null; }
  }
  return result;
}));
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const run = new AsyncFunction('args', 'agent', 'pipeline', 'log', SOURCE.replace('export const meta', 'const meta'));
run(INPUT, mockAgent, mockPipeline, message => logs.push(message)).then(
  result => OUTPUT(JSON.stringify({result, calls, logs})),
  error => OUTPUT(JSON.stringify({error: String(error), calls, logs})),
);
'''
        script = ('const SOURCE = ' + json.dumps(self.source) + ';\n' +
                  'const INPUT = ' + json.dumps(self.args if args is ... else args) + ';\n' +
                  'const MODE = ' + json.dumps(mode) + ';\n' +
                  'const OUTPUT = ' + self.print_expression + ';\n' + harness)
        completed = subprocess.run(self.runtime + [script], capture_output=True, text=True,
                                   timeout=20, check=True)
        return json.loads(completed.stdout)

    def test_two_stages_have_fresh_agents_explicit_route_and_file_receipts(self):
        output = self.run_workflow()
        self.assertNotIn('error', output)
        self.assertTrue(output['result']['complete'])
        self.assertEqual(len(output['calls']), 6)
        self.assertEqual(output['result']['expected_count'], 3)
        self.assertEqual(output['result']['incomplete_groups'], [])
        for call in output['calls']:
            self.assertEqual(call['opts']['model'], 'sonnet')
            self.assertEqual(len(call['input']['declaration_ids']), 1)
            self.assertNotIn('effort', call['opts'])
            self.assertNotIn('isolation', call['opts'])
            self.assertNotIn('resume', call['opts'])
            self.assertEqual(call['opts']['schema']['required'], ['result_path', 'count'])
        readbacks = [call for call in output['calls'] if call['opts']['agentType'] == 'formaliscope-readback']
        expectations = [call for call in output['calls'] if call['opts']['agentType'] == 'formaliscope-expectation']
        self.assertEqual(len(readbacks), 3)
        self.assertEqual(len(expectations), 3)
        for readback, expectation in zip(readbacks, expectations):
            self.assertLess(output['calls'].index(readback), output['calls'].index(expectation))
            self.assertEqual(expectation['input']['readback_path'], readback['input']['result_path'])
            self.assertEqual(expectation['input']['declaration_ids'], readback['input']['declaration_ids'])
            self.assertNotEqual(expectation['input']['result_path'], readback['input']['result_path'])
            self.assertEqual(expectation['input']['expectation_context_path'], self.args['expectationContext'])
            self.assertEqual(expectation['opts']['phase'], '内部预期判断')

    def test_stage_contracts_delivery_commands_and_allocated_paths_are_separate(self):
        output = self.run_workflow()
        paths = []
        for call in output['calls']:
            data = call['input']
            readback = call['opts']['agentType'] == 'formaliscope-readback'
            stage = 'readback' if readback else 'expectation'
            self.assertEqual(data['output_schema_path'], self.args['repoRoot'] +
                             f'/statement_workflow/schema/statement-{stage}-batch.v1.schema.json')
            self.assertEqual(data['manifest_path'], self.args['batchDir'] + '/manifest.json')
            self.assertEqual(data['delivery_script'], self.args['repoRoot'] + '/skills/scripts/collect.py')
            self.assertEqual(data['draft_path'], data['result_path'] + '.input.json')
            paths.extend([data['draft_path'], data['result_path']])
            self.assertIn(f'--deliver-{stage}', call['prompt'])
            self.assertNotIn('formaliscope-agent-batch.v2', call['prompt'])
            if readback:
                self.assertIn('不生成 expectation_assessment', call['prompt'])
                self.assertIn('不得直接写正式结果或自报摘要', call['prompt'])
                expectation = next(item for item in output['calls'] if
                                   item['opts']['label'] == call['opts']['label'].replace(':readback', ':expectation'))
                self.assertEqual(data['next_result_path'], expectation['input']['result_path'])
                self.assertNotIn('baseline_path', data)
            else:
                self.assertNotIn('baseline_path', data)
                self.assertIn('--check-readback', call['prompt'])
                self.assertIn('仅输出 declaration_id 和 expectation_assessment', call['prompt'])
                self.assertIn('不复制或输出正文、标题、分类、优先度及回译分值', call['prompt'])
                self.assertNotIn('next_result_path', data)
        self.assertEqual(len(paths), len(set(paths)))

    def test_generated_stage_commands_run_actual_cli_with_quoted_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "stage's files; $literal"
            source = root / 'source'
            (source / 'KIP126').mkdir(parents=True)
            (source / 'KIP126/Example.lean').write_text(
                'namespace Example\ndef a : Nat := 1\ndef b : Nat := 2\ndef c : Nat := 3\nend Example\n')
            snapshot = compile_statements(source, source_commit='a' * 40)
            snapshot_path = root / 'source-snapshot.json'
            snapshot_path.write_text(json.dumps(snapshot))
            batch = root / 'batch'
            manifest = prepare(snapshot_path, batch, config_path=SKILL / 'config.json',
                               directories=['KIP126'])
            args = deepcopy(self.args)
            args.update(repoRoot=str(ROOT), skillDir=str(SKILL), batchDir=str(batch),
                        declarationIds=manifest['declaration_ids'], expectationContext=None,
                        config=json.loads((batch / 'agent-config.json').read_text()))
            args['groups'] = [{'key': f'group-{index}', 'declarationIds': [identity]}
                              for index, identity in enumerate(manifest['declaration_ids'], 1)]
            output = self.run_workflow(args)
            self.assertTrue(output['result']['complete'])
            env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep +
                       os.environ.get('PATH', ''))
            for call in output['calls']:
                data = call['input']
                identity = data['declaration_ids'][0]
                commands = [data['delivery_command']]
                if call['opts']['agentType'] == 'formaliscope-readback':
                    annotation = {
                        'declaration_id': identity, 'title_zh': '测试标题',
                        'readback': {'text_zh': '[TEST] 合成协议回译。', 'confidence': 0.9},
                        'classification': {'role': 'definition', 'topics': []}, 'priority': None,
                    }
                    stage = 'readback'
                else:
                    commands.insert(0, data['check_readback_command'])
                    annotation = {'declaration_id': identity, 'expectation_assessment': {
                        'verdict': 'undetermined', 'reason_zh': '测试未提供独立预期材料。',
                        'confidence': 0.95}}
                    stage = 'expectation'
                Path(data['draft_path']).write_text(json.dumps({
                    'schema': f'formaliscope-{stage}-batch.v1', 'annotations': [annotation]}))
                for command in commands:
                    argv = shlex.split(command)
                    self.assertNotIn('--baseline', argv)
                    self.assertNotIn('--baseline-path', argv)
                    result = subprocess.run(['bash', '-c', command], cwd=temporary, env=env,
                                            capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    receipt = json.loads(result.stdout)
                    self.assertEqual(receipt['count'], 1)
                    expected = (data['readback_path'] if '--check-readback' in argv
                                else data['result_path'])
                    self.assertEqual(receipt['result_path'], expected)
                self.assertEqual(json.loads(Path(data['result_path']).read_text())['annotations'],
                                 [annotation])

    def test_shared_harness_prompts_use_stage_schemas_and_fixed_delivery(self):
        for harness in ('claude-code', 'codex', 'kimi-code'):
            skill = ROOT / 'skills' / harness / 'formaliscope-enrich'
            worker = (skill / 'references/worker-prompt.md').read_text(encoding='utf-8')
            expectation = (skill / 'references/expectation-prompt.md').read_text(encoding='utf-8')
            with self.subTest(harness=harness):
                self.assertIn('formaliscope-readback-batch.v1', worker)
                self.assertIn('--deliver-readback', worker)
                self.assertIn('draft_path', worker)
                self.assertIn('formaliscope-expectation-batch.v1', expectation)
                self.assertIn('--check-readback', expectation)
                self.assertIn('--deliver-expectation', expectation)
                self.assertIn('draft_path', expectation)
                for text in (worker, expectation):
                    self.assertNotIn('formaliscope-agent-batch.v2', text)
                for text, stage, fields in (
                    (worker, 'readback', {'declaration_id', 'title_zh', 'readback', 'classification', 'priority'}),
                    (expectation, 'expectation', {'declaration_id', 'expectation_assessment'}),
                ):
                    examples = [json.loads(block.split('```', 1)[0]) for block in text.split('```json\n')[1:]]
                    document = next(value for value in examples if value.get('schema') == f'formaliscope-{stage}-batch.v1')
                    self.assertEqual(set(document['annotations'][0]), fields)

    def test_groups_advance_without_waiting_for_other_readbacks(self):
        output = self.run_workflow(mode='pipeline-progress')
        self.assertTrue(output['result']['complete'])
        labels = [call['opts']['label'] for call in output['calls']]
        self.assertLess(labels.index('group-2:expectation'), labels.index('group-1:expectation'))
        self.assertEqual(len(labels), 6)

    def test_readback_receives_no_expectation_path_or_material_or_other_groups(self):
        output = self.run_workflow()
        for call in output['calls']:
            if call['opts']['agentType'] != 'formaliscope-readback':
                continue
            self.assertNotIn('expectation-context', call['prompt'])
            self.assertNotIn('expectation_context_path', call['input'])
            self.assertNotIn('config', call['input'])
            self.assertEqual(call['input']['topics'], self.args['config']['topics'])
            self.assertEqual(call['input']['snapshot_path'], self.args['batchDir'] + '/snapshot.json')
            self.assertEqual(call['opts']['phase'], 'Lean 回译')
            assigned = next(group['declarationIds'] for group in self.args['groups']
                            if call['opts']['label'].startswith(group['key'] + ':'))
            self.assertEqual(call['input']['declaration_ids'], assigned)

    def test_absent_expectation_still_runs_independent_second_agent_per_declaration(self):
        self.args['expectationContext'] = None
        output = self.run_workflow()
        self.assertTrue(output['result']['complete'])
        self.assertEqual(len(output['calls']), 6)
        expectations = [call for call in output['calls']
                        if call['opts']['agentType'] == 'formaliscope-expectation']
        self.assertEqual(len(expectations), 3)
        for call in expectations:
            self.assertIsNone(call['input']['expectation_context_path'])
            self.assertIn('undetermined', call['prompt'])
            self.assertIn('缺少独立预期材料', call['prompt'])
        for group in output['result']['groups']:
            self.assertNotEqual(group['result_path'], group['readback_path'])

    def test_route_and_supported_effort_come_from_frozen_config(self):
        self.args['config']['worker'] = {'model': 'fixture-custom-route', 'reasoning_effort': 'high'}
        output = self.run_workflow()
        self.assertTrue(output['result']['complete'])
        for call in output['calls']:
            self.assertEqual(call['opts']['model'], 'fixture-custom-route')
            self.assertEqual(call['opts']['effort'], 'high')

    def test_invalid_args_reject_before_any_agent(self):
        missing_ids = deepcopy(self.args)
        del missing_ids['declarationIds']
        invalid = [json.dumps(self.args), None, missing_ids]
        for field, value in (('groups', []), ('declarationIds', []), ('repoRoot', 'relative'),
                             ('skillDir', None), ('expectationContext', 'relative')):
            args = deepcopy(self.args)
            args[field] = value
            invalid.append(args)
        for effort in ('none', 'minimal', 'ultra', 'unsupported'):
            args = deepcopy(self.args)
            args['config']['worker']['reasoning_effort'] = effort
            invalid.append(args)
        for model in ('', ' '):
            args = deepcopy(self.args)
            args['config']['worker']['model'] = model
            invalid.append(args)
        args = deepcopy(self.args)
        args['config']['schema'] = 'formaliscope-enrichment-config.v1'
        invalid.append(args)
        for args in invalid:
            with self.subTest(args=args):
                output = self.run_workflow(args=args)
                self.assertIn('error', output)
                self.assertEqual(output['calls'], [])

    def test_group_partition_rejects_missing_duplicates_unknown_empty_and_unsafe_keys(self):
        invalid = []
        for mutation in ('missing', 'duplicate', 'unknown', 'empty', 'multiple', 'key', 'duplicate-key', 'duplicate-manifest'):
            args = deepcopy(self.args)
            if mutation == 'missing': args['groups'].pop()
            if mutation == 'duplicate': args['groups'][1]['declarationIds'] = ['statement::Example.a']
            if mutation == 'multiple':
                args['groups'][1]['declarationIds'].extend(args['groups'].pop()['declarationIds'])
            if mutation == 'unknown': args['groups'][0]['declarationIds'] = ['statement::Unknown']
            if mutation == 'empty': args['groups'][0]['declarationIds'] = []
            if mutation == 'key': args['groups'][0]['key'] = '../result'
            if mutation == 'duplicate-key': args['groups'][1]['key'] = 'group-1'
            if mutation == 'duplicate-manifest': args['declarationIds'].append('statement::Example.a')
            invalid.append((mutation, args))
        for mutation, args in invalid:
            with self.subTest(mutation=mutation):
                output = self.run_workflow(args=args)
                self.assertIn('error', output)
                self.assertEqual(output['calls'], [])

    def test_skipped_failed_or_invalid_receipts_are_not_reported_as_full_success(self):
        for mode in ('skip-readback', 'fail-readback', 'skip-expectation', 'wrong-path', 'wrong-count'):
            with self.subTest(mode=mode):
                output = self.run_workflow(mode=mode)
                self.assertFalse(output['result']['complete'])
                self.assertEqual(output['result']['incomplete_groups'], ['group-1'])
                self.assertEqual([group['key'] for group in output['result']['groups']], ['group-2', 'group-3'])
                self.assertTrue(any('group-1' in line for line in output['logs']))
                if mode != 'skip-expectation':
                    self.assertFalse(any(call['opts']['label'] == 'group-1:expectation'
                                         for call in output['calls']))

    def test_regeneration_changes_only_result_directory_not_frozen_inputs(self):
        self.args['resultDir'] = self.args['batchDir'] + '/retry-1'
        output = self.run_workflow()
        self.assertTrue(output['result']['complete'])
        for call in output['calls']:
            self.assertTrue(call['input']['result_path'].startswith(self.args['resultDir'] + '/'))
            self.assertEqual(call['input']['snapshot_path'], self.args['batchDir'] + '/snapshot.json')
            if call['opts']['agentType'] == 'formaliscope-expectation':
                self.assertTrue(call['input']['readback_path'].startswith(self.args['resultDir'] + '/'))
                self.assertEqual(call['input']['expectation_context_path'], self.args['expectationContext'])

    def test_runtime_cap_is_explicit_and_never_truncates_groups(self):
        self.args['declarationIds'] = ['statement::Example.' + str(index) for index in range(501)]
        self.args['groups'] = [{'key': 'group-' + str(index + 1), 'declarationIds': [identity]}
                               for index, identity in enumerate(self.args['declarationIds'])]
        for context in (self.args['expectationContext'], None):
            with self.subTest(context=context):
                self.args['expectationContext'] = context
                output = self.run_workflow()
                self.assertIn('超过 Workflow', output['error'])
                self.assertEqual(output['calls'], [])


if __name__ == '__main__':
    unittest.main()
