#!/usr/bin/env python3
"""Pull completed v2 backups over SSH and store authenticated encrypted archives.

This optional client never opens the running database, migrates data, or starts
services. Its only non-stdlib dependency (cryptography) is imported on demand.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

# Allow execution by absolute path from a scheduled task, without changing CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_app.build import validate_snapshot
from review_app.storage import BACKUP_SCHEMA, verify_backup

CONFIG_SCHEMA = "formaliscope-offsite-backup-config.v1"
REMOTE_SCHEMA = "formaliscope-offsite-backup-remote.v1"
STATUS_SCHEMA = "formaliscope-offsite-backup-status.v1"
FILES = ("manifest.json", "judgments.sqlite3", "snapshot.json")
NAME_RE = re.compile(r"\d{8}T\d{6}Z")
ARCHIVE_RE = re.compile(r"(\d{8}T\d{6}Z)\.tar\.gz\.fernet")
MAX_BYTES = 512 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
TAR_OVERHEAD = 20 * 1024
SSH_TIMEOUT = 180


class BackupError(ValueError):
    """A safe, stable message suitable for a local status file and CLI output."""


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BackupError("duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(BackupError("invalid JSON number")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BackupError("invalid JSON document") from error
    if not isinstance(value, dict):
        raise BackupError("JSON document must be an object")
    return value


def _absolute(value, *, remote=False) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise BackupError("invalid configured path")
    path = PurePosixPath(value) if remote else Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise BackupError("configured paths must be normalized absolute paths")
    if remote and not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise BackupError("invalid remote path")
    return value


def load_config(path: Path) -> dict:
    config = _json(_read_regular(path, MAX_MANIFEST))
    expected = {"schema", "ssh_host", "remote_backup_dir", "source_data_dir",
                "destination", "key_file", "keep"}
    if set(config) != expected or config.get("schema") != CONFIG_SCHEMA:
        raise BackupError("unsupported backup configuration")
    host = config["ssh_host"]
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise BackupError("invalid SSH host alias")
    for field in ("remote_backup_dir", "source_data_dir"):
        _absolute(config[field], remote=True)
    for field in ("destination", "key_file"):
        _absolute(config[field])
    if config["source_data_dir"] == "/" or config["remote_backup_dir"] == "/":
        raise BackupError("remote directories cannot be filesystem root")
    if isinstance(config["keep"], bool) or not isinstance(config["keep"], int) or config["keep"] < 1:
        raise BackupError("retention must be a positive integer")
    destination, key = Path(config["destination"]), Path(config["key_file"])
    if key == destination or destination in key.parents:
        raise BackupError("encryption key must be stored outside the archive directory")
    return config


def _no_symlink_parents(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise BackupError("symbolic links are not allowed for managed paths")


def _mkdir(path: Path, *, private=True) -> None:
    _no_symlink_parents(path)
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise BackupError("managed directory is not a directory")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if not path.is_dir():
        raise BackupError("managed directory is not a directory")
    if private:
        path.chmod(0o700)


def _fsync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _read_regular(path: Path, limit=MAX_BYTES) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise BackupError("required regular file is unavailable") from error
    with os.fdopen(descriptor, "rb") as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise BackupError("file is not regular or exceeds the size limit")
        content = source.read(limit + 1)
    if len(content) > limit:
        raise BackupError("file exceeds the size limit")
    return content


def _cipher(key: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:
        raise BackupError("install deploy/requirements-offsite-backup.txt for this optional helper") from error
    try:
        return Fernet(key)
    except (ValueError, TypeError) as error:
        raise BackupError("invalid encryption key") from error


def _key(config: dict, *, create=False) -> bytes:
    path = Path(config["key_file"])
    _no_symlink_parents(path)
    if not path.exists():
        destination = Path(config["destination"])
        # Any recognizable archive, including damaged or linked entries, means
        # the previous key is needed. Never silently replace a lost key.
        if not create or any(ARCHIVE_RE.fullmatch(p.name) for p in destination.iterdir()):
            raise BackupError("encryption key is missing; restore the original key")
        _mkdir(path.parent)
        from cryptography.fernet import Fernet
        try:
            _write_private(path, Fernet.generate_key())
        except FileExistsError:
            pass
        _fsync(path.parent)
    key = _read_regular(path, 1024)
    _cipher(key)
    path.chmod(0o600)
    return key


@contextmanager
def _lock(config: dict):
    destination = Path(config["destination"])
    _mkdir(destination)
    descriptor = os.open(destination / ".sync.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupError("invalid backup lock")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupError("another backup operation is already running") from error
        yield
    finally:
        os.close(descriptor)


def _ssh(config: dict, command: str, *, limit=MAX_MANIFEST) -> bytes:
    # Bound both runtime and accumulated bytes, including a stalled peer. No
    # intermediate download can grow an unlimited temporary disk file.
    try:
        process = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-o", "ConnectTimeout=10", "--", config["ssh_host"], command],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise BackupError("SSH backup transfer failed") from error
    target, deadline = io.BytesIO(), time.monotonic() + SSH_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(timeout=remaining):
                    raise BackupError("SSH backup transfer timed out")
                chunk = os.read(process.stdout.fileno(), min(64 * 1024, limit - target.tell() + 1))
                if not chunk:
                    break
                target.write(chunk)
                if target.tell() > limit:
                    raise BackupError("remote output exceeds the size limit")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise BackupError("SSH backup transfer failed")
        return target.getvalue()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError("SSH backup transfer failed or timed out") from error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


REMOTE_INSPECT = '''import base64, hashlib, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "/opt/formaliscope/current")
from review_app.storage import verify_backup
root, scope = Path(sys.argv[1]), Path(sys.argv[2])
if root.is_symlink() or not root.is_dir():
    raise ValueError("invalid backup root")
names = sorted((p.name for p in root.iterdir() if re.fullmatch(r"\\d{8}T\\d{6}Z", p.name)), reverse=True)
if not names:
    raise ValueError("no completed backup")
name = names[0]
directory = root / name
manifest = verify_backup(directory, data_dir=scope)
if datetime.fromisoformat(manifest["created_at"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") != name:
    raise ValueError("backup timestamp mismatch")
raw = (directory / "manifest.json").read_bytes()
if len(raw) > 1048576:
    raise ValueError("manifest too large")
print(json.dumps({"schema": "formaliscope-offsite-backup-remote.v1", "name": name,
 "manifest": base64.b64encode(raw).decode("ascii"), "manifest_sha256": hashlib.sha256(raw).hexdigest()}))
'''


def inspect_latest(config: dict) -> dict:
    command = "python3 -c " + shlex.quote(REMOTE_INSPECT) + " " + " ".join(
        shlex.quote(config[field]) for field in ("remote_backup_dir", "source_data_dir"))
    return _remote_metadata(_json(_ssh(config, command)), config)


def _remote_metadata(value: dict, config: dict) -> dict:
    if set(value) != {"schema", "name", "manifest", "manifest_sha256"} or value.get("schema") != REMOTE_SCHEMA:
        raise BackupError("unsupported remote backup metadata")
    if not isinstance(value["name"], str) or not NAME_RE.fullmatch(value["name"]):
        raise BackupError("invalid remote backup name")
    try:
        raw = base64.b64decode(value["manifest"], validate=True)
    except (ValueError, TypeError, binascii.Error) as error:
        raise BackupError("invalid remote manifest encoding") from error
    if len(raw) > MAX_MANIFEST or hashlib.sha256(raw).hexdigest() != value["manifest_sha256"]:
        raise BackupError("remote manifest hash mismatch")
    manifest = _json(raw)
    if manifest.get("schema") != BACKUP_SCHEMA or manifest.get("source_data_dir") != config["source_data_dir"]:
        raise BackupError("remote backup schema or scope mismatch")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None or created.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") != value["name"]:
            raise ValueError()
    except (KeyError, ValueError, TypeError) as error:
        raise BackupError("remote backup timestamp mismatch") from error
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(FILES[1:]):
        raise BackupError("invalid remote backup file metadata")
    total = len(raw)
    for entry in files.values():
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or isinstance(entry["size_bytes"], bool) or not isinstance(entry["size_bytes"], int)
                or not 0 < entry["size_bytes"] <= MAX_BYTES):
            raise BackupError("invalid remote backup file metadata")
        total += entry["size_bytes"]
    if total > MAX_BYTES:
        raise BackupError("backup exceeds the size limit")
    return value


def fetch_completed(config: dict, metadata: dict, directory: Path) -> None:
    manifest_raw = base64.b64decode(metadata["manifest"])
    manifest = _json(manifest_raw)
    for name in FILES:
        limit = MAX_MANIFEST if name == "manifest.json" else manifest["files"][name]["size_bytes"]
        remote = str(PurePosixPath(config["remote_backup_dir"]) / metadata["name"] / name)
        _write_private(directory / name, _ssh(config, "cat -- " + shlex.quote(remote), limit=limit))


def _verify(directory: Path, config: dict, *, name=None, manifest_hash=None) -> dict:
    raw = _read_regular(directory / "manifest.json", MAX_MANIFEST)
    if manifest_hash and hashlib.sha256(raw).hexdigest() != manifest_hash:
        raise BackupError("transferred manifest differs from the verified remote manifest")
    try:
        manifest = verify_backup(directory)
        # This is a remote POSIX scope, not a local filesystem location. macOS
        # resolves /var to /private/var, so local Path.resolve() would falsely
        # reject the Linux server's otherwise identical /var/lib scope.
        if manifest["source_data_dir"] != config["source_data_dir"]:
            raise ValueError("scope mismatch")
        validate_snapshot(_json(_read_regular(directory / "snapshot.json")))
        timestamp = datetime.fromisoformat(manifest["created_at"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if name and timestamp != name:
            raise ValueError("name mismatch")
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error) as error:
        raise BackupError("backup verification failed") from error
    return manifest


def _pack(directory: Path) -> bytes:
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for name in FILES:
            raw = _read_regular(directory / name, MAX_MANIFEST if name == "manifest.json" else MAX_BYTES)
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o600, 0
            archive.addfile(info, io.BytesIO(raw))
    if target.tell() > MAX_BYTES:
        raise BackupError("archive exceeds the size limit")
    return target.getvalue()


def _unpack(data: bytes, directory: Path) -> None:
    if len(data) > MAX_BYTES:
        raise BackupError("archive exceeds the size limit")
    names, total = set(), 0
    try:
        # Bound decompression before tarfile can allocate extended-header data.
        # The three fixed filenames need no PAX metadata; padding is bounded too.
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
            plain = compressed.read(MAX_BYTES + TAR_OVERHEAD + 1)
        if len(plain) > MAX_BYTES + TAR_OVERHEAD:
            raise BackupError("decompressed archive exceeds the size limit")
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as archive:
            for member in archive:
                if (member.name not in FILES or member.name in names or not member.isfile()
                        or member.issparse() or member.pax_headers or member.size < 0):
                    raise BackupError("archive must contain only the three unique regular backup files")
                total += member.size
                if total > MAX_BYTES or (member.name == "manifest.json" and member.size > MAX_MANIFEST):
                    raise BackupError("archive contents exceed the size limit")
                source = archive.extractfile(member)
                if source is None:
                    raise BackupError("archive member cannot be read")
                raw = source.read(member.size + 1)
                if len(raw) != member.size:
                    raise BackupError("archive member size mismatch")
                _write_private(directory / member.name, raw)
                names.add(member.name)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise BackupError("invalid compressed archive") from error
    if names != set(FILES):
        raise BackupError("archive is missing backup files")


def _decrypt_to(archive: Path, key: bytes, directory: Path, config: dict) -> dict:
    try:
        data = _cipher(key).decrypt(_read_regular(archive))
    except Exception as error:
        # InvalidToken contains no useful user-facing diagnostic; avoid any
        # dependency traceback or accidentally exposing credential material.
        if isinstance(error, BackupError):
            raise
        raise BackupError("encrypted archive authentication failed") from error
    _unpack(data, directory)
    match = ARCHIVE_RE.fullmatch(archive.name)
    return _verify(directory, config, name=match[1] if match else None)


def _check_archive(archive: Path, key: bytes, config: dict, *, manifest_hash=None) -> dict:
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=config["destination"]) as temporary:
        directory = Path(temporary)
        manifest = _decrypt_to(archive, key, directory, config)
        if manifest_hash and hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest() != manifest_hash:
            raise BackupError("existing encrypted backup has a different remote manifest")
        return manifest


def _publish(path: Path, data: bytes) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        # Linking is an atomic no-overwrite publication. os.replace would
        # silently destroy a conflicting already-published archive.
        os.link(temporary, path, follow_symlinks=False)
        _fsync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _prune(config: dict, key: bytes, *, preserve: Path) -> int:
    candidates = []
    for archive in Path(config["destination"]).iterdir():
        if not ARCHIVE_RE.fullmatch(archive.name) or archive.is_symlink():
            continue
        try:
            _check_archive(archive, key, config)
            candidates.append((archive.name, archive, archive.lstat()))
        except (BackupError, OSError):
            continue
    candidates.sort(reverse=True, key=lambda entry: entry[0])
    # The server's latest completed backup can regress after a restore or clock
    # adjustment. Always retain the archive selected by this successful sync.
    if not any(archive == preserve for _, archive, _ in candidates):
        return 0
    retained = {preserve}
    for _, archive, _ in candidates:
        if len(retained) < config["keep"]:
            retained.add(archive)
    removed = 0
    for _, archive, original in candidates:
        if archive in retained:
            continue
        try:
            current = archive.lstat()
            if ((current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
                    != (original.st_dev, original.st_ino, original.st_mtime_ns, original.st_size)):
                continue
            _check_archive(archive, key, config)
            archive.unlink()
            removed += 1
        except (BackupError, OSError):
            continue
    if removed:
        _fsync(Path(config["destination"]))
    return removed


def _status(config: dict, *, result=None, error=None) -> None:
    destination = Path(config["destination"])
    previous = None
    try:
        old = _json(_read_regular(destination / "status.json", MAX_MANIFEST))
        candidate = old.get("last_success")
        if (old.get("schema") == STATUS_SCHEMA and isinstance(candidate, dict)
                and set(candidate) == {"completed_at", "backup_name", "manifest_sha256", "archive_size_bytes", "pruned"}
                and isinstance(candidate["backup_name"], str) and NAME_RE.fullmatch(candidate["backup_name"])
                and isinstance(candidate["manifest_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", candidate["manifest_sha256"])):
            previous = candidate
    except (BackupError, OSError):
        pass
    now = datetime.now(timezone.utc).isoformat()
    payload = {"schema": STATUS_SCHEMA, "last_attempt": now,
               "status": "success" if result else "failed", "last_success": result or previous}
    if error:
        payload["error"] = error
    descriptor, filename = tempfile.mkstemp(prefix=".status-", dir=destination)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write((json.dumps(payload, sort_keys=True) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        if (destination / "status.json").is_symlink():
            raise BackupError("status file cannot be a symbolic link")
        os.replace(temporary, destination / "status.json")
        _fsync(destination)
    finally:
        temporary.unlink(missing_ok=True)


def sync(config: dict) -> dict:
    with _lock(config):
        try:
            key = _key(config, create=True)
            metadata = _remote_metadata(inspect_latest(config), config)
            archive = Path(config["destination"]) / (metadata["name"] + ".tar.gz.fernet")
            if archive.exists() or archive.is_symlink():
                _check_archive(archive, key, config, manifest_hash=metadata["manifest_sha256"])
            else:
                with tempfile.TemporaryDirectory(prefix=".download-", dir=config["destination"]) as temporary:
                    directory = Path(temporary)
                    fetch_completed(config, metadata, directory)
                    _verify(directory, config, name=metadata["name"], manifest_hash=metadata["manifest_sha256"])
                    encrypted = _cipher(key).encrypt(_pack(directory))
                    if len(encrypted) > MAX_BYTES:
                        raise BackupError("encrypted archive exceeds the size limit")
                    # Verify the actual encrypted bytes before publication.
                    staged = directory.parent / (".encrypted-" + metadata["name"])
                    try:
                        _write_private(staged, encrypted)
                        _check_archive(staged, key, config, manifest_hash=metadata["manifest_sha256"])
                        _publish(archive, encrypted)
                    finally:
                        staged.unlink(missing_ok=True)
            removed = _prune(config, key, preserve=archive)
            result = {"completed_at": datetime.now(timezone.utc).isoformat(), "backup_name": metadata["name"],
                      "manifest_sha256": metadata["manifest_sha256"],
                      "archive_size_bytes": archive.stat().st_size, "pruned": removed}
            _status(config, result=result)
            return result
        except Exception as error:
            # Detailed exception objects can contain remote stderr, local
            # paths, or secrets. Persist only a stable error category.
            _status(config, error="backup_failed")
            if isinstance(error, BackupError):
                raise
            raise BackupError("backup failed; no archives were pruned before verification") from error


def verify(config: dict, archive: Path) -> dict:
    with _lock(config):
        return _check_archive(archive, _key(config), config)


def restore(config: dict, archive: Path, output: Path) -> dict:
    _absolute(str(output))
    _no_symlink_parents(output)
    if output.exists():
        raise BackupError("restore output must be a new directory")
    _mkdir(output.parent, private=False)
    with _lock(config):
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=output.parent) as temporary:
            directory = Path(temporary) / "restored"
            directory.mkdir(mode=0o700)
            manifest = _decrypt_to(archive, _key(config), directory, config)
            # Never install a snapshot or call initialize: this is an offline
            # extraction for inspection or a separate recovery procedure.
            if output.exists() or output.is_symlink():
                raise BackupError("restore output changed while verifying")
            os.replace(directory, output)
            _fsync(output.parent)
            return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "verify", "restore"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--config", type=Path, required=True)
        if name != "sync":
            subparser.add_argument("--archive", type=Path, required=True)
        if name == "restore":
            subparser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "sync":
            result = sync(config)
            print(json.dumps({"status": "success", **result}, sort_keys=True))
        else:
            manifest = (verify(config, args.archive) if args.command == "verify"
                        else restore(config, args.archive, args.output))
            print(json.dumps({"status": "verified", "created_at": manifest["created_at"],
                              "database_schema_version": manifest["database_schema_version"]}, sort_keys=True))
        return 0
    except BackupError as error:
        print(f"Backup error: {error}", file=sys.stderr)
    except Exception:
        print("Backup error: operation failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
