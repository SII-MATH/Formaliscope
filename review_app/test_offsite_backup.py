"""Optional backup-client regressions; all data and transports are synthetic."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy import formaliscope_offsite_backup as backup
from .build import CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA, calculate_snapshot_digest
from .storage import BACKUP_SCHEMA


class OffsiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = {"schema": backup.CONFIG_SCHEMA, "ssh_host": "public-server",
                       "remote_backup_dir": "/var/backups/formaliscope", "source_data_dir": "/var/lib/formaliscope",
                       "destination": str(self.root / "archives"), "key_file": str(self.root / "keys" / "hk-vps.fernet"),
                       "keep": 30}
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config))

    def fixture(self, day=1, *, scope=None, snapshot_schema=SNAPSHOT_SCHEMA):
        name = f"202610{day:02d}T120000Z"
        directory = self.root / ("source-" + name)
        directory.mkdir()
        with sqlite3.connect(directory / "judgments.sqlite3") as database:
            # Deliberately avoid initialize: the fixture represents a portable
            # backup and must not exercise the working tree's DB migrations.
            database.execute("CREATE TABLE schema_migrations (version INTEGER)")
            database.execute("INSERT INTO schema_migrations VALUES (7)")
            database.execute("CREATE TABLE judgments (reviewer TEXT, reason TEXT)")
            database.execute("INSERT INTO judgments VALUES ('Fixture Reviewer', 'synthetic reasoning')")
        snapshot = {"schema": snapshot_schema, "source_commit": "b" * 40, "source_dirty": False,
                    "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME, "dependency_lock_digest": None,
                    "unlinked_nodes": 0, "cards": []}
        snapshot["digest"] = calculate_snapshot_digest(snapshot)
        (directory / "snapshot.json").write_text(json.dumps(snapshot))
        files = {}
        for filename in backup.FILES[1:]:
            content = (directory / filename).read_bytes()
            files[filename] = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
        manifest = {"schema": BACKUP_SCHEMA, "created_at": f"2026-10-{day:02d}T12:00:00+00:00",
                    "source_data_dir": scope or self.config["source_data_dir"], "files": files,
                    "source_commit": snapshot["source_commit"], "snapshot_digest": snapshot["digest"],
                    "sqlite_integrity_check": "ok", "database_schema_version": 7}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return directory, self.metadata(directory, name)

    @staticmethod
    def metadata(directory, name):
        raw = (directory / "manifest.json").read_bytes()
        return {"schema": backup.REMOTE_SCHEMA, "name": name,
                "manifest": base64.b64encode(raw).decode(), "manifest_sha256": hashlib.sha256(raw).hexdigest()}

    def run_sync(self, directory, metadata):
        def fetch(config, info, target):
            for filename in backup.FILES:
                backup._write_private(target / filename, (directory / filename).read_bytes())
        with patch.object(backup, "inspect_latest", return_value=metadata), patch.object(
                backup, "fetch_completed", side_effect=fetch) as fetched:
            result = backup.sync(self.config)
        return result, fetched.call_count

    def archive(self, name):
        return Path(self.config["destination"]) / (name + ".tar.gz.fernet")

    @contextmanager
    def ssh_peers(self, scripts):
        """Use real local pipe processes in place of the SSH executable."""
        original_popen = backup.subprocess.Popen
        pending = iter(scripts)
        processes = []

        def launch(arguments, **kwargs):
            self.assertEqual(arguments[0], "ssh")
            process = original_popen([sys.executable, "-c", next(pending)], **kwargs)
            processes.append(process)
            return process

        try:
            with patch.object(backup.subprocess, "Popen", side_effect=launch):
                yield processes
        finally:
            # A failed assertion must not leave a synthetic peer behind. The
            # tests assert lifecycle state before this fallback cleanup runs.
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

    @staticmethod
    def peer_script(data, *, exit_code=0, stall=False, close_stdout=False):
        encoded = base64.b64encode(data)
        script = ("import base64, os, sys, time\n"
                  f"sys.stdout.buffer.write(base64.b64decode({encoded!r}))\n"
                  "sys.stdout.buffer.flush()\n")
        if close_stdout:
            script += "os.close(1)\n"
        if stall:
            script += "time.sleep(3)\n"
        return script + f"sys.exit({exit_code})\n"

    def assert_peers_reaped(self, processes):
        self.assertTrue(processes)
        for process in processes:
            self.assertIsNotNone(process.returncode, "peer must be waited for before returning")
            self.assertTrue(process.stdout.closed, "the parent's pipe must be closed")

    def test_ssh_returns_binary_data_at_the_exact_limit_across_pipe_reads(self):
        payload = bytes(range(256)) * 1024
        script = ("import sys\n"
                  "sys.stdout.buffer.write(bytes(range(256)) * 1024)\n"
                  "sys.stdout.buffer.flush()\n")
        with self.ssh_peers([script]) as processes:
            self.assertEqual(backup._ssh(self.config, "synthetic command", limit=len(payload)), payload)
            self.assertEqual(len(processes), 1)
            self.assert_peers_reaped(processes)
            self.assertEqual(processes[0].returncode, 0)

    def test_ssh_stops_an_oversize_peer_and_discards_its_output(self):
        script = ("import sys, time\n"
                  "sys.stdout.buffer.write(b'x' * (128 * 1024))\n"
                  "sys.stdout.buffer.flush()\n"
                  "time.sleep(3)\n")
        with self.ssh_peers([script]) as processes:
            with self.assertRaisesRegex(backup.BackupError, "size limit"):
                backup._ssh(self.config, "synthetic command", limit=8192)
            self.assert_peers_reaped(processes)
            self.assertNotEqual(processes[0].returncode, 0)

    def test_ssh_rejects_a_nonzero_peer_even_after_complete_output(self):
        payload = b"valid-looking complete output\x00\xff"
        script = self.peer_script(payload, exit_code=7)
        with self.ssh_peers([script]) as processes:
            with self.assertRaisesRegex(backup.BackupError, "transfer failed"):
                backup._ssh(self.config, "synthetic command", limit=len(payload))
            self.assert_peers_reaped(processes)
            self.assertEqual(processes[0].returncode, 7)

    def test_ssh_times_out_and_reaps_stalled_peers_before_and_after_eof(self):
        for close_stdout in (False, True):
            with self.subTest(close_stdout=close_stdout):
                script = self.peer_script(b"partial transfer", stall=True, close_stdout=close_stdout)
                with self.ssh_peers([script]) as processes, patch.object(backup, "SSH_TIMEOUT", 0.3):
                    with self.assertRaisesRegex(backup.BackupError, "timed out"):
                        backup._ssh(self.config, "synthetic command")
                    self.assert_peers_reaped(processes)
                    self.assertNotEqual(processes[0].returncode, 0)

    def test_real_peer_transfer_failures_clean_partial_downloads_without_publish_or_prune(self):
        first, info = self.fixture()
        self.run_sync(first, info)
        second, second_info = self.fixture(2)
        self.run_sync(second, second_info)
        source, next_info = self.fixture(3)
        destination = Path(self.config["destination"])
        retained = {path.name: path.read_bytes() for path in destination.glob("*.fernet")}
        previous = json.loads((destination / "status.json").read_bytes())["last_success"]
        self.config["keep"] = 1
        snapshot = (source / "snapshot.json").read_bytes()
        failed_scripts = {
            "oversize": self.peer_script(snapshot + b"extra"),
            "nonzero exit": self.peer_script(snapshot, exit_code=7),
            "stalled partial output": self.peer_script(snapshot[:10], stall=True),
            "successful short EOF": self.peer_script(snapshot[:10]),
        }
        for failure, failed_script in failed_scripts.items():
            scripts = [self.peer_script((source / filename).read_bytes()) for filename in backup.FILES[:2]]
            scripts.append(failed_script)
            with self.subTest(failure=failure):
                with self.ssh_peers(scripts) as processes, patch.object(backup, "SSH_TIMEOUT", 0.5), patch.object(
                        backup, "inspect_latest", return_value=next_info), patch.object(
                        backup, "_publish", wraps=backup._publish) as published, patch.object(
                        backup, "_prune", wraps=backup._prune) as pruned:
                    with self.assertRaises(backup.BackupError):
                        backup.sync(self.config)
                    self.assertEqual(len(processes), 3, "failure must follow two completed file downloads")
                    self.assert_peers_reaped(processes)
                    published.assert_not_called()
                    pruned.assert_not_called()
                self.assertEqual({path.name: path.read_bytes() for path in destination.glob("*.fernet")}, retained)
                self.assertFalse(self.archive(next_info["name"]).exists())
                self.assertEqual({path.name for path in destination.iterdir()},
                                 set(retained) | {".sync.lock", "status.json"})
                state = json.loads((destination / "status.json").read_bytes())
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["last_success"], previous)
                self.assertEqual(state["error"], "backup_failed")

    def test_real_encryption_round_trip_restore_and_private_files(self):
        directory, metadata = self.fixture()
        result, calls = self.run_sync(directory, metadata)
        self.assertEqual(calls, 1)
        archive = self.archive(metadata["name"])
        self.assertEqual(result["backup_name"], metadata["name"])
        self.assertNotIn(b"Fixture Reviewer", archive.read_bytes())
        key = Path(self.config["key_file"]).read_bytes()
        with patch("review_app.database.initialize", side_effect=AssertionError("must not migrate")):
            self.assertEqual(backup.verify(self.config, archive)["database_schema_version"], 7)
            output = self.root / "restored"
            backup.restore(self.config, archive, output)
        for filename in backup.FILES:
            self.assertEqual((output / filename).read_bytes(), (directory / filename).read_bytes())
            self.assertEqual(stat.S_IMODE((output / filename).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.config["key_file"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.config["destination"]).stat().st_mode), 0o700)
        self.assertNotIn(key, archive.read_bytes())
        status = json.loads((archive.parent / "status.json").read_bytes())
        self.assertEqual(status["last_success"]["backup_name"], metadata["name"])

    def test_deduplication_and_remote_collision_cannot_overwrite(self):
        directory, metadata = self.fixture()
        self.run_sync(directory, metadata)
        archive = self.archive(metadata["name"])
        original, original_time = archive.read_bytes(), archive.stat().st_mtime_ns
        _, calls = self.run_sync(directory, metadata)
        self.assertEqual(calls, 0)
        self.assertEqual(archive.stat().st_mtime_ns, original_time)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        manifest["extra"] = "different original file"
        (directory / "manifest.json").write_text(json.dumps(manifest))
        changed = self.metadata(directory, metadata["name"])
        with self.assertRaisesRegex(backup.BackupError, "different remote manifest"):
            self.run_sync(directory, changed)
        self.assertEqual(archive.read_bytes(), original)
        with self.assertRaises(FileExistsError):
            backup._publish(archive, b"must not overwrite")
        self.assertEqual(archive.read_bytes(), original)

    def test_tampering_wrong_key_and_missing_key_are_rejected(self):
        directory, metadata = self.fixture()
        self.run_sync(directory, metadata)
        archive = self.archive(metadata["name"])
        original = archive.read_bytes()
        damaged = bytearray(original)
        damaged[len(damaged) // 2] ^= 1
        archive.write_bytes(damaged)
        with self.assertRaisesRegex(backup.BackupError, "authentication failed"):
            backup.verify(self.config, archive)
        archive.write_bytes(original)
        from cryptography.fernet import Fernet
        key_path = Path(self.config["key_file"])
        key_path.write_bytes(Fernet.generate_key())
        with self.assertRaisesRegex(backup.BackupError, "authentication failed"):
            backup.verify(self.config, archive)
        key_path.unlink()
        with self.assertRaisesRegex(backup.BackupError, "original key"):
            self.run_sync(directory, metadata)
        self.assertFalse(key_path.exists())

    def test_tar_rejects_traversal_duplicates_links_and_extra_files(self):
        _, metadata = self.fixture()
        destination = self.root / "unpacked"
        entries = [("../snapshot.json", tarfile.REGTYPE), ("/snapshot.json", tarfile.REGTYPE),
                   ("snapshot.json", tarfile.SYMTYPE), ("snapshot.json", tarfile.LNKTYPE),
                   ("auth-pepper", tarfile.REGTYPE), ("snapshot.json", tarfile.DIRTYPE)]
        for name, kind in entries:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory(dir=self.root) as target:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w:gz") as tar:
                    info = tarfile.TarInfo(name)
                    info.type, info.size = kind, 0
                    tar.addfile(info)
                with self.assertRaises(backup.BackupError):
                    backup._unpack(data.getvalue(), Path(target))
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            for _ in range(2):
                tar.addfile(tarfile.TarInfo("manifest.json"))
        destination.mkdir()
        with self.assertRaisesRegex(backup.BackupError, "unique regular"):
            backup._unpack(data.getvalue(), destination)
        self.assertFalse((self.root.parent / "snapshot.json").exists())

    def test_tar_rejects_pax_metadata_including_large_compressed_headers(self):
        limit = 4096
        for comment, message in (("synthetic metadata", "unique regular"),
                                 ("x" * (128 * 1024), "decompressed archive")):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                for filename in backup.FILES:
                    info = tarfile.TarInfo(filename)
                    if filename == "manifest.json":
                        info.pax_headers = {"comment": comment}
                    archive.addfile(info)
            compressed = data.getvalue()
            self.assertLess(len(compressed), limit, "compressed input must fit the input byte limit")
            with self.subTest(header_size=len(comment)), tempfile.TemporaryDirectory(dir=self.root) as target:
                with patch.object(backup, "MAX_BYTES", limit):
                    with self.assertRaisesRegex(backup.BackupError, message):
                        backup._unpack(compressed, Path(target))
                self.assertEqual(list(Path(target).iterdir()), [])

    def test_tar_rejects_expanded_regular_members_above_the_total_limit(self):
        limit = 4096
        for member_size, message in ((2000, "archive contents"),
                                     (64 * 1024, "decompressed archive")):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
                for filename in backup.FILES:
                    payload = b"x" * member_size
                    info = tarfile.TarInfo(filename)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            compressed = data.getvalue()
            self.assertLess(len(compressed), limit, "compressible member bytes must fit the input limit")
            with self.subTest(member_size=member_size), tempfile.TemporaryDirectory(dir=self.root) as target:
                with patch.object(backup, "MAX_BYTES", limit):
                    with self.assertRaisesRegex(backup.BackupError, message):
                        backup._unpack(compressed, Path(target))
                self.assertFalse((Path(target) / "snapshot.json").exists())
                if member_size > limit:
                    self.assertEqual(list(Path(target).iterdir()), [])

    def test_invalid_schema_remote_hash_or_transferred_manifest_fails(self):
        directory, metadata = self.fixture()
        cases = [dict(metadata, schema="unknown"), dict(metadata, manifest_sha256="0" * 64),
                 dict(metadata, name="../evil")]
        for info in cases:
            with self.subTest(info=info), self.assertRaises(backup.BackupError):
                self.run_sync(directory, info)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        manifest["schema"] = "kip126-review-backup.v1"
        (directory / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(backup.BackupError, "schema or scope"):
            self.run_sync(directory, self.metadata(directory, metadata["name"]))
        manifest["schema"] = BACKUP_SCHEMA
        manifest["extra"] = "changed after remote verification"
        (directory / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(backup.BackupError, "differs from"):
            self.run_sync(directory, metadata)
        invalid, info = self.fixture(2, snapshot_schema="unsupported-snapshot")
        with self.assertRaisesRegex(backup.BackupError, "verification failed"):
            self.run_sync(invalid, info)
        self.assertEqual(list(Path(self.config["destination"]).glob("*.fernet")), [])

    def test_failed_transfer_does_not_prune_and_preserves_last_success(self):
        first, info = self.fixture()
        self.run_sync(first, info)
        second, next_info = self.fixture(2)
        self.run_sync(second, next_info)
        self.config["keep"] = 1
        previous = json.loads((Path(self.config["destination"]) / "status.json").read_bytes())["last_success"]
        with patch.object(backup, "inspect_latest", side_effect=backup.BackupError("remote unavailable")):
            with self.assertRaises(backup.BackupError):
                backup.sync(self.config)
        self.assertEqual(len(list(Path(self.config["destination"]).glob("*.fernet"))), 2)
        state = json.loads((Path(self.config["destination"]) / "status.json").read_bytes())
        self.assertEqual(state["last_success"], previous)
        self.assertEqual(state["error"], "backup_failed")

    def test_retention_only_removes_verified_archives_of_our_scope(self):
        first, info = self.fixture()
        self.run_sync(first, info)
        key = Path(self.config["key_file"]).read_bytes()
        destination = Path(self.config["destination"])
        wrong, wrong_info = self.fixture(2, scope="/var/lib/another-project")
        backup._write_private(self.archive(wrong_info["name"]), backup._cipher(key).encrypt(backup._pack(wrong)))
        corrupted = self.archive("20261003T120000Z")
        corrupted.write_bytes(b"damaged")
        unknown = destination / "other-project.tar.gz.fernet"
        unknown.write_bytes(b"unknown")
        symlink = self.archive("20261004T120000Z")
        symlink.symlink_to(self.archive(info["name"]))
        latest, next_info = self.fixture(5)
        self.config["keep"] = 1
        result, _ = self.run_sync(latest, next_info)
        self.assertEqual(result["pruned"], 1)
        self.assertFalse(self.archive(info["name"]).exists())
        self.assertTrue(self.archive(wrong_info["name"]).exists())
        self.assertTrue(corrupted.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(self.archive(next_info["name"]).exists())

    def test_retention_preserves_the_selected_backup_when_remote_latest_regresses(self):
        first, info = self.fixture()
        self.run_sync(first, info)
        second, second_info = self.fixture(2)
        self.run_sync(second, second_info)
        original = self.archive(info["name"]).read_bytes()
        self.config["keep"] = 1
        result, calls = self.run_sync(first, info)
        self.assertEqual(calls, 0)
        self.assertEqual(result["backup_name"], info["name"])
        self.assertEqual(result["pruned"], 1)
        self.assertEqual(self.archive(info["name"]).read_bytes(), original)
        self.assertEqual(result["archive_size_bytes"], len(original))
        self.assertFalse(self.archive(second_info["name"]).exists())
        state = json.loads((Path(self.config["destination"]) / "status.json").read_bytes())
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["last_success"]["backup_name"], info["name"])

    def test_restore_cannot_replace_existing_data_or_follow_symlinks(self):
        source, info = self.fixture()
        self.run_sync(source, info)
        occupied = self.root / "occupied"
        occupied.mkdir()
        marker = occupied / "valuable"
        marker.write_bytes(b"must retain")
        with self.assertRaisesRegex(backup.BackupError, "new directory"):
            backup.restore(self.config, self.archive(info["name"]), occupied)
        self.assertEqual(marker.read_bytes(), b"must retain")
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(backup.BackupError, "new directory"):
            backup.restore(self.config, self.archive(info["name"]), empty)
        self.assertTrue(empty.is_dir())
        self.assertEqual(list(empty.iterdir()), [])
        linked = self.root / "linked"
        linked.symlink_to(occupied, target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "symbolic links"):
            backup.restore(self.config, self.archive(info["name"]), linked)

    def test_strict_configuration_and_no_symlink_key(self):
        self.assertEqual(backup.load_config(self.config_path), self.config)
        cases = [dict(self.config, ssh_host="-oProxyCommand=evil"), dict(self.config, keep=True),
                 dict(self.config, source_data_dir="/var/lib/../secret"),
                 dict(self.config, remote_backup_dir="/var/backups;secret"),
                 dict(self.config, key_file=str(Path(self.config["destination"]) / "key")),
                 dict(self.config, schema="unknown")]
        for case in cases:
            self.config_path.write_text(json.dumps(case))
            with self.subTest(case=case), self.assertRaises(backup.BackupError):
                backup.load_config(self.config_path)
        source, info = self.fixture()
        key = Path(self.config["key_file"])
        key.parent.mkdir()
        key.symlink_to(self.config_path)
        with self.assertRaisesRegex(backup.BackupError, "symbolic links"):
            self.run_sync(source, info)

    def test_lock_prevents_concurrent_operations(self):
        with backup._lock(self.config):
            with self.assertRaisesRegex(backup.BackupError, "already running"):
                with backup._lock(self.config):
                    self.fail("second process acquired lock")


if __name__ == "__main__":
    unittest.main()
