"""Authentication schema upgrades preserve identities, credentials and reviews."""

import hashlib
import hmac
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from .auth import AuthSettings, AuthStore
from .database import DB_SCHEMA_VERSION, MIGRATIONS, connect, initialize
from .name_auth import NameAuthStore
from .preview import PreviewAuthStore


LEGACY_AUTH_SCHEMA = (
    """CREATE TABLE login_challenges (
        id TEXT PRIMARY KEY, email TEXT NOT NULL, code_digest TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, used_at INTEGER
    )""",
    "CREATE INDEX login_challenges_email ON login_challenges(email, created_at)",
    """CREATE TABLE login_requests (
        email TEXT NOT NULL, ip_digest TEXT NOT NULL, created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX login_requests_email ON login_requests(email, created_at)",
    "CREATE INDEX login_requests_ip ON login_requests(ip_digest, created_at)",
    """CREATE TABLE login_sessions (
        token_digest TEXT PRIMARY KEY, email TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
    )""",
    "CREATE TABLE preview_identities (key_digest TEXT PRIMARY KEY, email TEXT NOT NULL)",
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def schema6(db):
    db.execute("""CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
    )""")
    for version, name, migration in MIGRATIONS[:6]:
        migration(db)
        db.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (version, name, 'legacy'))


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_and_all_auth_modes_create_the_same_complete_schema(self):
        modes = (
            initialize,
            lambda path: AuthStore(path, AuthSettings(mailer='none', allow_any_email=True)),
            NameAuthStore,
            PreviewAuthStore,
        )
        schemas = []
        for index, mode in enumerate(modes):
            path = self.root / str(index) / 'reviews.sqlite3'
            mode(path)
            with closing(connect(path)) as db:
                schemas.append([tuple(row) for row in db.execute(
                    'SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name')])
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0],
                                 DB_SCHEMA_VERSION)
                for table in ('login_sessions', 'preview_identities'):
                    columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                    self.assertIn('reviewer', columns)
                    self.assertNotIn('email', columns)
                for table in ('login_challenges', 'login_requests'):
                    self.assertIn('email', {row['name'] for row in db.execute(f'PRAGMA table_info({table})')})
                indexes = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue({'identity_requests_limits', 'login_challenges_email', 'login_requests_email',
                                 'login_requests_ip', 'login_sessions_reviewer', 'login_sessions_expiry',
                                 'preview_identities_reviewer'} <= indexes)
        for schema in schemas[1:]:
            self.assertEqual(schema, schemas[0])

    def test_schema6_upgrade_preserves_records_and_existing_authentication(self):
        path = self.root / 'reviews.sqlite3'
        now = 1_800_000_000
        pepper = b'p' * 32
        (self.root / 'auth-pepper').write_bytes(pepper)
        owner, disabled = 'u_' + '1' * 32, 'u_' + '2' * 32
        email, preview = 'legacy@example.org', 'original@preview.local'
        recovery, disabled_recovery, resume_key = 'KIP-' + 'A' * 43, 'KIP-' + 'B' * 43, 'C' * 43
        tokens = {'name-token': owner, 'disabled-token': disabled, 'email-token': email,
                  'preview-token': preview, 'expired-token': owner}
        tables = ('judgments', 'reviewer_profiles', 'name_identities', 'identity_requests',
                  'login_challenges', 'login_requests', 'login_sessions', 'preview_identities')
        with closing(connect(path)) as db:
            schema6(db)
            for ddl in LEGACY_AUTH_SCHEMA:
                db.execute(ddl)
            db.executemany('INSERT INTO reviewer_profiles VALUES (?, ?, ?)',
                           [(owner, '管理员', 0), (disabled, '停用', 0), (email, '旧邮箱', 0), (preview, '预览', 1)])
            db.executemany('INSERT INTO name_identities VALUES (?, ?, ?, ?)',
                           [(owner, digest(recovery), 1, 0), (disabled, digest(disabled_recovery), 1, 1)])
            db.execute('INSERT INTO judgments (id, request_id, card_id, fingerprint, reviewer, verdict, '
                       'rationale, created_at, source_commit, snapshot_digest, review_basis_scheme, '
                       'review_basis_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       ('j1', 'r1', 'card', 'fingerprint', owner, 'aligned', '原判断', 'original',
                        'commit', 'snapshot', 'basis', 'stable-fingerprint'))
            db.execute('INSERT INTO identity_requests VALUES (?, ?, ?)', ('recover', 'ip-digest', now - 1))
            code_digest = hmac.new(pepper, b'challenge:12345678', hashlib.sha256).hexdigest()
            db.execute('INSERT INTO login_challenges VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ('challenge', email, code_digest, now - 10, now + 600, 2, None))
            db.execute('INSERT INTO login_requests VALUES (?, ?, ?)', (email, 'ip-digest', now - 70))
            db.executemany('INSERT INTO login_sessions VALUES (?, ?, ?, ?)',
                           [(digest(token), reviewer, now - 10, now - 1 if token == 'expired-token' else now + 600)
                            for token, reviewer in tokens.items()])
            db.execute('INSERT INTO preview_identities VALUES (?, ?)', (digest(resume_key), preview))
            before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                      for table in tables}
            ledger = [tuple(row) for row in db.execute('SELECT * FROM schema_migrations ORDER BY version')]

        initialize(path)
        with closing(connect(path)) as db:
            for table in tables:
                self.assertEqual([tuple(row)[:len(before[table][0])] for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')],
                                 before[table], table)
            self.assertEqual([tuple(row) for row in db.execute(
                'SELECT * FROM schema_migrations WHERE version<=6 ORDER BY version')], ledger)
            migrated = list(db.iterdump())
        initialize(path)
        with closing(connect(path)) as db:
            self.assertEqual(list(db.iterdump()), migrated)

        name_auth = NameAuthStore(path, clock=lambda: now)
        self.assertEqual(name_auth.session_reviewer('name-token'), owner)
        self.assertEqual(name_auth.session_email('name-token'), owner)
        self.assertTrue(name_auth.is_admin(owner))
        self.assertIsNone(name_auth.session_reviewer('disabled-token'))
        self.assertFalse(name_auth.is_admin(disabled))
        self.assertIsNone(name_auth.session_reviewer('expired-token'))
        self.assertIsNone(name_auth.resume_reviewer(disabled_recovery))
        self.assertEqual(name_auth.session_reviewer(name_auth.resume_reviewer(recovery)), owner)

        email_auth = AuthStore(path, AuthSettings(mailer='none', allowed_emails=frozenset({email})),
                               clock=lambda: now)
        self.assertEqual(email_auth.session_reviewer('email-token'), email)
        self.assertEqual(email_auth.session_email(email_auth.verify_code(email, '12345678')), email)
        self.assertIsNone(email_auth.verify_code(email, '12345678'))

        preview_auth = PreviewAuthStore(path, clock=lambda: now)
        self.assertEqual(preview_auth.session_reviewer('preview-token'), preview)
        self.assertEqual(preview_auth.session_reviewer(preview_auth.resume_reviewer(resume_key)), preview)

    def test_failed_upgrade_rolls_back_owner_rename_and_migration_ledger(self):
        path = self.root / 'invalid.sqlite3'
        with closing(connect(path)) as db:
            schema6(db)
            db.execute(LEGACY_AUTH_SCHEMA[5])
            db.execute('INSERT INTO login_sessions VALUES (?, ?, ?, ?)', ('digest', 'old-owner', 1, 2))
            # A damaged pre-existing table makes creation of its reviewer index fail.
            db.execute('CREATE TABLE preview_identities (key_digest TEXT PRIMARY KEY)')
        with self.assertRaises(sqlite3.OperationalError):
            initialize(path)
        with closing(connect(path)) as db:
            self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 6)
            self.assertEqual(db.execute('SELECT email FROM login_sessions').fetchone()[0], 'old-owner')
            self.assertNotIn('login_challenges', {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})


if __name__ == '__main__':
    unittest.main()
