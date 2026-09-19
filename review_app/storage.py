"""Persistent data backup helpers for deployment."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def create_backup(data_dir: Path, output_root: Path, *, now: datetime | None = None) -> Path:
    """Create an internally consistent SQLite backup plus recovery metadata."""
    database = data_dir / "judgments.sqlite3"
    snapshot = data_dir / "snapshot.json"
    if not database.is_file():
        raise FileNotFoundError(f"database missing: {database}")
    if not snapshot.is_file():
        raise FileNotFoundError(f"snapshot missing: {snapshot}")

    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    name = created.strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_root.chmod(0o700)
    destination = output_root / name
    temporary = output_root / f".{name}.tmp"
    if destination.exists() or temporary.exists():
        raise FileExistsError(f"backup already exists for {name}")
    temporary.mkdir(mode=0o700)

    try:
        staged_db = temporary / ".staged.sqlite3"
        backup_db = temporary / "judgments.sqlite3"
        with sqlite3.connect(database) as source, sqlite3.connect(staged_db) as target:
            source.backup(target)
        with sqlite3.connect(staged_db) as staged:
            # Authentication state is intentionally disposable. A restore must
            # require fresh login and must not resurrect old OTPs or sessions.
            for table in ("login_challenges", "login_requests", "login_sessions"):
                if staged.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    staged.execute(f"DELETE FROM {table}")
            staged.commit()
            # VACUUM INTO builds a fresh file from live rows, so deleted auth
            # records do not remain recoverable in unused SQLite pages.
            staged.execute("VACUUM INTO ?", (str(backup_db),))
        for suffix in ("", "-wal", "-shm"):
            (Path(str(staged_db) + suffix)).unlink(missing_ok=True)
        with sqlite3.connect(f"file:{backup_db}?mode=ro", uri=True) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"backup integrity check failed: {integrity}")
        backup_db.chmod(0o600)

        snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
        shutil.copyfile(snapshot, temporary / "snapshot.json")
        (temporary / "snapshot.json").chmod(0o600)
        manifest = {
            "schema": "kip126-review-backup.v1",
            "created_at": created.isoformat(),
            "snapshot_digest": snapshot_payload.get("digest"),
            "source_commit": snapshot_payload.get("source_commit"),
            "sqlite_integrity_check": integrity,
            "auth_state": "excluded; restored users must sign in again",
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination
