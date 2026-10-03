from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .build import calculate_snapshot_digest
from .preflight import run_preflight
from .statements import compile_statements


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "source" / "KIP126").mkdir(parents=True)
        (self.root / "source" / "KIP126" / "Sample.lean").write_text("def sample : Nat := 1\n")
        self.data = self.root / "data"
        self.data.mkdir()
        self.snapshot = compile_statements(self.root / "source", source_commit="a" * 40)
        self.snapshot.pop("source_origin", None)
        self.write_snapshot()
        self.env = {"REVIEW_PUBLIC_ORIGIN": "https://review.example.org",
                    "REVIEW_COOKIE_PATH": "/review/", "REVIEW_MAILER": "smtp",
                    "REVIEW_SMTP_HOST": "smtp.example.org", "REVIEW_SMTP_USER": "sender",
                    "REVIEW_SMTP_PASSWORD": "unique-hidden-secret", "REVIEW_SMTP_FROM": "sender@example.org",
                    "REVIEW_ALLOW_ANY_EMAIL": "1", "REVIEW_ADMIN_EMAILS": "admin@example.org"}

    def tearDown(self):
        self.temp.cleanup()

    def write_snapshot(self):
        self.snapshot["digest"] = calculate_snapshot_digest(self.snapshot)
        (self.data / "snapshot.json").write_text(json.dumps(self.snapshot))

    def test_production_is_read_only_and_does_not_send_mail(self):
        before = {path.name: path.read_bytes() for path in self.data.iterdir()}
        with patch("review_app.auth.smtp_send", side_effect=AssertionError("mail must not be sent")):
            report = run_preflight(self.data, env=self.env)
        self.assertTrue(report["ready"], report)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.data.iterdir()})
        self.assertNotIn("unique-hidden-secret", json.dumps(report))
        self.assertNotIn("admin@example.org", json.dumps(report))

    def test_preview_needs_no_mail_but_rejects_public_binding(self):
        self.snapshot["source_dirty"] = True
        self.write_snapshot()
        self.assertTrue(run_preflight(self.data, preview=True, env={})["ready"])
        self.assertFalse(run_preflight(self.data, preview=True, host="0.0.0.0", env={})["ready"])
        self.assertFalse(run_preflight(self.data, env=self.env)["ready"])

    def test_unverified_archive_is_preview_only(self):
        self.snapshot["source_origin"] = "archive-unverified"
        self.write_snapshot()
        self.assertTrue(run_preflight(self.data, preview=True, env={})["ready"])
        self.assertFalse(run_preflight(self.data, env=self.env)["ready"])

    def test_config_and_digest_failures_are_actionable_and_redacted(self):
        self.snapshot["cards"][0]["title"] = "tampered"
        (self.data / "snapshot.json").write_text(json.dumps(self.snapshot))
        env = dict(self.env, REVIEW_PUBLIC_ORIGIN="http://review.example.org", REVIEW_COOKIE_PATH="/bad;secret")
        env.pop("REVIEW_ADMIN_EMAILS")
        report = run_preflight(self.data, env=env)
        failed = {item["id"] for item in report["checks"] if not item["ok"]}
        self.assertTrue({"snapshot_integrity", "public_origin", "cookie_path"}.issubset(failed))
        self.assertNotIn("/bad;secret", json.dumps(report))

    def test_missing_referenced_module_fails_resource_check(self):
        static = self.root / "static"
        static.mkdir()
        for name in ("login.html", "statement.html", "admin.html", "index.html"):
            (static / name).write_text('<script src="./new-module.js"></script>')
        for name in ("mathjax-tex-svg.js", "MATHJAX-LICENSE.txt"):
            (static / name).write_text("resource")
        report = run_preflight(self.data, preview=True, env={}, static_dir=static)
        self.assertFalse(report["ready"])
        (static / "new-module.js").write_text("module")
        self.assertTrue(run_preflight(self.data, preview=True, env={}, static_dir=static)["ready"])

    def test_protected_smtp_file_and_admitted_admin_required(self):
        password = self.root / "mail-password"
        password.write_text("not-in-output")
        password.chmod(0o644)
        env = dict(self.env, REVIEW_SMTP_PASSWORD_FILE=str(password))
        self.assertFalse(run_preflight(self.data, env=env)["ready"])
        password.chmod(0o640)
        self.assertTrue(run_preflight(self.data, env=env)["ready"])
        env.update(REVIEW_ALLOW_ANY_EMAIL="0", REVIEW_ALLOWED_EMAILS="other@example.org")
        self.assertFalse(run_preflight(self.data, env=env)["ready"])

    def test_blueprint_only_allowed_with_explicit_compatibility(self):
        self.snapshot.pop("review_mode")
        self.write_snapshot()
        self.assertFalse(run_preflight(self.data, env=self.env)["ready"])
        self.assertTrue(run_preflight(self.data, env=self.env, require_statements=False)["ready"])

    def test_v1_existing_blueprint_compatibility_is_explicit_and_candid(self):
        self.snapshot["schema"] = "kip126-review-snapshot.v1"
        self.snapshot.pop("review_mode")
        self.snapshot.pop("source_dirty")
        self.write_snapshot()
        self.assertFalse(run_preflight(self.data, env=self.env)["ready"])
        report = run_preflight(self.data, env=self.env, require_statements=False)
        self.assertTrue(report["ready"], report)
        self.assertIn("Legacy v1", " ".join(report["limitations"]))


if __name__ == "__main__":
    unittest.main()
