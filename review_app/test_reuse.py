"""Cross-version reuse, conservative context checks and immutable provenance."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from .build import calculate_snapshot_digest, validate_snapshot
from .database import connect, initialize, MIGRATIONS, DB_SCHEMA_VERSION
from .enrichment import enrich_snapshot
from .enrichment_v2 import DEFAULT_TOPICS
from .judgments import review_state, submit, reviewer_export
from .repositories import dataset_id, make_collection
from .reuse import context_basis, reuse_snapshot, predecessor
from .statements import compile_statements
from .storage import install_snapshot, create_backup, verify_backup
from .test_enrichment_v2 import fixture_document


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.file = self.source / 'Value.lean'
        self.file.write_text('namespace Example\ndef value : Nat := 1\nend Example\n')
        self.config = {'id': 'alpha', 'name': 'Alpha', 'roots': ['.'],
                       'topics': deepcopy(DEFAULT_TOPICS), 'main_targets': []}
        self.base = self.build('a')
        self.doc = fixture_document(self.base)
        self.old = enrich_snapshot(self.base, self.doc)
        self.new = self.build('b')
        self.data = self.root / 'runtime'
        self.db = self.data / 'judgments.sqlite3'
        self.reviewer = 'u_' + '1' * 32
        self.card_id = self.old['cards'][0]['id']

    def build(self, commit):
        return compile_statements(self.source, source_commit=commit * 40, repository=self.config)

    def install(self, snapshot):
        initialize(self.db)
        path = self.root / (uuid.uuid4().hex + '.json')
        path.write_text(json.dumps(snapshot))
        return install_snapshot(path, self.data, allow_dirty_source=True)

    def judge(self, snapshot, reviewer=None, rationale='original decision', verdict='aligned'):
        card = next(card for card in snapshot['cards'] if card['id'] == self.card_id)
        return submit(snapshot, self.db, reviewer or self.reviewer,
                      {'request_id': str(uuid.uuid4()), 'card_id': card['id'],
                       'fingerprint': card['fingerprint'], 'verdict': verdict, 'rationale': rationale})[1]['judgment']

    def test_carries_machine_fields_and_current_human_decisions_with_original_provenance(self):
        old_json, new_json = deepcopy(self.old), deepcopy(self.new)
        candidate = reuse_snapshot(self.new, self.old)
        self.assertEqual(self.old, old_json)
        self.assertEqual(self.new, new_json)
        self.assertEqual(candidate['cards'][0]['enrichment'], self.old['cards'][0]['enrichment'])
        self.assertEqual(candidate['cards'][0]['fingerprint'], self.old['cards'][0]['fingerprint'])
        self.assertNotIn('PRIVATE-ASSESSMENT', json.dumps(candidate))
        self.install(self.old)
        self.judge(self.old, rationale='superseded')
        original = self.judge(self.old)
        other = 'u_' + '2' * 32
        self.judge(self.old, reviewer=other, verdict='misaligned')
        installed, report = self.install(candidate)
        self.assertEqual(report['inherited_judgments'], 2)
        state = review_state(installed, self.db, self.card_id, self.reviewer)
        self.assertEqual(state['history_count'], 1)
        for key in ('created_at', 'source_commit', 'snapshot_digest', 'reviewer', 'rationale'):
            self.assertEqual(state['current'][key], original[key])
        self.assertEqual(state['current']['inherited_from_id'], original['id'])
        self.assertEqual(state['current']['inherited_from_dataset'], dataset_id(self.old))
        self.assertEqual(review_state(installed, self.db, self.card_id, other)['current']['verdict'], 'misaligned')
        self.assertEqual(self.install(candidate)[1].get('inherited_judgments', 0), 0)
        backup = create_backup(self.data, self.root / 'backups')
        self.assertEqual(verify_backup(backup)['database_schema_version'], DB_SCHEMA_VERSION)
        self.assertEqual(review_state(candidate, backup / 'judgments.sqlite3', self.card_id, self.reviewer), state)
        self.install(self.old)
        self.assertEqual(len(reviewer_export(self.old, self.db, self.reviewer)['judgments']), 2)

    def test_does_not_override_target_reviews_or_cross_repository_boundaries(self):
        candidate = reuse_snapshot(self.new, self.old)
        initialize(self.db)
        target = self.judge(candidate, rationale='new version decision', verdict='misaligned')
        self.judge(self.old)
        self.install(make_collection([self.old, candidate]))
        self.assertEqual(review_state(candidate, self.db, self.card_id, self.reviewer)['current']['id'], target['id'])
        other = compile_statements(self.source, source_commit='b' * 40, repository={**self.config, 'id': 'beta'})
        with self.assertRaisesRegex(ValueError, 'different repository'):
            reuse_snapshot(other, self.old)
        with self.assertRaisesRegex(ValueError, 'different source commit'):
            reuse_snapshot(self.base, self.old)

    def test_source_module_assumptions_and_imported_dependencies_invalidate_reuse(self):
        for text in ('namespace Example\ndef value : Nat := 2\nend Example\n',
                     'namespace Example\nvariable (n : Nat)\ndef value : Nat := 1\nend Example\n',
                     'namespace Example\ndef value : Nat := 1\ndef added : Nat := 3\nend Example\n'):
            with self.subTest(text=text):
                self.file.write_text(text)
                candidate = reuse_snapshot(self.build('b'), self.old)
                self.assertEqual(candidate['reuse']['cards'], {})
                self.assertNotIn('enrichment', candidate['cards'][0])
        # An imported module outside the configured scan roots is also frozen.
        self.config['roots'] = ['Value.lean']
        (self.source / 'Dependency.lean').write_text('import Transitive\ndef dependency : Nat := 1\n')
        (self.source / 'Transitive.lean').write_text('def transitive : Nat := 1\n')
        self.file.write_text('import Dependency\nnamespace Example\ndef value : Nat := dependency\nend Example\n')
        old = self.build('a')
        self.assertIn('Transitive.lean', old['context_modules'])
        self.assertEqual(len(reuse_snapshot(self.build('b'), old)['reuse']['cards']), 1)
        (self.source / 'Transitive.lean').write_text('def transitive : Nat := 2\n')
        self.assertEqual(reuse_snapshot(self.build('b'), old)['reuse']['cards'], {})

    def test_line_and_public_file_moves_reuse_but_layout_and_string_changes_do_not(self):
        self.file.write_text('\n\nnamespace Example\n\ndef value : Nat := 1\nend Example\n')
        self.file.rename(self.source / 'Moved.lean')
        candidate = reuse_snapshot(self.build('b'), self.old)
        self.assertEqual(len(candidate['reuse']['cards']), 1)
        self.assertEqual(candidate['cards'][0]['lean']['file'], 'Moved.lean')
        self.assertEqual(candidate['cards'][0]['blueprint_line'], candidate['cards'][0]['lean']['line'])
        from .reuse import _tokens
        self.assertNotEqual(_tokens('def x := "a\n\nb"'), _tokens('def x := "a\nb"'))
        self.assertNotEqual(_tokens('def x := by\n  exact 1'), _tokens('def x := by\n exact 1'))

    def test_external_environment_and_missing_historical_context_block_unsafe_reuse(self):
        (self.source / 'lean-toolchain').write_text('leanprover/lean4:v4.0.0')
        no_imports = self.build('a')
        (self.source / 'lean-toolchain').write_text('leanprover/lean4:v4.1.0')
        self.assertEqual(reuse_snapshot(self.build('b'), no_imports)['reuse']['cards'], {})
        self.file.write_text('import Mathlib\nnamespace Example\ndef value : Nat := 1\nend Example\n')
        self.assertIsNone(context_basis(self.build('a'), self.build('a')['cards'][0]))
        (self.source / 'lake-manifest.json').write_text('{"packages": []}')
        (self.source / 'lean-toolchain').write_text('leanprover/lean4:v4.0.0')
        old = self.build('a')
        self.assertEqual(len(reuse_snapshot(self.build('b'), old)['reuse']['cards']), 1)
        (self.source / 'lean-toolchain').write_text('leanprover/lean4:v4.1.0')
        self.assertEqual(reuse_snapshot(self.build('b'), old)['reuse']['cards'], {})
        old.pop('source_environment')
        old['digest'] = calculate_snapshot_digest(old)
        self.assertEqual(reuse_snapshot(self.build('b'), old)['reuse']['cards'], {})
        (self.source / 'lake-manifest.json').write_text('{"packages": [{"type": "path", "dir": "../dependency"}]}')
        self.assertIsNone(context_basis(self.build('b'), self.build('b')['cards'][0]))

    def test_install_verifies_predecessor_and_manifest_before_mutating_records(self):
        candidate = reuse_snapshot(self.new, self.old)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.install(candidate)
        broken = deepcopy(candidate)
        broken['reuse']['cards'][self.card_id] = '0' * 64
        broken['digest'] = calculate_snapshot_digest(broken)
        with self.assertRaisesRegex(ValueError, 'context'):
            validate_snapshot(broken)
        self.install(self.old)
        self.judge(self.old)
        broken = deepcopy(candidate)
        broken['reuse']['source_commit'] = 'c' * 40
        broken['reuse']['source_dataset'] = 'alpha@' + 'c' * 40
        broken['digest'] = calculate_snapshot_digest(broken)
        before = (self.data / 'snapshot.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.install(broken)
        self.assertEqual((self.data / 'snapshot.json').read_bytes(), before)
        self.assertEqual(reviewer_export(candidate, self.db, self.reviewer)['judgments'], [])

    def test_explicit_predecessor_selection_and_cli_do_not_overwrite_artifacts(self):
        collection = make_collection([self.old, self.new])
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            predecessor(self.new, collection)
        self.assertEqual(predecessor(self.new, collection, dataset_id(self.old)), self.old)
        old, new, output = (self.root / name for name in ('old.json', 'new.json', 'candidate.json'))
        old.write_text(json.dumps(self.old))
        new.write_text(json.dumps(self.new))
        command = [sys.executable, '-m', 'review_app', 'reuse-snapshot', '--snapshot', str(new),
                   '--reuse-from', str(old), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['reused'], 1)
        self.assertFalse(self.db.exists())
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        config = self.root / 'repository.json'
        config.write_text(json.dumps(self.config))
        built = self.root / 'built.json'
        command = [sys.executable, '-m', 'review_app', 'build', '--statements', '--source', str(self.source),
                   '--source-commit', 'b' * 40, '--repository-config', str(config),
                   '--reuse-from', str(old), '--output', str(built)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(built.read_text())['reuse']['cards']), 1)

    def test_numbered_migration_preserves_schema10_records(self):
        self.data.mkdir()
        with closing(connect(self.db)) as db:
            db.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)')
            for version, name, migration in MIGRATIONS[:10]:
                migration(db)
                db.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (version, name, 'old'))
        self.judge(self.old)
        initialize(self.db)
        state = review_state(self.old, self.db, self.card_id, self.reviewer)
        self.assertEqual(state['current']['rationale'], 'original decision')
        self.assertIsNone(state['current']['inherited_from_id'])

    def test_chain_and_reenrichment_keep_provenance_without_duplicate_reviews(self):
        candidate = reuse_snapshot(self.new, self.old)
        self.install(self.old)
        original = self.judge(self.old)
        self.install(candidate)
        document = fixture_document(candidate)
        refreshed = enrich_snapshot(candidate, document)
        self.assertNotIn('reuse_source_commit', refreshed['cards'][0])
        self.install(refreshed)
        third = reuse_snapshot(self.build('c'), refreshed)
        self.install(third)
        current = review_state(third, self.db, self.card_id, self.reviewer)['current']
        self.assertEqual(current['source_commit'], original['source_commit'])
        self.assertEqual(current['inherited_from_dataset'], dataset_id(candidate))
        self.assertEqual(len(reviewer_export(third, self.db, self.reviewer)['judgments']), 1)

    def test_changed_context_stays_pending_even_when_content_fingerprint_is_identical(self):
        self.install(self.base)
        self.judge(self.base)
        self.file.write_text('variable (n : Nat)\nnamespace Example\ndef value : Nat := 1\nend Example\n')
        candidate = reuse_snapshot(self.build('b'), self.base)
        self.assertEqual(candidate['cards'][0]['fingerprint'], self.base['cards'][0]['fingerprint'])
        self.install(candidate)
        self.assertIsNone(review_state(candidate, self.db, self.card_id, self.reviewer)['current'])

    def test_topic_removal_private_moves_and_fresh_translations_are_not_overwritten(self):
        self.config['topics'] = []
        self.assertEqual(reuse_snapshot(self.build('b'), self.old)['reuse']['cards'], {})
        fresh_doc = fixture_document(self.new)
        fresh_doc['annotations'][0]['readback']['text_zh'] = '新的回译。'
        fresh_doc['originals'][self.card_id]['readback']['text_zh'] = '新的回译。'
        fresh = enrich_snapshot(self.new, fresh_doc)
        candidate = reuse_snapshot(fresh, self.old)
        self.assertEqual(candidate['cards'][0]['statement'], '新的回译。')
        self.assertNotIn('reuse_source_commit', candidate['cards'][0])
        self.file.write_text('namespace Example\nprivate def value : Nat := 1\nend Example\n')
        prior = self.build('a')
        self.file.rename(self.source / 'Moved.lean')
        self.assertEqual(reuse_snapshot(self.build('b'), prior)['reuse']['cards'], {})


if __name__ == '__main__':
    unittest.main()
