"""Small loopback review server; immutable evidence and durable judgments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERDICTS = frozenset({"aligned", "partial", "misaligned", "uncertain"})
MAX_BODY = 16_384
WRITE_LOCK = threading.Lock()


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as db:
        # Journal mode is persistent. Setting it on every GET takes a database
        # lock and makes readers compete with one another under burst load.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS judgments (
            id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
            card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
            rationale TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS judgments_card_created ON judgments(card_id, created_at)")


def catalog(snapshot: dict, db_path: Path, *, initial_id: str | None = None) -> dict:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT card_id, fingerprint, verdict, created_at FROM judgments
            ORDER BY created_at, rowid""").fetchall()
    latest = {row["card_id"]: dict(row) for row in rows}
    cards = []
    for card in snapshot["cards"]:
        judgment = latest.get(card["id"])
        current = judgment if judgment and judgment["fingerprint"] == card["fingerprint"] else None
        cards.append({
            "id": card["id"], "label": card["label"], "title": card["title"],
            "chapter": card["chapter"], "kind": card["kind"],
            "declaration": card["declaration"], "source_status": card["source_status"],
            "verdict": current["verdict"] if current else None,
            "stale": bool(judgment and not current),
        })
    payload = {"digest": snapshot["digest"], "source_commit": snapshot["source_commit"],
               "unlinked_nodes": snapshot["unlinked_nodes"], "cards": cards}
    if initial_id is not None:
        if initial_id == "auto":
            initial_id = next((row["id"] for row in cards
                               if row["source_status"] == "local" and not row["verdict"]),
                              cards[0]["id"] if cards else None)
        payload["initial_evidence"] = next(
            (card for card in snapshot["cards"] if card["id"] == initial_id), None
        )
    return payload


def history(db_path: Path, card_id: str) -> list[dict]:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT id, fingerprint, reviewer, verdict, rationale, created_at
            FROM judgments WHERE card_id=? ORDER BY created_at DESC, rowid DESC""", (card_id,)).fetchall()
    return [dict(row) for row in rows]


def submit(snapshot: dict, db_path: Path, payload: dict) -> tuple[int, dict]:
    if not isinstance(payload, dict):
        return 400, {"error": "请求格式错误"}
    card_id = payload.get("card_id")
    card = next((item for item in snapshot["cards"] if item["id"] == card_id), None)
    if card is None:
        return 404, {"error": "审核对象不存在"}
    if payload.get("fingerprint") != card["fingerprint"]:
        return 409, {"error": "原文或 Lean 对象已更新，请重新打开卡片"}
    verdict = payload.get("verdict")
    rationale = payload.get("rationale", "")
    reviewer = payload.get("reviewer", "")
    request_id = payload.get("request_id")
    try:
        uuid.UUID(request_id)
    except (TypeError, ValueError):
        return 400, {"error": "缺少有效请求 ID"}
    if verdict not in VERDICTS:
        return 400, {"error": "请选择审核结论"}
    if not isinstance(rationale, str) or len(rationale) > 4000 or (verdict != "aligned" and not rationale.strip()):
        return 400, {"error": "除“对齐”外，请填写理由（最多 4000 字）"}
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 80:
        return 400, {"error": "请填写审核人姓名（最多 80 字）"}
    canonical = (card_id, card["fingerprint"], reviewer.strip(), verdict, rationale.strip())
    # This server has one process. Queue writes briefly in Python rather than
    # sending a simultaneous burst into SQLite's busy wait loop.
    with WRITE_LOCK:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT id, card_id, fingerprint, reviewer, verdict, rationale, created_at
                FROM judgments WHERE request_id=?""", (request_id,)).fetchone()
            if previous:
                old = (previous["card_id"], previous["fingerprint"], previous["reviewer"],
                       previous["verdict"], previous["rationale"])
                db.execute("COMMIT")
                return (200, {"judgment": dict(previous), "replayed": True}) if old == canonical else (409, {"error": "请求 ID 已用于另一条判断"})
            record = {
                "id": str(uuid.uuid4()), "request_id": request_id, "card_id": card_id,
                "fingerprint": card["fingerprint"], "reviewer": reviewer.strip(),
                "verdict": verdict, "rationale": rationale.strip(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute("""INSERT INTO judgments
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at)
                VALUES (:id, :request_id, :card_id, :fingerprint, :reviewer, :verdict, :rationale, :created_at)""", record)
            db.execute("COMMIT")
    return 201, {"judgment": record, "replayed": False}


def make_handler(snapshot: dict, db_path: Path, static_dir: Path):
    cards_by_id = {card["id"]: card for card in snapshot["cards"]}
    files = {"/": ("index.html", "text/html; charset=utf-8"),
             "/app.js": ("app.js", "text/javascript; charset=utf-8"),
             "/app.css": ("app.css", "text/css; charset=utf-8"),
             "/latex-renderer.js": ("latex-renderer.js", "text/javascript; charset=utf-8"),
             "/mathjax-tex-svg.js": ("mathjax-tex-svg.js", "text/javascript; charset=utf-8")}
    static_payloads = {}
    for path, (filename, media) in files.items():
        data = (static_dir / filename).read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest() + '"' if path != "/" else None
        static_payloads[path] = (data, media, etag)

    class Handler(BaseHTTPRequestHandler):
        server_version = "KIP126Review/1"

        def _headers(self, code: int, content_type: str, size: int, *, etag: str | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=0, must-revalidate" if etag else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()

        def _json(self, code: int, payload: dict | list, *, etag: str | None = None):
            if etag and self.headers.get("If-None-Match") == etag:
                self._headers(304, "application/json; charset=utf-8", 0, etag=etag)
                return
            data = json.dumps(payload, ensure_ascii=False).encode()
            self._headers(code, "application/json; charset=utf-8", len(data), etag=etag)
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The write may already be committed when a browser navigates away.
                # The request UUID lets that browser safely retry the same submit.
                pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path in static_payloads:
                data, media, etag = static_payloads[path]
                if etag and self.headers.get("If-None-Match") == etag:
                    self._headers(304, media, 0, etag=etag)
                    return
                self._headers(200, media, len(data), etag=etag)
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if path == "/api/catalog":
                initial = parse_qs(parsed.query).get("initial", [None])[0]
                self._json(200, catalog(snapshot, db_path, initial_id=initial))
                return
            if path == "/api/card":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                card = cards_by_id.get(card_id)
                if card is None:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                self._json(200, {"card": card, "history": history(db_path, card_id)},
                           etag=None)  # History changes after a judgment.
                return
            if path == "/api/history":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                if card_id not in cards_by_id:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                self._json(200, {"history": history(db_path, card_id)})
                return
            if path == "/api/evidence":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                card = cards_by_id.get(card_id)
                if card is None:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                self._json(200, card, etag='"' + card["fingerprint"] + '"')
                return
            if path == "/api/export":
                with closing(connect(db_path)) as db:
                    rows = [dict(row) for row in db.execute("SELECT * FROM judgments ORDER BY created_at, rowid")]
                self._json(200, {"snapshot_digest": snapshot["digest"], "source_commit": snapshot["source_commit"], "judgments": rows})
                return
            self._json(404, {"error": "页面不存在"})

        def do_POST(self):
            if urlsplit(self.path).path != "/api/judgments":
                self._json(404, {"error": "页面不存在"})
                return
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if (origin and origin not in {f"http://{host}", f"https://{host}"}) or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self._json(403, {"error": "请求来源无效"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > MAX_BODY:
                    raise ValueError("invalid body length")
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "请求内容无效或过大"})
                return
            code, result = submit(snapshot, db_path, payload)
            self._json(code, result)

    Handler.review_db_path = db_path
    return Handler


class ReviewHTTPServer(ThreadingHTTPServer):
    # A browser opens evidence and history in parallel. With the stdlib's
    # five-connection listen backlog, a small reviewer burst can make unlucky
    # clients wait for the OS SYN retry timer (roughly one second here).
    request_queue_size = 128
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler):
        # Keep WAL alive across short request connections. Otherwise the last
        # connection closing can checkpoint/delete WAL on nearly every request.
        self._db_keeper = connect(handler.review_db_path)
        try:
            self._db_keeper.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            super().__init__(address, handler)
        except BaseException:
            self._db_keeper.close()
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            self._db_keeper.close()


def serve(snapshot_path: Path, db_path: Path, static_dir: Path, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("仅允许监听本机；远程访问需先配置可信认证代理")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("schema") != "kip126-review-snapshot.v1":
        raise ValueError("unsupported snapshot schema")
    initialize(db_path)
    server = ReviewHTTPServer((host, port), make_handler(snapshot, db_path, static_dir))
    print(f"KIP126 review: http://{host}:{server.server_port}/", flush=True)
    server.serve_forever()
