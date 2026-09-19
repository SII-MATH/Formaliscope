from __future__ import annotations

import argparse
import os
from pathlib import Path

from .build import write_snapshot
from .server import serve
from .storage import create_backup


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
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    snapshot = data_dir / "snapshot.json"
    if args.command == "build":
        source = args.source.expanduser().resolve()
        if not (source / "blueprint/src/content.tex").is_file() or not (source / "KIP126").is_dir():
            parser.error(f"--source is not a KIP126 checkout: {source}")
        result = write_snapshot(source, snapshot)
        print(f"{len(result['cards'])} review cards, {result['unlinked_nodes']} unlinked Blueprint nodes")
        print(f"snapshot {result['digest']} -> {snapshot}")
    elif args.command == "serve":
        if not snapshot.exists():
            parser.error(f"snapshot missing in {data_dir}; run build first with the same --data-dir")
        serve(snapshot, data_dir / "judgments.sqlite3",
              app_root / "review_app" / "static", args.host, args.port)
    else:
        try:
            destination = create_backup(data_dir, args.output.expanduser().resolve())
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            parser.error(str(exc))
        print(f"consistent backup -> {destination}")


if __name__ == "__main__":
    main()
