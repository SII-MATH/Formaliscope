"""Real isolation boundaries: same names, same content and concurrent versions."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from .build import calculate_snapshot_digest, validate_snapshot
from .database import DB_SCHEMA_VERSION, connect, initialize
from .judgments import catalog, history, review_state, reviewer_export, save_draft, submit
from .preview import PreviewAuthStore
from .repositories import current_datasets, dataset_id, make_collection, qualify_legacy, repository_config, select_dataset
from .server import ReviewHTTPServer, make_handler
from .statements import compile_statements
from .storage import create_backup, install_snapshot, verify_backup
from .symbols import SymbolIndex


class RepositoryFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        (self.source / 'Src').mkdir(parents=True)
        (self.source / 'Src/Value.lean').write_text('namespace Shared\ndef value : Nat := 1\nend Shared\n')
        (self.source / 'Root.lean').write_text('namespace Shared\ntheorem main : True := by trivial\nend Shared\n')
        self.config = {'id': 'alpha', 'name': 'Alpha', 'roots': ['Src', 'Root.lean'],
                       'topics': [{'id': 'topology', 'name': '拓扑'}], 'main_targets': ['Shared.main']}
        self.first = compile_statements(self.source, source_commit='a' * 40, repository=self.config)
        self.second = compile_statements(self.source, source_commit='b' * 40, repository=self.config)
        self.other = compile_statements(self.source, source_commit='a' * 40,
            repository={**self.config, 'id': 'beta', 'name': 'Beta', 'topics': []})
        self.collection = make_collection([self.first, self.second, self.other])
        self.data = self.root / 'data'
        self.db = self.data / 'judgments.sqlite3'
        initialize(self.db)
        self.reviewer = 'u_' + '1' * 32

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, snapshot, **values):
        card = next(item for item in snapshot['cards'] if item['declaration'] == 'Shared.value')
        return {'card_id': card['id'], 'fingerprint': card['fingerprint'],
                'request_id': str(uuid.uuid4()), 'verdict': 'aligned', 'rationale': '', **values}


class RepositoryTests(RepositoryFixture, unittest.TestCase):
    def test_current_repository_versions_preserve_default_and_archives(self):
        first, second, other = deepcopy(self.first), deepcopy(self.second), deepcopy(self.other)
        first['generated_at'] = '2026-10-01T00:00:00+00:00'
        second['generated_at'] = '2026-10-02T00:00:00+00:00'
        older_other = deepcopy(other)
        older_other['source_commit'] = 'c' * 40
        older_other['generated_at'] = '2026-09-01T00:00:00+00:00'
        for item in (first, second, other, older_other):
            item['digest'] = calculate_snapshot_digest(item)
        collection = make_collection([first, second, other, older_other])
        self.assertEqual(current_datasets(collection), [first, other])
        collection['default_dataset'] = dataset_id(second)
        self.assertEqual(current_datasets(collection), [second, other])
        self.assertEqual(len(collection['datasets']), 4)
        self.assertEqual(current_datasets(first), [first])

    def test_roots_topics_main_targets_and_namespace_are_configurable(self):
        self.assertEqual(len(self.first['cards']), 2)
        self.assertEqual(self.first['enrichment_topics'], self.config['topics'])
        self.assertTrue(next(card for card in self.first['cards'] if card['declaration'] == 'Shared.main')['main_target'])
        self.assertNotEqual(self.first['cards'][0]['id'], self.other['cards'][0]['id'])
        validate_snapshot(self.collection)
        self.assertEqual(select_dataset(self.collection, dataset_id(self.second)), self.second)
        with self.assertRaises(ValueError):
            make_collection([self.first, self.first])
        with self.assertRaises(ValueError):
            select_dataset(self.collection, 'missing')
        broken = deepcopy(self.collection)
        broken['datasets'][1]['repository']['name'] = 'tampered'
        broken['digest'] = calculate_snapshot_digest(broken)
        with self.assertRaises(ValueError):
            validate_snapshot(broken)

    def test_scanning_refuses_escapes_and_excludes_dependency_caches(self):
        for roots in (['../source'], ['/tmp'], ['.git'], ['.lake'], []):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                repository_config({**self.config, 'roots': roots})
        (self.source / '.lake').mkdir()
        (self.source / '.lake/Cache.lean').write_text('def forbidden := 2')
        (self.source / 'lakefile.lean').write_text('def forbidden := 3')
        wide = compile_statements(self.source, source_commit='a' * 40, repository={**self.config, 'roots': ['.']})
        self.assertEqual(len(wide['cards']), 2)
        external = self.root / 'outside.lean'
        external.write_text('def escaped := 1')
        (self.source / 'Src/Escape.lean').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            compile_statements(self.source, source_commit='a' * 40, repository=self.config)

    def test_private_names_keep_file_identity_and_local_dependencies(self):
        for file in ('One', 'Two'):
            (self.source / f'Src/{file}.lean').write_text(
                f'namespace Shared\nprivate def helper : Nat := 1\ndef use{file} : Nat := helper\nend Shared\n')
        snapshot = compile_statements(self.source, source_commit='a' * 40, repository=self.config)
        helpers = [card for card in snapshot['cards'] if card['declaration'] == 'Shared.helper']
        self.assertEqual(len(helpers), 2)
        self.assertNotEqual(helpers[0]['id'], helpers[1]['id'])
        index = SymbolIndex(snapshot)
        for file in ('One', 'Two'):
            card = next(card for card in snapshot['cards'] if card['declaration'] == 'Shared.use' + file)
            helper = next(card for card in helpers if card['module_file'] == f'Src/{file}.lean')
            self.assertEqual(card['dependencies'], [helper['id']])
            column = card['lean']['source'].index('helper') + 1
            result = index.resolve(card['id'], 'helper', card['lean']['line'], column)
            self.assertEqual(result['target']['card_id'], helper['id'])

    def test_identical_evidence_still_has_independent_reviews_drafts_and_exports(self):
        self.assertEqual(self.first['cards'][0]['fingerprint'], self.second['cards'][0]['fingerprint'])
        self.assertEqual(submit(self.first, self.db, self.reviewer, self.payload(self.first))[0], 201)
        draft = self.payload(self.first, verdict='', rationale='alpha draft', revision=0)
        self.assertEqual(save_draft(self.first, self.db, self.reviewer, draft)[0], 200)
        for snapshot in (self.second, self.other):
            self.assertTrue(all(card['verdict'] is None for card in catalog(snapshot, self.db, self.reviewer)['cards']))
            self.assertEqual(reviewer_export(snapshot, self.db, self.reviewer)['judgments'], [])
            state = review_state(snapshot, self.db, self.payload(snapshot)['card_id'], self.reviewer)
            self.assertEqual(state, {'current': None, 'draft': None, 'draft_revision': 0, 'history_count': 0})
            self.assertEqual(save_draft(snapshot, self.db, self.reviewer,
                self.payload(snapshot, verdict='', rationale='independent', revision=0))[0], 200)
        self.assertEqual(review_state(self.first, self.db, draft['card_id'], self.reviewer)['draft']['rationale'], 'alpha draft')

    def test_nondefault_archive_and_legacy_automatic_update_cannot_bypass_install_policy(self):
        # The default is clean; an unverified nondefault child still blocks production.
        clean = deepcopy(self.first)
        clean['source_origin'] = 'git-checkout'
        clean['digest'] = calculate_snapshot_digest(clean)
        collection = make_collection([clean, self.other])
        candidate = self.root / 'collection.json'
        candidate.write_text(json.dumps(collection))
        with self.assertRaisesRegex(ValueError, 'unverified'):
            install_snapshot(candidate, self.root / 'production')
        self.assertFalse((self.root / 'production').exists())
        install_snapshot(candidate, self.data, allow_dirty_source=True)
        before = (self.data / 'snapshot.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'automatic updates'):
            install_snapshot(candidate, self.data, allow_dirty_source=True, legacy_kip126_only=True)
        self.assertEqual((self.data / 'snapshot.json').read_bytes(), before)

    def test_legacy_migration_backup_retry_and_snapshot_rollback(self):
        legacy_source = self.root / 'legacy'
        (legacy_source / 'KIP126').mkdir(parents=True)
        (legacy_source / 'KIP126/Value.lean').write_text('def value : Nat := 1\n')
        legacy = compile_statements(legacy_source, source_commit='c' * 40)
        old_file = self.root / 'old.json'
        old_file.write_text(json.dumps(legacy))
        install_snapshot(old_file, self.data, allow_dirty_source=True)
        card = legacy['cards'][0]
        payload = {'card_id': card['id'], 'fingerprint': card['fingerprint'], 'request_id': str(uuid.uuid4()),
                   'verdict': 'aligned', 'rationale': 'keep history'}
        submit(legacy, self.db, self.reviewer, payload)
        save_draft(legacy, self.db, self.reviewer, {**payload, 'request_id': str(uuid.uuid4()),
                                                 'verdict': '', 'rationale': 'keep draft', 'revision': 0})
        with closing(connect(self.db)) as db:
            db.execute('UPDATE judgments SET source_commit=NULL')
        original = reviewer_export(legacy, self.db, self.reviewer)['judgments']
        collection = make_collection([legacy, self.other])
        candidate = self.root / 'new.json'
        candidate.write_text(json.dumps(collection))
        install_snapshot(candidate, self.data, allow_dirty_source=True)
        selected = select_dataset(collection)
        state = review_state(selected, self.db, selected['cards'][0]['id'], self.reviewer)
        self.assertEqual(state['current']['rationale'], 'keep history')
        self.assertEqual(state['draft']['rationale'], 'keep draft')
        with closing(connect(self.db)) as db:
            count = db.execute('SELECT COUNT(*) FROM judgments').fetchone()[0]
        install_snapshot(candidate, self.data, allow_dirty_source=True)
        with closing(connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM judgments').fetchone()[0], count)
        backup = create_backup(self.data, self.root / 'backups')
        self.assertEqual(verify_backup(backup)['database_schema_version'], DB_SCHEMA_VERSION)
        self.assertEqual(review_state(selected, backup / 'judgments.sqlite3', selected['cards'][0]['id'], self.reviewer)['draft']['rationale'], 'keep draft')
        # Version isolation remains available when explicitly disabling reuse.
        # Commit-less legacy rows must not be guessed into a different commit.
        later = qualify_legacy(compile_statements(legacy_source, source_commit='d' * 40))
        changed_default = make_collection([selected, later, self.other], default=dataset_id(later))
        later_file = self.root / 'later.json'
        later_file.write_text(json.dumps(changed_default))
        install_snapshot(later_file, self.data, allow_dirty_source=True, reuse_unchanged=False)
        self.assertEqual(history(self.db, later['cards'][0]['id'], self.reviewer, dataset=dataset_id(later)), [])
        install_snapshot(old_file, self.data, allow_dirty_source=True)
        self.assertEqual(reviewer_export(legacy, self.db, self.reviewer)['judgments'], original)


class RepositoryHTTPTests(RepositoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.auth = PreviewAuthStore(self.db)
        handler = make_handler(self.collection, self.db, Path(__file__).parent / 'static', self.auth, preview=True)
        handler.log_message = lambda *args: None
        self.server = ReviewHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        _, headers, _ = self.request('/api/preview/session', body={'display_name': 'Operator'})
        self.admin = headers['Set-Cookie'].split(';')[0]
        _, headers, _ = self.request('/api/preview/session', body={'display_name': 'Reviewer'})
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, path, *, snapshot=None, cookie=None, body=None):
        if snapshot is not None:
            path += ('&' if '?' in path else '?') + urlencode({'dataset': dataset_id(snapshot)})
        headers = {'Origin': self.base}
        if cookie:
            headers['Cookie'] = cookie
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.base + path, headers=headers, data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if response.headers['Content-Type'].startswith('application/json') else raw

    def test_http_catalog_history_exports_and_admin_are_dataset_scoped(self):
        self.assertEqual(self.request('/api/datasets')[0], 401)
        selection = self.request('/api/datasets', cookie=self.cookie)[2]
        self.assertEqual(len(selection['datasets']), 3)
        self.assertEqual({item['id'] for item in selection['current_datasets']},
                         {dataset_id(select_dataset(self.collection)), dataset_id(self.other)})
        payload = self.payload(self.first, rationale='alpha-only')
        self.assertEqual(self.request('/api/judgments', snapshot=self.first, cookie=self.cookie, body=payload)[0], 201)
        self.assertEqual(self.request('/api/judgments', cookie=self.cookie, body=payload)[0], 400)
        self.assertEqual(self.request('/api/judgments', snapshot=self.other, cookie=self.cookie, body=payload)[0], 404)
        self.assertEqual(self.request('/api/judgments', snapshot=self.second, cookie=self.cookie, body=payload)[0], 409)
        for snapshot in (self.second, self.other):
            self.assertTrue(all(row['verdict'] is None for row in self.request('/api/catalog', snapshot=snapshot, cookie=self.cookie)[2]['cards']))
            card_id = self.payload(snapshot)['card_id']
            self.assertEqual(self.request('/api/history?' + urlencode({'id': card_id}), snapshot=snapshot, cookie=self.cookie)[2]['history'], [])
            self.assertEqual(self.request('/api/export?mode=history', snapshot=snapshot, cookie=self.cookie)[2]['judgments'], [])
            self.assertEqual(self.request('/api/admin/summary', snapshot=snapshot, cookie=self.admin)[2]['history_count'], 0)
        self.assertEqual(self.request('/api/admin/summary', snapshot=self.first, cookie=self.admin)[2]['history_count'], 1)
        self.assertEqual(self.request('/api/admin/summary', snapshot=self.first, cookie=self.cookie)[0], 403)
        self.assertEqual(self.request('/healthz')[0], 200)
        self.assertEqual(self.request('/api/catalog?dataset=unknown', cookie=self.cookie)[0], 404)

    def test_scoped_admin_permission_does_not_grant_other_versions_or_repositories(self):
        viewer = self.request('/api/auth/me', cookie=self.cookie)[2]['user_id']
        with closing(connect(self.db)) as db:
            db.execute('INSERT INTO dataset_admins VALUES (?, ?)', (dataset_id(self.first), viewer))
        self.assertTrue(self.request('/api/auth/me', snapshot=self.first, cookie=self.cookie)[2]['is_admin'])
        self.assertEqual(self.request('/api/admin/summary', snapshot=self.first, cookie=self.cookie)[0], 200)
        self.assertFalse(self.request('/api/auth/me', snapshot=self.first, cookie=self.cookie)[2]['can_view_users'])
        self.assertEqual(self.request('/api/admin/users', snapshot=self.first, cookie=self.cookie)[0], 403)
        self.assertEqual(self.request('/api/admin/user?reviewer=' + viewer, snapshot=self.first, cookie=self.cookie)[0], 403)
        for snapshot in (self.second, self.other):
            self.assertFalse(self.request('/api/auth/me', snapshot=snapshot, cookie=self.cookie)[2]['is_admin'])
            self.assertEqual(self.request('/api/admin/summary', snapshot=snapshot, cookie=self.cookie)[0], 403)


if __name__ == '__main__':
    unittest.main()
