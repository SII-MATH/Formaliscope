"""Read-only global account views, independent identities and version validity."""
from contextlib import closing
import json
import unittest

from .admin import user_directory, user_reviews
from .database import connect
from .judgments import submit, save_draft
from .name_auth import NameAuthStore
from .repositories import dataset_id, make_collection
from .test_repositories import RepositoryFixture
from .dataset_storage import preserve_legacy_records
from .reuse import reuse_snapshot, inherit_judgments
from .statements import compile_statements


class AdminReadModelTests(RepositoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.auth = NameAuthStore(self.db)
        token, _ = self.auth.create_identity('管理员', admin=True)
        self.recovery = 'KIP-' + 'A'*43
        self.admin = self.auth.session_reviewer(token)
        token, _ = self.auth.create_identity('同名')
        self.first_user = self.auth.session_reviewer(token)
        token, _ = self.auth.create_identity('同名2')
        self.second_user = self.auth.session_reviewer(token)
        token, _ = self.auth.create_identity(self.recovery)
        self.idle = self.auth.session_reviewer(token)
        self.collection = make_collection([self.first, self.second, self.other], default=dataset_id(self.second))

    def record(self, source, reviewer, **changes):
        code, result = submit(source, self.db, reviewer, self.payload(source, **changes))
        self.assertEqual(code, 201, result)
        return result['judgment']

    def test_all_accounts_current_versions_and_read_only_secret_free_views(self):
        self.record(self.first, self.first_user)
        self.record(self.second, self.first_user)
        self.record(self.second, self.first_user, verdict='misaligned', rationale='new opinion')
        self.record(self.other, self.first_user, verdict='uncertain')
        stale = self.record(self.second, self.second_user)
        save_draft(self.second, self.db, self.first_user,
                   self.payload(self.second, verdict='', rationale='PRIVATE DRAFT', revision=0))
        with closing(connect(self.db)) as db:
            db.execute('UPDATE name_identities SET disabled=1 WHERE reviewer=?', (self.second_user,))
            db.execute('UPDATE judgments SET fingerprint=?, review_basis_fingerprint=? WHERE id=?',
                       ('f' * 64, 'f' * 64, stale['id']))
            before = list(db.iterdump())
        directory = user_directory(self.collection, self.db)
        self.assertEqual(directory['stats'], {'registered': 4, 'enabled': 3, 'admins': 1, 'reviewers': 2})
        users = {item['reviewer']: item for item in directory['users']}
        self.assertEqual(users[self.first_user]['current_count'], 2)
        self.assertEqual(users[self.first_user]['history_count'], 4)
        self.assertEqual(users[self.first_user]['verdicts']['misaligned'], 1)
        self.assertEqual(users[self.first_user]['verdicts']['uncertain'], 1)
        self.assertEqual(users[self.second_user]['current_count'], 0)
        self.assertEqual(users[self.idle]['history_count'], 0)
        detail = user_reviews(self.second, self.db, self.first_user)
        self.assertEqual([row['status'] for row in detail['reviews']], ['current', 'superseded', 'historical'])
        encoded = json.dumps([directory, detail])
        for secret in (self.recovery, 'PRIVATE DRAFT', 'recovery_digest', 'token_digest', 'auth-pepper'):
            self.assertNotIn(secret, encoded)
        with closing(connect(self.db)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_paginated_details_include_older_versions_but_not_other_users_or_repositories(self):
        self.record(self.first, self.first_user, rationale='old version')
        self.record(self.other, self.first_user, rationale='other repository')
        first = self.record(self.second, self.first_user, rationale='first opinion')
        second = self.record(self.second, self.first_user, rationale='second opinion')
        self.record(self.second, self.second_user, rationale='another person')
        page = user_reviews(self.second, self.db, self.first_user, limit=1)
        self.assertEqual([row['id'] for row in page['reviews']], [second['id']])
        self.assertEqual(page['next_cursor'], 1)
        next_page = user_reviews(self.second, self.db, self.first_user, cursor=1, limit=1)
        self.assertEqual([row['id'] for row in next_page['reviews']], [first['id']])
        self.assertEqual(next_page['next_cursor'], 2)
        last_page = user_reviews(self.second, self.db, self.first_user, cursor=2, limit=1, installed=self.collection)
        self.assertEqual(last_page['history_count'], 3)
        self.assertEqual(last_page['reviews'][0]['status'], 'historical')
        self.assertEqual(last_page['reviews'][0]['rationale'], 'old version')
        self.assertEqual(last_page['reviews'][0]['source_dataset'], dataset_id(self.first))
        self.assertIsNone(last_page['next_cursor'])
        self.assertEqual(user_reviews(self.second, self.db, self.idle)['reviews'], [])
        with self.assertRaises(LookupError):
            user_reviews(self.second, self.db, 'missing')
        for cursor, limit in ((-1, 25), (0, 0), (0, 101)):
            with self.assertRaises(ValueError):
                user_reviews(self.second, self.db, self.first_user, cursor=cursor, limit=limit)

    def test_history_counts_user_saves_once_across_legacy_migration_and_inheritance(self):
        config = {**self.config, 'id': 'kip126'}
        old = compile_statements(self.source, source_commit='a'*40, repository=config)
        new = compile_statements(self.source, source_commit='b'*40, repository=config)
        first = self.record(old, self.first_user, rationale='first save')
        second = self.record(old, self.first_user, rationale='second save')
        with closing(connect(self.db)) as db:
            for record in (first, second):
                db.execute("UPDATE judgments SET dataset_id='', card_id=REPLACE(card_id, 'statement::kip126::', 'statement::') WHERE id=?", (record['id'],))
        self.assertEqual(preserve_legacy_records(self.db, None, old), 2)
        candidate = reuse_snapshot(new, old)
        self.assertEqual(inherit_judgments(self.db, [(old, candidate)]), 1)
        self.assertEqual(inherit_judgments(self.db, [(old, candidate)]), 0)
        collection = make_collection([old, candidate], default=dataset_id(candidate))
        with closing(connect(self.db)) as db:
            before = list(db.iterdump())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM judgments').fetchone()[0], 5)
        user = next(u for u in user_directory(collection, self.db)['users'] if u['reviewer']==self.first_user)
        self.assertEqual((user['current_count'], user['history_count']), (1, 2))
        detail = user_reviews(candidate, self.db, self.first_user, installed=collection)
        self.assertEqual((detail['current_count'], detail['history_count']), (1, 2))
        self.assertEqual([r['status'] for r in detail['reviews']], ['current', 'historical'])
        with closing(connect(self.db)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_independent_identical_saves_and_invalid_copy_links_remain_separate(self):
        first = self.record(self.second, self.first_user, rationale='same opinion')
        second = self.record(self.second, self.first_user, rationale='same opinion')
        other = self.record(self.second, self.second_user, rationale='same opinion')
        with closing(connect(self.db)) as db:
            db.execute('UPDATE judgments SET inherited_from_id=? WHERE id=?', (first['id'], other['id']))
            db.execute('UPDATE judgments SET inherited_from_id=? WHERE id=?', ('missing-record', second['id']))
        users = {u['reviewer']:u for u in user_directory(self.collection, self.db)['users']}
        self.assertEqual(users[self.first_user]['history_count'], 2)
        self.assertEqual(users[self.second_user]['history_count'], 1)

    def test_legacy_single_snapshot_reviews_remain_visible(self):
        (self.source / 'KIP126').mkdir()
        (self.source / 'KIP126/X.lean').write_text('def legacy : Nat := 1\n')
        legacy = compile_statements(self.source, source_commit='a'*40)
        card = legacy['cards'][0]
        result, _ = submit(legacy, self.db, self.first_user,
                           {'card_id':card['id'], 'fingerprint':card['fingerprint'],
                            'request_id':self.payload(self.first)['request_id'], 'verdict':'aligned', 'rationale':'legacy save'})
        self.assertEqual(result, 201)
        detail = user_reviews(legacy, self.db, self.first_user)
        self.assertEqual((detail['current_count'], detail['history_count']), (1, 1))
        self.assertEqual(detail['reviews'][0]['status'], 'current')


if __name__ == '__main__':
    unittest.main()
