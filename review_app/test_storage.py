from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .auth import AuthSettings, AuthStore
from .build import (CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA,
                    _content_fingerprint, calculate_snapshot_digest)
from .server import DB_SCHEMA_VERSION, initialize
from .storage import create_backup, install_snapshot


class StorageTests(unittest.TestCase):
    def test_consistent_backup_keeps_reviews_and_discards_auth_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            backups = root / "backups"
            database = data / "judgments.sqlite3"
            initialize(database)
            AuthStore(database, AuthSettings(mailer="agently", allow_any_email=True),
                      sender=lambda _email, _code: None)
            snapshot = {"digest": "a" * 64, "source_commit": "b" * 40}
            (data / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
            with sqlite3.connect(database) as db:
                db.execute("""INSERT INTO judgments
                    (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                           ("j1", "r1", "card", "f" * 64, "alice@example.org",
                            "aligned", "", "2026-09-19T00:00:00Z"))
                db.execute("INSERT INTO login_sessions VALUES (?, ?, ?, ?)",
                           ("secret-session-digest", "session-only@example.org", 1, 2))

            result = create_backup(
                data, backups, now=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))

            self.assertEqual(result.name, "20260919T120000Z")
            self.assertFalse((result / "auth-pepper").exists())
            manifest = json.loads((result / "manifest.json").read_text())
            self.assertEqual(manifest["sqlite_integrity_check"], "ok")
            self.assertEqual(manifest["database_schema_version"], DB_SCHEMA_VERSION)
            self.assertEqual(manifest["snapshot_digest"], snapshot["digest"])
            with sqlite3.connect(result / "judgments.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM login_sessions").fetchone()[0], 0)
            self.assertNotIn(b"secret-session-digest", (result / "judgments.sqlite3").read_bytes())
            self.assertEqual(result.stat().st_mode & 0o777, 0o700)
            for name in ("judgments.sqlite3", "snapshot.json", "manifest.json"):
                self.assertEqual((result / name).stat().st_mode & 0o777, 0o600)

    def test_backup_requires_database_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                create_backup(root / "missing", root / "backups")

    def test_backup_accepts_a_pre_versioned_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            with sqlite3.connect(data / "judgments.sqlite3") as db:
                db.execute("""CREATE TABLE judgments (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                    rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(reviewer, request_id))""")
            (data / "snapshot.json").write_text(
                json.dumps({"digest": "a" * 64, "source_commit": "b" * 40}),
                encoding="utf-8")
            result = create_backup(
                data, root / "backups",
                now=datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc))
            manifest = json.loads((result / "manifest.json").read_text())
            self.assertEqual(manifest["database_schema_version"], 0)

    def test_snapshot_install_validates_digest_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            artifact = root / "snapshot.json"
            card = {
                "id": "def:x::KIP126.X", "kind": "definition", "title": "X",
                "statement": "An X.", "declaration": "KIP126.X",
                "source_status": "local", "lean": {
                    "file": "KIP126/X.lean", "line": 1,
                    "source": "def X : True := True.intro", "truncated": False,
                },
            }
            nl_digest, lean_digest, fingerprint = _content_fingerprint(card, None)
            card.update({
                "nl_digest": nl_digest, "lean_digest": lean_digest,
                "fingerprint": fingerprint,
                "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                "fingerprints": {CURRENT_FINGERPRINT_SCHEME: fingerprint},
            })
            payload = {"schema": SNAPSHOT_SCHEMA, "source_commit": "a" * 40,
                       "source_dirty": False, "dependency_lock_digest": None,
                       "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                       "cards": [card], "unlinked_nodes": 0}
            payload["digest"] = calculate_snapshot_digest(payload)
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            installed, comparison = install_snapshot(artifact, data)
            self.assertEqual(installed["digest"], payload["digest"])
            self.assertEqual(comparison, {"unchanged": 0, "changed": 0, "added": 1, "removed": 0})
            before = (data / "snapshot.json").read_bytes()

            payload["cards"][0]["fingerprint"] = "0" * 64
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest"):
                install_snapshot(artifact, data)
            self.assertEqual((data / "snapshot.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
