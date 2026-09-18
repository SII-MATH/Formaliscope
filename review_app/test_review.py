from __future__ import annotations

import tempfile
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
        self.payload = {
            "request_id": str(uuid.uuid4()), "card_id": self.card["id"],
            "fingerprint": self.card["fingerprint"], "reviewer": "Reviewer",
            "verdict": "partial", "rationale": "Missing hypothesis",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotent_retry_and_conflicting_reuse(self):
        self.assertEqual(submit(self.snapshot, self.db, self.payload)[0], 201)
        self.assertEqual(submit(self.snapshot, self.db, self.payload)[0], 200)
        self.assertEqual(len(history(self.db, self.card["id"])), 1)
        self.assertEqual(submit(self.snapshot, self.db, {**self.payload, "rationale": "Changed"})[0], 409)

    def test_changed_source_invalidates_old_judgment(self):
        self.assertEqual(submit(self.snapshot, self.db, self.payload)[0], 201)
        changed = {**self.snapshot, "cards": [{**self.card, "fingerprint": "d" * 64}]}
        row = catalog(changed, self.db)["cards"][0]
        self.assertIsNone(row["verdict"])
        self.assertTrue(row["stale"])
        self.assertEqual(submit(changed, self.db, {**self.payload, "request_id": str(uuid.uuid4())})[0], 409)

    def test_non_aligned_judgment_requires_reason(self):
        result = submit(self.snapshot, self.db, {**self.payload, "rationale": ""})
        self.assertEqual(result[0], 400)
        self.assertEqual(history(self.db, self.card["id"]), [])

    def test_catalog_can_include_initial_evidence(self):
        value = catalog(self.snapshot, self.db, initial_id="auto")
        self.assertEqual(value["initial_evidence"]["id"], self.card["id"])
        self.assertEqual(catalog(self.snapshot, self.db)["cards"][0]["id"], self.card["id"])

    def test_concurrent_writes_are_all_durable(self):
        payloads = [
            {**self.payload, "request_id": str(uuid.uuid4()), "reviewer": f"Reviewer {number}"}
            for number in range(16)
        ]
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda item: submit(self.snapshot, self.db, item), payloads))
        self.assertTrue(all(code == 201 for code, _ in results))
        self.assertEqual(len(history(self.db, self.card["id"])), 16)


if __name__ == "__main__":
    unittest.main()
