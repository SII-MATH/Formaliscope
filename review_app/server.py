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
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .auth import AuthSettings, AuthStore, DeliveryError, SESSION_LIFETIME, normalize_email

VERDICTS = frozenset({"aligned", "partial", "misaligned", "uncertain"})
MAX_BODY = 16_384
SESSION_COOKIE = "kip126_review_session"
WRITE_LOCK = threading.Lock()


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path.parent.chmod(0o700)
    with closing(connect(db_path)) as db:
        # Journal mode is persistent. Setting it on every GET takes a database
        # lock and makes readers compete with one another under burst load.
        db.execute("PRAGMA journal_mode=WAL")
        old_schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='judgments'").fetchone()
        if old_schema and "request_id TEXT UNIQUE" in old_schema[0]:
            # The first release made request IDs globally unique. Rebuild the
            # table so retries and IDs are scoped to a reviewer's own account.
            db.execute("BEGIN IMMEDIATE")
            db.execute("ALTER TABLE judgments RENAME TO judgments_legacy")
            db.execute("""CREATE TABLE judgments (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(reviewer, request_id)
            )""")
            db.execute("""INSERT INTO judgments
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at)
                SELECT id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at
                FROM judgments_legacy ORDER BY rowid""")
            db.execute("DROP TABLE judgments_legacy")
            db.execute("COMMIT")
        else:
            db.execute("""CREATE TABLE IF NOT EXISTS judgments (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                card_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                rationale TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(reviewer, request_id)
            )""")
        db.execute("CREATE INDEX IF NOT EXISTS judgments_reviewer_card_created ON judgments(reviewer, card_id, created_at)")
    db_path.chmod(0o600)


def catalog(snapshot: dict, db_path: Path, reviewer: str, *, initial_id: str | None = None) -> dict:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT card_id, fingerprint, verdict, created_at FROM judgments
            WHERE reviewer=? ORDER BY created_at, rowid""", (reviewer,)).fetchall()
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


def history(db_path: Path, card_id: str, reviewer: str) -> list[dict]:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT id, fingerprint, reviewer, verdict, rationale, created_at
            FROM judgments WHERE card_id=? AND reviewer=? ORDER BY created_at DESC, rowid DESC""",
            (card_id, reviewer)).fetchall()
    return [dict(row) for row in rows]


