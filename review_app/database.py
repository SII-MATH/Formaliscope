"""SQLite connections and ordered, non-destructive review database migrations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .build import LEGACY_FINGERPRINT_SCHEME

DB_SCHEMA_VERSION = 9


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _migration_1(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS judgments (
        id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
        card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
        rationale TEXT NOT NULL, created_at TEXT NOT NULL
    )""")


def _migration_2(db: sqlite3.Connection) -> None:
    global_request_id = any(
        index[2] and [column[2] for column in db.execute(f"PRAGMA index_info('{index[1]}')")] == ["request_id"]
        for index in db.execute("PRAGMA index_list('judgments')")
    )
    if global_request_id:
        db.execute("ALTER TABLE judgments RENAME TO judgments_legacy")
        db.execute("""CREATE TABLE judgments (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
            card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
            rationale TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(reviewer, request_id)
        )""")
        db.execute("""INSERT INTO judgments
            (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at)
            SELECT id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at
            FROM judgments_legacy ORDER BY rowid""")
        db.execute("DROP TABLE judgments_legacy")
    db.execute("DROP INDEX IF EXISTS judgments_card_created")
    db.execute("CREATE INDEX IF NOT EXISTS judgments_reviewer_card_created ON judgments(reviewer, card_id, created_at)")


