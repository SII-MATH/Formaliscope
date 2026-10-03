"""Loopback HTTP transport, session authorization, and cached static assets."""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .auth import AuthSettings, AuthStore, DeliveryError, SESSION_LIFETIME, normalize_email
from .build import normalize_snapshot
# Keep the historical server imports working for existing integrations. New
# database consumers can import these modules without loading HTTP transport.
from .database import DB_SCHEMA_VERSION, MIGRATIONS, connect, database_schema_version, initialize
from .judgments import (VERDICTS, WRITE_LOCK, _card_fingerprints, _judgment_matches,
                        admin_summary, backfill_review_basis, catalog, history,
                        reviewer_export, reviewer_profile, submit, update_reviewer_profile)

MAX_BODY = 16_384
SESSION_COOKIE = "kip126_review_session"


def make_handler(snapshot: dict, db_path: Path, static_dir: Path, auth: AuthStore,
                 *, preview: bool = False, admin_emails: frozenset[str] = frozenset()):
    cards_by_id = {card["id"]: card for card in snapshot["cards"]}
    files = {"/": ("statement.html" if snapshot.get('review_mode') == 'statement' else "index.html", "text/html; charset=utf-8"),
             "/admin": ("admin.html", "text/html; charset=utf-8"),
             "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
             "/statement.js": ("statement.js", "text/javascript; charset=utf-8"),
             "/statement-api.js": ("statement-api.js", "text/javascript; charset=utf-8"),
             "/statement-identity.js": ("statement-identity.js", "text/javascript; charset=utf-8"),
             "/statement-graph.js": ("statement-graph.js", "text/javascript; charset=utf-8"),
             "/directory-tree.js": ("directory-tree.js", "text/javascript; charset=utf-8"),
             "/review-labels.js": ("review-labels.js", "text/javascript; charset=utf-8"),
             "/statement.css": ("statement.css", "text/css; charset=utf-8"),
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
            if path == "/healthz":
                # Static assets and the immutable snapshot have loaded before
                # the handler is created. Readiness must not read identities,
                # access card evidence, issue a login code, or mutate SQLite.
                self._json(200, {"ready": True, "snapshot_schema": snapshot.get("schema"),
                                 "database_schema": DB_SCHEMA_VERSION})
                return
            if path == '/api/config':
                self._json(200, {'preview': preview, 'review_mode': snapshot.get('review_mode', 'blueprint')})
                return
            if path == "/login":
                viewer = self._viewer()
                if viewer:
                    self._redirect("./")
                else:
                    self._serve_static(path)
                return
            if path in static_payloads and path not in {"/", "/admin"}:
                self._serve_static(path)
                return
            viewer = self._viewer()
            if path == "/" and not viewer and not preview:
                self._redirect("./login")
                return
            if path == "/api/auth/me":
                profile = reviewer_profile(db_path, viewer) if viewer else {}
                self._json(200, {"email": viewer, 'display_name': profile.get('display_name', ''),
                    'is_admin': viewer in admin_emails or bool(preview and profile.get('preview_admin')),
                    'preview': preview}) if viewer else self._json(401, {"error": "请先登录"})
                return
            if path == "/":
                self._serve_static(path)
                return
            if path == '/admin' or path == '/api/admin/summary':
                profile = reviewer_profile(db_path, viewer) if viewer else {}
                if not viewer:
                    if path.startswith('/api/'):
                        self._json(401, {'error': '请先登录'})
                    else:
                        self._redirect('./') if preview else self._redirect('./login')
                elif viewer not in admin_emails and not (preview and profile.get('preview_admin')):
                    self._json(403, {'error': '此入口仅对管理员开放'})
                elif path == '/admin':
                    self._serve_static(path)
                else:
                    self._json(200, admin_summary(snapshot, db_path))
                return
            if path.startswith("/api/") and not viewer:
                self._json(401, {"error": "登录已过期，请重新登录"})
                return
            if path == "/api/catalog":
                initial = parse_qs(parsed.query).get("initial", [None])[0]
                self._json(200, catalog(snapshot, db_path, viewer, initial_id=initial))
                return
            if path == '/api/module':
                module = parse_qs(parsed.query).get('file', [''])[0]
                source = snapshot.get('modules', {}).get(module)
                self._json(200, {'file': module, 'source': source}) if source is not None else self._json(404, {'error': '源码不存在'})
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
                self._json(200, reviewer_export(snapshot, db_path, viewer))
                return
            self._json(404, {"error": "页面不存在"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {"/api/judgments", "/api/auth/request-code",
                            "/api/auth/verify-code", "/api/auth/logout", '/api/profile', '/api/preview/session', '/api/preview/resume'}:
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
            if path == '/api/preview/session':
                name = payload.get('display_name')
                if not preview:
                    self._json(404, {'error': '页面不存在'})
                elif not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                    self._json(400, {'error': '请输入 1–60 字的姓名'})
                else:
                    token, resume_key = auth.create_reviewer(name.strip())
                    self._json(201, {'ok': True, 'resume_key': resume_key}, extra_headers={'Set-Cookie': self._cookie(token, max_age=SESSION_LIFETIME)})
                return
            if path == '/api/preview/resume':
                if not preview:
                    self._json(404, {'error': '页面不存在'})
                else:
                    token = auth.resume_reviewer(payload.get('resume_key'))
                    if not token:
                        self._json(401, {'error': '此身份的恢复凭证无效，请新建预览身份'})
                    else:
                        self._json(200, {'ok': True}, extra_headers={'Set-Cookie': self._cookie(token, max_age=SESSION_LIFETIME)})
                return
            if preview and path in {'/api/auth/request-code', '/api/auth/verify-code'}:
                self._json(404, {'error': '预览使用姓名入口'})
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
            if path == '/api/profile':
                code, result = update_reviewer_profile(db_path, viewer, payload.get('display_name'))
                self._json(code, result)
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


def serve(snapshot_path: Path, db_path: Path, static_dir: Path, host: str, port: int,
          *, preview: bool = False, admin_emails: frozenset[str] = frozenset()) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("仅允许监听本机；远程访问需先配置可信认证代理")
    snapshot = normalize_snapshot(json.loads(snapshot_path.read_text(encoding="utf-8")))
    initialize(db_path)
    backfill_review_basis(db_path, snapshot)
    if preview:
        from .preview import PreviewAuthStore
        auth = PreviewAuthStore(db_path)
    else:
        auth = AuthStore(db_path, AuthSettings.from_env())
    server = ReviewHTTPServer((host, port), make_handler(snapshot, db_path, static_dir, auth,
                             preview=preview, admin_emails=admin_emails))
    print(f"KIP126 review: http://{host}:{server.server_port}/", flush=True)
    server.serve_forever()
