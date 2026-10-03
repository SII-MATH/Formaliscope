from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from .build import CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA
from .database import initialize
from .judgments import (admin_summary, catalog, reviewer_export, reviewer_profile,
                        submit, update_reviewer_profile)


class JudgmentReadModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "review.sqlite3"
        initialize(self.db)
        self.card = {
            "id": "statement::X", "fingerprint": "a" * 64,
            "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
            "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "a" * 64},
            "label": "X", "title": "X", "chapter": "Test", "kind": "def",
            "declaration": "X", "source_status": "local",
        }
        self.snapshot = {
            "schema": SNAPSHOT_SCHEMA, "digest": "b" * 64,
            "source_commit": "c" * 40, "unlinked_nodes": 0,
            "review_mode": "statement", "cards": [self.card],
        }

    def tearDown(self):
        self.temp.cleanup()

    def record(self, reviewer, *, snapshot=None, verdict="aligned"):
        source = snapshot or self.snapshot
        status, result = submit(source, self.db, reviewer, {
            "request_id": str(uuid.uuid4()), "card_id": self.card["id"],
            "fingerprint": source["cards"][0]["fingerprint"],
            "verdict": verdict, "rationale": "",
        })
        self.assertEqual(status, 201)
        return result["judgment"]

    def test_profile_rename_preserves_identity_and_preview_admin(self):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO reviewer_profiles VALUES (?, ?, ?)",
                       ("alice@example.org", "Alice", 1))
        status, payload = update_reviewer_profile(self.db, "alice@example.org", "  新姓名  ")
        self.assertEqual((status, payload), (200, {"display_name": "新姓名"}))
        profile = reviewer_profile(self.db, "alice@example.org")
        self.assertEqual(profile["preview_admin"], 1)
        self.assertEqual(profile["reviewer"], "alice@example.org")
        update_reviewer_profile(self.db, "bob@example.org", "同名")
        self.assertEqual(reviewer_profile(self.db, "bob@example.org")["preview_admin"], 0)
        for invalid in ("  ", "x" * 61, None, 1):
            self.assertEqual(update_reviewer_profile(self.db, "alice@example.org", invalid)[0], 400)
        self.assertEqual(reviewer_profile(self.db, "alice@example.org")["display_name"], "新姓名")

    def test_export_is_reviewer_scoped_and_keeps_old_versions(self):
        first = self.record("alice@example.org")
        self.record("bob@example.org", verdict="uncertain")
        changed_card = {**self.card, "fingerprint": "d" * 64,
                        "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "d" * 64}}
        changed = {**self.snapshot, "digest": "e" * 64, "cards": [changed_card]}
        second = self.record("alice@example.org", snapshot=changed, verdict="misaligned")
        exported = reviewer_export(changed, self.db, "alice@example.org")
        self.assertEqual([row["id"] for row in exported["judgments"]], [first["id"], second["id"]])
        self.assertEqual({row["reviewer"] for row in exported["judgments"]}, {"alice@example.org"})
        self.assertEqual(exported["snapshot_digest"], changed["digest"])
        self.assertEqual(exported["judgments"][0]["snapshot_digest"], self.snapshot["digest"])
        self.assertEqual(reviewer_export(changed, self.db, "nobody@example.org")["judgments"], [])

    def test_admin_summary_selects_current_content_without_losing_history(self):
        self.record("alice@example.org")
        self.record("bob@example.org", verdict="uncertain")
        changed_card = {**self.card, "fingerprint": "d" * 64,
                        "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "d" * 64}}
        changed = {**self.snapshot, "cards": [changed_card]}
        new = self.record("alice@example.org", snapshot=changed, verdict="misaligned")
        update_reviewer_profile(self.db, "alice@example.org", "Alice")
        current = admin_summary(changed, self.db)
        self.assertEqual(current["history_count"], 3)
        self.assertEqual(current["stale_count"], 2)
        self.assertEqual([row["id"] for row in current["judgments"]], [new["id"]])
        self.assertEqual(current["judgments"][0]["display_name"], "Alice")
        restored = admin_summary(self.snapshot, self.db)
        self.assertEqual(len(restored["judgments"]), 2)
        self.assertEqual(restored["stale_count"], 1)
        removed = admin_summary({**self.snapshot, "cards": []}, self.db)
        self.assertEqual(removed["judgments"], [])
        self.assertEqual(removed["stale_count"], 3)

    def test_catalog_enrichment_is_lightweight_and_preserves_full_detail(self):
        annotation = {
            "classification": {"role": "model", "topics": ["sphere"]},
            "priority": {"level": "p1"}, "provenance": {"method": "agent_manual"},
            "readback": {"status": "draft", "text_zh": "回译", "unresolved": []},
            "evidence": [{"excerpt": "source evidence"}], "basis": {"source_sha256": "f" * 64},
        }
        card = {**self.card, "title_zh": "中文标题", "reading_summary_zh": "阅读摘要",
                "enrichment": annotation}
        source = {**self.snapshot, "cards": [card]}
        original = copy.deepcopy(source)
        row = catalog(source, self.db, "alice@example.org")["cards"][0]
        self.assertEqual(row["title_zh"], "中文标题")
        self.assertEqual(row["reading_summary_zh"], "阅读摘要")
        self.assertEqual(row["enrichment"]["classification"], annotation["classification"])
        self.assertEqual(row["enrichment"]["readback"], {"status": "draft"})
        self.assertNotIn("basis", row["enrichment"])
        self.assertNotIn("evidence", row["enrichment"])
        self.assertEqual(source, original)


if __name__ == "__main__":
    unittest.main()
