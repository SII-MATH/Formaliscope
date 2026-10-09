"""CLI path selection preserves explicit production and environment overrides."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .__main__ import main


class LocalPathTests(unittest.TestCase):
    def test_cli_default_environment_and_explicit_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for label, env, extra, expected in [
                ('default', {}, [], root / '.formaliscope/runtime'),
                ('environment', {'REVIEW_DATA_DIR': str(root / 'env-data')}, [], root / 'env-data'),
                ('explicit', {'REVIEW_DATA_DIR': str(root / 'env-data')},
                 ['--data-dir', str(root / 'explicit-data')], root / 'explicit-data'),
            ]:
                with self.subTest(label=label):
                    expected.mkdir(parents=True, exist_ok=True)
                    (expected / 'snapshot.json').write_text('{}')
                    with patch.dict(os.environ, env, clear=True), \
                         patch('review_app.local_paths.PROJECT_ROOT', root), \
                         patch('sys.argv', ['review_app', 'serve', *extra]), \
                         patch('review_app.__main__.serve') as serve:
                        main()
                    self.assertEqual(serve.call_args.args[:2],
                                     (expected / 'snapshot.json', expected / 'judgments.sqlite3'))
                    self.assertFalse((root / '.review').exists())
