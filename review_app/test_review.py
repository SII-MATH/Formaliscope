from __future__ import annotations

import tempfile
import sqlite3
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from .build import (CURRENT_FINGERPRINT_SCHEME, LEGACY_FINGERPRINT_SCHEME,
                    LEGACY_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA,
                    calculate_snapshot_digest, compare_snapshots, compile_snapshot,
                    normalize_snapshot)
from .server import (DB_SCHEMA_VERSION, backfill_review_basis, catalog, history,
                     initialize, submit)


class SnapshotTests(unittest.TestCase):
    def test_build_reads_an_external_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "blueprint/src").mkdir(parents=True)
            (source / "KIP126").mkdir()
            (source / "blueprint/src/content.tex").write_text("\\input{chapter}\n")
            (source / "blueprint/src/chapter.tex").write_text(
                "\\chapter{Example}\n"
                "\\begin{definition}[A sample]\\label{def:sample}"
                "A natural language statement.\\lean{KIP126.Sample.foo}"
                "\\end{definition}\n"
            )
            (source / "KIP126/Sample.lean").write_text(
                "namespace KIP126.Sample\n"
                "theorem foo : True := by trivial\n"
                "end KIP126.Sample\n"
            )
            with patch("review_app.build._git_head", return_value="0" * 40), \
                    patch("review_app.build._git_dirty", return_value=False):
                snapshot = compile_snapshot(source)
        self.assertEqual(len(snapshot["cards"]), 1)
        card = snapshot["cards"][0]
        self.assertEqual(card["source_status"], "local")
        self.assertEqual(card["id"], "def:sample::KIP126.Sample.foo")
        self.assertIn("theorem foo", card["lean"]["source"])
        self.assertEqual(snapshot["schema"], SNAPSHOT_SCHEMA)

    def test_grouped_blueprint_links_resolve_local_and_external_names(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'blueprint/src').mkdir(parents=True)
            (source / 'KIP126').mkdir()
            (source / 'blueprint/src/content.tex').write_text('\\input{chapter}\n')
            (source / 'blueprint/src/chapter.tex').write_text(
                '\\begin{definition}[A sample]\\label{def:sample}'
                'Shared prose.\\lean{ KIP126.Sample.foo,\n Nat.succ, }'
                '\\end{definition}\n')
            (source / 'KIP126/Sample.lean').write_text(
                'namespace KIP126.Sample\n'
                'theorem foo : True := by trivial\nend KIP126.Sample\n')
            with patch('review_app.build._git_head', return_value='0' * 40), \
                    patch('review_app.build._git_dirty', return_value=False):
                snapshot = compile_snapshot(source)
        cards = {card['declaration']: card for card in snapshot['cards']}
        self.assertEqual(set(cards), {'KIP126.Sample.foo', 'Nat.succ'})
        self.assertEqual(cards['KIP126.Sample.foo']['source_status'], 'local')
        self.assertEqual(cards['Nat.succ']['source_status'], 'external')
        self.assertEqual(cards['KIP126.Sample.foo']['id'], 'def:sample::KIP126.Sample.foo')

    def test_review_basis_survives_unrelated_version_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "blueprint/src").mkdir(parents=True)
            (source / "KIP126").mkdir()
            (source / "blueprint/src/content.tex").write_text("\\input{chapter}\n")
            chapter = source / "blueprint/src/chapter.tex"
            lean = source / "KIP126/Sample.lean"
            chapter.write_text(
                "\\chapter{Example}\n\\begin{definition}[A sample]"
                "\\label{def:sample}A statement.\\lean{KIP126.Sample.foo}"
                "\\end{definition}\n")
            lean.write_text("namespace KIP126.Sample\ntheorem foo : True := by trivial\nend KIP126.Sample\n")
            with patch("review_app.build._git_head", return_value="0" * 40), \
                    patch("review_app.build._git_dirty", return_value=False):
                before = compile_snapshot(source)
            chapter.write_text(
                "\\chapter{Example}\n\n\\begin{definition}[A sample]"
                "\\label{def:sample}A statement.\\lean{KIP126.Sample.foo}\\leanok"
                "\\end{definition}\n")
            lean.write_text("namespace KIP126.Sample\n\ntheorem foo : True := by trivial\nend KIP126.Sample\n")
            with patch("review_app.build._git_head", return_value="1" * 40), \
                    patch("review_app.build._git_dirty", return_value=False):
                after = compile_snapshot(source)
            self.assertNotEqual(before["source_commit"], after["source_commit"])
            self.assertNotEqual(before["cards"][0]["lean"]["line"], after["cards"][0]["lean"]["line"])
            self.assertEqual(before["cards"][0]["fingerprint"], after["cards"][0]["fingerprint"])
            self.assertEqual(compare_snapshots(before, after),
                             {"unchanged": 1, "changed": 0, "added": 0, "removed": 0})

            chapter.write_text(chapter.read_text().replace("A statement.", "A changed statement."))
            with patch("review_app.build._git_head", return_value="2" * 40), \
                    patch("review_app.build._git_dirty", return_value=False):
                nl_changed = compile_snapshot(source)
            self.assertNotEqual(after["cards"][0]["fingerprint"], nl_changed["cards"][0]["fingerprint"])

            chapter.write_text(chapter.read_text().replace("A changed statement.", "A statement."))
            lean.write_text(lean.read_text().replace(": True", ": False"))
            with patch("review_app.build._git_head", return_value="3" * 40), \
                    patch("review_app.build._git_dirty", return_value=False):
                lean_changed = compile_snapshot(source)
            self.assertNotEqual(after["cards"][0]["fingerprint"], lean_changed["cards"][0]["fingerprint"])

    def test_v1_snapshot_is_upgraded_in_memory_without_rewriting_its_digest(self):
        card = {
            "id": "def:sample::KIP126.Sample.foo", "label": "def:sample",
            "kind": "definition", "title": "A sample", "chapter": "Example",
            "statement": "A statement.", "declaration": "KIP126.Sample.foo",
            "source_status": "local", "fingerprint": "a" * 64,
            "lean": {"file": "KIP126/Sample.lean", "line": 19,
                     "source": "theorem foo : True := by trivial", "truncated": False},
        }
        legacy = {"schema": LEGACY_SNAPSHOT_SCHEMA, "source_commit": "0" * 40,
                  "unlinked_nodes": 0, "cards": [card]}
        legacy["digest"] = calculate_snapshot_digest(legacy)
        normalized = normalize_snapshot(legacy)
        upgraded = normalized["cards"][0]
        self.assertEqual(legacy["cards"][0]["fingerprint"], "a" * 64)
        self.assertEqual(upgraded["fingerprints"][LEGACY_FINGERPRINT_SCHEME], "a" * 64)
        self.assertEqual(upgraded["fingerprint_scheme"], CURRENT_FINGERPRINT_SCHEME)
        self.assertNotEqual(upgraded["fingerprint"], "a" * 64)


class JudgmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "review.sqlite3"
        initialize(self.db)
        self.card = {
            "id": "def:x::KIP126.X", "fingerprint": "a" * 64,
            "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
            "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "a" * 64},
            "label": "def:x", "title": "Example", "chapter": "Test",
            "kind": "definition", "declaration": "KIP126.X", "source_status": "local",
        }
        self.snapshot = {"schema": SNAPSHOT_SCHEMA, "digest": "b" * 64,
                         "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                         "source_dirty": False, "source_commit": "c" * 40,
                         "unlinked_nodes": 0, "cards": [self.card]}
        self.reviewer = "reviewer@example.org"
        self.payload = {
            "request_id": str(uuid.uuid4()), "card_id": self.card["id"],
            "fingerprint": self.card["fingerprint"],
            "verdict": "partial", "rationale": "Missing hypothesis",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotent_retry_and_conflicting_reuse(self):
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, self.payload)[0], 201)
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, self.payload)[0], 200)
        self.assertEqual(len(history(self.db, self.card["id"], self.reviewer)), 1)
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer,
                                {**self.payload, "rationale": "Changed"})[0], 409)

    def test_changed_source_invalidates_old_judgment(self):
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, self.payload)[0], 201)
        changed_card = {**self.card, "fingerprint": "d" * 64,
                        "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "d" * 64}}
        changed = {**self.snapshot, "cards": [changed_card]}
        row = catalog(changed, self.db, self.reviewer)["cards"][0]
        self.assertIsNone(row["verdict"])
        self.assertTrue(row["stale"])
        self.assertEqual(submit(changed, self.db, self.reviewer,
                                {**self.payload, "request_id": str(uuid.uuid4())})[0], 409)

    def test_non_aligned_judgment_requires_reason(self):
        result = submit(self.snapshot, self.db, self.reviewer, {**self.payload, "rationale": ""})
        self.assertEqual(result[0], 400)
        self.assertEqual(history(self.db, self.card["id"], self.reviewer), [])

    def test_catalog_can_include_initial_evidence(self):
        value = catalog(self.snapshot, self.db, self.reviewer, initial_id="auto")
        self.assertEqual(value["initial_evidence"]["id"], self.card["id"])
        self.assertEqual(catalog(self.snapshot, self.db, self.reviewer)["cards"][0]["id"], self.card["id"])

    def test_concurrent_writes_are_all_durable(self):
        payloads = [(f"reviewer{number}@example.org", {**self.payload, "request_id": str(uuid.uuid4())})
                    for number in range(16)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda item: submit(self.snapshot, self.db, *item), payloads))
        self.assertTrue(all(code == 201 for code, _ in results))
        for reviewer, _ in payloads:
            self.assertEqual(len(history(self.db, self.card["id"], reviewer)), 1)

    def test_same_request_id_is_independent_between_reviewers(self):
        other = "other@example.org"
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, self.payload)[0], 201)
        self.assertEqual(submit(self.snapshot, self.db, other, self.payload)[0], 201)
        self.assertEqual(len(history(self.db, self.card["id"], self.reviewer)), 1)
        self.assertEqual(len(history(self.db, self.card["id"], other)), 1)

    def test_review_is_reused_when_content_returns_to_an_older_version(self):
        self.assertEqual(submit(self.snapshot, self.db, self.reviewer, self.payload)[0], 201)
        changed_card = {**self.card, "fingerprint": "d" * 64,
                        "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "d" * 64}}
        changed = {**self.snapshot, "digest": "e" * 64, "cards": [changed_card]}
        changed_payload = {**self.payload, "request_id": str(uuid.uuid4()),
                           "fingerprint": "d" * 64, "verdict": "aligned", "rationale": ""}
        self.assertEqual(submit(changed, self.db, self.reviewer, changed_payload)[0], 201)
        restored = catalog(self.snapshot, self.db, self.reviewer)["cards"][0]
        self.assertEqual(restored["verdict"], "partial")
        self.assertFalse(restored["stale"])

    def test_migrates_existing_judgments_without_losing_rows(self):
        legacy_db = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_db) as db:
            db.execute("""CREATE TABLE judgments (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            db.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), self.payload["request_id"], self.card["id"],
                        self.card["fingerprint"], self.reviewer, "aligned", "", "2026-09-18T00:00:00Z"))
        initialize(legacy_db)
        rows = history(legacy_db, self.card["id"], self.reviewer)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fingerprint_scheme"], "kip126-review-legacy.v1")
        self.assertEqual(submit(self.snapshot, legacy_db, "other@example.org", self.payload)[0], 201)
        with sqlite3.connect(legacy_db) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                             DB_SCHEMA_VERSION)

    def test_backfills_old_judgment_with_stable_content_basis(self):
        legacy_db = Path(self.temp.name) / "backfill.sqlite3"
        with sqlite3.connect(legacy_db) as db:
            db.execute("""CREATE TABLE judgments (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(reviewer, request_id)
            )""")
            db.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), self.payload["request_id"], self.card["id"],
                        "f" * 64, self.reviewer, "aligned", "", "2026-09-18T00:00:00Z"))
        initialize(legacy_db)
        legacy_card = {
            **self.card,
            "fingerprint": "f" * 64,
            "statement": "A statement.",
            "lean": {"file": "KIP126/X.lean", "line": 10,
                     "source": "theorem X : True := by trivial", "truncated": False},
        }
        legacy_card.pop("fingerprint_scheme")
        legacy_card.pop("fingerprints")
        legacy = {"schema": LEGACY_SNAPSHOT_SCHEMA, "source_commit": "0" * 40,
                  "unlinked_nodes": 0, "cards": [legacy_card]}
        legacy["digest"] = calculate_snapshot_digest(legacy)
        normalized = normalize_snapshot(legacy)
        self.assertEqual(backfill_review_basis(legacy_db, normalized), 1)
        row = catalog(normalized, legacy_db, self.reviewer)["cards"][0]
        self.assertEqual(row["verdict"], "aligned")
        self.assertFalse(row["stale"])
        stored = history(legacy_db, self.card["id"], self.reviewer)[0]
        self.assertEqual(stored["fingerprint_scheme"], LEGACY_FINGERPRINT_SCHEME)
        self.assertEqual(stored["review_basis_scheme"], CURRENT_FINGERPRINT_SCHEME)

    def test_migrates_the_pre_versioned_composite_request_schema(self):
        old_db = Path(self.temp.name) / "pre-versioned.sqlite3"
        with sqlite3.connect(old_db) as db:
            db.execute("""CREATE TABLE judgments (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(reviewer, request_id)
            )""")
            db.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("j1", "r1", self.card["id"], "f" * 64, self.reviewer,
                        "aligned", "", "2026-09-18T00:00:00Z"))
        initialize(old_db)
        with sqlite3.connect(old_db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                             DB_SCHEMA_VERSION)
            columns = {row[1] for row in db.execute("PRAGMA table_info(judgments)")}
        self.assertIn("review_basis_fingerprint", columns)

    def test_refuses_a_database_from_a_newer_application(self):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                       (DB_SCHEMA_VERSION + 1, "future", "2026-09-19T00:00:00Z"))
        with self.assertRaisesRegex(ValueError, "newer"):
            initialize(self.db)


if __name__ == "__main__":
    unittest.main()
