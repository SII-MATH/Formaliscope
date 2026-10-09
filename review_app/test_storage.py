from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .auth import AuthSettings, AuthStore
from .build import (CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA,
                    _content_fingerprint, calculate_snapshot_digest)
from .server import DB_SCHEMA_VERSION, catalog, initialize, history
from .name_auth import NameAuthStore
from .storage import BACKUP_SCHEMA, create_backup, install_snapshot, prune_backups, verify_backup


def _snapshot(commit: str, statement: str) -> dict:
    card = {
        "id": "def:x::KIP126.X", "kind": "definition", "title": "X",
        "statement": statement, "declaration": "KIP126.X", "source_status": "local",
        "lean": {"file": "KIP126/X.lean", "line": 1,
                 "source": "def X : True := True.intro", "truncated": False},
    }
    nl_digest, lean_digest, fingerprint = _content_fingerprint(card, None)
    card.update({"nl_digest": nl_digest, "lean_digest": lean_digest, "fingerprint": fingerprint,
                 "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                 "fingerprints": {CURRENT_FINGERPRINT_SCHEME: fingerprint}})
    payload = {"schema": SNAPSHOT_SCHEMA, "source_commit": commit, "source_dirty": False,
               "dependency_lock_digest": None, "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
               "cards": [card], "unlinked_nodes": 0}
    payload["digest"] = calculate_snapshot_digest(payload)
    return payload


class StorageTests(unittest.TestCase):
    @staticmethod
    def _data(root: Path, name: str = "data") -> Path:
        data = root / name
        initialize(data / "judgments.sqlite3")
        (data / "snapshot.json").write_text(json.dumps({"digest": "a" * 64, "source_commit": "b" * 40}))
        return data

    @staticmethod
    def _at(day: int) -> datetime:
        return datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc)

    def test_manifest_hashes_are_portable_and_detect_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            result = create_backup(data, root / "backups", now=self._at(1))
            manifest = verify_backup(result)
            self.assertEqual(manifest["schema"], BACKUP_SCHEMA)
            self.assertEqual(manifest["source_data_dir"], str(data.resolve()))
            for name in ("snapshot.json", "judgments.sqlite3"):
                copied = (result / name).read_bytes()
                self.assertEqual(manifest["files"][name], {
                    "sha256": hashlib.sha256(copied).hexdigest(), "size_bytes": len(copied)})
            moved = root / "another-machine" / "received"
            shutil.copytree(result, moved)
            shutil.rmtree(data)
            self.assertEqual(verify_backup(moved), manifest)
            (moved / "snapshot.json").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "hash or size mismatch"):
                verify_backup(moved)
            manifest["snapshot_digest"] = "wrong"
            (result / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "snapshot metadata mismatch"):
                verify_backup(result)

    def test_backup_reads_snapshot_source_once_for_copy_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            source = data / "snapshot.json"
            original = source.read_bytes()
            actual_read = Path.read_bytes
            reads = []

            def read(path):
                content = actual_read(path)
                if path == source:
                    reads.append(path)
                    source.write_text(json.dumps({"digest": "different", "source_commit": "different"}))
                return content

            with patch.object(Path, "read_bytes", read):
                result = create_backup(data, root / "backups", now=self._at(1))
            self.assertEqual(reads, [source])
            self.assertEqual((result / "snapshot.json").read_bytes(), original)
            self.assertEqual(verify_backup(result)["snapshot_digest"], json.loads(original)["digest"])

    def test_backup_keeps_password_hash_without_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            auth = NameAuthStore(data / "judgments.sqlite3")
            token, _ = auth.create_identity("Reviewer", admin=True, password="private-password")
            reviewer = auth.session_email(token)
            (data / "private-password.txt").write_text("private-password")
            result = create_backup(data, root / "backups", now=self._at(1))
            self.assertEqual({path.name for path in result.iterdir()},
                             {"snapshot.json", "judgments.sqlite3", "manifest.json"})
            content = (result / "judgments.sqlite3").read_bytes()
            for secret in (token.encode(), b"private-password", auth.pepper):
                self.assertNotIn(secret, content)
            with sqlite3.connect(result / "judgments.sqlite3") as db:
                row = db.execute("SELECT p.password_hash, i.is_admin FROM name_identities i JOIN password_credentials p ON p.reviewer=i.reviewer WHERE i.reviewer=?",
                                 (reviewer,)).fetchone()
                from .passwords import verify_password
                self.assertTrue(verify_password("private-password", row[0]))
                self.assertEqual(row[1], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM login_sessions").fetchone()[0], 0)

    def test_backup_waits_until_snapshot_install_finishes_backfill_and_replace(self):
        from .judgments import backfill_review_basis
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            old = _snapshot("a" * 40, "Old X.")
            new = _snapshot("b" * 40, "New X.")
            (data / "snapshot.json").write_text(json.dumps(old))
            artifact = root / "candidate.json"
            artifact.write_text(json.dumps(new))
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            results, failures = [], []

            def pause_backfill(database, previous):
                backfill_review_basis(database, previous)
                with sqlite3.connect(database) as db:
                    db.execute("INSERT INTO reviewer_profiles (reviewer, display_name) VALUES (?, ?)",
                               ("generation", new["source_commit"]))
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release install")

            def install():
                try:
                    install_snapshot(artifact, data)
                except BaseException as error:
                    failures.append(error)

            def backup():
                try:
                    results.append(create_backup(data, root / "backups", now=self._at(1)))
                except BaseException as error:
                    failures.append(error)
                finally:
                    finished.set()

            with patch("review_app.judgments.backfill_review_basis", side_effect=pause_backfill):
                installer = threading.Thread(target=install)
                copier = threading.Thread(target=backup)
                installer.start()
                try:
                    self.assertTrue(entered.wait(5), "install did not reach backfill")
                    copier.start()
                    self.assertFalse(finished.wait(0.15), "backup interleaved with snapshot installation")
                finally:
                    release.set()
                    installer.join(5)
                    if copier.ident is not None:
                        copier.join(5)
            self.assertFalse(installer.is_alive())
            self.assertFalse(copier.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(verify_backup(results[0])["snapshot_digest"], new["digest"])
            with sqlite3.connect(results[0] / "judgments.sqlite3") as db:
                self.assertEqual(db.execute("SELECT display_name FROM reviewer_profiles WHERE reviewer='generation'").fetchone()[0],
                                 new["source_commit"])

    def test_retention_only_prunes_verified_backups_from_this_data_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            output = root / "backups"
            own = [create_backup(data, output, now=self._at(day)) for day in (1, 2, 3)]
            self.assertTrue(all(path.exists() for path in own), "default retention must keep all")
            other = create_backup(self._data(root, "other-data"), output, now=self._at(4))
            unknown = output / "20260905T120000Z"
            unknown.mkdir()
            (unknown / "operator.txt").write_text("keep")
            temporary = output / ".20260906T120000Z.tmp"
            temporary.mkdir()
            incomplete = output / "20260907T120000Z"
            incomplete.mkdir()
            (incomplete / "manifest.json").write_text("{}")
            def old_copy(day):
                created = datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc)
                destination = output / created.strftime("%Y%m%dT%H%M%SZ")
                shutil.copytree(own[0], destination)
                metadata = json.loads((destination / "manifest.json").read_text())
                metadata["created_at"] = created.isoformat()
                (destination / "manifest.json").write_text(json.dumps(metadata))
                # This is an eligible old backup before the individual
                # corruption/unknown-data change below disqualifies it.
                verify_backup(destination, data_dir=data)
                return destination

            offsite = create_backup(data, root / "offsite", now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
            link = output / offsite.name
            link.symlink_to(offsite, target_is_directory=True)
            legacy = old_copy(1)
            manifest = json.loads((legacy / "manifest.json").read_text())
            manifest["schema"] = "kip126-review-backup.v1"
            manifest.pop("source_data_dir")
            (legacy / "manifest.json").write_text(json.dumps(manifest))
            corrupt = old_copy(2)
            (corrupt / "snapshot.json").write_text("invalid")
            file_link = old_copy(3)
            (file_link / "snapshot.json").unlink()
            (file_link / "snapshot.json").symlink_to(data / "snapshot.json")
            extra = old_copy(4)
            (extra / "notes.txt").write_text("operator data")

            self.assertEqual(prune_backups(data, output, keep=2), [own[0]])
            self.assertFalse(own[0].exists())
            for path in (*own[1:], other, unknown, temporary, incomplete, legacy, corrupt, file_link, extra):
                self.assertTrue(path.exists(), path)
            self.assertTrue(link.is_symlink())
            self.assertTrue(offsite.exists())
            self.assertEqual((unknown / "operator.txt").read_text(), "keep")

    def test_retention_preserves_new_backup_even_if_clock_moves_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            output = root / "backups"
            previous = [create_backup(data, output, now=self._at(day)) for day in (20, 21, 22)]
            newest = create_backup(data, output, now=self._at(19), keep=2)
            self.assertEqual({entry.name for entry in output.iterdir()}, {newest.name, previous[-1].name})

    def test_failed_backup_or_invalid_retention_never_deletes_existing_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self._data(root)
            output = root / "backups"
            previous = [create_backup(data, output, now=self._at(day)) for day in (1, 2, 3)]
            contents = {path: (path / "manifest.json").read_bytes() for path in previous}
            with patch("review_app.storage.verify_backup", side_effect=ValueError("verification failed")):
                with self.assertRaisesRegex(ValueError, "verification failed"):
                    create_backup(data, output, now=self._at(4), keep=1)
            for invalid in (0, -1, True, "1"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    create_backup(data, output, now=self._at(5), keep=invalid)
            self.assertEqual(set(output.iterdir()), set(previous))
            for path, original in contents.items():
                self.assertEqual((path / "manifest.json").read_bytes(), original)

    def test_daily_helper_supports_old_cli_and_configurable_new_retention(self):
        script = (Path(__file__).parents[1] / "deploy/formaliscope-review-backup").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "fake-python"
            interpreter.write_text("""#!/usr/bin/env bash
if [[ "$*" == *"backup --help"* ]]; then
  if [[ "$BACKUP_TEST_VERSION" == new ]]; then
    printf '%s\\n' 'usage: backup --data-dir DIR --output DIR --keep KEEP'
  else
    printf '%s\\n' 'usage: backup --data-dir DIR --output DIR'
  fi
else
  printf '%s\\n' "$@" > "$BACKUP_TEST_ARGS"
fi
""")
            interpreter.chmod(0o700)
            (root / "id").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$BACKUP_TEST_UID"\n')
            (root / "id").chmod(0o700)
            (root / "runuser").write_text('#!/usr/bin/env bash\nshift 3\nexec "$@"\n')
            (root / "runuser").chmod(0o700)
            helper = root / "daily-backup"
            helper.write_text(script.replace("cd /opt/formaliscope/current", "cd " + shlex.quote(str(root)))
                              .replace("/usr/bin/python3", shlex.quote(str(interpreter))))
            arguments = root / "arguments.txt"
            for version in ("old", "new"):
                for uid in ("0", "1000"):
                    with self.subTest(version=version, uid=uid):
                        env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                               "BACKUP_TEST_VERSION": version, "BACKUP_TEST_UID": uid,
                               "BACKUP_TEST_ARGS": str(arguments), "REVIEW_BACKUP_KEEP": "17",
                               "REVIEW_DATA_DIR": str(root / "data"), "REVIEW_BACKUP_DIR": str(root / "output")}
                        result = subprocess.run(["bash", str(helper)], env=env, text=True, capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        actual = arguments.read_text().splitlines()
                        expected = ["-m", "review_app", "backup", "--data-dir", str(root / "data"),
                                    "--output", str(root / "output")]
                        if version == "new":
                            self.assertEqual(actual, expected + ["--keep", "17"])
                            self.assertEqual(result.stderr, "")
                        else:
                            self.assertEqual(actual, expected)
                            self.assertIn("does not support backup retention", result.stderr)

    def test_unverified_archive_needs_explicit_development_override(self):
        from .statements import compile_statements
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'source/KIP126').mkdir(parents=True)
            (root/'source/KIP126/Example.lean').write_text('def value : Nat := 1\n')
            payload = compile_statements(root/'source', source_commit='a'*40)
            artifact = root/'snapshot.json'
            artifact.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                install_snapshot(artifact, root/'production')
            self.assertFalse((root/'production').exists())
            installed, _ = install_snapshot(artifact, root/'preview', allow_dirty_source=True)
            self.assertEqual(installed['source_origin'], 'archive-unverified')

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

    def test_snapshot_install_backfills_before_replacing_a_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            old_card = {
                "id": "def:x::KIP126.X", "label": "def:x", "kind": "definition",
                "title": "X", "chapter": "Test", "statement": "An X.",
                "declaration": "KIP126.X", "source_status": "local",
                "blueprint_file": "chapter.tex", "blueprint_line": 4,
                "lean": {"file": "KIP126/X.lean", "line": 10,
                         "source": "theorem X : True := by trivial", "truncated": False},
                "dependencies": [], "fingerprint": "l" * 64,
            }
            old = {"schema": "kip126-review-snapshot.v1", "source_commit": "0" * 40,
                   "unlinked_nodes": 0, "cards": [old_card]}
            old["digest"] = calculate_snapshot_digest(old)
            (data / "snapshot.json").write_text(json.dumps(old), encoding="utf-8")
            with sqlite3.connect(data / "judgments.sqlite3") as db:
                db.execute("""CREATE TABLE judgments (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                    rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(reviewer, request_id))""")
                db.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           ("j1", "r1", old_card["id"], old_card["fingerprint"],
                            "alice@example.org", "aligned", "", "2026-09-19T00:00:00Z"))

            new_card = dict(old_card)
            new_card["lean"] = {**old_card["lean"], "line": 20}
            nl_digest, lean_digest, fingerprint = _content_fingerprint(new_card, None)
            new_card.update({
                "nl_digest": nl_digest, "lean_digest": lean_digest,
                "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                "fingerprint": fingerprint,
                "fingerprints": {CURRENT_FINGERPRINT_SCHEME: fingerprint},
            })
            new = {"schema": SNAPSHOT_SCHEMA, "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                   "source_commit": "1" * 40, "source_dirty": False,
                   "dependency_lock_digest": None, "unlinked_nodes": 0,
                   "cards": [new_card]}
            new["digest"] = calculate_snapshot_digest(new)
            artifact = root / "new.json"
            artifact.write_text(json.dumps(new), encoding="utf-8")
            _, comparison = install_snapshot(artifact, data)
            self.assertEqual(comparison["unchanged"], 1)
            normalized = json.loads((data / "snapshot.json").read_text())
            self.assertEqual(catalog(normalized, data / "judgments.sqlite3", "alice@example.org")["cards"][0]["verdict"], "aligned")
            self.assertEqual(history(data / "judgments.sqlite3", old_card["id"], "alice@example.org")[0]["review_basis_scheme"], CURRENT_FINGERPRINT_SCHEME)


if __name__ == "__main__":
    unittest.main()
