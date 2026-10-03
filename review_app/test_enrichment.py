from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .build import calculate_snapshot_digest, validate_snapshot
from .enrichment import enrich_snapshot, validate_enrichment
from .statements import compile_statements


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'KIP126').mkdir()
        (self.root/'KIP126/Example.lean').write_text('namespace Example\ndef value : Nat := 1\nend Example\n')
        self.snapshot = compile_statements(self.root, source_commit='a'*40)
        self.card = self.snapshot['cards'][0]
        self.annotation = {
            'declaration_id': self.card['id'],
            'basis': {'source_commit': self.snapshot['source_commit'], 'snapshot_digest': self.snapshot['digest'],
                      'source_sha256': hashlib.sha256(self.card['lean']['source'].encode()).hexdigest(), 'context_fingerprint': None},
            'title_zh': '测试夹具：自然数值', 'summary_zh': '测试夹具：阅读摘要，不是语义回译。',
            'readback': {'status': 'none', 'text_zh': None, 'unresolved': [], 'evidence_ids': []},
            'classification': {'status': 'agent_candidate', 'role': 'model', 'topics': [],
                               'rationale_zh': '测试夹具：依据定义分类。', 'evidence_ids': ['source']},
            'priority': {'level': 'p1', 'reason_zh': '测试夹具：优先度建议。', 'evidence_ids': ['source']},
            'evidence': [{'id': 'source', 'file': 'KIP126/Example.lean', 'line_start': 2, 'line_end': 2,
                          'excerpt': 'def value : Nat := 1'}],
            'provenance': {'method': 'agent_manual', 'model': 'fixture-no-model-call',
                           'created_at': '2026-10-03T00:00:00Z', 'policy_version': 'manual-enrichment.v1',
                           'context_completeness': 'unknown'}}
        self.document = {'schema': 'statement-enrichment.v1', 'annotations': [self.annotation]}

    def test_display_and_classification_do_not_overwrite_review_basis(self):
        original = deepcopy(self.snapshot)
        result = enrich_snapshot(self.snapshot, self.document)
        validate_snapshot(result)
        self.assertEqual(self.snapshot, original)
        updated = result['cards'][0]
        self.assertEqual(updated['title'], self.card['title'])
        self.assertEqual(updated['statement'], self.card['statement'])
        self.assertEqual(updated['fingerprint'], self.card['fingerprint'])
        self.assertEqual(updated['title_zh'], self.annotation['title_zh'])
        self.assertEqual(updated['statement_origin'], 'reading-summary')
        self.assertEqual(result['comparison']['unchanged'], 1)
        self.assertNotEqual(result['digest'], self.snapshot['digest'])

    def test_readback_changes_review_basis_without_old_matching_aliases(self):
        self.annotation['readback'].update(status='draft', text_zh='[TEST] 对应 Lean 的回译夹具。', evidence_ids=['source'])
        result = enrich_snapshot(self.snapshot, self.document)
        self.assertEqual(result['cards'][0]['statement_origin'], 'backtranslation')
        self.assertEqual(result['comparison']['changed'], 1)
        self.assertNotIn(self.card['fingerprint'], result['cards'][0]['fingerprints'].values())
        validate_snapshot(result)

    def test_missing_fields_unknown_fields_and_agent_verified_are_rejected(self):
        for mutation in ('extra', 'missing', 'verified', 'human', 'bad_time', 'empty_draft'):
            document = deepcopy(self.document)
            row = document['annotations'][0]
            if mutation == 'extra': row['risk'] = 'high'
            if mutation == 'missing': del row['title_zh']
            if mutation == 'verified': row['readback']['status'] = 'verified'
            if mutation == 'human': row['verdict'] = 'aligned'
            if mutation == 'bad_time': row['provenance']['created_at'] = '2026-10-03'
            if mutation == 'empty_draft': row['readback']['status'] = 'draft'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_enrichment(document, self.snapshot)

    def test_source_commit_snapshot_and_source_hash_binding(self):
        for field in ('source_commit', 'snapshot_digest', 'source_sha256'):
            original = self.annotation['basis'][field]
            self.annotation['basis'][field] = 'b'*len(original)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_enrichment(self.document, self.snapshot)
            self.annotation['basis'][field] = original
        changed = deepcopy(self.snapshot)
        changed['modules']['KIP126/Example.lean'] += '\n-- changed context\n'
        changed['digest'] = calculate_snapshot_digest(changed)
        with self.assertRaises(ValueError): validate_enrichment(self.document, changed)

    def test_duplicates_evidence_and_unverifiable_context_are_rejected(self):
        for mutation in ('duplicate_card', 'duplicate_evidence', 'missing_reference', 'false_excerpt',
                         'bad_range', 'unknown_file', 'unattributed_priority', 'unattributed_role', 'complete', 'context'):
            document = deepcopy(self.document)
            row = document['annotations'][0]
            if mutation == 'duplicate_card': document['annotations'].append(deepcopy(row))
            if mutation == 'duplicate_evidence': row['evidence'].append(deepcopy(row['evidence'][0]))
            if mutation == 'missing_reference': row['classification']['evidence_ids'] = ['absent']
            if mutation == 'false_excerpt': row['evidence'][0]['excerpt'] = 'def value : Nat := 2'
            if mutation == 'bad_range': row['evidence'][0]['line_end'] = 100
            if mutation == 'unknown_file': row['evidence'][0]['file'] = '/outside-file'
            if mutation == 'unattributed_priority': row['priority']['reason_zh'] = None
            if mutation == 'unattributed_role': row['classification']['evidence_ids'] = []
            if mutation == 'complete': row['provenance']['context_completeness'] = 'complete'
            if mutation == 'context': row['basis']['context_fingerprint'] = 'd'*64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_enrichment(document, self.snapshot)

    def test_null_priority_and_unclassified_do_not_fabricate_analysis(self):
        self.annotation['priority'].update(level=None, reason_zh=None, evidence_ids=[])
        self.annotation['classification'].update(role='unclassified', topics=[], rationale_zh=None, evidence_ids=[])
        result = enrich_snapshot(self.snapshot, self.document)
        self.assertIsNone(result['cards'][0]['enrichment']['priority']['level'])

    def test_cli_generates_new_artifact_without_installing_or_overwriting(self):
        source = self.root/'snapshot.json'; source.write_text(json.dumps(self.snapshot))
        document = self.root/'annotations.json'; document.write_text(json.dumps(self.document))
        output = self.root/'candidate.json'
        arguments = [sys.executable, '-m', 'review_app', 'enrich-snapshot', '--snapshot', str(source),
                     '--file', str(document), '--output', str(output)]
        completed = subprocess.run(arguments, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        validate_snapshot(json.loads(output.read_text()))
        self.assertFalse((self.root/'judgments.sqlite3').exists())
        previous = output.read_bytes()
        self.assertNotEqual(subprocess.run(arguments, capture_output=True).returncode, 0)
        self.assertEqual(previous, output.read_bytes())


if __name__ == '__main__':
    unittest.main()
