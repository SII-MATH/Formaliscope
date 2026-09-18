from __future__ import annotations

import argparse
from pathlib import Path

from .build import write_snapshot
from .server import serve


def main():
    app_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="KIP126 Blueprint correspondence review")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="rebuild immutable evidence snapshot")
    build.add_argument("--source", type=Path, required=True,
                       help="path to a separate KIP126 source checkout")
    start = sub.add_parser("serve", help="serve the local review site")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    snapshot = app_root / ".review" / "snapshot.json"
    if args.command == "build":
        source = args.source.expanduser().resolve()
        if not (source / "blueprint/src/content.tex").is_file() or not (source / "KIP126").is_dir():
            parser.error(f"--source is not a KIP126 checkout: {source}")
        result = write_snapshot(source, snapshot)
        print(f"{len(result['cards'])} review cards, {result['unlinked_nodes']} unlinked Blueprint nodes")
        print(f"snapshot {result['digest']} -> {snapshot}")
    else:
        if not snapshot.exists():
            parser.error("snapshot missing; run build --source /path/to/KIP126 first")
        serve(snapshot, app_root / ".review" / "judgments.sqlite3",
              app_root / "review_app" / "static", args.host, args.port)


if __name__ == "__main__":
    main()
