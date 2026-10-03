from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from .build import (CURRENT_FINGERPRINT_SCHEME, LEGACY_SNAPSHOT_SCHEMA,
                    calculate_snapshot_digest, compile_snapshot, normalize_snapshot,
                    validate_snapshot, write_snapshot)
from .judgments import catalog, history
from .snapshot_artifacts import RUNTIME_MARKERS, write_candidate_artifact
from .storage import install_snapshot


class SnapshotLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "KIP126").mkdir(parents=True)
        (self.source / "blueprint/src").mkdir(parents=True)
        (self.source / "KIP126/Sample.lean").write_text(
            "namespace KIP126.Sample\ntheorem foo : True := by trivial\nend KIP126.Sample\n",
            encoding="utf-8")
        (self.source / "blueprint/src/content.tex").write_text("\\input{chapter}\n", encoding="utf-8")
        (self.source / "blueprint/src/chapter.tex").write_text(
            "\\chapter{Example}\n\\begin{definition}[A sample]"
            "\\label{def:sample}A statement.\\lean{KIP126.Sample.foo}"
            "\\end{definition}\n", encoding="utf-8")
        for command in (["git", "init", "--quiet"], ["git", "add", "."],
                        ["git", "-c", "user.name=Snapshot Test", "-c", "user.email=snapshot@example.org",
                         "commit", "--quiet", "-m", "Fixture"]):
            subprocess.run(command, cwd=self.source, check=True, capture_output=True)
        self.runtime = self.root / "runtime"

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "review_app", *map(str, arguments)],
                              cwd=Path(__file__).resolve().parents[1],
                              env={**os.environ, "REVIEW_DATA_DIR": str(self.runtime)},
                              capture_output=True, text=True)

    def build(self, output: Path, *, statements=False):
        flags = ["--statements"] if statements else []
        return self.cli("build", "--source", self.source, "--output", output,
                        "--require-clean", *flags)

    def test_blueprint_and_statement_build_only_produce_candidates_then_install(self):
        for statements in (False, True):
            with self.subTest(statements=statements):
                artifact = self.root / f"candidate-{statements}.json"
                result = self.build(artifact, statements=statements)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("install-snapshot --file", result.stdout)
                candidate = json.loads(artifact.read_text(encoding="utf-8"))
                validate_snapshot(candidate)
                self.assertEqual(candidate.get("review_mode", "blueprint"),
                                 "statement" if statements else "blueprint")
                self.assertFalse(self.runtime.exists())
                self.assertFalse((artifact.parent / "judgments.sqlite3").exists())
                frozen = artifact.read_bytes()
                data_dir = self.root / f"installed-{statements}"
                installed = self.cli("install-snapshot", "--file", artifact, "--data-dir", data_dir)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                active = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
                self.assertEqual(active["digest"], candidate["digest"])
                self.assertEqual(artifact.read_bytes(), frozen)

    def test_build_requires_explicit_output_and_does_not_use_runtime_default(self):
        result = self.cli("build", "--source", self.source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--output", result.stderr)
        self.assertFalse(self.runtime.exists())

    def test_clean_source_requirement_refuses_both_build_modes_without_an_artifact(self):
        (self.source / "KIP126/Sample.lean").write_text(
            "namespace KIP126.Sample\ntheorem foo : True := by trivial\nend KIP126.Sample\n-- local change\n",
            encoding="utf-8")
        for statements in (False, True):
            artifact = self.root / f"dirty-{statements}.json"
            with self.subTest(statements=statements):
                result = self.build(artifact, statements=statements)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("uncommitted", result.stderr)
                self.assertFalse(artifact.exists())
        self.assertFalse(self.runtime.exists())

    def test_existing_outputs_source_files_and_source_tree_are_never_changed(self):
        frozen = self.root / "frozen.json"
        frozen.write_bytes(b"frozen evidence\n")
        source_file = self.source / "KIP126/Sample.lean"
        source_before = source_file.read_bytes()
        for statements in (False, True):
            for output in (frozen, source_file, self.source / "candidate.json"):
                with self.subTest(statements=statements, output=output):
                    self.assertNotEqual(self.build(output, statements=statements).returncode, 0)
        self.assertEqual(frozen.read_bytes(), b"frozen evidence\n")
        self.assertEqual(source_file.read_bytes(), source_before)
        self.assertFalse((self.source / "candidate.json").exists())
        with self.assertRaises(FileExistsError):
            write_snapshot(self.source, frozen)

    def test_runtime_markers_block_candidates_and_nested_candidates_without_changes(self):
        for marker in RUNTIME_MARKERS:
            data_dir = self.root / f"runtime-{marker}"
            data_dir.mkdir()
            marker_file = data_dir / marker
            marker_file.write_bytes(b"runtime state\n")
            current = data_dir / "snapshot.json"
            current.write_bytes(b"installed evidence\n")
            for statements in (False, True):
                for output in (data_dir / "candidate.json", data_dir / "nested/candidate.json"):
                    with self.subTest(marker=marker, statements=statements, output=output):
                        result = self.build(output, statements=statements)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("runtime data directory", result.stderr)
                        self.assertFalse(output.exists())
            self.assertEqual(marker_file.read_bytes(), b"runtime state\n")
            self.assertEqual(current.read_bytes(), b"installed evidence\n")
            self.assertFalse((data_dir / "nested").exists())

    def test_compatibility_data_dir_is_exclusive_and_never_changes_a_database(self):
        for statements in (False, True):
            flags = ["--statements"] if statements else []
            candidate_dir = self.root / f"legacy-output-{statements}"
            args = ("build", "--source", self.source, "--data-dir", candidate_dir, *flags)
            first = self.cli(*args)
            self.assertEqual(first.returncode, 0, first.stderr)
            artifact = candidate_dir / "snapshot.json"
            before = artifact.read_bytes()
            self.assertFalse((candidate_dir / "judgments.sqlite3").exists())
            self.assertNotEqual(self.cli(*args).returncode, 0)
            self.assertEqual(artifact.read_bytes(), before)
            db_dir = self.root / f"database-only-{statements}"
            db_dir.mkdir()
            database = db_dir / "judgments.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE judgments (human_record TEXT)")
                connection.execute("INSERT INTO judgments VALUES ('keep')")
            before_db = database.read_bytes()
            result = self.cli("build", "--source", self.source, "--data-dir", db_dir, *flags)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(database.read_bytes(), before_db)
            self.assertFalse((db_dir / "snapshot.json").exists())

    def test_frozen_artifact_directory_accepts_a_new_named_candidate(self):
        frozen_dir = self.root / "frozen"
        frozen_dir.mkdir()
        previous = frozen_dir / "snapshot.json"
        previous.write_bytes(b"old frozen evidence\n")
        result = self.build(frozen_dir / "new-candidate.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(previous.read_bytes(), b"old frozen evidence\n")

    def test_enrichment_uses_the_same_candidate_safety_rules_and_preserves_json_output(self):
        frozen = self.root / "frozen.json"
        result = self.build(frozen, statements=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = frozen.read_bytes()
        annotations = self.root / "annotations.json"
        annotations.write_text(json.dumps({"schema": "statement-enrichment.v1", "annotations": []}),
                               encoding="utf-8")
        output = self.root / "enriched.json"
        args = ("enrich-snapshot", "--snapshot", frozen, "--file", annotations)
        result = self.cli(*args, "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        guidance = json.loads(result.stdout)
        self.assertIn("install-snapshot --file", guidance["install_command"])
        validate_snapshot(json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(frozen.read_bytes(), before)
        self.assertNotEqual(self.cli(*args, "--output", frozen).returncode, 0)
        self.assertEqual(frozen.read_bytes(), before)
        self.runtime.mkdir()
        (self.runtime / ".data.lock").write_bytes(b"")
        self.assertNotEqual(self.cli(*args, "--output", self.runtime / "new.json").returncode, 0)
        self.assertFalse((self.runtime / "new.json").exists())

    def test_artifact_publication_fsyncs_private_complete_content_and_refuses_races(self):
        payload = {"evidence": "完整的候选"}
        output = self.root / "durable.json"
        synced = []
        original_fsync = os.fsync

        def sync(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            return original_fsync(descriptor)

        with patch("review_app.snapshot_artifacts.os.fsync", side_effect=sync):
            write_candidate_artifact(payload, output)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(synced, [False, True])

        raced = self.root / "raced.json"
        original_link = os.link

        def race(source, destination):
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), payload)
            raced.write_bytes(b"another writer\n")
            return original_link(source, destination)

        with patch("review_app.snapshot_artifacts.os.link", side_effect=race):
            with self.assertRaises(FileExistsError):
                write_candidate_artifact(payload, raced)
        self.assertEqual(raced.read_bytes(), b"another writer\n")
        self.assertEqual(list(self.root.glob(".snapshot-candidate-*")), [])

    def test_candidates_refuse_dangling_symlinks_and_reserved_database_filenames(self):
        dangling = self.root / "dangling.json"
        target = self.root / "missing.json"
        dangling.symlink_to(target)
        with self.assertRaises(FileExistsError):
            write_candidate_artifact({}, dangling)
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(target.exists())
        for name in RUNTIME_MARKERS:
            with self.subTest(name=name), self.assertRaises(ValueError):
                write_candidate_artifact({}, self.root / name)

    def test_build_leaves_legacy_judgments_untouched_install_reuses_their_basis(self):
        original = compile_snapshot(self.source)
        card = deepcopy(original["cards"][0])
        card["fingerprint"] = "a" * 64
        for field in ("fingerprint_scheme", "fingerprints", "nl_digest", "lean_digest"):
            card.pop(field, None)
        old = {"schema": LEGACY_SNAPSHOT_SCHEMA, "source_commit": "0" * 40,
               "unlinked_nodes": 0, "cards": [card]}
        old["digest"] = calculate_snapshot_digest(old)
        self.runtime.mkdir()
        active_file = self.runtime / "snapshot.json"
        active_file.write_text(json.dumps(old), encoding="utf-8")
        database = self.runtime / "judgments.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("""CREATE TABLE judgments (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, card_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(reviewer, request_id))""")
            connection.execute("INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               ("j1", "r1", card["id"], card["fingerprint"], "alice@example.org",
                                "aligned", "", "2026-10-03T00:00:00Z"))
        old_snapshot = active_file.read_bytes()
        old_database = database.read_bytes()
        # Only positions change. The install comparison and legacy backfill
        # must use the old active basis, not a candidate build comparison.
        lean = self.source / "KIP126/Sample.lean"
        lean.write_text("\n" + lean.read_text(encoding="utf-8"), encoding="utf-8")
        artifact = self.root / "candidate.json"
        result = self.cli("build", "--source", self.source, "--output", artifact)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(active_file.read_bytes(), old_snapshot)
        self.assertEqual(database.read_bytes(), old_database)
        installed, comparison = install_snapshot(artifact, self.runtime, allow_dirty_source=True)
        self.assertEqual(comparison, {"unchanged": 1, "changed": 0, "added": 0, "removed": 0})
        active = normalize_snapshot(installed)
        self.assertEqual(catalog(active, database, "alice@example.org")["cards"][0]["verdict"], "aligned")
        review = history(database, card["id"], "alice@example.org")[0]
        self.assertEqual(review["review_basis_scheme"], CURRENT_FINGERPRINT_SCHEME)
        self.assertEqual(review["review_basis_fingerprint"], active["cards"][0]["fingerprint"])


if __name__ == "__main__":
    unittest.main()
