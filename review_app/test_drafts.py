"""Draft checkpoints, completion history, paging and backup preservation."""
from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from .database import MIGRATIONS, initialize
from .judgments import (admin_summary, catalog, history, history_page, review_state,
                        reviewer_export, save_draft, submit)
from .statements import compile_statements
from .storage import create_backup


class ReviewDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / 'source/KIPBase'
        source.mkdir(parents=True)
        (source / 'Sample.lean').write_text('def X : Nat := 1\ndef Y : Nat := 2\n')
        self.snapshot = compile_statements(source.parent, source_commit='a' * 40)
        self.card = self.snapshot['cards'][0]
        self.db = self.root / 'data/judgments.sqlite3'
        initialize(self.db)
        self.reviewer = 'u_' + '1' * 32

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, rationale='', verdict='aligned', **extra):
        return {'card_id': self.card['id'], 'fingerprint': self.card['fingerprint'],
                'request_id': str(uuid.uuid4()), 'verdict': verdict, 'rationale': rationale, **extra}

    def state(self, reviewer=None, snapshot=None):
        return review_state(snapshot or self.snapshot, self.db, self.card['id'], reviewer or self.reviewer)

    def save(self, rationale='', verdict='aligned'):
        payload = self.payload(rationale, verdict, revision=self.state()['draft_revision'])
        code, result = save_draft(self.snapshot, self.db, self.reviewer, payload)
        self.assertEqual(code, 200, result)
        return payload, result['draft']

    def complete(self, draft):
        payload = self.payload(draft['rationale'], draft['verdict'], draft_revision=draft['revision'])
        code, result = submit(self.snapshot, self.db, self.reviewer, payload)
        self.assertIn(code, (200, 201), result)
        return payload, result['judgment']

    def test_typing_replaces_one_draft_and_completion_adds_one_version(self):
        for i in range(10):
            _, draft = self.save('完整意见'[:1 + i])
        self.assertEqual(history(self.db, self.card['id'], self.reviewer), [])
        self.assertEqual(catalog(self.snapshot, self.db, self.reviewer)['cards'][0]['verdict'], None)
        self.assertEqual(admin_summary(self.snapshot, self.db)['history_count'], 0)
        self.assertEqual(reviewer_export(self.snapshot, self.db, self.reviewer)['judgments'], [])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_drafts').fetchone()[0], 1)
        payload, judgment = self.complete(draft)
        self.assertEqual(self.state()['current']['id'], judgment['id'])
        self.assertIsNone(self.state()['draft'])
        self.assertEqual(self.state()['draft_revision'], 10)
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, payload)[1]['judgment']['id'], judgment['id'])
        self.assertEqual(self.state()['history_count'], 1)
        _, draft = self.save('修订后的完整意见')
        self.complete(draft)
        self.assertEqual(self.state()['history_count'], 2)
        self.assertEqual(judgment['source_commit'], self.snapshot['source_commit'])
        self.assertEqual(judgment['snapshot_digest'], self.snapshot['digest'])

    def test_note_only_draft_is_recoverable_but_cannot_complete(self):
        _, draft = self.save('未选择结论也应保存', '')
        self.assertEqual(self.state()['draft']['rationale'], '未选择结论也应保存')
        payload = self.payload(draft['rationale'], '', draft_revision=draft['revision'])
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, payload)[0], 400)
        self.assertEqual(self.state()['history_count'], 0)
        _, draft = self.save('未选择结论也应保存', 'uncertain')
        self.complete(draft)
        self.assertIsNone(self.state()['draft'])

    def test_revision_conflicts_and_retries_never_overwrite_newer_input(self):
        first, draft = self.save('第一份')
        code, replay = save_draft(self.snapshot, self.db, self.reviewer, first)
        self.assertEqual((code, replay['replayed'], replay['draft']['revision']), (200, True, 1))
        self.assertEqual(save_draft(self.snapshot, self.db, self.reviewer, {**first, 'rationale': '篡改'})[0], 409)
        _, newer = self.save('第二份')
        self.assertEqual(save_draft(self.snapshot, self.db, self.reviewer, first)[0], 409)
        stale_completion = self.payload('第一份', draft_revision=draft['revision'])
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, stale_completion)[0], 409)
        self.assertEqual(self.state()['draft']['rationale'], '第二份')
        request, judgment = self.complete(newer)
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, request)[0], 200)
        _, latest = self.save('第三份')
        self.assertEqual(save_draft(self.snapshot, self.db, self.reviewer, first)[0], 409)
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, request)[1]['judgment']['id'], judgment['id'])
        self.assertEqual(self.state()['draft']['revision'], latest['revision'])
        self.assertEqual(self.state()['draft']['rationale'], '第三份')

    def test_unchanged_completion_and_its_retry_do_not_append_history(self):
        _, draft = self.save('完整意见')
        self.complete(draft)
        _, repeated = self.save('完整意见  ')
        payload, row = self.complete(repeated)
        self.assertEqual(self.state()['history_count'], 1)
        self.assertIsNone(self.state()['draft'])
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, payload)[1]['judgment']['id'], row['id'])
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, {**payload, 'rationale': '不同意见'})[0], 409)
        _, pending = self.save('完成后的新编辑')
        submit(self.snapshot, self.db, self.reviewer, payload)
        self.assertEqual(self.state()['draft']['revision'], pending['revision'])

    def test_identity_and_changed_evidence_are_isolated_and_exports_are_distinct(self):
        _, draft = self.save('完成第一版')
        _, first = self.complete(draft)
        _, draft = self.save('完成第二版', 'uncertain')
        _, second = self.complete(draft)
        self.save('尚未完成的改动', 'misaligned')
        self.assertIsNone(self.state('u_' + '2' * 32)['draft'])
        self.assertIsNone(self.state('u_' + '2' * 32)['current'])
        latest = reviewer_export(self.snapshot, self.db, self.reviewer, mode='latest')
        full = reviewer_export(self.snapshot, self.db, self.reviewer, mode='history')
        self.assertEqual([row['id'] for row in latest['judgments']], [second['id']])
        self.assertEqual([row['id'] for row in full['judgments']], [first['id'], second['id']])
        changed_card = {**self.card, 'fingerprint': 'd' * 64,
                        'fingerprints': {self.card['fingerprint_scheme']: 'd' * 64}}
        changed = {**self.snapshot, 'cards': [changed_card]}
        self.assertIsNone(self.state(snapshot=changed)['draft'])
        self.assertIsNone(self.state(snapshot=changed)['current'])
        self.assertEqual(reviewer_export(changed, self.db, self.reviewer, mode='latest')['judgments'], [])
        self.assertEqual(len(reviewer_export(changed, self.db, self.reviewer)['judgments']), 2)
        self.assertEqual(save_draft(changed, self.db, self.reviewer, self.payload('旧内容', revision=3))[0], 409)
        with self.assertRaises(ValueError):
            reviewer_export(self.snapshot, self.db, self.reviewer, mode='other')

    def test_keyset_paging_is_complete_with_equal_timestamps_and_concurrent_insert(self):
        for i in range(57):
            code, _ = submit(self.snapshot, self.db, self.reviewer, self.payload(str(i)))
            self.assertEqual(code, 201)
        # Tie every timestamp to exercise the deterministic rowid tiebreaker.
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE judgments SET created_at=?', ('2026-10-04T00:00:00Z',))
        expected = [row['id'] for row in history(self.db, self.card['id'], self.reviewer)]
        first = history_page(self.db, self.card['id'], self.reviewer)
        self.assertEqual(len(first['history']), 25)
        self.assertEqual(history_page(self.db, self.card['id'], 'u_' + '2' * 32)['history'], [])
        submit(self.snapshot, self.db, self.reviewer, self.payload('分页期间新记录'))
        found = [row['id'] for row in first['history']]
        cursor = first['next_cursor']
        while cursor:
            page = history_page(self.db, self.card['id'], self.reviewer, cursor=cursor)
            found.extend(row['id'] for row in page['history'])
            cursor = page['next_cursor']
        self.assertEqual(found, expected)
        for limit in (0, 101):
            with self.assertRaises(ValueError):
                history_page(self.db, self.card['id'], self.reviewer, limit=limit)
        for cursor in ('invalid!', base64.urlsafe_b64encode(b'[null, true]').decode()):
            with self.assertRaises(ValueError):
                history_page(self.db, self.card['id'], self.reviewer, cursor=cursor)

    def test_schema7_upgrade_keeps_legacy_history_and_backup_keeps_pending_draft(self):
        old_path = self.root / 'old/judgments.sqlite3'
        old_path.parent.mkdir()
        with sqlite3.connect(old_path) as db:
            db.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)')
            for version, name, migration in MIGRATIONS[:7]:
                migration(db)
                db.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (version, name, 'old'))
        # Seed a genuine schema-7 record; the current writer requires schema 10.
        from .judgments import _record
        record = _record(self.snapshot, self.card, self.reviewer, self.payload('旧历史'))
        record.pop('dataset_id')
        with sqlite3.connect(old_path) as db:
            columns = ','.join(record)
            db.execute(f"INSERT INTO judgments ({columns}) VALUES ({','.join('?' for _ in record)})", list(record.values()))
        initialize(old_path)
        restored = history(old_path, self.card['id'], self.reviewer)[0]
        self.assertEqual(restored, {key: value for key, value in record.items() if key not in {'card_id', 'request_id'}})
        initialize(old_path)
        self.assertEqual(history(old_path, self.card['id'], self.reviewer)[0]['id'], record['id'])
        self.save('等待继续编辑', '')
        (self.db.parent / 'snapshot.json').write_text(json.dumps(self.snapshot))
        backup = create_backup(self.db.parent, self.root / 'backups')
        restored = review_state(self.snapshot, backup / 'judgments.sqlite3', self.card['id'], self.reviewer)
        self.assertEqual(restored['draft']['rationale'], '等待继续编辑')
        self.assertEqual(restored['history_count'], 0)


if __name__ == '__main__':
    unittest.main()