def submit(snapshot: dict, db_path: Path, reviewer: str, payload: dict) -> tuple[int, dict]:
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
    request_id = payload.get("request_id")
    try:
        uuid.UUID(request_id)
    except (TypeError, ValueError):
        return 400, {"error": "缺少有效请求 ID"}
    if verdict not in VERDICTS:
        return 400, {"error": "请选择审核结论"}
    if not isinstance(rationale, str) or len(rationale) > 4000 or (verdict != "aligned" and not rationale.strip()):
        return 400, {"error": "除“对齐”外，请填写理由（最多 4000 字）"}
    if normalize_email(reviewer) != reviewer:
        return 400, {"error": "审核人邮箱无效"}
    canonical = (card_id, card["fingerprint"], reviewer, verdict, rationale.strip())
    # This server has one process. Queue writes briefly in Python rather than
    # sending a simultaneous burst into SQLite's busy wait loop.
    with WRITE_LOCK:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT id, card_id, fingerprint, reviewer, verdict, rationale, created_at
                FROM judgments WHERE reviewer=? AND request_id=?""", (reviewer, request_id)).fetchone()
            if previous:
                old = (previous["card_id"], previous["fingerprint"], previous["reviewer"],
                       previous["verdict"], previous["rationale"])
                db.execute("COMMIT")
                return (200, {"judgment": dict(previous), "replayed": True}) if old == canonical else (409, {"error": "请求 ID 已用于另一条判断"})
            record = {
                "id": str(uuid.uuid4()), "request_id": request_id, "card_id": card_id,
                "fingerprint": card["fingerprint"], "reviewer": reviewer,
                "verdict": verdict, "rationale": rationale.strip(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute("""INSERT INTO judgments
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at)
                VALUES (:id, :request_id, :card_id, :fingerprint, :reviewer, :verdict, :rationale, :created_at)""", record)
            db.execute("COMMIT")
    return 201, {"judgment": record, "replayed": False}


def make_handler(snapshot: dict, db_path: Path, static_dir: Path, auth: AuthStore):
    cards_by_id = {card["id"]: card for card in snapshot["cards"]}
    files = {"/": ("index.html", "text/html; charset=utf-8"),
             "/login": ("login.html", "text/html; charset=utf-8"),
             "/login.js": ("login.js", "text/javascript; charset=utf-8"),
             "/login.css": ("login.css", "text/css; charset=utf-8"),
             "/app.js": ("app.js", "text/javascript; charset=utf-8"),
             "/app.css": ("app.css", "text/css; charset=utf-8"),
             "/latex-renderer.js": ("latex-renderer.js", "text/javascript; charset=utf-8"),
             "/lean-renderer.js": ("lean-renderer.js", "text/javascript; charset=utf-8"),
             "/mathjax-tex-svg.js": ("mathjax-tex-svg.js", "text/javascript; charset=utf-8")}
    static_payloads = {}
    for path, (filename, media) in files.items():
        data = (static_dir / filename).read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest() + '"' if path != "/" else None
        static_payloads[path] = (data, media, etag)

    class Handler(BaseHTTPRequestHandler):
        server_version = "KIP126Review/1"

        def _headers(self, code: int, content_type: str, size: int, *, etag: str | None = None,
                     extra_headers: dict[str, str] | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=0, must-revalidate" if etag else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
            if etag:
                self.send_header("ETag", etag)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _json(self, code: int, payload: dict | list, *, etag: str | None = None,
                  extra_headers: dict[str, str] | None = None):
            if etag and self.headers.get("If-None-Match") == etag:
                self._headers(304, "application/json; charset=utf-8", 0, etag=etag)
                return
            data = json.dumps(payload, ensure_ascii=False).encode()
            self._headers(code, "application/json; charset=utf-8", len(data), etag=etag,
                          extra_headers=extra_headers)
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The write may already be committed when a browser navigates away.
                # The request UUID lets that browser safely retry the same submit.
                pass

        def _session_token(self) -> str | None:
            try:
                cookies = SimpleCookie()
                cookies.load(self.headers.get("Cookie", ""))
                item = cookies.get(SESSION_COOKIE)
                return item.value if item else None
            except CookieError:
                return None

        def _viewer(self) -> str | None:
            return auth.session_email(self._session_token())

        def _cookie(self, token: str, *, max_age: int) -> str:
            value = (f"{SESSION_COOKIE}={token}; Path={auth.settings.cookie_path}; "
                     f"Max-Age={max_age}; HttpOnly; SameSite=Strict")
            if auth.settings.public_origin and auth.settings.public_origin.startswith("https://"):
                value += "; Secure"
            return value

        def _redirect(self, location: str):
            self._headers(303, "text/plain; charset=utf-8", 0,
                          extra_headers={"Location": location})

        def _serve_static(self, path: str):
            data, media, etag = static_payloads[path]
            if etag and self.headers.get("If-None-Match") == etag:
                self._headers(304, media, 0, etag=etag)
                return
            self._headers(200, media, len(data), etag=etag)
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            expected = ({auth.settings.public_origin} if auth.settings.public_origin
                        else {f"http://{host}", f"https://{host}"})
            return bool(origin and origin in expected)

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/login":
                viewer = self._viewer()
                if viewer:
                    self._redirect("./")
                else:
                    self._serve_static(path)
                return
            if path in static_payloads and path != "/":
                self._serve_static(path)
                return
            viewer = self._viewer()
            if path == "/" and not viewer:
                self._redirect("./login")
                return
            if path == "/api/auth/me":
                self._json(200, {"email": viewer}) if viewer else self._json(401, {"error": "请先登录"})
                return
            if path == "/":
                self._serve_static(path)
                return
            if path.startswith("/api/") and not viewer:
                self._json(401, {"error": "登录已过期，请重新登录"})
                return
            if path == "/api/catalog":
                initial = parse_qs(parsed.query).get("initial", [None])[0]
                self._json(200, catalog(snapshot, db_path, viewer, initial_id=initial))
                return
            if path == "/api/card":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                card = cards_by_id.get(card_id)
                if card is None:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                self._json(200, {"card": card, "history": history(db_path, card_id, viewer)},
                           etag=None)  # History changes after a judgment.
                return
            if path == "/api/history":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                if card_id not in cards_by_id:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                self._json(200, {"history": history(db_path, card_id, viewer)})
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
                    rows = [dict(row) for row in db.execute(
                        "SELECT * FROM judgments WHERE reviewer=? ORDER BY created_at, rowid", (viewer,))]
                self._json(200, {"snapshot_digest": snapshot["digest"], "source_commit": snapshot["source_commit"],
                                 "reviewer": viewer, "judgments": rows})
                return
            self._json(404, {"error": "页面不存在"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {"/api/judgments", "/api/auth/request-code",
                            "/api/auth/verify-code", "/api/auth/logout"}:
                self._json(404, {"error": "页面不存在"})
                return
            if (not self._origin_ok() or
                    self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json"):
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
            if not isinstance(payload, dict):
                self._json(400, {"error": "请求格式错误"})
                return
            if path == "/api/auth/request-code":
                email = normalize_email(payload.get("email"))
                if not email:
                    self._json(400, {"error": "请输入有效邮箱地址"})
                    return
                try:
                    auth.request_code(email, self.client_address[0])
                except DeliveryError:
                    self._json(503, {"error": "验证码暂时无法发送，请稍后再试"})
                    return
                self._json(200, {"message": "如果邮箱可用，验证码已发送；请检查收件箱。"})
                return
            if path == "/api/auth/verify-code":
                email = normalize_email(payload.get("email"))
                code = payload.get("code")
                token = auth.verify_code(email, code) if email and auth.settings.permits(email) else None
                if not token:
                    self._json(400, {"error": "验证码无效或已过期"})
                    return
                self._json(200, {"email": email}, extra_headers={
                    "Set-Cookie": self._cookie(token, max_age=SESSION_LIFETIME)})
                return
            if path == "/api/auth/logout":
                auth.logout(self._session_token())
                self._json(200, {"ok": True}, extra_headers={
                    "Set-Cookie": self._cookie("", max_age=0)})
                return
            viewer = self._viewer()
            if not viewer:
                self._json(401, {"error": "登录已过期，请重新登录"})
                return
            code, result = submit(snapshot, db_path, viewer, payload)
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
    auth = AuthStore(db_path, AuthSettings.from_env())
    server = ReviewHTTPServer((host, port), make_handler(snapshot, db_path, static_dir, auth))
    print(f"KIP126 review: http://{host}:{server.server_port}/", flush=True)
    server.serve_forever()
