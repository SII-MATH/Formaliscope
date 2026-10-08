"""Validate Workflow inputs, stage isolation and failure handling with simulated agents."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from skills.scripts.config import load_config


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
        self.args = {
            'repoRoot': '/fixture/repo',
            'skillDir': '/fixture/repo/.claude/skills/formaliscope-enrich',
            'batchDir': '/fixture/repo/.formaliscope/tasks/batches/test',
            'config': load_config(SKILL / 'config.json'),
            'declarationIds': ['statement::Example.a', 'statement::Example.b', 'statement::Example.c'],
            'groups': [
                {'key': 'group-1', 'declarationIds': ['statement::Example.a']},
                {'key': 'group-2', 'declarationIds': ['statement::Example.b', 'statement::Example.c']},
            ],
            'expectationContext': '/fixture/repo/.formaliscope/tasks/batches/test/expectation-context.txt',
        }

    def run_workflow(self, args=..., mode='normal'):
        harness = r'''
const calls = [], logs = [];
const mockAgent = async (prompt, opts) => {
  const input = JSON.parse(prompt.split('任务数据：')[1]);
  const key = opts.label.split(':')[0];
  calls.push({prompt, opts, input});
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
        self.assertEqual(len(output['calls']), 4)
        self.assertEqual(output['result']['expected_count'], 3)
        self.assertEqual(output['result']['incomplete_groups'], [])
        for call in output['calls']:
            self.assertEqual(call['opts']['model'], 'luna6')
            self.assertNotIn('effort', call['opts'])
            self.assertNotIn('isolation', call['opts'])
            self.assertNotIn('resume', call['opts'])
            self.assertEqual(call['opts']['schema']['required'], ['result_path', 'count'])
        readbacks = [call for call in output['calls'] if call['opts']['agentType'] == 'formaliscope-readback']
        expectations = [call for call in output['calls'] if call['opts']['agentType'] == 'formaliscope-expectation']
        for readback, expectation in zip(readbacks, expectations):
            self.assertEqual(expectation['input']['readback_path'], readback['input']['result_path'])
            self.assertEqual(expectation['input']['declaration_ids'], readback['input']['declaration_ids'])
            self.assertNotEqual(expectation['input']['result_path'], readback['input']['result_path'])
            self.assertEqual(expectation['input']['expectation_context_path'], self.args['expectationContext'])
            self.assertEqual(expectation['opts']['phase'], '内部预期判断')

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

    def test_absent_expectation_uses_readback_as_final_without_extra_agents(self):
        self.args['expectationContext'] = None
        output = self.run_workflow()
        self.assertTrue(output['result']['complete'])
        self.assertEqual(len(output['calls']), 2)
        for group in output['result']['groups']:
            self.assertEqual(group['result_path'], group['readback_path'])

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
        for mutation in ('missing', 'duplicate', 'unknown', 'empty', 'key', 'duplicate-key', 'duplicate-manifest'):
            args = deepcopy(self.args)
            if mutation == 'missing': args['groups'].pop()
            if mutation == 'duplicate': args['groups'][1]['declarationIds'].append('statement::Example.a')
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
                self.assertEqual([group['key'] for group in output['result']['groups']], ['group-2'])
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
        output = self.run_workflow()
        self.assertIn('超过 Workflow', output['error'])
        self.assertEqual(output['calls'], [])


if __name__ == '__main__':
    unittest.main()
