"""Explicit loopback-only preview identities; never used by production serve."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from contextlib import closing

from .auth import AuthStore, AuthSettings, SESSION_LIFETIME


class PreviewAuthStore(AuthStore):
    def __init__(self, db_path):
        super().__init__(db_path, AuthSettings(allow_any_email=True))
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS preview_identities (key_digest TEXT PRIMARY KEY, email TEXT NOT NULL)')

    def create_reviewer(self, name: str) -> str:
        # Names are display labels. A random identity keeps homonyms separate.
        email = str(uuid.uuid4()) + '@preview.local'
        token = secrets.token_urlsafe(32)
        resume_key = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = int(self.clock())
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                first = not db.execute('SELECT 1 FROM reviewer_profiles WHERE preview_admin=1').fetchone()
                db.execute('INSERT INTO reviewer_profiles VALUES (?, ?, ?)', (email, name, int(first)))
                db.execute('INSERT INTO preview_identities VALUES (?, ?)', (hashlib.sha256(resume_key.encode()).hexdigest(), email))
                db.execute('INSERT INTO login_sessions VALUES (?, ?, ?, ?)',
                           (digest, email, now, now+SESSION_LIFETIME))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._cache_session(digest, email, now+SESSION_LIFETIME)
        return token, resume_key

    def resume_reviewer(self, key):
        if not isinstance(key, str) or not 30 <= len(key) <= 128:
            return None
        with self.lock, closing(self._connect()) as db:
            row = db.execute('SELECT email FROM preview_identities WHERE key_digest=?', (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            now = int(self.clock())
            db.execute('INSERT INTO login_sessions VALUES (?, ?, ?, ?)', (digest, row['email'], now, now+SESSION_LIFETIME))
            self._cache_session(digest, row['email'], now+SESSION_LIFETIME)
            return token
