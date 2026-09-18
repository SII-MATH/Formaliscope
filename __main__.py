from __future__ import annotations

import argparse
from pathlib import Path

from .build import write_snapshot
from .server import serve


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="KIP126 Blueprint correspondence review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="rebuild immutable evidence snapshot")
    start = sub.add_parser("serve", help="serve the local review site")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    snapshot = repo / ".review" / "snapshot.json"
    if args.command == "build":
        result = write_snapshot(repo, snapshot)
        print(f"{len(result['cards'])} review cards, {result['unlinked_nodes']} unlinked Blueprint nodes")
        print(f"snapshot {result['digest']} -> {snapshot}")
    else:
        if not snapshot.exists():
            write_snapshot(repo, snapshot)
        serve(snapshot, repo / ".review" / "judgments.sqlite3", repo / "review_app" / "static", args.host, args.port)


if __name__ == "__main__":
    main()
