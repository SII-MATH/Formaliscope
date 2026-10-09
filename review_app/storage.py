"""Persistent snapshot installation and portable deployment backups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .build import compare_snapshots, normalize_snapshot, validate_snapshot
from .data_lock import data_lock


BACKUP_SCHEMA = "kip126-review-backup.v2"
_BACKUP_FILES = {"judgments.sqlite3", "snapshot.json", "manifest.json"}
_DISPOSABLE_AUTH_TABLES = ("login_challenges", "login_requests", "login_sessions",
                           "preview_identities", "identity_requests")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _file_metadata(path: Path) -> dict:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": length}


def _validate_install_payload(payload: dict, allow_dirty_source: bool) -> None:
    validate_snapshot(payload)
    from .repositories import COLLECTION_SCHEMA, datasets
    if payload.get('schema') == COLLECTION_SCHEMA:
        for item in datasets(payload):
            _validate_install_payload(item, allow_dirty_source)
        return
    if payload.get("source_dirty") and not allow_dirty_source:
        raise ValueError("refusing a snapshot built from a dirty reviewed-source checkout")
    if payload.get("source_origin") == "archive-unverified" and not allow_dirty_source:
        raise ValueError("unverified source archives are for preview; production requires a clean Git checkout")


def install_snapshot(source: Path, data_dir: Path, *, allow_dirty_source: bool = False,
                     legacy_kip126_only: bool = False) -> tuple[dict, dict[str, int]]:
    """Validate and atomically install an immutable reviewed-source artifact."""
    # Reject an invalid artifact without creating a production directory. The
    # captured bytes are immutable even if the source path changes meanwhile.
    payload = json.loads(source.read_bytes())
    _validate_install_payload(payload, allow_dirty_source)
    with data_lock(data_dir):
        _validate_install_payload(payload, allow_dirty_source)
        destination = data_dir / "snapshot.json"
        previous = None
        if destination.is_file():
            previous = normalize_snapshot(json.loads(destination.read_bytes()))
        if legacy_kip126_only and any(item and (item.get('repository') or item.get('datasets'))
                                      for item in (previous, payload)):
            raise ValueError('legacy KIP126 automatic updates cannot replace repository datasets; build and install a complete collection explicitly')
        comparison = compare_snapshots(previous, normalize_snapshot(payload))
        from .reuse import installation_reuse, inherit_judgments
        reuse_pairs = installation_reuse(previous, payload)
        # Preserve v1 judgments while their only old fingerprint mapping is
        # still available; backup must not interleave with this migration.
        database = data_dir / "judgments.sqlite3"
        if previous is not None and database.is_file():
            from .database import initialize
            from .judgments import backfill_review_basis
            initialize(database)
            backfill_review_basis(database, previous)
            from .dataset_storage import preserve_legacy_records
            preserve_legacy_records(database, previous, payload)
        if reuse_pairs and database.is_file():
            from .database import initialize
            initialize(database)
            comparison['inherited_judgments'] = inherit_judgments(database, reuse_pairs)
        payload["comparison"] = comparison
        descriptor, filename = tempfile.mkstemp(prefix=".snapshot.", suffix=".tmp", dir=data_dir)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(destination)
            _fsync_directory(data_dir)
        finally:
            temporary.unlink(missing_ok=True)
        return payload, comparison


def _database_metadata(database: Path, *, require_clean_auth: bool = False) -> tuple[str, int]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as check:
        results = [row[0] for row in check.execute("PRAGMA integrity_check")]
        integrity = "ok" if results == ["ok"] else "; ".join(results)
        if integrity != "ok":
            raise ValueError(f"backup integrity check failed: {integrity}")
        has_migrations = check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        schema_row = (check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                      if has_migrations else None)
        version = int(schema_row[0] or 0) if schema_row else 0
        if require_clean_auth:
            for table in _DISPOSABLE_AUTH_TABLES:
                if check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    if check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                        raise ValueError(f"backup contains disposable authentication state: {table}")
    return integrity, version


def verify_backup(directory: Path, *, data_dir: Path | None = None) -> dict:
    """Verify a v2 backup's files, hashes, SQLite integrity and snapshot metadata.

    Verification does not depend on the original data directory existing, so
    the complete private directory can be transferred to another machine.
    An optional data_dir also verifies its scope before local retention.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("backup must be a real directory")
    if {entry.name for entry in directory.iterdir()} != _BACKUP_FILES:
        raise ValueError("backup has missing or unexpected files")
    for name in _BACKUP_FILES:
        if not stat.S_ISREG((directory / name).lstat().st_mode):
            raise ValueError("backup files must be regular files")
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise ValueError("unsupported backup manifest")
    scope = manifest.get("source_data_dir")
    if not isinstance(scope, str) or not Path(scope).is_absolute():
        raise ValueError("backup manifest has no data-directory scope")
    if data_dir is not None and scope != str(data_dir.resolve()):
        raise ValueError("backup belongs to a different data directory")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("backup manifest has an invalid timestamp") from None
    if created.tzinfo is None:
        raise ValueError("backup timestamp must include a timezone")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"judgments.sqlite3", "snapshot.json"}:
        raise ValueError("backup manifest has no file hashes")
    for name in ("judgments.sqlite3", "snapshot.json"):
        if _file_metadata(directory / name) != files.get(name):
            raise ValueError(f"backup file hash or size mismatch: {name}")
    snapshot = json.loads((directory / "snapshot.json").read_bytes())
    if (not isinstance(snapshot, dict)
            or snapshot.get("source_commit") != manifest.get("source_commit")
            or snapshot.get("digest") != manifest.get("snapshot_digest")):
        raise ValueError("backup snapshot metadata mismatch")
    integrity, version = _database_metadata(directory / "judgments.sqlite3", require_clean_auth=True)
    if integrity != manifest.get("sqlite_integrity_check") or version != manifest.get("database_schema_version"):
        raise ValueError("backup database metadata mismatch")
    return manifest


