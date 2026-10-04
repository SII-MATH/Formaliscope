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

from review_app.enrichment import validate_enrichment
from review_app.statements import compile_statements


SCRIPTS = Path(__file__).resolve().parents[1] / '.agents/skills/formaliscope-enrich/scripts'


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

    def prepare(self, *, output=None, **kwargs):
        self.manifest = preparer.prepare(self.snapshot_path, output or self.batch,
                                         config_path=self.config_path,
                                         directories=['KIP126/Sub'], **kwargs)
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

    def test_prepare_freezes_selection_config_context_and_private_permissions(self):
        context = self.context_batch()
        self.assertEqual(len(self.manifest['declaration_ids']), 3)
        self.assertEqual(self.manifest['run']['model'], 'fixture-worker')
        self.assertEqual(self.manifest['run']['expectation_context_digest'],
                         hashlib.sha256(context.read_bytes()).hexdigest())
        self.assertEqual((self.batch / 'expectation-context.txt').read_bytes(), context.read_bytes())
        self.assertEqual(collector._read(self.batch / 'agent-config.json'), self.config)
        self.assertEqual(stat.S_IMODE(self.batch.stat().st_mode), 0o700)
        for path in self.batch.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.config['worker']['model'] = 'another-model'
        self.write('config.json', self.config)
        self.assertEqual(collector._read(self.batch / 'agent-config.json')['worker']['model'], 'fixture-worker')
        self.snapshot_path.write_text('{}')
        self.assertEqual(collector._read(self.batch / 'snapshot.json'), self.snapshot)

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


if __name__ == '__main__':
    unittest.main()
