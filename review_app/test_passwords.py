"""Password ownership, migration, forced changes and administrator reset boundaries."""
import http.client
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from .database import MIGRATIONS, connect, initialize
from .name_auth import NameAuthStore, RateLimited
from .passwords import INITIAL_PASSWORD, hash_password, verify_password
from . import test_name_auth


class PasswordIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'reviews.sqlite3'
        self.now = 1_800_000_000
        self.auth = NameAuthStore(self.db, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_salted_hashes_and_invalid_inputs(self):
        one, two = hash_password('my-password'), hash_password('my-password')
        self.assertNotEqual(one, two)
        self.assertTrue(verify_password('my-password', one))
        self.assertFalse(verify_password('different', one))
        for value in (None, [], 'short', 'x'*129, 'password\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hash_password(value)
            self.assertFalse(verify_password(value, one))
        for value in (None, 'not-a-hash', one.replace('600000', '999999999'), 'pbkdf2_sha256$600000$'+'z'*32+'$'+'f'*64):
            self.assertFalse(verify_password('my-password', value))

    def test_default_change_preserves_owner_recovery_and_revokes_cached_devices(self):
        token, recovery = self.auth.create_identity('原用户', admin=True)
        owner = self.auth.session_reviewer(token)
        other = self.auth.login_password(owner, INITIAL_PASSWORD)
        self.assertEqual(self.auth.session_reviewer(other), owner)
        password_token = self.auth.login_password(owner, INITIAL_PASSWORD)
        self.assertEqual(self.auth.session_reviewer(password_token), owner)
        self.assertTrue(self.auth.must_change_password(owner))
        with closing(connect(self.db)) as db:
            before = tuple(db.execute('SELECT * FROM name_identities').fetchone())
        self.auth.change_password(owner, INITIAL_PASSWORD, 'user-private-987', token)
        self.assertFalse(self.auth.must_change_password(owner))
        self.assertIsNone(self.auth.session_reviewer(other))
        self.assertIsNone(self.auth.session_reviewer(password_token))
        self.assertEqual(self.auth.session_reviewer(token), owner)
        self.assertIsNone(self.auth.login_password(owner, INITIAL_PASSWORD))
        self.assertEqual(self.auth.session_reviewer(self.auth.login_password('原用户', 'user-private-987')), owner)
        self.assertFalse(hasattr(self.auth, 'resume_reviewer'))
        self.assertTrue(self.auth.is_admin(owner))
        restarted = NameAuthStore(self.db, clock=lambda: self.now)
        self.assertEqual(restarted.session_reviewer(restarted.login_password(owner, 'user-private-987')), owner)
        with closing(connect(self.db)) as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM name_identities').fetchone()), before)
            dump = '\n'.join(db.iterdump())
            for secret in ('user-private-987', token):
                self.assertNotIn(secret, dump)

    def test_same_names_disabled_accounts_and_limits_across_aliases(self):
        first, _ = self.auth.create_identity('同名', password='first-password')
        with self.assertRaises(ValueError):
            self.auth.create_identity(' 同名 ', password='second-password')
        second, _ = self.auth.create_identity('另一用户', password='second-password')
        owner = self.auth.session_reviewer(first)
        self.assertIsNone(self.auth.login_password('同名', 'wrong-password'))
        self.assertIsNone(self.auth.login_password(owner, 'second-password'))
        self.assertEqual(self.auth.session_reviewer(self.auth.login_password(owner, 'first-password')), owner)
        self.assertNotEqual(owner, self.auth.session_reviewer(second))
        with closing(connect(self.db)) as db:
            db.execute('UPDATE reviewer_profiles SET display_name=? WHERE reviewer=?', ('唯一姓名', owner))
            db.execute('UPDATE name_identities SET normalized_name=? WHERE reviewer=?', ('唯一姓名', owner))
        for attempt in range(7):
            self.assertIsNone(self.auth.login_password('唯一姓名' if attempt % 2 else owner, 'wrong-password', f'10.0.0.{attempt+1}'))
        with self.assertRaises(RateLimited):
            self.auth.login_password('唯一姓名', 'first-password', '10.0.0.9')
        self.now += 60
        self.assertEqual(self.auth.session_reviewer(self.auth.login_password('唯一姓名', 'first-password')), owner)
        with closing(connect(self.db)) as db:
            db.execute('UPDATE name_identities SET disabled=1 WHERE reviewer=?', (owner,))
        self.assertIsNone(self.auth.login_password(owner, 'first-password'))
        self.assertIsNone(self.auth.session_reviewer(first))

    def test_admin_reset_needs_role_and_password_and_only_affects_exact_target(self):
        admin_token, _ = self.auth.create_identity('管理员', admin=True, password='admin-password')
        admin = self.auth.session_reviewer(admin_token)
        token, recovery = self.auth.create_identity('同名', password='user-password')
        owner = self.auth.session_reviewer(token)
        other_token, _ = self.auth.create_identity('另一用户', password='other-password')
        other = self.auth.session_reviewer(other_token)
        for actor, target, password in ((other, owner, 'other-password'), (admin, owner, 'wrong-password'),
                                       (admin, admin, 'admin-password'), (admin, {}, 'admin-password')):
            with self.subTest(actor=actor, target=target), self.assertRaises(ValueError):
                self.auth.reset_password(actor, target, password)
        with closing(connect(self.db)) as db:
            original = [tuple(row) for row in db.execute('SELECT * FROM name_identities ORDER BY reviewer')]
        self.auth.reset_password(admin, owner, 'admin-password')
        self.assertIsNone(self.auth.session_reviewer(token))
        self.assertEqual(self.auth.session_reviewer(other_token), other)
        self.assertEqual(self.auth.session_reviewer(admin_token), admin)
        self.assertIsNone(self.auth.login_password(owner, 'user-password'))
        reset_token = self.auth.login_password(owner, INITIAL_PASSWORD)
        self.assertEqual(self.auth.session_reviewer(reset_token), owner)
        self.assertTrue(self.auth.must_change_password(owner))
        self.assertFalse(hasattr(self.auth, 'resume_reviewer'))
        with closing(connect(self.db)) as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM name_identities ORDER BY reviewer')], original)
            db.execute('UPDATE name_identities SET is_admin=0 WHERE reviewer=?', (admin,))
        with self.assertRaises(ValueError):
            self.auth.reset_password(admin, other, 'admin-password')

    def test_migration_initializes_once_and_preserves_every_existing_row(self):
        old = self.db.parent / 'old.sqlite3'
        with closing(connect(old)) as db:
            db.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)')
            for version, name, migration in MIGRATIONS[:11]:
                migration(db)
                db.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (version, name, 'old'))
            db.executemany('INSERT INTO name_identities VALUES (?, ?, ?, ?)', [('one', 'digest-one', 1, 0), ('two', 'digest-two', 0, 1)])
            db.execute('INSERT INTO reviewer_profiles VALUES (?, ?, ?)', ('one', '旧姓名', 0))
            db.execute('INSERT INTO login_sessions VALUES (?, ?, ?, ?)', ('token-digest', 'one', 1, 2))
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'schema_migrations'")]
            before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')] for table in tables}
        initialize(old)
        with closing(connect(old)) as db:
            for table in tables:
                if table == 'name_identities':
                    self.assertEqual([tuple(row)[:3] for row in db.execute(f'SELECT * FROM {table}')], [('one',1,0),('two',0,1)])
                    self.assertNotIn('recovery_digest', {row['name'] for row in db.execute('PRAGMA table_info(name_identities)')})
                elif table == 'login_sessions':
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM login_sessions').fetchone()[0], 0)
                else:
                    self.assertEqual([tuple(row)[:len(before[table][0])] if before[table] else tuple(row) for row in db.execute(f'SELECT * FROM {table}')], before[table], table)
            rows = db.execute('SELECT * FROM password_credentials ORDER BY reviewer').fetchall()
            self.assertEqual([row['reviewer'] for row in rows], ['one', 'two'])
            self.assertNotEqual(rows[0]['password_hash'], rows[1]['password_hash'])
            self.assertTrue(all(row['must_change'] and verify_password(INITIAL_PASSWORD, row['password_hash']) for row in rows))
            changed = hash_password('changed-password')
            db.execute('UPDATE password_credentials SET password_hash=?, must_change=0 WHERE reviewer=?', (changed, 'one'))
        initialize(old)
        with closing(connect(old)) as db:
            row = db.execute('SELECT * FROM password_credentials WHERE reviewer=?', ('one',)).fetchone()
            self.assertEqual(row['password_hash'], changed)
            self.assertFalse(row['must_change'])


class PasswordHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_name_auth.NameHTTPTests()
        self.fixture.setUp()
        self.auth, self.request = self.fixture.auth, self.fixture.request

    def tearDown(self):
        self.fixture.tearDown()

    def cookie(self, token):
        return 'kip126_review_session=' + token

    def test_forced_change_is_enforced_on_pages_reads_writes_and_recovery(self):
        token, recovery = self.auth.create_identity('旧用户', admin=True)
        cookie = self.cookie(token)
        owner = self.auth.session_reviewer(token)
        self.assertTrue(self.request('/api/auth/me', cookie=cookie)[2]['must_change_password'])
        for path in ('/', '/admin', '/login'):
            connection = http.client.HTTPConnection('127.0.0.1', self.fixture.server.server_port, timeout=5)
            try:
                connection.request('GET', path, headers={'Cookie': cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader('Location'), './password')
                response.read()
            finally:
                connection.close()
        for path in ('/api/catalog', '/api/admin/users', '/api/admin/summary', '/api/export'):
            status, _, data = self.request(path, cookie=cookie)
            self.assertEqual(status, 403)
            self.assertTrue(data['password_change_required'])
        for path in ('/api/profile', '/api/judgments', '/api/drafts', '/api/admin/reset-password'):
            self.assertEqual(self.request(path, {}, cookie)[0], 403)
        status, headers, data = self.request('/api/auth/password-login', {'account': owner, 'password': INITIAL_PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(data['must_change_password'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(self.request('/api/auth/password', {'current_password': 'wrong-pass', 'new_password': 'new-password'}, cookie)[0], 400)
        self.assertEqual(self.request('/api/auth/password', {'current_password': INITIAL_PASSWORD, 'new_password': 'new-password'}, cookie)[0], 200)
        self.assertEqual(self.request('/api/admin/users', cookie=cookie)[0], 200)
        self.assertFalse(self.request('/api/auth/me', cookie=cookie)[2]['must_change_password'])
        self.assertEqual(self.request('/api/auth/recover', {'recovery_code': recovery})[0], 404)
        self.assertEqual(self.request('/api/auth/password-login', {'account': owner, 'password': INITIAL_PASSWORD})[0], 401)

    def test_reset_protection_registration_and_sessions(self):
        status, headers, data = self.request('/api/auth/register', {'display_name': '新人', 'password': 'personal-password', 'is_admin': True})
        self.assertEqual(status, 201)
        user_cookie = headers['Set-Cookie'].split(';', 1)[0]
        owner = data['user_id']
        me = self.request('/api/auth/me', cookie=user_cookie)[2]
        self.assertFalse(me['is_admin'])
        self.assertFalse(me['must_change_password'])
        admin_token, _ = self.auth.create_identity('管理员', admin=True, password='admin-password')
        admin_cookie = self.cookie(admin_token)
        payload = {'reviewer': owner, 'current_password': 'admin-password'}
        self.assertEqual(self.request('/api/admin/reset-password', payload)[0], 401)
        self.assertEqual(self.request('/api/admin/reset-password', payload, user_cookie)[0], 403)
        self.assertEqual(self.request('/api/admin/reset-password', payload, admin_cookie, origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('/api/admin/reset-password', {**payload, 'current_password': 'wrong-password'}, admin_cookie)[0], 400)
        self.assertEqual(self.request('/api/admin/reset-password', payload, admin_cookie)[0], 200)
        self.assertEqual(self.request('/api/auth/me', cookie=user_cookie)[0], 401)
        self.assertEqual(self.request('/api/auth/password-login', {'account': owner, 'password': 'personal-password'})[0], 401)
        self.assertEqual(self.request('/api/auth/password-login', {'account': owner, 'password': INITIAL_PASSWORD})[0], 200)
        directory = self.request('/api/admin/users', cookie=admin_cookie)[2]
        serialized = str(directory)
        self.assertNotIn('password_hash', serialized)
        self.assertNotIn('recovery_digest', serialized)

    def test_login_cooldown_returns_one_minute_retry(self):
        _, owner = self.auth.create_identity('限流用户', password='correct-password')
        payload = {'account': owner, 'password': 'wrong-password'}
        for _ in range(10):
            self.assertEqual(self.request('/api/auth/password-login', payload)[0], 401)
        status, headers, _ = self.request('/api/auth/password-login', payload)
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '60')
        self.fixture.now += 60
        self.assertEqual(self.request('/api/auth/password-login',
                                      {'account': owner, 'password': 'correct-password'})[0], 200)


if __name__ == '__main__':
    unittest.main()
