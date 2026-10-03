"""Coordinate data-directory maintenance across threads and processes."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_active_fds: set[int] = set()
_held = threading.local()


def _after_fork() -> None:
    # A child must not inherit a locked Python mutex or retain its parent's
    # open file description (which would keep the parent's flock alive).
    global _registry_guard, _thread_locks, _active_fds, _held
    for descriptor in _active_fds:
        os.close(descriptor)
    _registry_guard = threading.Lock()
    _thread_locks = {}
    _active_fds = set()
    _held = threading.local()


os.register_at_fork(after_in_child=_after_fork)


@contextmanager
def data_lock(data_dir: Path):
    """Hold the private, reentrant maintenance lock for one data directory.

    SQLite still coordinates normal live writes. This lock pairs snapshot
    installation/migration with a backup's snapshot and database capture.
    The lock file must stay in place: unlinking it breaks process exclusion.
    """
    data_dir = data_dir.resolve()
    key = str(data_dir)
    with _registry_guard:
        mutex = _thread_locks.setdefault(key, threading.RLock())
    with mutex:
        held = getattr(_held, "directories", None)
        if held is None:
            held = _held.directories = set()
        if key in held:
            yield
            return
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_dir.chmod(0o700)
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(data_dir / ".data.lock", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("data maintenance lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            with _registry_guard:
                _active_fds.add(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            with _registry_guard:
                _active_fds.discard(descriptor)
            os.close(descriptor)


data_directory_lock = data_lock
