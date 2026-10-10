"""Synthetic schema and privacy regressions; no model calls or math audit."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote
from urllib.request import Request, urlopen

from .build import _content_fingerprint, calculate_snapshot_digest, validate_snapshot
from .database import initialize
from .enrichment import enrich_snapshot, validate_enrichment
from .enrichment_v2 import DEFAULT_TOPICS, read_document, validate_agent_annotations, validate_stage_batch
from .judgments import catalog, reviewer_export
from .preview import PreviewAuthStore
from .server import ReviewHTTPServer, make_handler
from .statements import compile_statements


def fixture_document(snapshot):
    rows = [{'declaration_id': card['id'], 'title_zh': '测试夹具',
             'readback': {'text_zh': '[TEST] 中文回译协议夹具。', 'confidence': 0.95},
             'classification': {'role': 'definition', 'topics': ['adams']},
             'priority': None,
             'expectation_assessment': {'verdict': 'misaligned',
                 'reason_zh': 'PRIVATE-ASSESSMENT-REASON', 'confidence': 0.91}}
            for card in snapshot['cards']]
    return {'schema': 'statement-enrichment.v2',
            'run': {'run_id': 'fixture-run', 'model': 'PRIVATE-WORKER-MODEL',
                    'reasoning_effort': 'high', 'created_at': '2026-10-04T08:00:00Z',
                    'policy_version': 'statement-fields.v2',
                    'source_commit': snapshot['source_commit'],
                    'snapshot_digest': snapshot['digest'],
                    'expectation_context_digest': 'e' * 64,
                    'topics': deepcopy(DEFAULT_TOPICS), 'threshold': 0.8},
            'annotations': rows,
            'sources': {card['id']: hashlib.sha256(card['lean']['source'].encode()).hexdigest()
                        for card in snapshot['cards']},
            'originals': {row['declaration_id']: deepcopy(row) for row in rows},
            'reviews': {}}


class EnrichmentV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'KIP126').mkdir()
        (self.root / 'KIP126/Example.lean').write_text('namespace Example\ndef value : Nat := 1\nend Example\n')
        self.snapshot = compile_statements(self.root, source_commit='a' * 40)
        self.document = fixture_document(self.snapshot)
        self.identity = self.document['annotations'][0]['declaration_id']

    def test_minimal_worker_fields_do_not_require_source_excerpt_or_metadata(self):
        row = self.document['annotations'][0]
        self.assertEqual(validate_agent_annotations([row], self.snapshot), [row])
        self.assertEqual(validate_enrichment(self.document, self.snapshot), [row])
        for extra in ('summary_zh', 'unresolved', 'evidence', 'basis', 'provenance', 'verdict'):
            altered = deepcopy(row)
            altered[extra] = None
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_agent_annotations([altered], self.snapshot)

    def test_conditional_reason_and_finite_independent_scores(self):
        for verdict in ('misaligned', 'undetermined'):
            for reason in (None, '', '  '):
                row = deepcopy(self.document['annotations'][0])
                row['expectation_assessment'].update(verdict=verdict, reason_zh=reason)
                with self.subTest(verdict=verdict, reason=reason), self.assertRaises(ValueError):
                    validate_agent_annotations([row], self.snapshot)
        row['expectation_assessment'].update(verdict='aligned', reason_zh=None)
        validate_agent_annotations([row], self.snapshot)
        for key in ('readback', 'expectation_assessment'):
            for score in (True, False, '0.9', None, -0.01, 1.01, float('nan'), float('inf'), 10**500):
                altered = deepcopy(row)
                altered[key]['confidence'] = score
                with self.subTest(key=key, score=score), self.assertRaises(ValueError):
                    validate_agent_annotations([altered], self.snapshot)

    def test_unknown_role_empty_topics_and_custom_project_topics(self):
        row = deepcopy(self.document['annotations'][0])
        row['classification'] = {'role': None, 'topics': []}
        validate_agent_annotations([row], self.snapshot)
        row['classification']['topics'] = ['geometry']
        with self.assertRaises(ValueError):
            validate_agent_annotations([row], self.snapshot)
        validate_agent_annotations([row], self.snapshot, [{'id': 'geometry', 'name': '几何'}])
        row['classification']['topics'] *= 2
        with self.assertRaises(ValueError):
            validate_agent_annotations([row], self.snapshot, [{'id': 'geometry', 'name': '几何'}])

    def test_frozen_sources_and_original_identity_are_checked(self):
        for mutation in ('commit', 'digest', 'source', 'original_id', 'sources_extra', 'originals_missing'):
            doc = deepcopy(self.document)
            if mutation == 'commit': doc['run']['source_commit'] = 'b' * 40
            if mutation == 'digest': doc['run']['snapshot_digest'] = 'b' * 64
            if mutation == 'source': doc['sources'][self.identity] = 'b' * 64
            if mutation == 'original_id': doc['originals'][self.identity]['declaration_id'] = 'statement::Other'
            if mutation == 'sources_extra': doc['sources']['statement::Other'] = 'b' * 64
            if mutation == 'originals_missing': doc['originals'].clear()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_enrichment(doc, self.snapshot)

    def test_no_expectation_requires_unknown_even_with_high_confidence(self):
        self.document['run']['expectation_context_digest'] = None
        with self.assertRaises(ValueError): validate_enrichment(self.document, self.snapshot)
        for row in (self.document['annotations'][0], self.document['originals'][self.identity]):
            row['expectation_assessment'].update(verdict='undetermined', reason_zh='缺少预期说明。', confidence=0.99)
        validate_enrichment(self.document, self.snapshot)

    def test_review_keeps_original_scores_and_assessment(self):
        row = self.document['annotations'][0]
        row['readback']['confidence'] = 0.79
        self.document['originals'][self.identity] = deepcopy(row)
        with self.assertRaises(ValueError): validate_enrichment(self.document, self.snapshot)
        self.document['reviews'][self.identity] = {'model': 'fixture-main', 'reviewed_at': '2026-10-04T09:00:00Z'}
        row['readback']['text_zh'] = '[TEST] 经复核修正后的回译。'
        validate_enrichment(self.document, self.snapshot)
        row['readback']['confidence'] = 0.9
        with self.assertRaises(ValueError): validate_enrichment(self.document, self.snapshot)
        row['readback']['confidence'] = 0.79
        row['expectation_assessment']['verdict'] = 'aligned'
        with self.assertRaises(ValueError): validate_enrichment(self.document, self.snapshot)

    def test_missing_readback_and_high_score_review_cannot_be_accepted(self):
        doc = deepcopy(self.document)
        doc['reviews'][self.identity] = {'model': 'fixture-main', 'reviewed_at': '2026-10-04T09:00:00Z'}
        with self.assertRaises(ValueError): validate_enrichment(doc, self.snapshot)
        row = self.document['annotations'][0]
        row['readback']['text_zh'] = None
        self.document['originals'][self.identity] = deepcopy(row)
        validate_agent_annotations([row], self.snapshot)
        with self.assertRaises(ValueError): validate_enrichment(self.document, self.snapshot)

    def test_review_cannot_turn_generation_failure_into_a_success(self):
        row = self.document['annotations'][0]
        row['readback']['confidence'] = 0.79
        original = deepcopy(row)
        original['readback']['text_zh'] = None
        self.document['originals'][self.identity] = original
        self.document['reviews'][self.identity] = {'model': 'fixture-main', 'reviewed_at': '2026-10-04T09:00:00Z'}
        with self.assertRaises(ValueError): validate_enrichment(self.document, self.snapshot)

    def test_duplicate_json_keys_and_nonfinite_constants_are_rejected(self):
        path = self.root / 'invalid.json'
        for encoded in ('{"schema":"v1","schema":"v2"}', '{"confidence":NaN}'):
            path.write_text(encoded)
            with self.subTest(encoded=encoded), self.assertRaises(ValueError): read_document(path)

    def test_malformed_frozen_source_reports_a_validation_error(self):
        card = self.snapshot['cards'][0]
        card['lean']['source'] = 1
        nl, lean, fingerprint = _content_fingerprint(card, self.snapshot.get('dependency_lock_digest'))
        card.update(nl_digest=nl, lean_digest=lean, fingerprint=fingerprint,
                    fingerprints={card['fingerprint_scheme']: fingerprint})
        self.snapshot['digest'] = calculate_snapshot_digest(self.snapshot)
        self.document['run']['snapshot_digest'] = self.snapshot['digest']
        with self.assertRaisesRegex(ValueError, 'source text'):
            validate_enrichment(self.document, self.snapshot)

    def test_public_projection_drops_private_fields_and_summary(self):
        original = deepcopy(self.snapshot)
        self.snapshot['cards'][0]['reading_summary_zh'] = 'OLD-SUMMARY'
        self.snapshot['digest'] = calculate_snapshot_digest(self.snapshot)
        self.document = fixture_document(self.snapshot)
        result = enrich_snapshot(self.snapshot, self.document)
        validate_snapshot(result)
        card = result['cards'][0]
        encoded = json.dumps(result)
        for private in ('expectation_assessment', 'PRIVATE-ASSESSMENT-REASON', 'PRIVATE-WORKER-MODEL', 'confidence', 'originals', 'OLD-SUMMARY'):
            self.assertNotIn(private, encoded)
        self.assertEqual(card['enrichment']['readback']['status'], 'draft')
        self.assertIsNone(card['enrichment']['priority'])
        self.assertEqual(self.snapshot['cards'][0]['lean'], original['cards'][0]['lean'])
        self.assertEqual(result['enrichment_topics'], DEFAULT_TOPICS)

    def test_bilingual_readback_is_public_and_changes_review_basis(self):
        chinese = enrich_snapshot(self.snapshot, self.document)
        for row in (self.document['annotations'][0], self.document['originals'][self.identity]):
            row['readback']['text_en'] = '[TEST] An English mathematical statement.'
        bilingual = enrich_snapshot(self.snapshot, self.document)
        validate_snapshot(bilingual)
        readback = bilingual['cards'][0]['enrichment']['readback']
        self.assertEqual(readback['text_en'], '[TEST] An English mathematical statement.')
        self.assertEqual(bilingual['cards'][0]['statement'], chinese['cards'][0]['statement'])
        self.assertNotEqual(bilingual['cards'][0]['fingerprint'], chinese['cards'][0]['fingerprint'])
        self.assertNotIn('text_en', chinese['cards'][0]['enrichment']['readback'])

    def test_bilingual_readback_rejects_one_missing_language(self):
        row = deepcopy(self.document['annotations'][0])
        row['readback']['text_en'] = None
        with self.assertRaisesRegex(ValueError, 'both'):
            validate_agent_annotations([row], self.snapshot)
        row['readback']['text_zh'] = None
        validate_agent_annotations([row], self.snapshot)
        row['readback']['text_en'] = 'English only'
        with self.assertRaisesRegex(ValueError, 'both'):
            validate_agent_annotations([row], self.snapshot)

    def test_first_stage_accepts_bilingual_and_legacy_readbacks(self):
        row = deepcopy(self.document['annotations'][0])
        row.pop('expectation_assessment')
        batch = {'schema': 'formaliscope-readback-batch.v1', 'annotations': [row]}
        validate_stage_batch(batch, 'readback', self.snapshot)
        row['readback']['text_en'] = '[TEST] An English mathematical statement.'
        validate_stage_batch(batch, 'readback', self.snapshot)
        row['readback']['text_en'] = None
        with self.assertRaisesRegex(ValueError, 'both'):
            validate_stage_batch(batch, 'readback', self.snapshot)

    def test_internal_assessment_changes_do_not_change_public_snapshot_digest(self):
        first = enrich_snapshot(self.snapshot, self.document)
        for row in (self.document['annotations'][0], self.document['originals'][self.identity]):
            row['expectation_assessment'].update(verdict='aligned', reason_zh=None, confidence=0.97)
        self.document['run']['model'] = 'another-internal-model'
        second = enrich_snapshot(self.snapshot, self.document)
        self.assertEqual(first['digest'], second['digest'])
        self.assertEqual(first['cards'][0]['fingerprint'], second['cards'][0]['fingerprint'])

    def test_private_snapshot_fields_and_extra_public_fields_are_rejected(self):
        result = enrich_snapshot(self.snapshot, self.document)
        for placement in ('card', 'public', 'root'):
            altered = deepcopy(result)
            target = altered if placement == 'root' else altered['cards'][0] if placement == 'card' else altered['cards'][0]['enrichment']
            target['expectation_assessment'] = {'verdict': 'aligned'}
            altered['digest'] = calculate_snapshot_digest(altered)
            with self.subTest(placement=placement), self.assertRaises(ValueError): validate_snapshot(altered)
        result['cards'][0]['enrichment']['readback']['confidence'] = 0.9
        result['digest'] = calculate_snapshot_digest(result)
        with self.assertRaises(ValueError): validate_snapshot(result)

    def test_topic_config_is_protected_by_digest_and_reaches_catalog(self):
        self.document['run']['topics'] = [{'id': 'geometry', 'name': '几何'}]
        for row in (self.document['annotations'][0], self.document['originals'][self.identity]):
            row['classification']['topics'] = ['geometry']
        result = enrich_snapshot(self.snapshot, self.document)
        db = self.root / 'reviews.sqlite3'
        initialize(db)
        self.assertEqual(catalog(result, db, 'fixture')['enrichment_topics'], [{'id': 'geometry', 'name': '几何'}])
        self.assertEqual(catalog(self.snapshot, db, 'fixture')['enrichment_topics'], DEFAULT_TOPICS)
        result['enrichment_topics'][0]['name'] = '改名'
        with self.assertRaises(ValueError): validate_snapshot(result)


class EnrichmentPrivacyHTTPTests(unittest.TestCase):
    def test_internal_database_records_never_reach_reviewer_endpoints(self):
        from .agent_assessments import import_agent_assessments
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'KIP126').mkdir()
            (root / 'KIP126/Example.lean').write_text('def value : Nat := 1\n')
            base = compile_statements(root, source_commit='a' * 40)
            doc = fixture_document(base)
            snapshot = enrich_snapshot(base, doc)
            data = root / 'data'
            imported = import_agent_assessments(data, base, doc)
            self.assertEqual(imported['inserted'], 1)
            db = data / 'judgments.sqlite3'
            auth = PreviewAuthStore(db)
            handler = make_handler(snapshot, db, Path(__file__).parent / 'static', auth, preview=True)
            handler.log_message = lambda *args: None
            server = ReviewHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}'
            try:
                request = Request(url + '/api/preview/session', data=json.dumps({'display_name': 'fixture'}).encode(),
                                  headers={'Origin': url, 'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    cookie = response.headers['Set-Cookie'].split(';')[0]
                identity = quote(snapshot['cards'][0]['id'])
                paths = ['/api/catalog?initial=auto', '/api/card?id=' + identity,
                         '/api/evidence?id=' + identity, '/api/export?mode=history',
                         '/api/export?mode=latest', '/api/admin/summary']
                for path in paths:
                    with self.subTest(path=path), urlopen(Request(url + path, headers={'Cookie': cookie}), timeout=5) as response:
                        raw = response.read().decode()
                        for private in ('PRIVATE-ASSESSMENT-REASON', 'PRIVATE-WORKER-MODEL', 'expectation_assessment'):
                            self.assertNotIn(private, raw)
                payload = catalog(snapshot, db, 'nonexistent', initial_id='auto')
                self.assertIsNone(payload['cards'][0]['verdict'])
                self.assertEqual(reviewer_export(snapshot, db, 'nonexistent')['judgments'], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
