"""Name registration with opaque identities and private recovery credentials."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import uuid
from contextlib import closing
from typing import Mapping
from urllib.parse import urlsplit

from .auth import AuthSettings
from .session_store import SessionStore

NAME_SESSION_LIFETIME = 30 * 86400


class RateLimited(Exception):
    pass


def valid_name(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value.strip()) <= 60
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def valid_reviewer_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"u_[0-9a-f]{32}", value))


def name_settings(env: Mapping[str, str] | None = None) -> AuthSettings:
    env = os.environ if env is None else env
    text = env.get('REVIEW_PUBLIC_ORIGIN', '').strip().rstrip('/')
    try:
        origin = urlsplit(text)
    except ValueError:
        raise ValueError('invalid REVIEW_PUBLIC_ORIGIN') from None
    try:
        local = origin.hostname == 'localhost' or ipaddress.ip_address(origin.hostname or '').is_loopback
    except ValueError:
        local = False
    if (not origin.hostname or origin.username or origin.password or origin.path or origin.query
            or origin.fragment or origin.netloc.endswith(':')
            or text != f'{origin.scheme}://{origin.netloc}'
            or not (origin.scheme == 'https' or (origin.scheme == 'http' and local))):
        raise ValueError('set REVIEW_PUBLIC_ORIGIN to an HTTPS origin (HTTP is only allowed for loopback development)')
    try:
        if origin.port is not None and not 1 <= origin.port <= 65535:
            raise ValueError('invalid origin port')
    except ValueError:
        raise ValueError('invalid origin port') from None
    cookie = env.get('REVIEW_COOKIE_PATH', '/').strip()
    if not re.fullmatch(r'/(?:[^\s;?#\\]*\/)?', cookie):
        raise ValueError('REVIEW_COOKIE_PATH must be / or an absolute prefix ending in /')
    return AuthSettings(mailer='none', allow_any_email=True, public_origin=text, cookie_path=cookie)


class NameAuthStore(SessionStore):
    session_lifetime = NAME_SESSION_LIFETIME

    def __init__(self, db_path, settings=None, **kwargs):
        super().__init__(db_path, **kwargs)
        self.settings = settings or AuthSettings(mailer='none', allow_any_email=True)

    @staticmethod
    def _recovery():
        return 'KIP-' + secrets.token_urlsafe(32)

    def _limit(self, action, client_ip):
        now = int(self.clock())
        digest = hmac.new(self.pepper, client_ip.encode(), hashlib.sha256).hexdigest()
        window, maximum, global_maximum = (3600, 120, 30) if action == 'register' else (600, 60, 120)
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM identity_requests WHERE created_at < ?', (now - 3600,))
            count = db.execute('SELECT COUNT(*) FROM identity_requests WHERE action=? AND ip_digest=? AND created_at>?',
                               (action, digest, now-window)).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM identity_requests WHERE action=? AND created_at>?',
                               (action, now-60)).fetchone()[0]
            if count >= maximum or total >= global_maximum:
                db.execute('COMMIT')
                raise RateLimited
            db.execute('INSERT INTO identity_requests VALUES (?, ?, ?)', (action, digest, now))
            db.execute('COMMIT')

    def create_reviewer(self, name, client_ip='127.0.0.1'):
        if not valid_name(name):
            raise ValueError('请输入 1–60 字的姓名，不含控制字符')
        self._limit('register', client_ip)
        return self.create_identity(name)

    def create_identity(self, name, *, admin=False, existing_reviewer=None):
        """Called with admin=True only by an operator's local CLI, never HTTP."""
        if not valid_name(name):
            raise ValueError('请输入 1–60 字的姓名，不含控制字符')
        reviewer = existing_reviewer or 'u_' + uuid.uuid4().hex
        recovery = self._recovery()
        now = int(self.clock())
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if existing_reviewer and not (
                    db.execute('SELECT 1 FROM reviewer_profiles WHERE reviewer=?', (reviewer,)).fetchone()
                    or db.execute('SELECT 1 FROM judgments WHERE reviewer=? LIMIT 1', (reviewer,)).fetchone()
                ):
                    raise ValueError('existing reviewer has no profile or review records')
                if db.execute('SELECT 1 FROM name_identities WHERE reviewer=?', (reviewer,)).fetchone():
                    raise ValueError('identity already has a recovery credential')
                db.execute('INSERT INTO name_identities VALUES (?, ?, ?, 0)',
                           (reviewer, self._digest(recovery), int(admin)))
                db.execute('INSERT INTO reviewer_profiles (reviewer, display_name) VALUES (?, ?) '
                           'ON CONFLICT(reviewer) DO UPDATE SET display_name=excluded.display_name',
                           (reviewer, name.strip()))
                token = self._insert_session(db, reviewer, now)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._remember_session(token, reviewer, now)
        return token, recovery

    def resume_reviewer(self, recovery, client_ip='127.0.0.1'):
        self._limit('recover', client_ip)
        if not isinstance(recovery, str):
            return None
        recovery = recovery.strip()
        if not re.fullmatch(r'KIP-[A-Za-z0-9_-]{43}', recovery):
            return None
        now = int(self.clock())
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT reviewer FROM name_identities WHERE recovery_digest=? AND disabled=0',
                             (self._digest(recovery),)).fetchone()
            if not row:
                db.execute('COMMIT')
                return None
            db.execute('DELETE FROM login_sessions WHERE expires_at<=?', (now,))
            token = self._insert_session(db, row['reviewer'], now)
            db.execute('COMMIT')
            self._remember_session(token, row['reviewer'], now)
        return token

    def _session_allowed(self, reviewer):
        with closing(self._connect()) as db:
            return bool(db.execute('SELECT 1 FROM name_identities WHERE reviewer=? AND disabled=0',
                                   (reviewer,)).fetchone())

    def is_admin(self, reviewer):
        with closing(self._connect()) as db:
            return bool(db.execute('SELECT 1 FROM name_identities WHERE reviewer=? AND is_admin=1 AND disabled=0',
                                   (reviewer,)).fetchone())

    def rotate_recovery(self, reviewer, keep_token):
        recovery, keep_digest = self._recovery(), self._digest(keep_token)
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE name_identities SET recovery_digest=? WHERE reviewer=? AND disabled=0',
                                 (self._digest(recovery), reviewer)).rowcount
            if not changed:
                db.execute('ROLLBACK')
                raise ValueError('身份不可用')
            db.execute('DELETE FROM login_sessions WHERE reviewer=? AND token_digest<>?', (reviewer, keep_digest))
            db.execute('COMMIT')
            for digest, (owner, _) in list(self.sessions.items()):
                if owner == reviewer and digest != keep_digest:
                    self.sessions.pop(digest, None)
        return recovery
