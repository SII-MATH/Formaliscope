from __future__ import annotations

import tempfile
import sqlite3
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from .build import compile_snapshot
from .server import catalog, history, initialize, submit


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
            with patch("review_app.build._git_head", return_value="0" * 40):
                snapshot = compile_snapshot(source)
        self.assertEqual(len(snapshot["cards"]), 1)
        card = snapshot["cards"][0]
        self.assertEqual(card["source_status"], "local")
        self.assertEqual(card["id"], "def:sample::KIP126.Sample.foo")
        self.assertIn("theorem foo", card["lean"]["source"])


class JudgmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "review.sqlite3"
        initialize(self.db)
        self.card = {
            "id": "def:x::KIP126.X", "fingerprint": "a" * 64,
            "label": "def:x", "title": "Example", "chapter": "Test",
            "kind": "definition", "declaration": "KIP126.X", "source_status": "local",
        }
        self.snapshot = {"digest": "b" * 64, "source_commit": "c" * 40,
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
        changed = {**self.snapshot, "cards": [{**self.card, "fingerprint": "d" * 64}]}
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

    def test_migrates_existing_judgments_without_losing_rows(self):
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE judgments")
            db.execute("""CREATE TABLE judgments (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            db.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), self.payload["request_id"], self.card["id"],
                        self.card["fingerprint"], self.reviewer, "aligned", "", "2026-09-18T00:00:00Z"))
        initialize(self.db)
        self.assertEqual(len(history(self.db, self.card["id"], self.reviewer)), 1)
        self.assertEqual(submit(self.snapshot, self.db, "other@example.org", self.payload)[0], 201)


if __name__ == "__main__":
    unittest.main()
