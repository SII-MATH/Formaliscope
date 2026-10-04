"""Exercise backup delivery and retention through the actual command-line API."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .database import DB_SCHEMA_VERSION, initialize
from .storage import create_backup


class BackupCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'runtime'
        initialize(self.data / 'judgments.sqlite3')
        (self.data / 'snapshot.json').write_text(json.dumps({
            'digest': 'a' * 64, 'source_commit': 'b' * 40,
        }), encoding='utf-8')
        self.backups = self.root / 'backups'

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'review_app', *map(str, args)],
                              cwd=Path(__file__).resolve().parents[1],
                              env={**os.environ, 'REVIEW_DATA_DIR': str(self.data)},
                              capture_output=True, text=True)

    def test_offsite_verification_does_not_require_or_recreate_original_runtime(self):
        original = create_backup(self.data, self.backups)
        moved = self.root / 'offsite-copy'
        shutil.copytree(original, moved)
        before = {p.name: p.read_bytes() for p in moved.iterdir()}
        shutil.rmtree(self.data)
        result = self.cli('verify-backup', '--directory', moved)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['valid'])
        self.assertEqual(report['database_schema_version'], DB_SCHEMA_VERSION)
        self.assertEqual(report['snapshot_digest'], 'a' * 64)
        self.assertFalse(self.data.exists())
        self.assertEqual({p.name: p.read_bytes() for p in moved.iterdir()}, before)

    def test_cli_retention_invalid_keep_and_corrupt_delivery(self):
        for day in (1, 2):
            create_backup(self.data, self.backups,
                          now=datetime(2000, 1, day, tzinfo=timezone.utc))
        result = self.cli('backup', '--output', self.backups, '--keep', 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        completed = list(self.backups.iterdir())
        self.assertEqual(len(completed), 1)
        latest = completed[0]
        before = {p.name: p.read_bytes() for p in latest.iterdir()}
        rejected = self.cli('backup', '--output', self.backups, '--keep', 0)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('positive integer', rejected.stderr)
        self.assertEqual(list(self.backups.iterdir()), completed)
        self.assertEqual({p.name: p.read_bytes() for p in latest.iterdir()}, before)

        (latest / 'snapshot.json').write_bytes(b'corrupted in transit')
        damaged = {p.name: p.read_bytes() for p in latest.iterdir()}
        rejected = self.cli('verify-backup', '--directory', latest)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('hash or size mismatch', rejected.stderr)
        self.assertNotIn('Traceback', rejected.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in latest.iterdir()}, damaged)

    def test_missing_backup_verification_is_read_only(self):
        missing = self.root / 'never-created'
        result = self.cli('verify-backup', '--directory', missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
