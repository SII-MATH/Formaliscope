"""Private v2 batch preparation, two-stage binding and confidence routing."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_app.enrichment import validate_enrichment
from review_app.statements import compile_statements
from skills.scripts.config import load_config, resolve_worker_model


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/scripts'


def load(name):
    spec = importlib.util.spec_from_file_location('formaliscope_v2_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector, preparer = load('collect'), load('prepare')


class SkillBatchV2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / 'source'
        (source / 'KIP126/Sub').mkdir(parents=True)
        (source / 'KIP126/Sub/Example.lean').write_text(
            'namespace Example\ndef a : Nat := 1\ndef b : Nat := 2\ndef c : Nat := 3\nend Example\n')
        (source / 'KIP126/Other.lean').write_text('def other : Nat := 4\n')
        self.snapshot = compile_statements(source, source_commit='a' * 40)
        self.snapshot_path = self.write('source-snapshot.json', self.snapshot)
        self.config = {'schema': 'formaliscope-enrichment-config.v2',
                       'worker': {'model': 'fixture-worker', 'reasoning_effort': 'high'},
                       'topics': [{'id': 'fixture_topic', 'name': '夹具主题'}]}
        self.config_path = self.write('config.json', self.config)
        self.batch = self.root / 'batch'
        self.output = self.root / 'collected'
        self.result_path = self.root / 'worker.json'
        self.first_path = self.root / 'first-stage.json'
        self.prepare()
        self.annotations = [self.annotation(identity, score) for identity, score in
                            zip(self.manifest['declaration_ids'], (0.79, 0.8, 0.95))]
        self.result = {'schema': 'formaliscope-agent-batch.v2', 'annotations': self.annotations}
        self.ids = [row['declaration_id'] for row in self.annotations]

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def annotation(self, identity, score):
        return {'declaration_id': identity, 'title_zh': '合成测试标题',
                'readback': {'text_zh': '[TEST] 合成协议回译，非实际数学标注。', 'confidence': score},
                'classification': {'role': 'definition', 'topics': ['fixture_topic']},
                'priority': None,
                'expectation_assessment': {'verdict': 'undetermined',
                                          'reason_zh': '未提供该声明的独立预期。', 'confidence': 0.95}}

    def historical_batch(self, batch):
        manifest = collector._read(batch / 'manifest.json')
        manifest['schema'] = 'formaliscope-enrichment-batch.v2'
        del manifest['result_protocol'], manifest['harness']
        (batch / 'manifest.json').write_text(json.dumps(manifest))
        return manifest

    def prepare(self, *, output=None, **kwargs):
        batch = output or self.batch
        preparer.prepare(self.snapshot_path, batch, config_path=self.config_path,
                         directories=['KIP126/Sub'], **kwargs)
        self.manifest = self.historical_batch(batch)
        return self.manifest

    def collect(self, *, reviews=None, results=None, first=None, model='fixture-worker'):
        self.result_path.write_text(json.dumps(self.result, ensure_ascii=False), encoding='utf-8')
        return collector.collect(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                 results or [self.result_path], reviews or [], self.output,
                                 executed_model=model, readback_paths=first)

    def review(self, index=0):
        return {'declaration_id': self.ids[index], 'annotation': deepcopy(self.annotations[index]),
                'model': 'fixture-main', 'reviewed_at': '2026-10-04T09:00:00+08:00'}

    def review_file(self, rows):
        return self.write('reviews.json', {'schema': 'formaliscope-enrichment-review.v2', 'reviews': rows})

    def rejected(self, **kwargs):
        with self.assertRaises(ValueError):
            self.collect(**kwargs)
        self.assertFalse(self.output.exists())

    def context_batch(self):
        context = self.root / 'expectation.txt'
        context.write_text('独立的预期说明，测试夹具。', encoding='utf-8')
        self.batch = self.root / 'with-context'
        self.prepare(expectation_context=context)
        self.first_path = self.write('first-stage.json', deepcopy(self.result))
        return context

    def test_prepare_records_selection_config_context_and_private_permissions(self):
        context = self.context_batch()
        self.assertEqual(len(self.manifest['declaration_ids']), 3)
        self.assertEqual(self.manifest['run']['model'], 'fixture-worker')
        self.assertEqual(self.manifest['run']['expectation_context_digest'],
                         hashlib.sha256(context.read_bytes()).hexdigest())
        self.assertEqual((self.batch / 'expectation-context.txt').read_bytes(), context.read_bytes())
        self.assertEqual(collector._read(self.batch / 'agent-config.json'), self.config)
        self.assertEqual(stat.S_IMODE(self.batch.stat().st_mode), 0o700)
        for path in self.batch.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),
                             0o400 if path.name == 'snapshot.json' else 0o600)
        self.config['worker']['model'] = 'another-model'
        self.write('config.json', self.config)
        self.assertEqual(collector._read(self.batch / 'agent-config.json')['worker']['model'], 'fixture-worker')
        self.snapshot_path.write_text('{}')
        self.assertEqual(collector._read(self.batch / 'snapshot.json'), self.snapshot)

    def test_tasks_share_one_snapshot_and_survive_original_input_removal(self):
        other = self.root / 'second-task'
        self.prepare(output=other)
        link = self.batch / 'snapshot.json'
        self.assertTrue(link.is_symlink())
        self.assertFalse(Path(link.readlink()).is_absolute())
        self.assertEqual(link.resolve(), (other / 'snapshot.json').resolve())
        self.assertEqual(len(list((self.root / '.snapshots').iterdir())), 1)
        self.assertEqual(stat.S_IMODE(link.resolve().parent.stat().st_mode), 0o700)
        self.snapshot_path.unlink()
        self.assertEqual(collector._read(link), self.snapshot)
        self.collect()

    def test_changed_input_uses_a_new_shared_snapshot_without_changing_old_tasks(self):
        old_target = (self.batch / 'snapshot.json').resolve()
        source = self.root / 'source/KIP126/Sub/Example.lean'
        source.write_text(source.read_text().replace(':= 1', ':= 5'))
        newer = compile_statements(self.root / 'source', source_commit='b' * 40)
        self.snapshot_path.write_text(json.dumps(newer))
        other = self.root / 'new-source-task'
        self.prepare(output=other)
        self.assertNotEqual(old_target, (other / 'snapshot.json').resolve())
        self.assertEqual(collector._read(self.batch / 'snapshot.json'), self.snapshot)
        self.assertEqual(collector._read(other / 'snapshot.json'), newer)

    def test_corrupted_shared_snapshot_is_rejected_without_overwriting_or_output(self):
        target = (self.batch / 'snapshot.json').resolve()
        target.chmod(0o600)
        target.write_text('{}')
        self.rejected()
        other = self.root / 'corrupted-task'
        with self.assertRaisesRegex(ValueError, 'shared snapshot content changed'):
            self.prepare(output=other)
        self.assertFalse(other.exists())
        self.assertEqual(target.read_text(), '{}')

    def test_old_v2_task_with_full_snapshot_remains_collectable(self):
        link = self.batch / 'snapshot.json'
        raw = link.read_bytes()
        link.unlink()
        link.write_bytes(raw)
        link.chmod(0o600)
        self.collect()

    def blueprint_batch(self, *, additional=None):
        source = self.root / 'source'
        (source / 'blueprint/src').mkdir(parents=True)
        (source / 'blueprint/src/content.tex').write_text('\\input{chapter}\n')
        (source / 'blueprint/src/chapter.tex').write_text(
            '\\begin{definition}[Two values]\\label{def:values}'
            'The first two values are one and two.'
            '\\lean{Example.a, Example.b}\\end{definition}\n')
        self.snapshot = compile_statements(source, source_commit='a' * 40)
        self.snapshot_path = self.write('blueprint-snapshot.json', self.snapshot)
        self.batch = self.root / 'blueprint-batch'
        self.prepare(expectation_context=additional)
        self.first_path = self.write('first-stage.json', deepcopy(self.result))

    def test_blueprint_expectations_are_automatically_frozen_per_declaration(self):
        self.blueprint_batch()
        context = collector._read(self.batch / 'expectation-context.txt')
        self.assertEqual(context['source_commit'], self.snapshot['source_commit'])
        self.assertEqual(context['snapshot_digest'], self.snapshot['digest'])
        self.assertEqual(context['references'][self.ids[0]][0]['declarations'], ['Example.a', 'Example.b'])
        self.assertEqual(context['references'][self.ids[2]], [])
        self.assertEqual(self.manifest['run']['expectation_context_digest'],
                         hashlib.sha256((self.batch / 'expectation-context.txt').read_bytes()).hexdigest())
        self.annotations[0]['expectation_assessment'].update(verdict='aligned', reason_zh=None)
        self.annotations[1]['expectation_assessment'].update(verdict='misaligned', reason_zh='测试夹具的第二个值不同。')
        self.collect(first=[self.first_path])

    def test_blueprint_context_does_not_authorize_alignment_for_an_unbound_declaration(self):
        self.blueprint_batch()
        self.annotations[2]['expectation_assessment'].update(verdict='aligned', reason_zh=None)
        self.rejected(first=[self.first_path])

    def test_blueprint_assessment_requires_unchanged_saved_readback(self):
        self.blueprint_batch()
        self.annotations[1]['readback']['text_zh'] = '从预期反写的文字，不应接受。'
        self.rejected(first=[self.first_path])

    def test_explicit_expectation_context_supplements_blueprint_references(self):
        additional = self.root / 'additional.txt'
        additional.write_text('用户对第三条声明的明确预期。', encoding='utf-8')
        self.blueprint_batch(additional=additional)
        context = collector._read(self.batch / 'expectation-context.txt')
        self.assertEqual(context['additional_context'], additional.read_text())
        self.assertTrue(context['references'][self.ids[0]])
        self.annotations[2]['expectation_assessment'].update(verdict='aligned', reason_zh=None)
        self.collect(first=[self.first_path])

    def test_prepare_requires_explicit_valid_selection_and_unique_run_ids(self):
        for selectors in ({}, {'directories': ['missing']}, {'directories': ['../KIP126']},
                          {'files': ['/KIP126/Sub/Example.lean']},
                          {'declaration_ids': ['Unknown']}, {'directories': ['.']}):
            output = self.root / 'invalid-prepared'
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                preparer.prepare(self.snapshot_path, output, config_path=self.config_path, **selectors)
            self.assertFalse(output.exists())
        other = preparer.prepare(self.snapshot_path, self.root / 'by-id', config_path=self.config_path,
                                 declaration_ids=['Example.a', 'statement::Example.a'])
        self.assertEqual(other['declaration_ids'], ['statement::Example.a'])
        self.assertNotEqual(other['run']['run_id'], self.manifest['run']['run_id'])

    def test_prepare_file_directory_and_id_selection_union_stays_in_snapshot_order(self):
        other = preparer.prepare(self.snapshot_path, self.root / 'union', config_path=self.config_path,
                                 directories=['KIP126/Sub/'], files=['KIP126/Other.lean'],
                                 declaration_ids=['Example.a'])
        self.assertEqual(other['declaration_ids'], [card['id'] for card in self.snapshot['cards']])

    def test_invalid_topic_configuration_and_threshold_do_not_create_a_batch(self):
        original = deepcopy(self.config)
        for mutation in ('duplicate_topic', 'invalid_topic', 'empty_model', 'extra_agent_metadata'):
            self.config = deepcopy(original)
            if mutation == 'duplicate_topic': self.config['topics'].append(deepcopy(self.config['topics'][0]))
            if mutation == 'invalid_topic': self.config['topics'][0]['id'] = 'topic/old'
            if mutation == 'empty_model': self.config['worker']['model'] = ' '
            if mutation == 'extra_agent_metadata': self.config['worker']['provenance'] = {}
            self.write('config.json', self.config)
            output = self.root / 'invalid-config'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.prepare(output=output)
            self.assertFalse(output.exists())
        self.config = original
        self.write('config.json', self.config)
        for threshold in (True, -0.01, 1.01, float('nan')):
            output = self.root / 'invalid-threshold'
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.prepare(output=output, threshold=threshold)
            self.assertFalse(output.exists())

    def test_model_confirmation_required_and_mismatch_does_not_publish(self):
        self.rejected(model=None)
        self.rejected(model='silent-fallback')
        self.collect()

    def test_frozen_configuration_missing_or_changed_rejected_before_publish(self):
        config_path = self.batch / 'agent-config.json'
        original = config_path.read_bytes()
        for mutation in ('missing', 'schema', 'model', 'reasoning_effort', 'topics'):
            config = deepcopy(self.config)
            if mutation == 'missing':
                config_path.unlink()
            else:
                if mutation == 'schema': config['schema'] = 'formaliscope-enrichment-config.v1'
                if mutation == 'model': config['worker']['model'] = 'different-worker'
                if mutation == 'reasoning_effort': config['worker']['reasoning_effort'] = 'low'
                if mutation == 'topics': config['topics'][0]['name'] = '修改后的主题名称'
                config_path.write_text(json.dumps(config))
            with self.subTest(mutation=mutation): self.rejected()
            config_path.write_bytes(original)
        self.collect()

    def test_frozen_source_or_manifest_threshold_mismatch_rejected_before_publish(self):
        original = deepcopy(self.manifest)
        for mutation in ('source_commit', 'snapshot_digest', 'threshold'):
            manifest = deepcopy(original)
            manifest[mutation] = 0.5 if mutation == 'threshold' else 'b' * len(manifest[mutation])
            (self.batch / 'manifest.json').write_text(json.dumps(manifest))
            with self.subTest(mutation=mutation): self.rejected()
        (self.batch / 'manifest.json').write_text(json.dumps(original))
        self.collect()

    def test_threshold_boundary_and_generation_failure_are_distinct(self):
        self.annotations[2]['readback']['text_zh'] = None
        self.annotations[1]['priority'] = 'p0'
        self.annotations[1]['expectation_assessment']['confidence'] = 0.01
        report = self.collect()
        self.assertEqual([row['route'] for row in report['entries']], ['pending', 'direct', 'failed'])
        self.assertEqual(report['counts'], {'direct': 1, 'reviewed': 0, 'pending': 1, 'failed': 1})
        document = collector._read(self.output / 'enrichment.json')
        self.assertEqual([row['declaration_id'] for row in document['annotations']], [self.ids[1]])
        self.assertEqual(set(document['originals']), {self.ids[1]})
        validate_enrichment(document, self.snapshot)
        self.assertEqual(collector._read(self.output / 'review-queue.json')['annotations'], self.annotations[:1])

    def test_expectation_result_and_low_judgment_confidence_never_add_review_routes(self):
        self.context_batch()
        self.annotations[1]['expectation_assessment'].update(
            verdict='misaligned', reason_zh='预期与对象范围不同。', confidence=0.05)
        self.annotations[2]['expectation_assessment'].update(verdict='aligned', reason_zh=None, confidence=0.01)
        report = self.collect(first=[self.first_path])
        self.assertEqual([row['route'] for row in report['entries']], ['pending', 'direct', 'direct'])
        self.assertEqual(report['entries'][1]['expectation_confidence'], 0.05)

    def test_review_preserves_original_scores_assessment_and_separate_provenance(self):
        review = self.review()
        review['annotation']['readback']['text_zh'] = '[TEST] 复核后的完整夹具回译。'
        before = deepcopy(self.result)
        report = self.collect(reviews=[self.review_file([review])])
        self.assertEqual([row['route'] for row in report['entries']], ['reviewed', 'direct', 'direct'])
        document = collector._read(self.output / 'enrichment.json')
        self.assertEqual(document['originals'][self.ids[0]], before['annotations'][0])
        self.assertEqual(document['annotations'][0], review['annotation'])
        self.assertEqual(document['reviews'][self.ids[0]], {'model': 'fixture-main',
                                                         'reviewed_at': review['reviewed_at']})
        self.assertEqual(document['sources'][self.ids[0]], hashlib.sha256(
            next(card['lean']['source'] for card in self.snapshot['cards'] if card['id'] == self.ids[0]).encode()).hexdigest())
        self.assertEqual(collector._read(self.result_path), before)
        self.assertEqual((report['entries'][0]['readback_confidence'],
                          report['entries'][0]['expectation_confidence']), (0.79, 0.95))
        validate_enrichment(document, self.snapshot)

    def test_review_rejects_score_assessment_identity_high_score_and_null_changes(self):
        for mutation in ('readback_score', 'expectation_score', 'assessment', 'identity', 'high', 'null', 'time', 'model'):
            review = self.review(1 if mutation == 'high' else 0)
            if mutation == 'readback_score': review['annotation']['readback']['confidence'] = 0.9
            if mutation == 'expectation_score': review['annotation']['expectation_assessment']['confidence'] = 0.9
            if mutation == 'assessment': review['annotation']['expectation_assessment']['reason_zh'] = '重新评估。'
            if mutation == 'identity': review['annotation']['declaration_id'] = self.ids[1]
            if mutation == 'null': review['annotation']['readback']['text_zh'] = None
            if mutation == 'time': review['reviewed_at'] = '2026-10-04T10:00:00'
            if mutation == 'model': review['model'] = ' '
            with self.subTest(mutation=mutation):
                self.rejected(reviews=[self.review_file([review])])

    def test_failed_readback_cannot_enter_review_or_successful_output(self):
        self.annotations[0]['readback']['text_zh'] = None
        review = self.review()
        review['annotation']['readback']['text_zh'] = '[TEST] 意外补出的正文。'
        self.rejected(reviews=[self.review_file([review])])

    def test_first_stage_required_and_immutable_when_expectation_is_provided(self):
        self.context_batch()
        self.rejected()
        for field, value in (('text_zh', '根据预期改写的正文。'), ('confidence', 0.99)):
            original = self.annotations[0]['readback'][field]
            self.annotations[0]['readback'][field] = value
            with self.subTest(field=field): self.rejected(first=[self.first_path])
            self.annotations[0]['readback'][field] = original
        (self.batch / 'expectation-context.txt').write_text('changed')
        self.rejected(first=[self.first_path])

    def test_no_expectation_cannot_be_reported_as_aligned_or_misaligned(self):
        for verdict in ('aligned', 'misaligned'):
            self.annotations[1]['expectation_assessment']['verdict'] = verdict
            self.rejected()

    def test_missing_duplicate_extra_ids_and_unregistered_topics_fail_before_output(self):
        original = deepcopy(self.result)
        for mutation in ('missing', 'duplicate', 'unknown', 'topic', 'old_role', 'evidence', 'summary'):
            self.result = deepcopy(original)
            rows = self.result['annotations']
            if mutation == 'missing': rows.pop()
            if mutation == 'duplicate': rows.append(deepcopy(rows[0]))
            if mutation == 'unknown': rows[0]['declaration_id'] = 'statement::other'
            if mutation == 'topic': rows[0]['classification']['topics'] = ['sphere']
            if mutation == 'old_role': rows[0]['classification']['role'] = 'model'
            if mutation == 'evidence': rows[0]['evidence'] = []
            if mutation == 'summary': rows[0]['summary_zh'] = 'obsolete'
            with self.subTest(mutation=mutation): self.rejected()

    def test_reason_and_both_confidences_are_checked(self):
        original = deepcopy(self.result)
        for field, value in (('reason_zh', None), ('reason_zh', ''), ('reason_zh', '   '),
                             ('confidence', True), ('confidence', float('nan')), ('confidence', 1.01)):
            self.result = deepcopy(original)
            self.result['annotations'][0]['expectation_assessment'][field] = value
            with self.subTest(field=field, value=value): self.rejected()
        self.result = deepcopy(original)
        self.result['annotations'][0]['readback']['confidence'] = True
        self.rejected()

    def test_all_inputs_validate_before_writing_existing_output_and_raw_results_preserved(self):
        invalid = self.write('invalid.json', {'schema': 'wrong'})
        self.rejected(results=[self.result_path, invalid])
        self.collect()
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for path in self.output.iterdir(): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, 'must not already exist'): self.collect()
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.output.iterdir()})

    def test_cli_from_unrelated_directory_and_no_database_creation(self):
        self.result_path.write_text(json.dumps(self.result))
        caller = self.root / 'caller'; caller.mkdir()
        completed = subprocess.run([sys.executable, str(SCRIPTS / 'collect.py'),
                                    '--snapshot', str(self.batch / 'snapshot.json'),
                                    '--manifest', str(self.batch / 'manifest.json'),
                                    '--result', str(self.result_path), '--executed-model', 'fixture-worker',
                                    '--output', str(self.output)], cwd=caller, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['routes'],
                         {'direct': 2, 'reviewed': 0, 'pending': 1, 'failed': 0})
        self.assertFalse(list(self.root.rglob('judgments.sqlite3')))

    def test_prepare_cli_requires_explicit_harness_config(self):
        output = self.root / 'without-config'
        completed = subprocess.run([sys.executable, str(SCRIPTS / 'prepare.py'),
                                    '--snapshot', str(self.snapshot_path),
                                    '--directory', 'KIP126/Sub', '--output', str(output)],
                                   cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn('--config', completed.stderr)
        self.assertFalse(output.exists())

    def task_config(self, **overrides):
        task = {'defaults': str(self.config_path), 'snapshot': str(self.snapshot_path),
                'output': str(self.root / 'configured-batch'),
                'selection': {'directories': ['KIP126/Sub']}}
        task.update(overrides)
        return self.write('task-config.json', task)

    def test_task_config_inherits_defaults_merges_worker_and_replaces_lists(self):
        task = self.task_config(defaults='skills/codex/formaliscope-enrich/config.json',
                                worker={'reasoning_effort': None},
                                topics=self.config['topics'], threshold=0.7)
        resolved = load_config(task)
        self.assertEqual(resolved['worker'], {'model': 'luna6', 'reasoning_effort': None})
        self.assertEqual(resolved['topics'], self.config['topics'])
        self.assertEqual(resolved['selection'], {'directories': ['KIP126/Sub'],
                                                'files': [], 'declaration_ids': []})
        batch, manifest = preparer.prepare_from_config(task)
        self.assertEqual(manifest['threshold'], 0.7)
        self.assertEqual(len(manifest['declaration_ids']), 3)
        self.assertEqual(collector._read(batch / 'task-config.json')['worker'], resolved['worker'])

    def test_default_model_alias_is_isolated_to_claude_code(self):
        for harness, model in (('claude-code', 'sonnet'), ('codex', 'luna6'), ('kimi-code', 'luna6')):
            with self.subTest(harness=harness):
                config = load_config(SCRIPTS.parent / harness / 'formaliscope-enrich/config.json')
                original = deepcopy(config)
                self.assertEqual(config['harness'], harness)
                self.assertEqual(config['worker']['model'], 'luna6')
                self.assertEqual(resolve_worker_model(config), model)
                self.assertEqual(config, original)
        config = load_config(self.config_path)
        self.assertIsNone(config['harness'])
        self.assertEqual(resolve_worker_model(config), 'fixture-worker')

    def test_model_aliases_merge_override_and_resolve_only_once(self):
        task = self.task_config(defaults='skills/claude-code/formaliscope-enrich/config.json',
                                model_aliases={'claude-code': {'luna6': 'custom-route',
                                                               'custom-route': 'not-recursive'},
                                               'custom-harness': {'luna6': 'custom-harness-route'}})
        config = load_config(task)
        self.assertEqual(resolve_worker_model(config), 'custom-route')
        self.assertEqual(config['model_aliases']['claude-code']['custom-route'], 'not-recursive')
        config['worker']['model'] = 'unknown-model'
        self.assertEqual(resolve_worker_model(config), 'unknown-model')
        config['worker']['model'] = 'luna6'
        config['harness'] = 'custom-harness'
        self.assertEqual(resolve_worker_model(config), 'custom-harness-route')
        config['harness'] = 'unmapped-harness'
        self.assertEqual(resolve_worker_model(config), 'luna6')
        child = self.write('alias-child.json', {'defaults': str(task),
                           'model_aliases': {'claude-code': {'another-model': 'another-route'}}})
        config = load_config(child)
        self.assertEqual(resolve_worker_model(config), 'custom-route')
        self.assertEqual(config['model_aliases']['claude-code']['another-model'], 'another-route')
        self.assertEqual(resolve_worker_model(load_config(SCRIPTS.parent / 'claude-code/formaliscope-enrich/config.json')),
                         'sonnet')

    def test_invalid_harness_aliases_and_unknown_fields_fail_before_preparation(self):
        invalid = [{'harness': value} for value in ('', ' ', 1, [], {})]
        invalid.extend({'model_aliases': value} for value in (None, [], 'sonnet',
                       {'': {}}, {' ': {}}, {'claude-code': None}, {'new-harness': []},
                       {'claude-code': {'luna6': ''}}, {'new-harness': {'model': ' '}},
                       {'claude-code': {'': 'sonnet'}}, {'claude-code': {'luna6': 1}},
                       {'claude-code': {'luna6': {'nested': 'sonnet'}}}))
        invalid.extend(({'model_alias': {}}, {'worker': {'model_aliases': {}}}))
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                task = self.task_config(**overrides)
                with self.assertRaises(ValueError): preparer.prepare_from_config(task)
                self.assertFalse((self.root / 'configured-batch').exists())

    def test_aliased_batch_preserves_logical_config_and_collects_against_frozen_route(self):
        parent_config = {'defaults': 'skills/claude-code/formaliscope-enrich/config.json',
                         'topics': self.config['topics']}
        parent = self.write('alias-defaults.json', parent_config)
        task = self.task_config(defaults=str(parent))
        original_task = task.read_bytes()
        batch, manifest = preparer.prepare_from_config(task)
        self.assertEqual(task.read_bytes(), original_task)
        self.assertEqual(manifest['run']['model'], 'sonnet')
        frozen = collector._read(batch / 'agent-config.json')
        self.assertEqual(frozen['worker']['model'], 'sonnet')
        saved = collector._read(batch / 'task-config.json')
        self.assertEqual(saved['worker']['model'], 'luna6')
        self.assertEqual(saved['harness'], 'claude-code')
        self.assertEqual(saved['model_aliases']['claude-code']['luna6'], 'sonnet')
        self.historical_batch(batch)
        collection = {'results': [], 'readback_results': []}
        for index, annotation in enumerate(self.annotations, 1):
            row = {'schema': 'formaliscope-agent-batch.v2', 'annotations': [annotation]}
            first = self.write(f'group-{index}-readback.json', row)
            final = self.write(f'group-{index}.json', row)
            collection['readback_results'].append(str(first))
            collection['results'].append(str(final))
        config = collector._read(task)
        config['collection'] = collection
        for model in (None, 'luna6', 'gpt-6.1-sol', 'unconfirmed-route'):
            with self.subTest(model=model):
                collection['executed_model'] = model
                task.write_text(json.dumps(config))
                with self.assertRaisesRegex(ValueError, 'executed model must be confirmed and match'):
                    collector.collect_from_config(task)
                self.assertFalse((batch / 'collected').exists())
        parent_config['model_aliases'] = {'claude-code': {'luna6': 'new-default-route'}}
        self.write('alias-defaults.json', parent_config)
        self.assertEqual(resolve_worker_model(load_config(task)), 'new-default-route')
        collection['executed_model'] = 'sonnet'
        task.write_text(json.dumps(config))
        output, report = collector.collect_from_config(task)
        self.assertEqual(report['counts'], {'direct': 2, 'reviewed': 0, 'pending': 1, 'failed': 0})
        self.assertEqual(collector._read(output / 'enrichment.json')['run']['model'], 'sonnet')
        self.assertEqual(collector._read(batch / 'agent-config.json'), frozen)
        self.assertEqual(collector._read(batch / 'task-config.json'), saved)

    def test_project_relative_inputs_and_automatic_batch_name(self):
        task = self.task_config(defaults='config.json', snapshot='source-snapshot.json', output=None)
        with patch('skills.scripts.config.REPO', self.root):
            batch, manifest = preparer.prepare_from_config(task)
        self.assertEqual(batch, self.root / '.formaliscope/tasks/batches/task-config')
        self.assertEqual(len(manifest['declaration_ids']), 3)
        self.assertTrue((batch / 'snapshot.json').is_symlink())
        self.assertEqual((batch / 'snapshot.json').resolve().parent,
                         (self.root / '.formaliscope/cache/snapshots').resolve())
        self.snapshot_path.unlink()
        self.assertEqual(collector._read(batch / 'snapshot.json'), self.snapshot)

    def test_config_only_prepare_and_collect_cli_use_saved_batch_inputs(self):
        context = self.root / 'reference.txt'
        context.write_text('测试夹具的补充预期。')
        task = self.task_config(threshold=0.9, expectation_context=str(context))
        prepared = subprocess.run([sys.executable, str(SCRIPTS / 'prepare.py'),
                                   '--config', str(task)], cwd=self.root,
                                  capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        batch = Path(json.loads(prepared.stdout)['output'])
        self.historical_batch(batch)
        first = self.write('first-configured.json', self.result)
        result = self.write('final-configured.json', self.result)
        config = collector._read(task)
        config['collection'] = {'results': [str(result)], 'readback_results': [str(first)],
                                'executed_model': 'fixture-worker'}
        task.write_text(json.dumps(config))
        # Collection uses the prepared snapshot/config even if defaults or input move on.
        self.snapshot_path.unlink()
        self.config['worker']['model'] = 'new-default-model'
        self.write('config.json', self.config)
        collected = subprocess.run([sys.executable, str(SCRIPTS / 'collect.py'),
                                    '--config', str(task)], cwd=self.root,
                                   capture_output=True, text=True)
        self.assertEqual(collected.returncode, 0, collected.stderr)
        report = collector._read(batch / 'collected/report.json')
        self.assertEqual(report['threshold'], 0.9)
        self.assertEqual([row['route'] for row in report['entries']], ['pending', 'pending', 'direct'])
        self.assertEqual(collector._read(batch / 'agent-config.json')['worker']['model'], 'fixture-worker')

    def test_configured_collection_requires_actual_model_and_unchanged_readback(self):
        context = self.root / 'reference.txt'
        context.write_text('合成预期。')
        task = self.task_config(expectation_context=str(context))
        batch, _ = preparer.prepare_from_config(task)
        self.historical_batch(batch)
        first = self.write('first-configured.json', deepcopy(self.result))
        result = self.write('final-configured.json', self.result)
        config = collector._read(task)
        config['collection'] = {'results': [str(result)], 'readback_results': [str(first)]}
        task.write_text(json.dumps(config))
        with self.assertRaises(ValueError): collector.collect_from_config(task)
        config['collection']['executed_model'] = 'fixture-worker'
        task.write_text(json.dumps(config))
        self.result['annotations'][0]['readback']['text_zh'] = '反写基线的错误结果。'
        result.write_text(json.dumps(self.result))
        with self.assertRaises(ValueError): collector.collect_from_config(task)
        self.assertFalse((batch / 'collected').exists())

    def test_invalid_task_config_fails_before_creating_output(self):
        for overrides in ({'threshold': True}, {'threshold': 1.2},
                          {'selection': {'directories': 'KIP126/Sub'}},
                          {'selection': {'directories': []}}, {'snapshot': None},
                          {'threshhold': 0.9}, {'worker': {'reasoning_effort': 'unknown'}}):
            with self.subTest(overrides=overrides):
                task = self.task_config(**overrides)
                with self.assertRaises(ValueError): preparer.prepare_from_config(task)
                self.assertFalse((self.root / 'configured-batch').exists())

    def test_defaults_cycles_and_duplicate_keys_are_rejected(self):
        first = self.write('cycle-a.json', {'defaults': str(self.root / 'cycle-b.json')})
        self.write('cycle-b.json', {'defaults': str(first)})
        with self.assertRaisesRegex(ValueError, 'cycle'): load_config(first)
        first.write_text('{"threshold": 0.7, "threshold": 0.8}')
        with self.assertRaisesRegex(ValueError, 'duplicate'): load_config(first)

    def test_explicit_null_clears_supplement_but_preserves_blueprint_reference(self):
        self.blueprint_batch()
        context = self.root / 'reference.txt'
        context.write_text('用户补充材料。')
        parent = self.task_config(expectation_context=str(context))
        task = self.write('clear-supplement.json', {'defaults': str(parent),
                           'output': str(self.root / 'cleared-context'), 'expectation_context': None})
        batch, _ = preparer.prepare_from_config(task)
        reference = collector._read(batch / 'expectation-context.txt')
        self.assertIsNone(reference['additional_context'])
        self.assertTrue(reference['references'][self.ids[0]])

    def test_prepare_cli_freezes_each_harness_config_from_unrelated_directory(self):
        for harness in ('codex', 'claude-code', 'kimi-code'):
            with self.subTest(harness=harness):
                config_path = SCRIPTS.parent / harness / 'formaliscope-enrich/config.json'
                config = load_config(config_path)
                output = self.root / harness
                completed = subprocess.run([sys.executable, str(SCRIPTS / 'prepare.py'),
                                            '--config', str(config_path),
                                            '--snapshot', str(self.snapshot_path),
                                            '--directory', 'KIP126/Sub', '--output', str(output)],
                                           cwd=self.root, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                model = 'sonnet' if harness == 'claude-code' else 'luna6'
                self.assertEqual(json.loads(completed.stdout)['model'], model)
                frozen = {key: deepcopy(config[key]) for key in ('schema', 'worker', 'topics')}
                frozen['worker']['model'] = model
                self.assertEqual(json.loads((output / 'agent-config.json').read_text()), frozen)
                self.assertEqual(config['worker']['model'], 'luna6')
                self.assertEqual(collector._read(output / 'task-config.json')['worker'], config['worker'])
                run = json.loads((output / 'manifest.json').read_text())['run']
                self.assertEqual(run['model'], model)
                self.assertEqual(run['reasoning_effort'], config['worker']['reasoning_effort'])
                self.assertEqual(run['topics'], config['topics'])


class StageBatchTests(unittest.TestCase):
    setUp = SkillBatchV2Tests.setUp
    write = SkillBatchV2Tests.write
    annotation = SkillBatchV2Tests.annotation
    review = SkillBatchV2Tests.review
    review_file = SkillBatchV2Tests.review_file

    def prepare(self, *, output=None, **kwargs):
        settings = load_config(self.config_path)
        settings['harness'] = getattr(self, 'harness', 'claude-code')
        self.manifest = preparer.prepare(self.snapshot_path, output or self.batch,
                                         config_path=self.config_path, directories=['KIP126/Sub'],
                                         _settings=settings, **kwargs)
        return self.manifest

    def stage_document(self, stage, rows):
        fields = (('declaration_id', 'title_zh', 'readback', 'classification', 'priority')
                  if stage == 'readback' else ('declaration_id', 'expectation_assessment'))
        return {'schema': f'formaliscope-{stage}-batch.v1',
                'annotations': [{key: deepcopy(row[key]) for key in fields} for row in rows]}

    def deliver(self, *, groups=None):
        self.firsts, self.finals = [], []
        groups = groups or [[row] for row in self.annotations]
        for index, rows in enumerate(groups, 1):
            first = self.root / f'group-{index}-readback.json'
            final = self.root / f'group-{index}.json' if collector._needs_expectation(self.manifest) else None
            draft = self.write(f'draft-{index}.json', self.stage_document('readback', rows))
            receipt = collector.deliver_readback(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                                 draft, first, [row['declaration_id'] for row in rows], final)
            self.assertEqual(receipt, {'result_path': str(first), 'count': len(rows)})
            self.firsts.append(first)
            if final is not None:
                draft = self.write(f'assessment-draft-{index}.json', self.stage_document('expectation', rows))
                collector.deliver_expectation(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                              draft, final, first)
                self.finals.append(final)

    def collect(self, *, first=None, results=None, model='fixture-worker', reviews=None):
        return collector.collect(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                 self.finals if results is None else results, reviews or [], self.output,
                                 executed_model=model, readback_paths=self.firsts if first is None else first)

    def rejected(self, **kwargs):
        with self.assertRaises((ValueError, OSError)):
            self.collect(**kwargs)
        self.assertFalse(self.output.exists())

    def test_new_manifest_and_stage_schemas_merge_by_id_preserving_every_first_stage_field(self):
        self.assertEqual(self.manifest['schema'], 'formaliscope-enrichment-batch.v3')
        self.assertEqual(self.manifest['result_protocol'], 'formaliscope-stage-results.v1')
        self.deliver()
        raw = {path: path.read_bytes() for path in self.firsts + self.finals}
        report = self.collect(results=list(reversed(self.finals)), first=list(reversed(self.firsts)))
        self.assertEqual(report['counts'], {'direct': 2, 'reviewed': 0, 'pending': 1, 'failed': 0})
        document = collector._read(self.output / 'enrichment.json')
        self.assertEqual(document['annotations'], self.annotations[1:])
        self.assertEqual(document['originals'], {row['declaration_id']: row for row in self.annotations[1:]})
        validate_enrichment(document, self.snapshot)
        self.assertEqual(raw, {path: path.read_bytes() for path in raw})
        for first in self.firsts:
            baseline = collector._read(collector._baseline_path(first))
            self.assertEqual(baseline['readback_sha256'], hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertNotIn('expectation_assessment', collector._read(first)['annotations'][0])

    def test_second_stage_cannot_smuggle_any_first_stage_field_or_metadata(self):
        self.deliver()
        original = collector._read(self.finals[0])
        for field in ('readback', 'title_zh', 'classification', 'priority', 'model'):
            value = deepcopy(original)
            value['annotations'][0][field] = deepcopy(self.annotations[0].get(field, 'spoof'))
            self.finals[0].write_text(json.dumps(value))
            with self.subTest(field=field): self.rejected()
        value = deepcopy(original)
        value['annotations'][0]['expectation_assessment']['evidence'] = []
        self.finals[0].write_text(json.dumps(value))
        self.rejected()

    def test_first_stage_cannot_supply_assessment_and_delivery_does_not_publish_invalid_draft(self):
        document = self.stage_document('readback', self.annotations[:1])
        document['annotations'][0]['expectation_assessment'] = self.annotations[0]['expectation_assessment']
        draft = self.write('invalid-draft.json', document)
        target = self.root / 'new-readback.json'
        with self.assertRaisesRegex(ValueError, 'unexpected field'):
            collector.deliver_readback(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                       draft, target, self.ids[:1], self.root / 'next.json')
        self.assertFalse(target.exists())
        self.assertFalse(collector._baseline_path(target).exists())
        self.assertEqual(collector._read(draft), document)

    def test_original_baseline_change_detected_even_if_new_text_is_valid(self):
        self.deliver()
        path = self.firsts[0]
        raw = path.read_bytes()
        for field in ('title_zh', 'readback', 'classification', 'priority', 'whitespace'):
            row = collector._read(path)
            if field == 'readback': row['annotations'][0][field]['text_zh'] = '删字后的新正文。'
            elif field == 'classification': row['annotations'][0][field]['role'] = 'input'
            elif field == 'priority': row['annotations'][0][field] = 'p0'
            elif field == 'title_zh': row['annotations'][0][field] = '修改标题'
            path.write_bytes(raw + b' ' if field == 'whitespace' else json.dumps(row).encode())
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'baseline digest changed'):
                self.collect()
            self.assertFalse(self.output.exists())
            path.write_bytes(raw)
        self.collect()

    def test_missing_or_wrong_baseline_and_cross_batch_receipt_rejected(self):
        self.deliver()
        path = collector._baseline_path(self.firsts[0])
        raw = path.read_bytes()
        path.unlink()
        self.rejected()
        path.write_bytes(raw)
        for field, value in (('run_id', 'other-run'), ('source_commit', 'b' * 40),
                             ('manifest_sha256', '0' * 64), ('readback_path', str(self.firsts[1])),
                             ('declaration_ids', [self.ids[1]]), ('expectation_path', None)):
            document = json.loads(raw)
            document[field] = value
            path.write_text(json.dumps(document))
            with self.subTest(field=field): self.rejected()
        path.write_bytes(raw)
        self.collect()

    def test_missing_duplicate_wrong_ids_and_group_counts_reject_before_collection_output(self):
        self.deliver()
        self.rejected(first=[])
        self.rejected(first=self.firsts[:-1])
        self.rejected(first=self.firsts + self.firsts[:1])
        self.rejected(results=self.finals[:-1])
        self.rejected(results=self.finals + self.finals[:1])
        raw = self.finals[0].read_bytes()
        for mutation in ('missing', 'duplicate', 'wrong', 'unknown', 'extra', 'old-schema'):
            document = json.loads(raw)
            rows = document['annotations']
            if mutation == 'missing': rows.clear()
            if mutation == 'duplicate': rows.append(deepcopy(rows[0]))
            if mutation == 'wrong': rows[0]['declaration_id'] = self.ids[1]
            if mutation == 'unknown': rows[0]['declaration_id'] = 'statement::Unknown'
            if mutation == 'extra': rows.append(self.stage_document('expectation', [self.annotations[1]])['annotations'][0])
            if mutation == 'old-schema': document['schema'] = 'formaliscope-agent-batch.v2'
            self.finals[0].write_text(json.dumps(document))
            with self.subTest(mutation=mutation): self.rejected()
        self.finals[0].write_bytes(raw)
        self.collect()

    def test_stage_contract_types_options_reasons_scores_and_duplicate_json_keys(self):
        from review_app.enrichment_v2 import validate_stage_batch
        for stage in ('readback', 'expectation'):
            original = self.stage_document(stage, self.annotations[:1])
            for mutation in ('boolean', 'nan', 'range', 'unknown', 'bad-option', 'empty'):
                document = deepcopy(original)
                row = document['annotations'][0]
                score = row['readback' if stage == 'readback' else 'expectation_assessment']
                if mutation == 'boolean': score['confidence'] = True
                if mutation == 'nan': score['confidence'] = float('nan')
                if mutation == 'range': score['confidence'] = -0.01
                if mutation == 'unknown': score['unknown'] = True
                if mutation == 'bad-option':
                    if stage == 'readback': row['classification']['role'] = 'old-role'
                    else: score['verdict'] = 'maybe'
                if mutation == 'empty': score['text_zh' if stage == 'readback' else 'reason_zh'] = ' '
                with self.subTest(stage=stage, mutation=mutation), self.assertRaises(ValueError):
                    validate_stage_batch(document, stage, self.snapshot, self.config['topics'])
        self.deliver()
        raw = self.finals[0].read_text()
        self.finals[0].write_text(raw.replace('"confidence": 0.95', '"confidence": 0.95, "confidence": 0.99'))
        self.rejected()

    def test_threshold_null_failure_and_review_preserve_original_scores(self):
        self.annotations[2]['readback']['text_zh'] = None
        self.annotations[1]['expectation_assessment']['confidence'] = 0.01
        self.deliver()
        review = self.review()
        review['annotation']['readback']['text_zh'] = '复核后的合成正文。'
        report = self.collect(reviews=[self.review_file([review])])
        self.assertEqual(report['counts'], {'direct': 1, 'reviewed': 1, 'pending': 0, 'failed': 1})
        document = collector._read(self.output / 'enrichment.json')
        self.assertEqual(document['originals'][self.ids[0]], self.annotations[0])
        self.assertEqual(document['annotations'][0]['readback']['confidence'], 0.79)
        self.assertEqual(document['annotations'][1]['expectation_assessment']['confidence'], 0.01)
        self.assertNotIn(self.ids[2], document['originals'])
        validate_enrichment(document, self.snapshot)

    def test_cannot_raise_original_score_during_review(self):
        self.deliver()
        review = self.review()
        review['annotation']['readback']['confidence'] = 0.99
        self.rejected(reviews=[self.review_file([review])])

    def test_codex_and_kimi_no_context_skip_second_stage_but_require_sealed_readback(self):
        for harness in ('codex', 'kimi-code'):
            self.harness = harness
            self.batch = self.root / harness
            self.prepare()
            folder = self.root / (harness + '-results')
            folder.mkdir()
            old_root = self.root
            self.root = folder
            self.output = folder / 'collected'
            self.deliver(groups=[self.annotations])
            self.assertEqual(self.finals, [])
            report = self.collect()
            self.assertEqual(report['counts']['direct'], 2)
            document = collector._read(self.output / 'enrichment.json')
            assessment = document['annotations'][0]['expectation_assessment']
            self.assertEqual(assessment['verdict'], 'undetermined')
            self.assertEqual(assessment['confidence'], 1.0)
            self.assertIn('跳过第二阶段', assessment['reason_zh'])
            self.root = old_root

    def test_context_requires_second_stage_for_other_harnesses_and_supports_multi_id_groups(self):
        self.harness = 'codex'
        self.batch = self.root / 'context-batch'
        context = self.root / 'context.txt'
        context.write_text('合成独立预期。')
        self.prepare(expectation_context=context)
        self.annotations[1]['expectation_assessment'].update(verdict='misaligned', reason_zh='预期不一致。')
        self.deliver(groups=[list(reversed(self.annotations))])
        raw = self.finals[0].read_bytes()
        document = json.loads(raw)
        document['annotations'].reverse()
        self.finals[0].write_text(json.dumps(document))
        report = self.collect()
        self.assertEqual(report['entries'][1]['route'], 'direct')
        self.assertEqual(collector._read(self.output / 'enrichment.json')['annotations'], self.annotations[1:])

    def test_claude_cannot_skip_second_stage_or_use_multi_declaration_group(self):
        draft = self.write('first-draft.json', self.stage_document('readback', self.annotations))
        for ids, next_path in ((self.ids, self.root / 'next.json'), (self.ids[:1], None)):
            with self.subTest(ids=ids, next_path=next_path), self.assertRaises(ValueError):
                collector.deliver_readback(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                           draft, self.result_path, ids, next_path)
        self.assertFalse(self.result_path.exists())

    def test_delivery_never_overwrites_first_second_or_baseline_paths(self):
        self.deliver()
        first, final = self.firsts[0], self.finals[0]
        paths = [first, final, collector._baseline_path(first)]
        raw = {path: path.read_bytes() for path in paths}
        with self.assertRaises(ValueError):
            collector.deliver_readback(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                       first, first, self.ids[:1], final)
        with self.assertRaises(ValueError):
            collector.deliver_expectation(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                          final, final, first)
        self.assertEqual(raw, {path: path.read_bytes() for path in paths})

    def test_model_context_and_protocol_mismatch_do_not_publish_partial_collection(self):
        self.deliver()
        self.rejected(model=None)
        self.rejected(model='unverified-model')
        original = (self.batch / 'manifest.json').read_bytes()
        for field, value in (('result_protocol', 'unknown'), ('schema', 'unknown')):
            manifest = json.loads(original)
            manifest[field] = value
            (self.batch / 'manifest.json').write_text(json.dumps(manifest))
            self.rejected()
        (self.batch / 'manifest.json').write_bytes(original)
        self.collect()

    def test_public_candidate_and_private_import_keep_existing_final_v2_semantics(self):
        from review_app.enrichment import enrich_snapshot
        from review_app.agent_assessments import import_agent_assessments
        self.deliver()
        self.collect()
        document = collector._read(self.output / 'enrichment.json')
        candidate = enrich_snapshot(self.snapshot, document)
        encoded = json.dumps(candidate)
        for key in ('expectation_assessment', 'reason_zh', 'confidence', 'originals', 'run_id'):
            self.assertNotIn('"' + key + '"', encoded)
        data = self.root / 'private-data'
        result = import_agent_assessments(data, self.snapshot, document)
        self.assertEqual(result['inserted'], 2)
        self.assertEqual(import_agent_assessments(data, self.snapshot, document)['unchanged'], 2)
        self.assertFalse((data / 'snapshot.json').exists())

    def test_expectation_delivery_rejects_extra_fields_without_writing_or_changing_baseline(self):
        first, final = self.root / 'sealed.json', self.root / 'assessment.json'
        draft = self.write('seal-draft.json', self.stage_document('readback', self.annotations[:1]))
        collector.deliver_readback(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                   draft, first, self.ids[:1], final)
        baseline = collector._baseline_path(first)
        raw = {path: path.read_bytes() for path in (first, baseline)}
        document = self.stage_document('expectation', self.annotations[:1])
        document['annotations'][0]['readback'] = self.annotations[0]['readback']
        assessment = self.write('invalid-assessment-draft.json', document)
        with self.assertRaisesRegex(ValueError, 'unexpected field'):
            collector.deliver_expectation(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                          assessment, final, first)
        self.assertFalse(final.exists())
        self.assertEqual(raw, {path: path.read_bytes() for path in raw})

    def test_baseline_check_and_expectation_delivery_reject_modified_readback(self):
        first, final = self.root / 'sealed.json', self.root / 'assessment.json'
        draft = self.write('seal-draft.json', self.stage_document('readback', self.annotations[:1]))
        collector.deliver_readback(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                   draft, first, self.ids[:1], final)
        command = [sys.executable, str(SCRIPTS / 'collect.py'), '--check-readback',
                   '--snapshot', str(self.batch / 'snapshot.json'),
                   '--manifest', str(self.batch / 'manifest.json'), '--readback-result', str(first)]
        checked = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout), {'result_path': str(first), 'count': 1})
        first.write_bytes(first.read_bytes() + b'\n')
        checked = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 2)
        self.assertIn('baseline digest changed', checked.stderr)
        assessment = self.write('assessment.json.input', self.stage_document('expectation', self.annotations[:1]))
        with self.assertRaisesRegex(ValueError, 'baseline digest changed'):
            collector.deliver_expectation(self.batch / 'snapshot.json', self.batch / 'manifest.json',
                                          assessment, final, first)
        self.assertFalse(final.exists())

    def test_frozen_context_and_snapshot_changes_rejected_before_output(self):
        context = self.root / 'fixed-context.txt'
        context.write_text('合成独立预期。')
        self.batch = self.root / 'fixed-context-batch'
        self.prepare(expectation_context=context)
        self.deliver()
        frozen = self.batch / 'expectation-context.txt'
        raw = frozen.read_bytes()
        frozen.write_bytes(raw + b'changed')
        self.rejected()
        frozen.write_bytes(raw)
        snapshot = self.batch / 'snapshot.json'
        original = snapshot.read_bytes()
        snapshot.chmod(0o600)
        changed = json.loads(original)
        changed['source_commit'] = 'b' * 40
        snapshot.write_text(json.dumps(changed))
        self.rejected()
        snapshot.write_bytes(original)
        self.collect()

    def test_staged_blueprint_requires_reference_for_each_declaration(self):
        SkillBatchV2Tests.blueprint_batch(self)
        self.annotations[1]['expectation_assessment'].update(verdict='aligned', reason_zh=None)
        self.deliver()
        unbound = self.finals[2]
        original = unbound.read_bytes()
        document = json.loads(original)
        document['annotations'][0]['expectation_assessment'].update(verdict='aligned', reason_zh=None)
        unbound.write_text(json.dumps(document))
        self.rejected()
        unbound.write_bytes(original)
        self.collect()

    def test_no_context_direct_collection_cli_accepts_only_readback_results(self):
        self.harness = 'codex'
        self.batch = self.root / 'cli-skipped-batch'
        self.prepare()
        self.deliver(groups=[self.annotations])
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / 'collect.py'), '--snapshot', str(self.batch / 'snapshot.json'),
             '--manifest', str(self.batch / 'manifest.json'), '--readback-result', str(self.firsts[0]),
             '--executed-model', 'fixture-worker', '--output', str(self.output)],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['routes'],
                         {'direct': 2, 'reviewed': 0, 'pending': 1, 'failed': 0})

    def test_delivery_and_config_collection_cli_from_unrelated_directory(self):
        task = self.write('stage-task.json', {'defaults': str(self.config_path), 'output': str(self.batch)})
        finals, firsts = [], []
        for index, row in enumerate(self.annotations):
            first, final = self.root / f'cli-{index}-readback.json', self.root / f'cli-{index}.json'
            for stage in ('readback', 'expectation'):
                draft = self.write(f'cli-{index}-{stage}-draft.json', self.stage_document(stage, [row]))
                command = [sys.executable, str(SCRIPTS / 'collect.py'), f'--deliver-{stage}',
                           '--snapshot', str(self.batch / 'snapshot.json'),
                           '--manifest', str(self.batch / 'manifest.json'), '--input', str(draft),
                           '--result', str(first if stage == 'readback' else final)]
                command += (['--declaration-id', row['declaration_id'], '--next-result', str(final)]
                            if stage == 'readback' else ['--readback-result', str(first)])
                completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)['count'], 1)
            firsts.append(str(first)); finals.append(str(final))
        settings = collector._read(task)
        settings['collection'] = {'results': finals, 'readback_results': firsts, 'executed_model': 'fixture-worker'}
        task.write_text(json.dumps(settings))
        completed = subprocess.run([sys.executable, str(SCRIPTS / 'collect.py'), '--config', str(task)],
                                   cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['routes']['direct'], 2)
        self.assertFalse(list(self.root.rglob('judgments.sqlite3')))


if __name__ == '__main__':
    unittest.main()
