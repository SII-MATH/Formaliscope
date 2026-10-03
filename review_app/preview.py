"""Explicit loopback-only preview identities; never used by production serve."""
from __future__ import annotations

import secrets
import uuid
from contextlib import closing

from .auth import AuthSettings
from .session_store import SessionStore


class PreviewAuthStore(SessionStore):
    def __init__(self, db_path, **kwargs):
        super().__init__(db_path, **kwargs)
        self.settings = AuthSettings(allow_any_email=True)

    def create_reviewer(self, name: str) -> tuple[str, str]:
        # Names are display labels. A random identity keeps homonyms separate.
        reviewer = str(uuid.uuid4()) + '@preview.local'
        resume_key = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                first = not db.execute('SELECT 1 FROM reviewer_profiles WHERE preview_admin=1').fetchone()
                db.execute('INSERT INTO reviewer_profiles VALUES (?, ?, ?)', (reviewer, name, int(first)))
                db.execute('INSERT INTO preview_identities (key_digest, reviewer) VALUES (?, ?)',
                           (self._digest(resume_key), reviewer))
                token = self._insert_session(db, reviewer, now)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._remember_session(token, reviewer, now)
        return token, resume_key

    def resume_reviewer(self, key):
        if not isinstance(key, str) or not 30 <= len(key) <= 128:
            return None
        with self.lock, closing(self._connect()) as db:
            row = db.execute('SELECT reviewer FROM preview_identities WHERE key_digest=?', (self._digest(key),)).fetchone()
            if not row:
                return None
            now = int(self.clock())
            token = self._insert_session(db, row['reviewer'], now)
            self._remember_session(token, row['reviewer'], now)
            return token
