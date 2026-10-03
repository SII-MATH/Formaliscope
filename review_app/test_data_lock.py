from __future__ import annotations

import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path

from .data_lock import data_lock


def _acquire_in_process(directory, started, entered):
    started.set()
    with data_lock(Path(directory)):
        entered.set()


class DataLockTests(unittest.TestCase):
    def test_process_waits_for_lock_and_fork_does_not_inherit_mutex(self):
        for method in ("spawn", "fork"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                context = multiprocessing.get_context(method)
                started, entered = context.Event(), context.Event()
                process = context.Process(target=_acquire_in_process, args=(directory, started, entered))
                try:
                    with data_lock(Path(directory)):
                        process.start()
                        self.assertTrue(started.wait(5), "child did not start")
                        self.assertFalse(entered.wait(0.15), "another process entered the held lock")
                    self.assertTrue(entered.wait(5), "child did not enter the released lock")
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
                    self.assertEqual((Path(directory) / ".data.lock").stat().st_mode & 0o777, 0o600)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)

    def test_nested_lock_and_exception_release_allow_another_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            entered = threading.Event()

            def acquire():
                with data_lock(data):
                    entered.set()

            with self.assertRaisesRegex(RuntimeError, "failure"):
                with data_lock(data):
                    with data_lock(data):
                        thread = threading.Thread(target=acquire)
                        thread.start()
                        self.assertFalse(entered.wait(0.15))
                    raise RuntimeError("failure")
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(entered.is_set())
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)

    def test_symlink_lock_is_rejected_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "other.txt"
            target.write_text("operator data")
            target.chmod(0o644)
            data = root / "data"
            data.mkdir()
            (data / ".data.lock").symlink_to(target)
            with self.assertRaises(OSError):
                with data_lock(data):
                    self.fail("followed symlink maintenance lock")
            self.assertEqual(target.read_text(), "operator data")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
