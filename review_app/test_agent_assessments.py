"""Private machine imports are versioned, atomic and independent of reviews."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from .agent_assessments import import_agent_assessments
from .database import DB_SCHEMA_VERSION, MIGRATIONS, connect, initialize
from .data_lock import data_lock
from .enrichment import enrich_snapshot
from .judgments import catalog, history, reviewer_export
from .statements import compile_statements
from .storage import create_backup, verify_backup


class AgentAssessmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        (self.source / 'KIP126').mkdir(parents=True)
        (self.source / 'KIP126/Example.lean').write_text(
            'namespace Example\ndef one : Nat := 1\ndef two : Nat := 2\nend Example\n',
            encoding='utf-8')
        self.snapshot = compile_statements(self.source, source_commit='a' * 40)
        self.document = self.batch(self.snapshot)
        self.data = self.root / 'runtime'

    @staticmethod
    def batch(snapshot, run_id='test-run'):
        annotations = [{
            'declaration_id': card['id'], 'title_zh': '自然数常量',
            'readback': {'text_zh': '该定义给出一个自然数常量。', 'confidence': 0.95},
            'classification': {'role': 'definition', 'topics': []},
            'priority': 'p2',
            'expectation_assessment': {'verdict': 'undetermined',
                'reason_zh': '任务尚未提供独立预期。', 'confidence': 0.99},
        } for card in snapshot['cards']]
        return {'schema': 'statement-enrichment.v2',
            'run': {'run_id': run_id, 'model': 'fixture-no-model-call',
                'reasoning_effort': 'high', 'created_at': '2026-10-04T00:00:00Z',
                'policy_version': 'statement-fields.v2',
                'source_commit': snapshot['source_commit'], 'snapshot_digest': snapshot['digest'],
                'expectation_context_digest': None,
                'topics': [{'id': 'spectral_sequence', 'name': '谱序列'}], 'threshold': 0.8},
            'annotations': annotations,
            'sources': {card['id']: hashlib.sha256(card['lean']['source'].encode()).hexdigest()
                        for card in snapshot['cards']},
            'originals': {row['declaration_id']: deepcopy(row) for row in annotations},
            'reviews': {}}

    @staticmethod
    def select(document, *identities):
        result = deepcopy(document)
        selected = set(identities)
        result['annotations'] = [row for row in result['annotations']
                                 if row['declaration_id'] in selected]
        result['sources'] = {key: value for key, value in result['sources'].items() if key in selected}
        result['originals'] = {key: value for key, value in result['originals'].items() if key in selected}
        result['reviews'] = {key: value for key, value in result['reviews'].items() if key in selected}
        return result

    def rows(self, table):
        with closing(connect(self.data / 'judgments.sqlite3')) as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]

    def cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'review_app', *map(str, arguments)],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)

    def test_schema8_migration_preserves_human_history_drafts_and_sessions(self):
        self.data.mkdir()
        database = self.data / 'judgments.sqlite3'
        with closing(connect(database)) as db:
            db.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)')
            for version, name, migration in MIGRATIONS[:8]:
                migration(db)
                db.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (version, name, 'original'))
            db.execute("""INSERT INTO judgments
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at)
                VALUES ('human-1', 'request-1', 'statement::Example.one', 'fingerprint',
                        'reviewer', 'aligned', '人工结论', 'original')""")
            db.execute("""INSERT INTO review_drafts
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale,
                 created_at, fingerprint_scheme, review_basis_scheme, review_basis_fingerprint, revision)
                VALUES ('draft-1', 'request-2', 'statement::Example.two', 'fingerprint',
                        'reviewer', '', '人工草稿', 'original', 'scheme', 'scheme', 'basis', 1)""")
            db.execute("INSERT INTO login_sessions VALUES ('token-digest', 'reviewer', 1, 2)")
            db.execute("INSERT INTO reviewer_profiles VALUES ('reviewer', '姓名', 0)")
            tables = ('judgments', 'review_drafts', 'login_sessions', 'reviewer_profiles')
            before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
                      for table in tables}
            ledger = [tuple(row) for row in db.execute('SELECT * FROM schema_migrations')]
        initialize(database)
        with closing(connect(database)) as db:
            self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], DB_SCHEMA_VERSION)
            for table in tables:
                self.assertEqual([tuple(row)[:len(before[table][0])] for row in db.execute(f'SELECT * FROM {table}')], before[table])
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM schema_migrations WHERE version<=8')], ledger)
            migrated = list(db.iterdump())
        initialize(database)
        with closing(connect(database)) as db:
            self.assertEqual(list(db.iterdump()), migrated)

    def test_import_retains_original_scores_runtime_and_content_bindings(self):
        report = import_agent_assessments(self.data, self.snapshot, self.document)
        self.assertEqual(report['inserted'], 2)
        self.assertEqual(report['unchanged'], 0)
        self.assertEqual(report['database_schema_version'], DB_SCHEMA_VERSION)
        self.assertFalse((self.data / 'snapshot.json').exists())
        run = self.rows('agent_assessment_runs')[0]
        self.assertEqual(json.loads(run['run_json']), self.document['run'])
        for row in self.rows('agent_assessments'):
            original = self.document['originals'][row['declaration_id']]
            self.assertEqual(row['readback_confidence'], original['readback']['confidence'])
            self.assertEqual(row['expectation_confidence'], original['expectation_assessment']['confidence'])
            self.assertEqual(row['expectation_verdict'], 'undetermined')
            self.assertEqual(row['expectation_reason_zh'], '任务尚未提供独立预期。')
            self.assertEqual(json.loads(row['original_json']), original)
            self.assertEqual(row['source_sha256'], self.document['sources'][row['declaration_id']])
            self.assertEqual(row['readback_sha256'], hashlib.sha256(original['readback']['text_zh'].encode()).hexdigest())
        self.assertEqual(self.rows('judgments'), [])
        self.assertEqual(self.rows('review_drafts'), [])

    def test_retries_do_not_duplicate_or_rewrite_records(self):
        import_agent_assessments(self.data, self.snapshot, self.document)
        with closing(connect(self.data / 'judgments.sqlite3')) as db:
            before = list(db.iterdump())
        report = import_agent_assessments(self.data, self.snapshot, self.document)
        self.assertEqual((report['inserted'], report['unchanged']), (0, 2))
        with closing(connect(self.data / 'judgments.sqlite3')) as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_invalid_batch_and_legacy_input_do_not_create_database_or_data_directory(self):
        for mutation in ('v1', 'confidence', 'source', 'reason', 'unknown'):
            document = deepcopy(self.document)
            if mutation == 'v1': document['schema'] = 'statement-enrichment.v1'
            if mutation == 'confidence':
                document['annotations'][-1]['readback']['confidence'] = 1.1
            if mutation == 'source': document['sources'][document['annotations'][-1]['declaration_id']] = 'b' * 64
            if mutation == 'reason': document['annotations'][-1]['expectation_assessment']['reason_zh'] = ' '
            if mutation == 'unknown': document['annotations'][-1]['declaration_id'] = 'statement::Unknown'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                import_agent_assessments(self.data, self.snapshot, document)
            self.assertFalse(self.data.exists())

    def test_run_metadata_conflict_does_not_change_existing_records(self):
        import_agent_assessments(self.data, self.snapshot, self.document)
        document = deepcopy(self.document)
        document['run']['model'] = 'another-fixture-model'
        before = self.rows('agent_assessment_runs'), self.rows('agent_assessments')
        with self.assertRaisesRegex(ValueError, 'run_id.*metadata'):
            import_agent_assessments(self.data, self.snapshot, document)
        self.assertEqual((self.rows('agent_assessment_runs'), self.rows('agent_assessments')), before)

    def test_result_conflict_rolls_back_other_new_rows_in_the_batch(self):
        first, second = [row['declaration_id'] for row in self.document['annotations']]
        import_agent_assessments(self.data, self.snapshot, self.select(self.document, second))
        document = deepcopy(self.document)
        document['annotations'][1]['title_zh'] = '另一标题'
        document['originals'][second]['title_zh'] = '另一标题'
        before = self.rows('agent_assessments')
        with self.assertRaisesRegex(ValueError, 'different immutable result'):
            import_agent_assessments(self.data, self.snapshot, document)
        self.assertEqual(self.rows('agent_assessments'), before)
        self.assertNotIn(first, {row['declaration_id'] for row in self.rows('agent_assessments')})

    def test_insert_failure_rolls_back_the_run_and_all_rows(self):
        initialize(self.data / 'judgments.sqlite3')
        second = self.document['annotations'][1]['declaration_id']
        with closing(connect(self.data / 'judgments.sqlite3')) as db:
            db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON agent_assessments "
                       "WHEN NEW.declaration_id='" + second.replace("'", "''") + "' "
                       "BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture write failure'):
            import_agent_assessments(self.data, self.snapshot, self.document)
        self.assertEqual(self.rows('agent_assessment_runs'), [])
        self.assertEqual(self.rows('agent_assessments'), [])

    def test_distinct_source_and_expectation_versions_keep_history(self):
        import_agent_assessments(self.data, self.snapshot, self.document)
        same_source = deepcopy(self.document)
        same_source['run'].update(run_id='new-expectation', expectation_context_digest='e' * 64)
        import_agent_assessments(self.data, self.snapshot, same_source)
        (self.source / 'KIP126/Example.lean').write_text(
            'namespace Example\ndef one : Nat := 10\ndef two : Nat := 20\nend Example\n', encoding='utf-8')
        new_snapshot = compile_statements(self.source, source_commit='b' * 40)
        import_agent_assessments(self.data, new_snapshot, self.batch(new_snapshot, 'new-source'))
        self.assertEqual(len(self.rows('agent_assessment_runs')), 3)
        self.assertEqual(len(self.rows('agent_assessments')), 6)
        self.assertEqual({row['source_commit'] for row in self.rows('agent_assessment_runs')}, {'a' * 40, 'b' * 40})

    def test_repository_versions_keep_independent_machine_runs(self):
        from .repositories import dataset_id
        snapshots = [compile_statements(self.source, source_commit=commit, repository={
            'id': repository, 'name': repository, 'roots': ['KIP126'], 'topics': []})
            for repository, commit in [('alpha', 'a' * 40), ('alpha', 'b' * 40), ('beta', 'a' * 40)]]
        for number, snapshot in enumerate(snapshots):
            import_agent_assessments(self.data, snapshot, self.batch(snapshot, f'repository-run-{number}'))
        runs = self.rows('agent_assessment_runs')
        self.assertEqual({row['dataset_id'] for row in runs}, {dataset_id(item) for item in snapshots})
        self.assertEqual(len(self.rows('agent_assessments')), 6)
        self.assertEqual(snapshots[0]['cards'][0]['fingerprint'], snapshots[1]['cards'][0]['fingerprint'])
        before = self.rows('agent_assessment_runs'), self.rows('agent_assessments')
        with self.assertRaisesRegex(ValueError, 'run_id.*metadata'):
            import_agent_assessments(self.data, snapshots[1], self.batch(snapshots[1], 'repository-run-0'))
        self.assertEqual((self.rows('agent_assessment_runs'), self.rows('agent_assessments')), before)

    def test_all_verdicts_and_boundary_confidences_are_preserved(self):
        identity = self.document['annotations'][0]['declaration_id']
        for verdict, reason, confidence in (
                ('aligned', None, 1),
                ('misaligned', '该结论遗漏预期要求的适用条件。', 0),
                ('undetermined', '预期材料不足。', 0.9)):
            document = self.select(self.document, identity)
            document['run'].update(run_id=verdict, expectation_context_digest='e' * 64)
            assessment = {'verdict': verdict, 'reason_zh': reason, 'confidence': confidence}
            document['annotations'][0]['expectation_assessment'] = assessment
            document['originals'][identity]['expectation_assessment'] = deepcopy(assessment)
            import_agent_assessments(self.data, self.snapshot, document)
        records = {row['expectation_verdict']: row for row in self.rows('agent_assessments')}
        self.assertEqual(set(records), {'aligned', 'misaligned', 'undetermined'})
        self.assertIsNone(records['aligned']['expectation_reason_zh'])
        self.assertEqual(records['aligned']['expectation_confidence'], 1)
        self.assertEqual(records['misaligned']['expectation_confidence'], 0)

    def test_review_preserves_raw_judgment_scores_and_both_readback_hashes(self):
        document = deepcopy(self.document)
        original = document['annotations'][0]
        original['readback']['confidence'] = 0.7
        document['originals'][original['declaration_id']] = deepcopy(original)
        original['readback']['text_zh'] = '复核修正后的自然数常量说明。'
        document['reviews'][original['declaration_id']] = {
            'model': 'fixture-main-review-model', 'reviewed_at': '2026-10-04T00:10:00Z'}
        import_agent_assessments(self.data, self.snapshot, document)
        row = self.rows('agent_assessments')[0]
        self.assertEqual(row['readback_confidence'], 0.7)
        self.assertEqual(row['expectation_confidence'], 0.99)
        self.assertNotEqual(row['readback_sha256'], row['original_readback_sha256'])
        self.assertEqual(json.loads(row['review_json']), document['reviews'][original['declaration_id']])

    def test_missing_readback_is_not_imported_as_a_successful_result(self):
        document = deepcopy(self.document)
        identity = document['annotations'][0]['declaration_id']
        document['annotations'][0]['readback']['text_zh'] = None
        document['originals'][identity]['readback']['text_zh'] = None
        with self.assertRaisesRegex(ValueError, 'missing readback'):
            import_agent_assessments(self.data, self.snapshot, document)
        self.assertFalse(self.data.exists())

    def test_unspecified_reasoning_effort_is_kept_as_null(self):
        document = deepcopy(self.document)
        document['run']['reasoning_effort'] = None
        import_agent_assessments(self.data, self.snapshot, document)
        self.assertIsNone(self.rows('agent_assessment_runs')[0]['reasoning_effort'])

    def test_import_waits_for_the_shared_maintenance_lock(self):
        entered, finished = threading.Event(), threading.Event()
        results, errors = [], []

        def importing():
            entered.set()
            try:
                results.append(import_agent_assessments(self.data, self.snapshot, self.document))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=importing)
        try:
            with data_lock(self.data):
                worker.start()
                self.assertTrue(entered.wait(5))
                self.assertFalse(finished.wait(0.15), 'import ignored the shared data-directory lock')
                self.assertFalse((self.data / 'judgments.sqlite3').exists())
        finally:
            if worker.ident is not None:
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['inserted'], 2)

    def test_backup_retains_private_tables_and_reviewer_outputs_remain_separate(self):
        import_agent_assessments(self.data, self.snapshot, self.document)
        candidate = enrich_snapshot(self.snapshot, self.document)
        (self.data / 'snapshot.json').write_text(json.dumps(candidate), encoding='utf-8')
        backup = create_backup(self.data, self.root / 'backups')
        self.assertEqual(verify_backup(backup)['database_schema_version'], DB_SCHEMA_VERSION)
        with closing(connect(backup / 'judgments.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_assessments').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_assessment_runs').fetchone()[0], 1)
        database = self.data / 'judgments.sqlite3'
        payload = catalog(candidate, database, 'reviewer', initial_id='auto')
        self.assertTrue(all(row['verdict'] is None for row in payload['cards']))
        for row in candidate['cards']:
            self.assertEqual(history(database, row['id'], 'reviewer'), [])
        outputs = [candidate, payload, reviewer_export(candidate, database, 'reviewer')]
        for output in outputs:
            serialized = json.dumps(output, ensure_ascii=False)
            self.assertNotIn('expectation_assessment', serialized)
            self.assertNotIn('任务尚未提供独立预期。', serialized)

    def test_cli_requires_explicit_target_and_original_frozen_snapshot(self):
        base_file = self.root / 'base.json'
        base_file.write_text(json.dumps(self.snapshot), encoding='utf-8')
        annotation_file = self.root / 'enrichment.json'
        annotation_file.write_text(json.dumps(self.document), encoding='utf-8')
        arguments = ['import-agent-assessments', '--snapshot', base_file, '--file', annotation_file]
        omitted = self.cli(*arguments)
        self.assertNotEqual(omitted.returncode, 0)
        self.assertFalse(self.data.exists())
        result = self.cli(*arguments, '--data-dir', self.data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['inserted'], 2)
        repeated = self.cli(*arguments, '--data-dir', self.data)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)['unchanged'], 2)
        candidate_file = self.root / 'candidate.json'
        candidate_file.write_text(json.dumps(enrich_snapshot(self.snapshot, self.document)), encoding='utf-8')
        wrong = self.cli('import-agent-assessments', '--snapshot', candidate_file,
                         '--file', annotation_file, '--data-dir', self.data)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertNotIn('Traceback', wrong.stderr)
        self.assertEqual(len(self.rows('agent_assessments')), 2)


if __name__ == '__main__':
    unittest.main()
