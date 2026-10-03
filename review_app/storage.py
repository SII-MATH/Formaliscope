"""Persistent data backup helpers for deployment."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .build import compare_snapshots, normalize_snapshot, validate_snapshot


def install_snapshot(source: Path, data_dir: Path, *, allow_dirty_source: bool = False) -> tuple[dict, dict[str, int]]:
    """Validate and atomically install an immutable reviewed-source artifact."""
    payload = json.loads(source.read_text(encoding="utf-8"))
    validate_snapshot(payload)
    if payload.get("source_dirty") and not allow_dirty_source:
        raise ValueError("refusing a snapshot built from a dirty reviewed-source checkout")
    if payload.get('source_origin') == 'archive-unverified' and not allow_dirty_source:
        raise ValueError('unverified source archives are for preview; production requires a clean Git checkout')
    destination = data_dir / "snapshot.json"
    previous = None
    if destination.is_file():
        previous = normalize_snapshot(json.loads(destination.read_text(encoding="utf-8")))
    comparison = compare_snapshots(previous, normalize_snapshot(payload))
    # Preserve v1 judgments before replacing the only copy of the old
    # snapshot. This makes install-snapshot safe even if an operator omitted
    # the explicit migrate step; the old snapshot still supplies the mapping
    # from positional fingerprints to stable content fingerprints.
    database = data_dir / "judgments.sqlite3"
    if previous is not None and database.is_file():
        from .database import initialize
        from .judgments import backfill_review_basis
        initialize(database)
        backfill_review_basis(database, previous)
    payload["comparison"] = comparison
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    temporary = data_dir / ".snapshot.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(destination)
    return payload, comparison


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
            for table in ("login_challenges", "login_requests", "login_sessions", "preview_identities"):
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
            has_migrations = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            schema_row = (check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                          if has_migrations else None)
            database_schema_version = int(schema_row[0] or 0) if schema_row else 0
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
            "database_schema_version": database_schema_version,
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
