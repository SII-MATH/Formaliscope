"""Synthetic protocol fixtures; these tests do not validate mathematics."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from review_app.enrichment import validate_enrichment
from review_app.statements import compile_statements


SCRIPT = Path(__file__).resolve().parents[1] / '.agents/skills/formaliscope-enrich/scripts/collect.py'
spec = importlib.util.spec_from_file_location('formaliscope_skill_collect', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class SkillBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source'
        (source / 'KIP126').mkdir(parents=True)
        (source / 'KIP126/Example.lean').write_text(
            'namespace Example\ndef a : Nat := 1\ndef b : Nat := 2\ndef c : Nat := 3\nend Example\n')
        self.snapshot = compile_statements(source, source_commit='a' * 40)
        self.annotations = []
        for card in self.snapshot['cards']:
            self.annotations.append({
                'declaration_id': card['id'],
                'basis': {'source_commit': self.snapshot['source_commit'],
                          'snapshot_digest': self.snapshot['digest'],
                          'source_sha256': hashlib.sha256(card['lean']['source'].encode()).hexdigest(),
                          'context_fingerprint': None},
                'title_zh': '测试夹具', 'summary_zh': '测试夹具，非真实语义结果。',
                'readback': {'status': 'draft', 'text_zh': '[TEST] 合成回译协议夹具。',
                             'unresolved': [], 'evidence_ids': ['source']},
                'classification': {'status': 'agent_candidate', 'role': 'unclassified',
                                   'topics': [], 'rationale_zh': None, 'evidence_ids': []},
                'priority': {'level': None, 'reason_zh': None, 'evidence_ids': []},
                'evidence': [{'id': 'source', 'file': card['lean']['file'],
                              'line_start': card['lean']['line'],
                              'line_end': card['lean']['line'] + len(card['lean']['source'].splitlines()) - 1,
                              'excerpt': card['lean']['source']}],
                'provenance': {'method': 'agent_manual', 'model': 'fixture-worker',
                               'created_at': '2026-10-04T08:00:00+08:00',
                               'policy_version': 'manual-enrichment.v1', 'context_completeness': 'unknown'}})
        self.ids = [row['declaration_id'] for row in self.annotations]
        self.manifest = {'schema': 'formaliscope-enrichment-batch.v1',
                         'source_commit': self.snapshot['source_commit'],
                         'snapshot_digest': self.snapshot['digest'], 'threshold': 0.8,
                         'declaration_ids': self.ids}
        self.result = {'schema': 'formaliscope-agent-batch.v1',
                       'enrichment': {'schema': 'statement-enrichment.v1', 'annotations': self.annotations},
                       'confidence': dict(zip(self.ids, (0.79, 0.8, 0.81)))}
        self.snapshot_path = self.write('snapshot.json', self.snapshot)
        self.manifest_path = self.root / 'manifest.json'
        self.result_path = self.root / 'result.json'
        self.output = self.root / 'output'

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def collect(self, reviews=None, results=None):
        self.write('manifest.json', self.manifest)
        self.write('result.json', self.result)
        return helper.collect(self.snapshot_path, self.manifest_path,
                              results or [self.result_path], reviews or [], self.output)

    def review(self, index=0):
        return {'declaration_id': self.ids[index], 'annotation': deepcopy(self.annotations[index]),
                'model': 'fixture-main-agent', 'reviewed_at': '2026-10-04T00:30:00Z'}

    def review_file(self, rows):
        return self.write('review.json', {'schema': 'formaliscope-enrichment-review.v1', 'reviews': rows})

    def rejected(self, results=None, reviews=None):
        with self.assertRaises(ValueError):
            self.collect(results=results, reviews=reviews)
        self.assertFalse(self.output.exists())

    def test_threshold_boundaries_and_unresolved_do_not_add_review_triggers(self):
        self.annotations[1]['readback']['unresolved'] = [
            {'object': 'fixture object', 'reason': 'fixture missing context', 'evidence_ids': ['source']}]
        report = self.collect()
        self.assertEqual([row['route'] for row in report['entries']], ['pending', 'direct', 'direct'])
        accepted = helper._read(self.output / 'enrichment.json')
        self.assertEqual([row['declaration_id'] for row in accepted['annotations']], self.ids[1:])
        validate_enrichment(accepted, self.snapshot)
        queue = helper._read(self.output / 'review-queue.json')
        self.assertEqual(queue['enrichment']['annotations'], self.annotations[:1])
        self.assertEqual(queue['confidence'], {self.ids[0]: 0.79})
        self.assertEqual(self.result_path.read_text(), json.dumps(self.result))

    def test_multi_result_merge_and_review_keep_original_score_and_models(self):
        self.result['enrichment']['annotations'] = self.annotations[:1]
        self.result['confidence'] = {self.ids[0]: 0.79}
        second = self.write('second.json', {'schema': 'formaliscope-agent-batch.v1',
                            'enrichment': {'schema': 'statement-enrichment.v1', 'annotations': self.annotations[1:]},
                            'confidence': {self.ids[1]: 0.8, self.ids[2]: 0.81}})
        review = self.review()
        review['annotation']['readback']['text_zh'] = '[TEST] 主 Agent 修订后的合成夹具。'
        review_path = self.review_file([review])
        report = self.collect(results=[self.result_path, second], reviews=[review_path])
        entry = report['entries'][0]
        self.assertEqual((entry['confidence'], entry['route']), (0.79, 'reviewed'))
        self.assertEqual((entry['original_model'], entry['review_model']), ('fixture-worker', 'fixture-main-agent'))
        self.assertEqual(entry['reviewed_at'], review['reviewed_at'])
        accepted = helper._read(self.output / 'enrichment.json')
        self.assertEqual(accepted['annotations'][0], review['annotation'])
        self.assertEqual(helper._read(self.output / 'review-queue.json')['confidence'], {})
        self.assertEqual(helper._read(self.result_path)['confidence'][self.ids[0]], 0.79)

    def test_generic_and_legacy_results_merge_without_restricting_worker_models(self):
        self.result['enrichment']['annotations'] = self.annotations[:1]
        self.result['confidence'] = {self.ids[0]: 0.79}
        self.annotations[0]['provenance']['model'] = 'fixture-configured-model'
        self.annotations[1]['provenance']['model'] = 'fixture-previous-model'
        legacy = self.write('legacy.json', {
            'schema': 'formaliscope-luna-batch.v1',
            'enrichment': {'schema': 'statement-enrichment.v1', 'annotations': self.annotations[1:]},
            'confidence': {self.ids[1]: 0.8, self.ids[2]: 0.81}})
        report = self.collect(results=[self.result_path, legacy])
        self.assertEqual([row['route'] for row in report['entries']], ['pending', 'direct', 'direct'])
        self.assertEqual(report['entries'][0]['original_model'], 'fixture-configured-model')
        self.assertEqual(report['entries'][1]['original_model'], 'fixture-previous-model')
        queue = helper._read(self.output / 'review-queue.json')
        self.assertEqual(queue['schema'], 'formaliscope-agent-batch.v1')
        self.assertEqual(queue['confidence'], {self.ids[0]: 0.79})

    def test_user_configured_threshold_controls_the_same_single_routing_rule(self):
        self.manifest['threshold'] = 0.81
        report = self.collect()
        self.assertEqual(report['threshold'], 0.81)
        self.assertEqual([row['route'] for row in report['entries']], ['pending', 'pending', 'direct'])

    def test_review_can_confirm_an_unavailable_readback_without_fabricating_one(self):
        self.annotations[0]['readback'].update(status='none', text_zh=None, unresolved=[], evidence_ids=[])
        review = self.review()
        report = self.collect(reviews=[self.review_file([review])])
        self.assertEqual(report['entries'][0]['route'], 'reviewed')
        accepted = helper._read(self.output / 'enrichment.json')['annotations'][0]
        self.assertEqual(accepted['readback'], self.annotations[0]['readback'])
        self.assertEqual(accepted['readback']['status'], 'none')

    def test_wrong_missing_duplicate_and_empty_declarations_are_rejected(self):
        original = deepcopy(self.result)
        for mutation in ('unknown', 'missing', 'duplicate', 'empty', 'confidence_missing', 'confidence_extra'):
            self.result = deepcopy(original)
            rows = self.result['enrichment']['annotations']
            scores = self.result['confidence']
            if mutation == 'unknown': rows[0]['declaration_id'] = 'statement::Unknown'
            if mutation == 'missing': rows.pop(); scores.pop(self.ids[-1])
            if mutation == 'duplicate': rows.append(deepcopy(rows[0]))
            if mutation == 'empty': rows.clear(); scores.clear()
            if mutation == 'confidence_missing': scores.pop(self.ids[0])
            if mutation == 'confidence_extra': scores['statement::Other'] = 1
            with self.subTest(mutation=mutation): self.rejected()
        self.result = original
        second = self.write('duplicate.json', self.result)
        self.rejected(results=[self.result_path, second])

    def test_manifest_source_selection_and_threshold_are_checked(self):
        original = deepcopy(self.manifest)
        for field, value in (('source_commit', 'b' * 40), ('snapshot_digest', 'b' * 64),
                             ('declaration_ids', []), ('declaration_ids', self.ids + self.ids[:1]),
                             ('declaration_ids', ['wrong']), ('threshold', -0.01), ('threshold', 1.01),
                             ('threshold', True), ('threshold', '0.8'), ('threshold', float('nan')),
                             ('threshold', float('inf'))):
            self.manifest = deepcopy(original)
            self.manifest[field] = value
            with self.subTest(field=field, value=value): self.rejected()

    def test_illegal_confidence_is_rejected_before_output(self):
        for value in (True, False, None, '0.9', -0.01, 1.01, float('nan'),
                      float('inf'), float('-inf'), 10 ** 500):
            self.result['confidence'][self.ids[0]] = value
            with self.subTest(value=value): self.rejected()

    def test_existing_source_and_contract_validation_is_reused(self):
        original = deepcopy(self.result)
        for mutation in ('source', 'basis', 'reference', 'range', 'verdict', 'verified'):
            self.result = deepcopy(original)
            annotation = self.result['enrichment']['annotations'][0]
            if mutation == 'source': annotation['evidence'][0]['excerpt'] = 'invented source'
            if mutation == 'basis': annotation['basis']['source_sha256'] = 'b' * 64
            if mutation == 'reference': annotation['readback']['evidence_ids'] = ['unknown']
            if mutation == 'range': annotation['evidence'][0]['line_end'] = 100
            if mutation == 'verdict': annotation['verdict'] = 'aligned'
            if mutation == 'verified': annotation['readback']['status'] = 'verified'
            with self.subTest(mutation=mutation): self.rejected()

    def test_review_only_accepts_low_scores_and_preserves_identity_basis_and_contract(self):
        for mutation in ('high', 'unknown', 'duplicate', 'identity', 'basis', 'evidence',
                         'model', 'time', 'verdict', 'verified', 'confidence'):
            review = self.review(1 if mutation == 'high' else 0)
            rows = [review]
            if mutation == 'unknown': review['declaration_id'] = 'statement::Unknown'
            if mutation == 'duplicate': rows.append(deepcopy(review))
            if mutation == 'identity': review['annotation'] = deepcopy(self.annotations[1])
            if mutation == 'basis': review['annotation']['basis']['source_commit'] = 'b' * 40
            if mutation == 'evidence': review['annotation']['evidence'][0]['excerpt'] = 'invented source'
            if mutation == 'model': review['model'] = ''
            if mutation == 'time': review['reviewed_at'] = '2026-10-04T00:00:00'
            if mutation == 'verdict': review['annotation']['verdict'] = 'aligned'
            if mutation == 'verified': review['annotation']['readback']['status'] = 'verified'
            if mutation == 'confidence': review['confidence'] = 1
            with self.subTest(mutation=mutation): self.rejected(reviews=[self.review_file(rows)])

    def test_duplicate_json_keys_are_rejected_without_silent_overwrite(self):
        self.write('manifest.json', self.manifest)
        encoded = json.dumps(self.result)
        self.result_path.write_text(encoded.replace('"confidence": {', '"confidence": {}, "confidence": {', 1))
        with self.assertRaisesRegex(ValueError, 'duplicate JSON object key'):
            helper.collect(self.snapshot_path, self.manifest_path, [self.result_path], [], self.output)
        self.assertFalse(self.output.exists())
        self.result_path.write_text(encoded.replace('"' + self.ids[0] + '": 0.79',
                                                  '"' + self.ids[0] + '": 1, "' + self.ids[0] + '": 0.79'))
        with self.assertRaisesRegex(ValueError, 'duplicate JSON object key'):
            helper.collect(self.snapshot_path, self.manifest_path, [self.result_path], [], self.output)

    def test_all_inputs_validate_before_writing_and_existing_output_is_preserved(self):
        invalid = self.write('invalid.json', {'schema': 'wrong'})
        self.rejected(results=[self.result_path, invalid])
        self.collect()
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        with self.assertRaisesRegex(ValueError, 'must not already exist'): self.collect()
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.output.iterdir()})

    def test_cli_runs_from_an_unrelated_working_directory(self):
        self.write('manifest.json', self.manifest)
        self.write('result.json', self.result)
        caller = self.root / 'caller'; caller.mkdir()
        completed = subprocess.run([sys.executable, str(SCRIPT), '--snapshot', str(self.snapshot_path),
                                    '--manifest', str(self.manifest_path), '--result', str(self.result_path),
                                    '--output', str(self.output)], cwd=caller, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['routes'], {'direct': 2, 'reviewed': 0, 'pending': 1})
        self.assertFalse((caller / 'judgments.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
