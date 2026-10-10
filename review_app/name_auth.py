"""Unique name accounts with password credentials and opaque reviewer identities."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import uuid
from contextlib import closing
from typing import Mapping
from urllib.parse import urlsplit

from .auth import AuthSettings
from .session_store import SessionStore
from .passwords import INITIAL_PASSWORD, ITERATIONS, hash_password, valid_password, verify_password, name_key, name_taken

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

    def _limit(self, action, client_ip):
        now = int(self.clock())
        digest = hmac.new(self.pepper, client_ip.encode(), hashlib.sha256).hexdigest()
        window, maximum, global_maximum = (3600, 120, 30) if action == 'register' else (600, 60, 120)
        if action == 'password':
            window, maximum, global_maximum = 60, 20, 60
        elif action == 'password-account':
            window, maximum, global_maximum = 60, 10, 120
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

    def create_reviewer(self, name, client_ip='127.0.0.1', *, password=None):
        if not valid_name(name):
            raise ValueError('请输入 1–60 字的姓名，不含控制字符')
        self._limit('register', client_ip)
        return self.create_identity(name, password=password)

    def create_identity(self, name, *, admin=False, existing_reviewer=None, password=None):
        """Called with admin=True only by an operator's local CLI, never HTTP."""
        if not valid_name(name):
            raise ValueError('请输入 1–60 字的姓名，不含控制字符')
        if password is not None and password == INITIAL_PASSWORD:
            raise ValueError('请设置自己的密码，不要使用初始密码')
        password_hash = hash_password(INITIAL_PASSWORD if password is None else password)
        reviewer = existing_reviewer or 'u_' + uuid.uuid4().hex
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
                    raise ValueError('identity already has a password account')
                if name_taken(db, name) or valid_reviewer_id(name.strip()):
                    raise ValueError('该姓名已注册，请使用账号和密码登录；忘记密码请联系管理员')
                db.execute('INSERT INTO name_identities VALUES (?, ?, 0, ?)',
                           (reviewer, int(admin), name_key(name)))
                db.execute('INSERT INTO password_credentials VALUES (?, ?, ?)',
                           (reviewer, password_hash, int(password is None)))
                db.execute('INSERT INTO reviewer_profiles (reviewer, display_name) VALUES (?, ?) '
                           'ON CONFLICT(reviewer) DO UPDATE SET display_name=excluded.display_name',
                           (reviewer, name.strip()))
                token = self._insert_session(db, reviewer, now)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._remember_session(token, reviewer, now)
        return token, reviewer

    def must_change_password(self, reviewer):
        with closing(self._connect()) as db:
            row = db.execute('SELECT must_change FROM password_credentials WHERE reviewer=?', (reviewer,)).fetchone()
            return bool(row and row['must_change'])

    def login_password(self, account, password, client_ip='127.0.0.1'):
        self._limit('password', client_ip)
        if not isinstance(account, str) or not 1 <= len(account.strip()) <= 254 or not valid_password(password):
            return None
        account = account.strip()
        now = int(self.clock())
        with closing(self._connect()) as db:
            # IDs take precedence; names work only when they identify one account.
            rows = db.execute('''SELECT i.reviewer, p.password_hash FROM name_identities i
                JOIN password_credentials p ON p.reviewer=i.reviewer
                WHERE i.reviewer=? AND i.disabled=0''', (account,)).fetchall()
            if not rows:
                rows = db.execute('''SELECT i.reviewer, p.password_hash FROM account_merges a
                    JOIN name_identities i ON i.reviewer=a.target_reviewer
                    JOIN password_credentials p ON p.reviewer=i.reviewer
                    WHERE a.source_reviewer=? AND i.disabled=0''', (account,)).fetchall()
            if not rows:
                rows = db.execute('''SELECT i.reviewer, p.password_hash FROM name_identities i
                    JOIN password_credentials p ON p.reviewer=i.reviewer
                    WHERE i.normalized_name=? AND i.disabled=0''', (name_key(account),)).fetchall()
            if len(rows) != 1:
                # Do the same expensive work for unknown or ambiguous accounts.
                hashlib.pbkdf2_hmac('sha256', password.encode(), b'unknown-account!', ITERATIONS)
                return None
            row = rows[0]
            # Account limits count across IPs and name/ID aliases.
            # _limit uses its own connection before the credential transaction.
        self._limit('password-account', row['reviewer'])
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                fresh = db.execute('''SELECT p.password_hash FROM password_credentials p
                    JOIN name_identities i ON i.reviewer=p.reviewer
                    WHERE p.reviewer=? AND i.disabled=0''', (row['reviewer'],)).fetchone()
                if not fresh or not verify_password(password, fresh['password_hash']):
                    db.execute('COMMIT')
                    return None
                db.execute('DELETE FROM login_sessions WHERE expires_at<=?', (now,))
                token = self._insert_session(db, row['reviewer'], now)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._remember_session(token, row['reviewer'], now)
            return token

    def _revoke_password_sessions(self, db, reviewer, keep_digest=None):
        db.execute('DELETE FROM login_sessions WHERE reviewer=? AND token_digest<>?',
                   (reviewer, keep_digest or ''))
        # Cache eviction happens only after the enclosing transaction commits.

    def _forget_password_sessions(self, reviewer, keep_digest=None):
        for digest, (owner, _) in list(self.sessions.items()):
            if owner == reviewer and digest != keep_digest:
                self.sessions.pop(digest, None)

    def change_password(self, reviewer, current_password, new_password, keep_token, client_ip='127.0.0.1'):
        self._limit('password', client_ip)
        if new_password == INITIAL_PASSWORD or new_password == current_password:
            raise ValueError('请设置与当前密码和初始密码不同的新密码')
        encoded = hash_password(new_password)
        keep_digest = self._digest(keep_token)
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('''SELECT p.password_hash FROM password_credentials p
                    JOIN name_identities i ON i.reviewer=p.reviewer
                    JOIN login_sessions s ON s.reviewer=p.reviewer
                    WHERE p.reviewer=? AND i.disabled=0 AND s.token_digest=? AND s.expires_at>?''',
                    (reviewer, keep_digest, int(self.clock()))).fetchone()
                if not row or not verify_password(current_password, row['password_hash']):
                    raise ValueError('当前密码不正确或登录已过期')
                db.execute('UPDATE password_credentials SET password_hash=?, must_change=0 WHERE reviewer=?',
                           (encoded, reviewer))
                self._revoke_password_sessions(db, reviewer, keep_digest)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._forget_password_sessions(reviewer, keep_digest)

    def reset_password(self, actor, reviewer, actor_password, client_ip='127.0.0.1'):
        self._limit('password', client_ip)
        if not isinstance(reviewer, str) or not 1 <= len(reviewer) <= 254:
            raise ValueError('请选择一个有效账号')
        if reviewer == actor:
            raise ValueError('请通过修改密码功能修改自己的密码')
        encoded = hash_password(INITIAL_PASSWORD)
        with self.lock, closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                admin = db.execute('''SELECT p.password_hash, p.must_change FROM password_credentials p
                    JOIN name_identities i ON i.reviewer=p.reviewer
                    WHERE p.reviewer=? AND i.is_admin=1 AND i.disabled=0''', (actor,)).fetchone()
                if not admin or admin['must_change'] or not verify_password(actor_password, admin['password_hash']):
                    raise ValueError('管理员密码不正确或权限已失效')
                target = db.execute('SELECT 1 FROM name_identities WHERE reviewer=? AND disabled=0', (reviewer,)).fetchone()
                if not target:
                    raise ValueError('账号不存在或已停用')
                db.execute('UPDATE password_credentials SET password_hash=?, must_change=1 WHERE reviewer=?',
                           (encoded, reviewer))
                self._revoke_password_sessions(db, reviewer)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            self._forget_password_sessions(reviewer)

    def _session_allowed(self, reviewer):
        with closing(self._connect()) as db:
            return bool(db.execute('SELECT 1 FROM name_identities WHERE reviewer=? AND disabled=0',
                                   (reviewer,)).fetchone())

    def is_admin(self, reviewer):
        with closing(self._connect()) as db:
            return bool(db.execute('SELECT 1 FROM name_identities WHERE reviewer=? AND is_admin=1 AND disabled=0',
                                   (reviewer,)).fetchone())
