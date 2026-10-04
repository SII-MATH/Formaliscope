"""Preview identities use persistent shared sessions without name collisions."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from .preview import PreviewAuthStore
from .session_store import SESSION_LIFETIME


class PreviewIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'data' / 'reviews.sqlite3'
        self.now = 1_800_000_000
        self.auth = PreviewAuthStore(self.db, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_resume_logout_and_roles_preserve_opaque_owner(self):
        token, key = self.auth.create_reviewer('同名')
        second, _ = self.auth.create_reviewer('同名')
        owner = self.auth.session_reviewer(token)
        other = self.auth.session_reviewer(second)
        self.assertNotEqual(owner, other)
        restarted = PreviewAuthStore(self.db, clock=lambda: self.now)
        self.assertEqual(restarted.session_email(token), owner)
        restarted.logout(token)
        self.assertIsNone(restarted.session_reviewer(token))
        resumed = restarted.resume_reviewer(key)
        self.assertEqual(restarted.session_reviewer(resumed), owner)
        self.assertEqual(restarted.session_email(resumed), owner)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(dict(db.execute('SELECT reviewer, preview_admin FROM reviewer_profiles')),
                             {owner: 1, other: 0})
            dump = '\n'.join(db.iterdump())
        for secret in (token, key, resumed):
            self.assertNotIn(secret, dump)

    def test_expired_cached_and_persisted_sessions_can_resume(self):
        token, key = self.auth.create_reviewer('用户')
        owner = self.auth.session_reviewer(token)
        self.now += SESSION_LIFETIME
        self.assertIsNone(self.auth.session_reviewer(token))
        restarted = PreviewAuthStore(self.db, clock=lambda: self.now)
        self.assertIsNone(restarted.session_reviewer(token))
        self.assertEqual(restarted.session_reviewer(restarted.resume_reviewer(key)), owner)
        self.assertIsNone(restarted.resume_reviewer('bad-key'))
        for invalid in (None, 123, '', 'x' * 129):
            self.assertIsNone(restarted.session_reviewer(invalid))
            restarted.logout(invalid)


if __name__ == '__main__':
    unittest.main()