def _validate_keep(keep: int | None) -> None:
    if keep is not None and (isinstance(keep, bool) or not isinstance(keep, int) or keep < 1):
        raise ValueError("backup retention keep must be a positive integer")


def prune_backups(data_dir: Path, output_root: Path, *, keep: int, preserve: Path | None = None) -> list[Path]:
    """Remove only verified complete v2 backups belonging to this data directory.

    Legacy backups without a scope, failed/temporary backups, unknown entries,
    symbolic links and modified backups are deliberately left for an operator.
    """
    _validate_keep(keep)
    if keep is None:
        raise ValueError("backup retention keep must be a positive integer")
    with data_lock(data_dir):
        if output_root.is_symlink() or not output_root.is_dir():
            return []
        candidates = []
        for directory in output_root.iterdir():
            if not re.fullmatch(r"\d{8}T\d{6}Z", directory.name):
                continue
            try:
                manifest = verify_backup(directory, data_dir=data_dir)
                created = datetime.fromisoformat(manifest["created_at"]).astimezone(timezone.utc)
                if created.strftime("%Y%m%dT%H%M%SZ") != directory.name:
                    continue
                candidates.append((created, directory, directory.stat()))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
                continue
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        protected = next((entry for entry in candidates if preserve is not None
                          and entry[1].absolute() == preserve.absolute()), None)
        if preserve is not None and protected is None:
            return []
        retained = {protected[1]} if protected else set()
        for _, directory, _ in candidates:
            if len(retained) < keep:
                retained.add(directory)
        removed = []
        for _, directory, original_stat in reversed(candidates):
            if directory in retained:
                continue
            try:
                current = directory.lstat()
                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino, current.st_mtime_ns) != (
                    original_stat.st_dev, original_stat.st_ino, original_stat.st_mtime_ns
                ):
                    continue
                # Recheck immediately before deletion; unexpected added files
                # or changed manifests turn the directory into operator data.
                verify_backup(directory, data_dir=data_dir)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
                continue
            shutil.rmtree(directory)
            removed.append(directory)
        if removed:
            _fsync_directory(output_root)
        return removed


def create_backup(data_dir: Path, output_root: Path, *, now: datetime | None = None, keep: int | None = None) -> Path:
    """Create and verify a consistent backup; retention is explicitly opt-in."""
    _validate_keep(keep)
    database = data_dir / "judgments.sqlite3"
    snapshot = data_dir / "snapshot.json"
    if not database.is_file():
        raise FileNotFoundError(f"database missing: {database}")
    if not snapshot.is_file():
        raise FileNotFoundError(f"snapshot missing: {snapshot}")
    with data_lock(data_dir):
        # Read this source once: the exact bytes supply both the copy and the
        # manifest while the shared maintenance lock excludes installation.
        snapshot_bytes = snapshot.read_bytes()
        snapshot_payload = json.loads(snapshot_bytes)
        if not isinstance(snapshot_payload, dict):
            raise ValueError("snapshot must be a JSON object")
        created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        name = created.strftime("%Y%m%dT%H%M%SZ")
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_root.chmod(0o700)
        destination = output_root / name
        temporary = output_root / f".{name}.tmp"
        if destination.exists() or destination.is_symlink() or temporary.exists() or temporary.is_symlink():
            raise FileExistsError(f"backup already exists for {name}")
        temporary.mkdir(mode=0o700)
        try:
            staged_db = temporary / ".staged.sqlite3"
            backup_db = temporary / "judgments.sqlite3"
            _write_private(staged_db, b"")
            _write_private(backup_db, b"")
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(staged_db)) as target:
                    source.backup(target)
            with closing(sqlite3.connect(staged_db)) as staged:
                for table in _DISPOSABLE_AUTH_TABLES:
                    if staged.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        staged.execute(f"DELETE FROM {table}")
                staged.commit()
                # Rebuild from live rows so deleted credentials cannot survive
                # in unused SQLite pages or journals transferred off-machine.
                staged.execute("VACUUM")
                # Keep the output private from its first byte. Older SQLite
                # versions refuse VACUUM INTO an existing empty private file.
                with closing(sqlite3.connect(backup_db)) as target:
                    staged.backup(target)
                    if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise ValueError("backup could not become a standalone SQLite file")
            backup_db.chmod(0o600)
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(staged_db) + suffix).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(backup_db) + suffix).unlink(missing_ok=True)
            integrity, version = _database_metadata(backup_db, require_clean_auth=True)
            with backup_db.open("rb") as copied:
                os.fsync(copied.fileno())
            _write_private(temporary / "snapshot.json", snapshot_bytes)
            manifest = {
                "schema": BACKUP_SCHEMA,
                "created_at": created.isoformat(),
                "source_data_dir": str(data_dir.resolve()),
                "snapshot_digest": snapshot_payload.get("digest"),
                "source_commit": snapshot_payload.get("source_commit"),
                "sqlite_integrity_check": integrity,
                "database_schema_version": version,
                "files": {filename: _file_metadata(temporary / filename)
                          for filename in ("judgments.sqlite3", "snapshot.json")},
                "auth_state": "sessions excluded; name identity and recovery digests retained; restored users sign in with recovery codes",
            }
            _write_private(temporary / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
            verify_backup(temporary, data_dir=data_dir)
            _fsync_directory(temporary)
            temporary.replace(destination)
            _fsync_directory(output_root)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if keep is not None:
            prune_backups(data_dir, output_root, keep=keep, preserve=destination)
        return destination
