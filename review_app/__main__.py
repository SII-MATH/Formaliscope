from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .build import normalize_snapshot, write_snapshot
from .server import (backfill_review_basis, database_schema_version, initialize,
                     serve)
from .storage import create_backup, install_snapshot


def main():
    app_root = Path(__file__).resolve().parents[1]
    default_data_dir = Path(os.environ.get("REVIEW_DATA_DIR", app_root / ".review"))
    parser = argparse.ArgumentParser(description="KIP126 Blueprint correspondence review")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="rebuild immutable evidence snapshot")
    build.add_argument("--source", type=Path, required=True,
                       help="path to a separate KIP126 source checkout")
    build.add_argument("--data-dir", type=Path, default=default_data_dir,
                       help="persistent data directory (or REVIEW_DATA_DIR)")
    build.add_argument("--require-clean", action="store_true",
                       help="refuse a reviewed-source checkout with local changes")
    start = sub.add_parser("serve", help="serve the local review site")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)
    start.add_argument("--data-dir", type=Path, default=default_data_dir,
                       help="persistent data directory (or REVIEW_DATA_DIR)")
    backup = sub.add_parser("backup", help="create a consistent online backup")
    backup.add_argument("--data-dir", type=Path, default=default_data_dir,
                        help="persistent data directory (or REVIEW_DATA_DIR)")
    backup.add_argument("--output", type=Path, required=True,
                        help="directory that will contain timestamped backups")
    install = sub.add_parser("install-snapshot", help="validate and install a reviewed-source snapshot")
    install.add_argument("--file", type=Path, required=True, help="snapshot artifact to install")
    install.add_argument("--data-dir", type=Path, default=default_data_dir,
                         help="persistent data directory (or REVIEW_DATA_DIR)")
    install.add_argument("--allow-dirty-source", action="store_true",
                         help="allow a development snapshot built from local source changes")
    migrate = sub.add_parser("migrate", help="apply pending database migrations")
    migrate.add_argument("--data-dir", type=Path, default=default_data_dir,
                         help="persistent data directory (or REVIEW_DATA_DIR)")
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    snapshot = data_dir / "snapshot.json"
    if args.command == "build":
        source = args.source.expanduser().resolve()
        if not (source / "blueprint/src/content.tex").is_file() or not (source / "KIP126").is_dir():
            parser.error(f"--source is not a KIP126 checkout: {source}")
        try:
            result = write_snapshot(source, snapshot, require_clean=args.require_clean)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"{len(result['cards'])} review cards, {result['unlinked_nodes']} unlinked Blueprint nodes")
        comparison = result["comparison"]
        print("source update: " + ", ".join(f"{name}={count}" for name, count in comparison.items()))
        print(f"snapshot {result['digest']} -> {snapshot}")
    elif args.command == "serve":
        if not snapshot.exists():
            parser.error(f"snapshot missing in {data_dir}; run build first with the same --data-dir")
        serve(snapshot, data_dir / "judgments.sqlite3",
              app_root / "review_app" / "static", args.host, args.port)
    elif args.command == "backup":
        try:
            destination = create_backup(data_dir, args.output.expanduser().resolve())
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            parser.error(str(exc))
        print(f"consistent backup -> {destination}")
    elif args.command == "install-snapshot":
        try:
            installed, comparison = install_snapshot(
                args.file.expanduser().resolve(), data_dir,
                allow_dirty_source=args.allow_dirty_source)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print("source update: " + ", ".join(f"{name}={count}" for name, count in comparison.items()))
        print(f"installed snapshot {installed['digest']} from source {installed['source_commit']}")
    else:
        if not snapshot.is_file():
            parser.error(f"snapshot missing in {data_dir}; install or build it before migrating")
        before = database_schema_version(data_dir / "judgments.sqlite3")
        initialize(data_dir / "judgments.sqlite3")
        active_snapshot = normalize_snapshot(json.loads(snapshot.read_text(encoding="utf-8")))
        backfilled = backfill_review_basis(data_dir / "judgments.sqlite3", active_snapshot)
        after = database_schema_version(data_dir / "judgments.sqlite3")
        print(f"database schema: {before} -> {after}")
        print(f"review bases backfilled: {backfilled}")


if __name__ == "__main__":
    main()
