from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .auth import AuthSettings, AuthStore
from .server import initialize
from .storage import create_backup


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
                db.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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


if __name__ == "__main__":
    unittest.main()