def _migration_3(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info('judgments')")}
    if "fingerprint_scheme" not in columns:
        db.execute(f"ALTER TABLE judgments ADD COLUMN fingerprint_scheme TEXT NOT NULL DEFAULT '{LEGACY_FINGERPRINT_SCHEME}'")
    if "source_commit" not in columns:
        db.execute("ALTER TABLE judgments ADD COLUMN source_commit TEXT")
    if "snapshot_digest" not in columns:
        db.execute("ALTER TABLE judgments ADD COLUMN snapshot_digest TEXT")


def _migration_4(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info('judgments')")}
    if "review_basis_scheme" not in columns:
        db.execute("ALTER TABLE judgments ADD COLUMN review_basis_scheme TEXT")
    if "review_basis_fingerprint" not in columns:
        db.execute("ALTER TABLE judgments ADD COLUMN review_basis_fingerprint TEXT")


def _migration_5(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS reviewer_profiles (
        reviewer TEXT PRIMARY KEY, display_name TEXT NOT NULL,
        preview_admin INTEGER NOT NULL DEFAULT 0
    )''')


def _migration_6(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS name_identities (
        reviewer TEXT PRIMARY KEY, recovery_digest TEXT UNIQUE NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS identity_requests (
        action TEXT NOT NULL, ip_digest TEXT NOT NULL, created_at INTEGER NOT NULL
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS identity_requests_limits ON identity_requests(action, ip_digest, created_at)')


def _migration_7(db: sqlite3.Connection) -> None:
    # These tables were previously created by individual authentication modes,
    # outside the migration history. Preserve their rows and opaque identities.
    for table in ("login_sessions", "preview_identities"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info('{table}')")}
        if "email" in columns and "reviewer" not in columns:
            db.execute(f"ALTER TABLE {table} RENAME COLUMN email TO reviewer")
    db.execute("""CREATE TABLE IF NOT EXISTS login_challenges (
        id TEXT PRIMARY KEY, email TEXT NOT NULL, code_digest TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, used_at INTEGER
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS login_challenges_email ON login_challenges(email, created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS login_requests (
        email TEXT NOT NULL, ip_digest TEXT NOT NULL, created_at INTEGER NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS login_requests_email ON login_requests(email, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS login_requests_ip ON login_requests(ip_digest, created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS login_sessions (
        token_digest TEXT PRIMARY KEY, reviewer TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS login_sessions_reviewer ON login_sessions(reviewer)")
    db.execute("CREATE INDEX IF NOT EXISTS login_sessions_expiry ON login_sessions(expires_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS preview_identities (
        key_digest TEXT PRIMARY KEY, reviewer TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS preview_identities_reviewer ON preview_identities(reviewer)")


def _migration_8(db: sqlite3.Connection) -> None:
    # Keep one recoverable draft per reviewer and evidence version. Completed
    # checkpoints retain their revision so an old browser cannot overwrite them.
    db.execute("""CREATE TABLE review_drafts (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
        card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        reviewer TEXT NOT NULL, verdict TEXT NOT NULL, rationale TEXT NOT NULL,
        created_at TEXT NOT NULL, fingerprint_scheme TEXT NOT NULL,
        review_basis_scheme TEXT NOT NULL, review_basis_fingerprint TEXT NOT NULL,
        source_commit TEXT, snapshot_digest TEXT,
        revision INTEGER NOT NULL, completed_revision INTEGER NOT NULL DEFAULT 0,
        completion_request_id TEXT, judgment_id TEXT,
        UNIQUE(reviewer, card_id, fingerprint)
    )""")


def _migration_9(db: sqlite3.Connection) -> None:
    # Machine assessments remain private and do not become reviewer judgments.
    # A run and its rows are immutable; retries compare their complete payloads.
    db.execute("""CREATE TABLE agent_assessment_runs (
        run_id TEXT PRIMARY KEY, model TEXT NOT NULL,
        reasoning_effort TEXT, created_at TEXT NOT NULL,
        policy_version TEXT NOT NULL, source_commit TEXT NOT NULL,
        snapshot_digest TEXT NOT NULL, expectation_context_digest TEXT,
        threshold REAL NOT NULL CHECK(threshold >= 0 AND threshold <= 1),
        run_json TEXT NOT NULL, imported_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE agent_assessments (
        run_id TEXT NOT NULL, declaration_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        readback_sha256 TEXT, original_readback_sha256 TEXT,
        readback_confidence REAL NOT NULL
            CHECK(readback_confidence >= 0 AND readback_confidence <= 1),
        expectation_verdict TEXT NOT NULL
            CHECK(expectation_verdict IN ('aligned', 'misaligned', 'undetermined')),
        expectation_reason_zh TEXT,
        expectation_confidence REAL NOT NULL
            CHECK(expectation_confidence >= 0 AND expectation_confidence <= 1),
        original_json TEXT NOT NULL, annotation_json TEXT NOT NULL,
        review_json TEXT, payload_sha256 TEXT NOT NULL,
        PRIMARY KEY(run_id, declaration_id),
        FOREIGN KEY(run_id) REFERENCES agent_assessment_runs(run_id),
        CHECK(expectation_verdict = 'aligned' OR
            (expectation_reason_zh IS NOT NULL AND length(trim(expectation_reason_zh)) > 0))
    )""")
    db.execute("""CREATE INDEX agent_assessments_declaration
        ON agent_assessments(declaration_id, run_id)""")
    db.execute("""CREATE INDEX agent_assessment_runs_basis
        ON agent_assessment_runs(source_commit, snapshot_digest, expectation_context_digest)""")


MIGRATIONS = (
    (1, "create-judgments", _migration_1),
    (2, "scope-request-id-by-reviewer", _migration_2),
    (3, "record-review-provenance", _migration_3),
    (4, "record-stable-review-basis", _migration_4),
    (5, "reviewer-display-names", _migration_5),
    (6, "name-identities-and-recovery", _migration_6),
    (7, "unify-authentication-and-session-storage", _migration_7),
    (8, "separate-review-drafts-from-history", _migration_8),
    (9, "private-versioned-agent-assessments", _migration_9),
)


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path.parent.chmod(0o700)
    with closing(connect(db_path)) as db:
        # Journal mode is persistent. Setting it on every GET takes a database
        # lock and makes readers compete with one another under burst load.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
        )""")
        applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        if applied and (max(applied) > DB_SCHEMA_VERSION or applied != set(range(1, max(applied) + 1))):
            raise ValueError("database schema is newer than or inconsistent with this application")
        for version, name, migration in MIGRATIONS:
            if version in applied:
                continue
            db.execute("BEGIN IMMEDIATE")
            try:
                migration(db)
                db.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                           (version, name, datetime.now(timezone.utc).isoformat()))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
    db_path.chmod(0o600)


def database_schema_version(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    with closing(connect(db_path)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return 0
        row = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)
