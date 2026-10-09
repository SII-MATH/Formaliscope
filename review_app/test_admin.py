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
        self.assertEqual([row['status'] for row in detail['reviews']], ['current', 'superseded'])
        encoded = json.dumps([directory, detail])
        for secret in (self.recovery, 'PRIVATE DRAFT', 'recovery_digest', 'token_digest', 'auth-pepper'):
            self.assertNotIn(secret, encoded)
        with closing(connect(self.db)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_paginated_details_do_not_cross_accounts_or_versions(self):
        self.record(self.first, self.first_user, rationale='old version')
        first = self.record(self.second, self.first_user, rationale='first opinion')
        second = self.record(self.second, self.first_user, rationale='second opinion')
        self.record(self.second, self.second_user, rationale='another person')
        page = user_reviews(self.second, self.db, self.first_user, limit=1)
        self.assertEqual([row['id'] for row in page['reviews']], [second['id']])
        self.assertEqual(page['next_cursor'], 1)
        next_page = user_reviews(self.second, self.db, self.first_user, cursor=1, limit=1)
        self.assertEqual([row['id'] for row in next_page['reviews']], [first['id']])
        self.assertIsNone(next_page['next_cursor'])
        self.assertEqual(user_reviews(self.second, self.db, self.idle)['reviews'], [])
        with self.assertRaises(LookupError):
            user_reviews(self.second, self.db, 'missing')
        for cursor, limit in ((-1, 25), (0, 0), (0, 101)):
            with self.assertRaises(ValueError):
                user_reviews(self.second, self.db, self.first_user, cursor=cursor, limit=limit)


if __name__ == '__main__':
    unittest.main()
