"""Confirmed duplicate merges retain all judgments and conflicting draft originals."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
import uuid

from .account_merge import merge_accounts
from .database import connect
from .name_auth import NameAuthStore
from .judgments import submit, save_draft, update_reviewer_profile
from .repositories import dataset_id
from .test_repositories import RepositoryFixture


class AccountMergeTests(RepositoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.auth = NameAuthStore(self.db)
        self.target_token, self.target = self.auth.create_identity('本人', password='target-password')
        self.source_token, self.source = self.auth.create_identity('本人旧', admin=True, password='source-password')
        # Reproduce an existing, unmerged legacy duplicate group. Production
        # registration cannot create this state anymore.
        with closing(connect(self.db)) as db:
            db.execute('UPDATE name_identities SET normalized_name=NULL WHERE reviewer IN (?, ?)', (self.source, self.target))
            db.execute('UPDATE reviewer_profiles SET display_name=? WHERE reviewer=?', ('本人', self.source))

    def test_judgments_drafts_roles_aliases_sessions_and_idempotent_merge(self):
        request = str(uuid.uuid4())
        for owner, verdict, reason in ((self.target, 'aligned', '较早意见'), (self.source, 'misaligned', '较新意见')):
            self.assertEqual(submit(self.second, self.db, owner, self.payload(self.second,
                request_id=request, verdict=verdict, rationale=reason))[0], 201)
            self.assertEqual(save_draft(self.second, self.db, owner, self.payload(self.second,
                revision=0, verdict=verdict, rationale=reason+'草稿'))[0], 200)
        with closing(connect(self.db)) as db:
            db.execute('UPDATE review_drafts SET created_at=? WHERE reviewer=?', ('2026-10-01T00:00:00+00:00', self.target))
            db.execute('UPDATE review_drafts SET created_at=? WHERE reviewer=?', ('2026-10-02T00:00:00+00:00', self.source))
            db.execute('INSERT INTO dataset_admins VALUES (?, ?)', (dataset_id(self.other), self.source))
            original = {row['id']: dict(row) for row in db.execute('SELECT * FROM judgments')}
            old_drafts = {row['id']: dict(row) for row in db.execute('SELECT * FROM review_drafts')}
        result = merge_accounts(self.db.parent, self.source, self.target)
        self.assertEqual(result['judgments_moved'], 1)
        restarted = NameAuthStore(self.db)
        self.assertIsNone(restarted.session_reviewer(self.target_token))
        self.assertIsNone(restarted.session_reviewer(self.source_token))
        self.assertTrue(restarted.is_admin(self.target))
        self.assertEqual(restarted.session_reviewer(restarted.login_password(self.source, '12345678')), self.target)
        self.assertEqual(restarted.session_reviewer(restarted.login_password('本人', '12345678')), self.target)
        self.assertIsNone(restarted.login_password(self.target, 'target-password'))
        self.assertTrue(restarted.must_change_password(self.target))
        with closing(connect(self.db)) as db:
            current = {row['id']: dict(row) for row in db.execute('SELECT * FROM judgments')}
            self.assertEqual(set(current), set(original))
            for key, old in original.items():
                self.assertEqual(current[key]['reviewer'], self.target)
                for field in old.keys() - {'reviewer', 'request_id', 'original_reviewer'}:
                    self.assertEqual(current[key][field], old[field], field)
            source_record = next(row for row in current.values() if row['original_reviewer'])
            self.assertEqual(source_record['original_reviewer'], self.source)
            self.assertNotEqual(source_record['request_id'], request)
            self.assertEqual(db.execute('SELECT rationale FROM review_drafts').fetchone()[0], '较新意见草稿')
            archive = json.loads(db.execute('SELECT archive_json FROM account_merges').fetchone()[0])
            self.assertEqual({row['id'] for row in archive['drafts']}, set(old_drafts))
            self.assertEqual(db.execute('SELECT reviewer FROM dataset_admins').fetchone()[0], self.target)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM name_identities').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            before = list(db.iterdump())
        self.assertTrue(merge_accounts(self.db.parent, self.source, self.target)['already_merged'])
        with closing(connect(self.db)) as db:
            self.assertEqual(list(db.iterdump()), before)
        with self.assertRaises(ValueError):
            restarted.create_reviewer(' 本人 ')

    def test_different_names_and_invalid_targets_do_not_merge(self):
        _, other = self.auth.create_identity('其他人')
        with closing(connect(self.db)) as db:
            before = list(db.iterdump())
        for source, target in ((self.source, other), (self.source, 'missing'), (self.target, self.target)):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                merge_accounts(self.db.parent, source, target)
        with closing(connect(self.db)) as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_registration_normalization_concurrency_and_profile_collisions(self):
        def register(_):
            try:
                return self.auth.create_identity(' Ａlice ')
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(register, range(4)))
        self.assertEqual(sum(outcome is not None for outcome in outcomes), 1)
        with self.assertRaises(ValueError):
            self.auth.create_identity('alice')
        owner = next(outcome[1] for outcome in outcomes if outcome)
        self.assertEqual(update_reviewer_profile(self.db, owner, '本人')[0], 409)
        self.assertEqual(update_reviewer_profile(self.db, owner, '新姓名')[0], 200)


if __name__ == '__main__':
    unittest.main()
