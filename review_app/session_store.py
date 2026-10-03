"""Shared opaque reviewer sessions for name, email and preview authentication."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Callable

from .database import connect, initialize

SESSION_LIFETIME = 12 * 3600
SESSION_CACHE_LIMIT = 4096


class SessionStore:
    session_lifetime = SESSION_LIFETIME

    def __init__(self, db_path: Path, *, clock: Callable[[], float] = time.time):
        initialize(db_path)
        self.db_path = db_path
        self.clock = clock
        self.lock = threading.Lock()
        self.sessions: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self.pepper = self._load_pepper(db_path.parent / "auth-pepper")

    @staticmethod
    def _load_pepper(path: Path) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(secrets.token_bytes(32))
        value = path.read_bytes()
        if len(value) != 32:
            raise ValueError("auth-pepper must contain exactly 32 bytes")
        return value

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _insert_session(self, db: sqlite3.Connection, reviewer: str, now: int) -> str:
        """Issue within the caller's transaction; cache only after it commits."""
        token = secrets.token_urlsafe(32)
        db.execute("""INSERT INTO login_sessions
            (token_digest, reviewer, created_at, expires_at) VALUES (?, ?, ?, ?)""",
            (self._digest(token), reviewer, now, now + self.session_lifetime))
        return token

    def _remember_session(self, token: str, reviewer: str, now: int) -> None:
        self._cache_session(self._digest(token), reviewer, now + self.session_lifetime)

    def _cache_session(self, digest: str, reviewer: str, expires_at: int) -> None:
        """Caller holds self.lock. This server has exactly one serving process."""
        self.sessions[digest] = (reviewer, expires_at)
        self.sessions.move_to_end(digest)
        if len(self.sessions) > SESSION_CACHE_LIMIT:
            self.sessions.popitem(last=False)

    def _session_allowed(self, reviewer: str) -> bool:
        return True

    def session_reviewer(self, token: str | None) -> str | None:
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        digest = self._digest(token)
        now = int(self.clock())
        with self.lock:
            cached = self.sessions.get(digest)
            if cached:
                reviewer, expires_at = cached
                if expires_at > now and self._session_allowed(reviewer):
                    self.sessions.move_to_end(digest)
                    return reviewer
                self.sessions.pop(digest, None)
                return None
            with closing(self._connect()) as db:
                row = db.execute("""SELECT reviewer, expires_at FROM login_sessions
                    WHERE token_digest=? AND expires_at>?""", (digest, now)).fetchone()
            if row and self._session_allowed(row["reviewer"]):
                self._cache_session(digest, row["reviewer"], row["expires_at"])
                return row["reviewer"]
            return None

    def session_email(self, token: str | None) -> str | None:
        """Historical adapter: owners may be email addresses or opaque IDs."""
        return self.session_reviewer(token)

    def logout(self, token: str | None) -> None:
        if isinstance(token, str) and token and len(token) <= 128:
            digest = self._digest(token)
            with self.lock:
                with closing(self._connect()) as db:
                    db.execute("DELETE FROM login_sessions WHERE token_digest=?", (digest,))
                self.sessions.pop(digest, None)
