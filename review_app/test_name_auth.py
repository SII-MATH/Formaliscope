"""Name identities, HTTP ownership, administrator provisioning and recovery."""
import hashlib
import hmac
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import patch

from .auth import AuthSettings, AuthStore, IP_HOURLY_LIMIT
from .build import CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA
from .judgments import history, submit, update_reviewer_profile
from .name_auth import NAME_SESSION_LIFETIME, NameAuthStore, RateLimited, name_settings
from .preflight import run_preflight
from .server import ReviewHTTPServer, make_handler, serve
from .statements import compile_statements
from .storage import create_backup


class NameIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'data/judgments.sqlite3'
        self.now = 1_800_000_000
        self.auth = NameAuthStore(self.db, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_unique_names_rename_restart_and_expiry(self):
        first, owner = self.auth.create_reviewer('用户', password='private-password')
        with self.assertRaises(ValueError):
            self.auth.create_reviewer(' 用户 ')
        second, _ = self.auth.create_reviewer('另一用户')
        self.assertNotEqual(owner, self.auth.session_email(second))
        self.assertFalse(self.auth.is_admin(owner))
        self.assertEqual(update_reviewer_profile(self.db, owner, '改名')[0], 200)
        restarted = NameAuthStore(self.db, clock=lambda: self.now)
        self.assertEqual(restarted.session_email(first), owner)
        resumed = restarted.login_password('改名', 'private-password')
        self.assertEqual(restarted.session_email(resumed), owner)
        self.now += NAME_SESSION_LIFETIME
        self.assertIsNone(restarted.session_email(resumed))

    def test_disabled_identity_rejects_cached_session_and_recovery(self):
        token, key = self.auth.create_identity('用户', admin=True)
        owner = self.auth.session_email(token)
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE name_identities SET disabled=1 WHERE reviewer=?', (owner,))
        self.assertIsNone(self.auth.session_email(token))
        self.assertIsNone(self.auth.login_password(owner, '12345678'))
        self.assertFalse(self.auth.is_admin(owner))

    def test_name_sessions_validate_identity_without_email_allowlist(self):
        auth = NameAuthStore(self.db, AuthSettings(mailer='none'), clock=lambda: self.now)
        token, recovery = auth.create_identity('用户')
        owner = auth.session_reviewer(token)
        self.assertRegex(owner, r'^u_[0-9a-f]{32}$')
        self.assertEqual(auth.session_email(token), owner)
        restarted = NameAuthStore(self.db, auth.settings, clock=lambda: self.now)
        self.assertEqual(restarted.session_reviewer(token), owner)
        self.assertEqual(restarted.session_reviewer(restarted.login_password(owner, '12345678')), owner)
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE name_identities SET disabled=1 WHERE reviewer=?', (owner,))
        self.assertIsNone(restarted.session_reviewer(token))
        self.assertIsNone(restarted.session_email(token))

    def test_credential_guesses_are_limited_and_origin_is_required(self):
        for _ in range(20):
            self.assertIsNone(self.auth.login_password('unknown', 'short'))
        with self.assertRaises(RateLimited):
            self.auth.login_password('unknown', 'short')
        self.now += 601
        self.assertIsNone(self.auth.login_password('unknown', 'short'))
        for origin in ('', 'http://public.example.org', 'https://u:p@example.org', 'https://example.org/path', 'https://example.org:99999'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                name_settings({'REVIEW_PUBLIC_ORIGIN': origin})
        self.assertEqual(name_settings({'REVIEW_PUBLIC_ORIGIN': 'http://127.0.0.1:8890'}).mailer, 'none')

    def test_backup_preserves_recovery_without_old_sessions_or_pepper(self):
        token, key = self.auth.create_identity('管理员', admin=True, password='private-password')
        owner = self.auth.session_email(token)
        (self.db.parent / 'snapshot.json').write_text(json.dumps({'digest': 'a'*64, 'source_commit': 'b'*40}))
        backup = create_backup(self.db.parent, self.root / 'backups')
        restored = NameAuthStore(backup / 'judgments.sqlite3')
        self.assertNotEqual(restored.pepper, self.auth.pepper)
        self.assertIsNone(restored.session_email(token))
        self.assertEqual(restored.session_email(restored.login_password(owner, 'private-password')), owner)
        self.assertTrue(restored.is_admin(owner))

    def test_operator_cli_private_file_and_existing_identity_binding(self):
        output = self.root / 'admin-account.txt'
        command = [sys.executable, '-m', 'review_app', 'create-admin', '--data-dir', str(self.db.parent),
                   '--name', '管理员', '--output', str(output)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        account_file = output.read_text().strip()
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('12345678', first.stdout + first.stderr)
        owner = json.loads(first.stdout)['reviewer']
        self.assertTrue(self.auth.is_admin(owner))
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(output.read_text().strip(), account_file)
        update_reviewer_profile(self.db, 'legacy@example.org', '旧用户')
        bound = self.root / 'legacy.txt'
        result = subprocess.run([sys.executable, '-m', 'review_app', 'bind-account', '--data-dir', str(self.db.parent),
                                 '--reviewer', 'legacy@example.org', '--name', '旧用户', '--output', str(bound)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        recovered = self.auth.login_password('legacy@example.org', '12345678')
        self.assertEqual(self.auth.session_email(recovered), 'legacy@example.org')
        self.assertFalse(self.auth.is_admin('legacy@example.org'))

    def test_default_name_preflight_no_mail_and_read_only_admin_check(self):
        source = self.root / 'source/KIP126'
        source.mkdir(parents=True)
        (source / 'X.lean').write_text('def x : Nat := 1\n')
        snapshot = compile_statements(source.parent, source_commit='a'*40)
        snapshot.pop('source_origin', None)
        from .build import calculate_snapshot_digest
        snapshot['digest'] = calculate_snapshot_digest(snapshot)
        (self.db.parent / 'snapshot.json').write_text(json.dumps(snapshot))
        env = {'REVIEW_PUBLIC_ORIGIN': 'https://review.example.org'}
        self.assertFalse(run_preflight(self.db.parent, env=env)['ready'])
        self.auth.create_identity('管理员', admin=True)
        with sqlite3.connect(self.db) as db:
            before = list(db.iterdump())
        report = run_preflight(self.db.parent, env=env)
        self.assertTrue(report['ready'], report)
        self.assertEqual(report['auth_mode'], 'name')
        self.assertNotIn('mailer', {item['id'] for item in report['checks']})
        with sqlite3.connect(self.db) as db:
            self.assertEqual(before, list(db.iterdump()))


class NameHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'judgments.sqlite3'
        self.now = 1_800_000_000
        self.auth = NameAuthStore(self.db, clock=lambda: self.now)
        self.card = {'id': 'statement::KIP126.X', 'fingerprint': 'a'*64,
                     'fingerprint_scheme': CURRENT_FINGERPRINT_SCHEME,
                     'fingerprints': {CURRENT_FINGERPRINT_SCHEME: 'a'*64},
                     'label': 'X', 'title': 'X', 'chapter': 'Test', 'kind': 'def',
                     'declaration': 'KIP126.X', 'source_status': 'local', 'dependencies': []}
        self.snapshot = {'schema': SNAPSHOT_SCHEMA, 'digest': 'b'*64, 'source_commit': 'c'*40,
                         'review_mode': 'statement', 'cards': [self.card], 'unlinked_nodes': 0}
        self.start_server()

    def start_server(self, *, auth=None, trust_proxy_ip=False, peer=None):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        auth = auth or self.auth
        handler = make_handler(self.snapshot, self.db, Path(__file__).parent/'static', auth,
                               admin_emails=frozenset({'管理员'}), trust_proxy_ip=trust_proxy_ip)
        handler.log_message = lambda *_: None
        self.server = ReviewHTTPServer(('127.0.0.1', 0), handler)
        if peer is not None:
            accept = self.server.get_request

            def get_request():
                connection, address = accept()
                return connection, (peer, address[1])

            self.server.get_request = get_request
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, cookie=None, origin=None, extra_headers=None):
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        if body is not None:
            headers['Origin'] = origin if origin is not None else self.base
        if cookie:
            headers['Cookie'] = cookie
        headers.update(extra_headers or {})
        request = Request(self.base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.status, dict(response.headers), json.loads(content) if content else None

    def register(self, name=None):
        self.registration_sequence = getattr(self, 'registration_sequence', 0) + 1
        name = name or '用户' + str(self.registration_sequence)
        status, headers, data = self.request('/api/auth/register', {'display_name': name, 'password': 'test-pass-987', 'is_admin': True, 'reviewer': 'spoof'})
        self.assertEqual(status, 201)
        return headers['Set-Cookie'].split(';', 1)[0], data['user_id']

    def test_global_admin_user_directory_and_details_are_read_only_and_protected(self):
        first, recovery = self.register('同名')
        second, _ = self.register('另一用户')
        token, _ = self.auth.create_identity('后台管理员', admin=True, password='test-pass-987')
        admin = 'kip126_review_session=' + token
        viewer = self.request('/api/auth/me', cookie=first)[2]['user_id']
        self.assertFalse(self.request('/api/auth/me', cookie=first)[2]['can_view_users'])
        self.assertTrue(self.request('/api/auth/me', cookie=admin)[2]['can_view_users'])
        for path in ('/api/admin/users', '/api/admin/user?reviewer=' + viewer):
            self.assertEqual(self.request(path)[0], 401)
            self.assertEqual(self.request(path, cookie=first)[0], 403)
            self.assertEqual(self.request(path, {}, admin)[0], 404)
        directory = self.request('/api/admin/users', cookie=admin)[2]
        self.assertEqual(directory['stats']['registered'], 3)
        self.assertEqual(len({row['reviewer'] for row in directory['users']}), 3)
        self.assertNotIn('test-pass-987', json.dumps(directory))
        body = {'request_id': str(uuid.uuid4()), 'card_id': self.card['id'],
                'fingerprint': self.card['fingerprint'], 'verdict': 'aligned', 'rationale': '可查看的意见'}
        self.assertEqual(self.request('/api/judgments', body, first)[0], 201)
        detail = self.request('/api/admin/user?reviewer=' + viewer, cookie=admin)[2]
        self.assertEqual(detail['current_count'], 1)
        self.assertEqual(detail['reviews'][0]['rationale'], '可查看的意见')
        self.assertEqual(self.request('/api/admin/user?reviewer=missing', cookie=admin)[0], 404)
        for query in ('cursor=-1', 'limit=101', 'cursor=no', 'reviewer=extra'):
            self.assertEqual(self.request('/api/admin/user?reviewer=' + viewer + '&' + query, cookie=admin)[0], 400)

    def test_drafts_completion_pagination_and_exports_use_authenticated_owner(self):
        first, recovery = self.register()
        second, _ = self.register()
        path = '/api/review-state?id=' + self.card['id']
        body = {'request_id': str(uuid.uuid4()), 'card_id': self.card['id'],
                'fingerprint': self.card['fingerprint'], 'verdict': 'aligned',
                'rationale': '第一行', 'revision': 0, 'reviewer': 'spoof'}
        self.assertEqual(self.request('/api/drafts', body)[0], 401)
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request('/api/drafts', body, first, origin='https://other.example')[0], 403)
        self.assertEqual(self.request('/api/drafts', body, first)[0], 200)
        state = self.request(path, cookie=first)[2]
        self.assertEqual(state['draft']['rationale'], '第一行')
        self.assertEqual(state['history_count'], 0)
        self.assertIsNone(self.request(path + '&reviewer=spoof', cookie=second)[2]['draft'])
        self.assertEqual(self.request('/api/catalog', cookie=first)[2]['cards'][0]['verdict'], None)
        finish = {**body, 'draft_revision': 1, 'request_id': str(uuid.uuid4())}
        self.assertEqual(self.request('/api/judgments', finish, second)[0], 409)
        self.assertEqual(self.request('/api/judgments', finish, first)[0], 201)
        self.assertEqual(self.request('/api/judgments', finish, first)[0], 200)
        self.assertEqual(self.request(path, cookie=first)[2]['history_count'], 1)
        for i in range(30):
            payload = {**body, 'rationale': str(i), 'request_id': str(uuid.uuid4())}
            self.assertEqual(self.request('/api/judgments', payload, first)[0], 201)
        page = self.request('/api/history?id=' + self.card['id'], cookie=first)[2]
        self.assertEqual(len(page['history']), 25)
        self.assertEqual(len(self.request('/api/card?id=' + self.card['id'], cookie=first)[2]['history']), 25)
        older_path = '/api/history?id=' + self.card['id'] + '&cursor=' + quote(page['next_cursor'])
        self.assertEqual(len(self.request(older_path, cookie=first)[2]['history']), 6)
        self.assertEqual(self.request(older_path, cookie=second)[2]['history'], [])
        for suffix in ('&limit=0', '&limit=101', '&limit=bad', '&cursor=bad'):
            self.assertEqual(self.request('/api/history?id=' + self.card['id'] + suffix, cookie=first)[0], 400)
        latest = self.request('/api/export?mode=latest', cookie=first)[2]
        full = self.request('/api/export?mode=history', cookie=first)[2]
        self.assertEqual(len(latest['judgments']), 1)
        self.assertEqual(latest['judgments'][0]['rationale'], '29')
        self.assertEqual(len(full['judgments']), 31)
        self.assertEqual(self.request('/api/export?mode=latest', cookie=second)[2]['judgments'], [])
        self.assertEqual(self.request('/api/export?mode=invalid', cookie=first)[0], 400)
        self.request('/api/auth/logout', {}, first)
        _, headers, _ = self.request('/api/auth/password-login', {'account': recovery, 'password': 'test-pass-987'})
        recovered = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(self.request(path, cookie=recovered)[2]['history_count'], 31)

    def test_same_name_ownership_spoof_rename_and_recovery(self):
        first, key = self.register()
        second, _ = self.register()
        owner = self.request('/api/auth/me', cookie=first)[2]['user_id']
        self.assertNotEqual(owner, self.request('/api/auth/me', cookie=second)[2]['user_id'])
        body = {'request_id': str(uuid.uuid4()), 'card_id': self.card['id'], 'fingerprint': 'a'*64,
                'verdict': 'aligned', 'rationale': '私人的判断', 'reviewer': 'spoof', 'display_name': 'spoof'}
        self.assertEqual(self.request('/api/judgments', body, first)[0], 201)
        self.assertEqual(self.request('/api/history?id='+self.card['id'], cookie=second)[2]['history'], [])
        self.assertEqual(self.request('/api/export', cookie=second)[2]['judgments'], [])
        self.assertEqual(self.request('/api/profile', {'display_name': '改名'}, first)[0], 200)
        self.assertEqual(self.request('/api/auth/me', cookie=first)[2]['user_id'], owner)
        self.assertEqual(self.request('/api/auth/logout', {}, first)[0], 200)
        self.assertEqual(self.request('/api/auth/me', cookie=first)[0], 401)
        status, headers, _ = self.request('/api/auth/password-login', {'account': key, 'password': 'test-pass-987'})
        self.assertEqual(status, 200)
        recovered = headers['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(self.request('/api/auth/me', cookie=recovered)[2]['display_name'], '改名')
        history_data = self.request('/api/history?id='+self.card['id'], cookie=recovered)[2]['history']
        self.assertEqual(len(history_data), 1)
        self.assertEqual(history_data[0]['reviewer'], owner)
        self.assertNotIn('test-pass-987', json.dumps(self.request('/api/auth/me', cookie=recovered)[2]))
        self.assertEqual(self.request('/api/auth/register', {'display_name': 'new'}, recovered)[0], 409)

    def test_no_admin_by_name_or_first_registration_and_operator_login(self):
        cookie, _ = self.register('管理员')
        self.assertFalse(self.request('/api/auth/me', cookie=cookie)[2]['is_admin'])
        self.assertEqual(self.request('/api/admin/summary', cookie=cookie)[0], 403)
        _, key = self.auth.create_identity('后台操作员', admin=True, password='test-pass-987')
        status, headers, _ = self.request('/api/auth/password-login', {'account': key, 'password': 'test-pass-987'})
        self.assertEqual(status, 200)
        admin_cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.assertTrue(self.request('/api/auth/me', cookie=admin_cookie)[2]['is_admin'])
        self.assertEqual(self.request('/api/admin/summary', cookie=admin_cookie)[0], 200)

    def test_origin_legacy_routes_and_secure_prefix_cookie(self):
        self.assertEqual(self.request('/api/auth/register', {'display_name': 'X'}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('/api/auth/request-code', {'email': 'x@example.org'})[0], 404)
        self.assertEqual(self.request('/api/auth/verify-code', {'email': 'x@example.org', 'code': '12345678'})[0], 404)
        self.assertEqual(self.request('/api/preview/session', {'display_name': 'X'})[0], 404)
        self.assertEqual(self.request('/api/auth/recovery', {})[0], 404)
        self.auth.settings = AuthSettings(mailer='none', allow_any_email=True,
                                         public_origin='https://review.example.org', cookie_path='/review/')
        status, headers, _ = self.request('/api/auth/register', {'display_name': 'X'}, origin='https://review.example.org')
        self.assertEqual(status, 201)
        for attribute in ('Path=/review/', f'Max-Age={NAME_SESSION_LIFETIME}', 'HttpOnly', 'SameSite=Strict', 'Secure'):
            self.assertIn(attribute, headers['Set-Cookie'])
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_recovery_routes_are_removed_and_names_cannot_be_registered_twice(self):
        cookie, owner = self.register('唯一姓名')
        for path in ('/api/auth/recovery', '/api/auth/recover'):
            self.assertEqual(self.request(path, {'recovery_code': 'KIP-'+'A'*43}, cookie)[0], 404)
        self.assertEqual(self.request('/api/auth/register', {'display_name': ' 唯一姓名 ', 'password': 'test-pass-987'})[0], 409)
        self.assertEqual(self.request('/api/auth/me', cookie=cookie)[2]['user_id'], owner)

    def ip_digest(self, address, auth=None):
        return hmac.new((auth or self.auth).pepper, address.encode(), hashlib.sha256).hexdigest()

    def test_local_proxy_keeps_name_registration_and_recovery_quotas_separate(self):
        self.start_server(trust_proxy_ip=True)
        first, second = '198.51.100.10', '2001:db8::20'
        with sqlite3.connect(self.db) as db:
            # Existing requests fill one address's quotas, outside the separate
            # global minute limit; another student must still be able to enter.
            for action, count in (('register', 120), ('password', 20)):
                db.executemany('INSERT INTO identity_requests VALUES (?, ?, ?)',
                               [(action, self.ip_digest(first), self.now-120)]*count)
        self.assertEqual(self.request('/api/auth/register', {'display_name': 'A'},
                                      extra_headers={'X-Real-IP': first})[0], 429)
        status, _, data = self.request('/api/auth/register', {'display_name': 'B'},
                                       extra_headers={'X-Real-IP': '2001:0db8:0:0:0:0:0:20'})
        self.assertEqual(status, 201)
        account = data['user_id']
        self.assertEqual(self.request('/api/auth/password-login', {'account': account, 'password': '12345678'},
                                      extra_headers={'X-Real-IP': first})[0], 429)
        self.assertEqual(self.request('/api/auth/password-login', {'account': account, 'password': '12345678'},
                                      extra_headers={'X-Real-IP': second})[0], 200)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT action, COUNT(*) FROM identity_requests WHERE ip_digest=? '
                                        'GROUP BY action ORDER BY action', (self.ip_digest(second),)).fetchall(),
                             [('password', 1), ('register', 1)])
        self.assertEqual(self.request('/api/auth/register', {'display_name': 'C'},
                                      origin='https://evil.example', extra_headers={'X-Real-IP': second})[0], 403)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM identity_requests WHERE ip_digest=?',
                                        (self.ip_digest(second),)).fetchone()[0], 2)

    def test_spoofed_proxy_headers_are_ignored_by_default_and_for_nonlocal_peers(self):
        for trust, peer in ((False, '127.0.0.1'), (True, '203.0.113.5')):
            with self.subTest(trust=trust, peer=peer):
                self.start_server(trust_proxy_ip=trust, peer=peer)
                with sqlite3.connect(self.db) as db:
                    db.execute('DELETE FROM identity_requests')
                for address in ('198.51.100.10', '198.51.100.20'):
                    self.assertEqual(self.request('/api/auth/register', {'display_name': '学生'+str(trust)+address},
                                                  extra_headers={'X-Real-IP': address})[0], 201)
                self.assertEqual(self.request('/api/auth/password-login', {'account': 'unknown', 'password': 'short'},
                                              extra_headers={'X-Real-IP': '198.51.100.30'})[0], 401)
                with sqlite3.connect(self.db) as db:
                    self.assertEqual(db.execute('SELECT DISTINCT ip_digest FROM identity_requests').fetchall(),
                                     [(self.ip_digest(peer),)])

    def test_missing_invalid_list_and_duplicate_real_ip_headers_fall_back_to_peer(self):
        self.start_server(trust_proxy_ip=True)
        cases = [{}, {'X-Forwarded-For': '198.51.100.10'}, {'Forwarded': 'for=198.51.100.10'}]
        cases.extend({'X-Real-IP': value} for value in
                     ('not-an-ip', '198.51.100.10, 198.51.100.20', 'example.org',
                      '198.51.100.10:4321', '[2001:db8::1]', 'fe80::1%lo', ''))
        for index, headers in enumerate(cases):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/auth/register', {'display_name': '学生'+str(index)},
                                              extra_headers=headers)[0], 201)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            body = json.dumps({'display_name': '最后学生'}).encode()
            connection.putrequest('POST', '/api/auth/register')
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Origin', self.base)
            connection.putheader('Content-Length', str(len(body)))
            connection.putheader('X-Real-IP', '198.51.100.10')
            connection.putheader('X-Real-IP', '198.51.100.20')
            connection.endheaders(body)
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            response.read()
        finally:
            connection.close()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT ip_digest, COUNT(*) FROM identity_requests GROUP BY ip_digest').fetchall(),
                             [(self.ip_digest('127.0.0.1'), len(cases)+1)])

    def test_local_proxy_keeps_legacy_email_network_quotas_separate(self):
        sent = []
        auth = AuthStore(self.db, AuthSettings(mailer='none', allow_any_email=True),
                         sender=lambda email, code: sent.append(email), clock=lambda: self.now)
        self.start_server(auth=auth, trust_proxy_ip=True)
        first, second = '198.51.100.10', '198.51.100.20'
        with sqlite3.connect(self.db) as db:
            db.executemany('INSERT INTO login_requests VALUES (?, ?, ?)',
                           [(f'previous{index}@example.org', self.ip_digest(first, auth), self.now-120)
                            for index in range(IP_HOURLY_LIMIT)])
        for address, email in ((first, 'limited@example.org'), (second, 'student@example.org')):
            self.assertEqual(self.request('/api/auth/request-code', {'email': email},
                                          extra_headers={'X-Real-IP': address})[0], 200)
        self.assertEqual(sent, ['student@example.org'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT ip_digest FROM login_requests WHERE email=?',
                                        ('student@example.org',)).fetchone()[0], self.ip_digest(second, auth))

    def test_serve_only_enables_proxy_trust_for_explicit_one(self):
        snapshot_path = self.db.parent/'snapshot.json'
        snapshot_path.write_text(json.dumps(self.snapshot))
        for value, enabled in (('', False), ('0', False), ('true', False), ('1', True)):
            with self.subTest(value=value), patch.dict(os.environ, {
                    'REVIEW_AUTH_MODE': 'name', 'REVIEW_PUBLIC_ORIGIN': 'http://127.0.0.1:8765',
                    'REVIEW_TRUST_PROXY_IP': value}), \
                    patch('review_app.server.normalize_snapshot', return_value=self.snapshot), \
                    patch('review_app.server.backfill_review_basis'), \
                    patch('review_app.server.make_handler', wraps=make_handler) as make, \
                    patch('review_app.server.ReviewHTTPServer'), patch('builtins.print'):
                serve(snapshot_path, self.db, Path(__file__).parent/'static', '127.0.0.1', 8765)
                self.assertIs(make.call_args.kwargs['trust_proxy_ip'], enabled)


if __name__ == '__main__':
    unittest.main()
