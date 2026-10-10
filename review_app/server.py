"""Loopback HTTP transport, session authorization, and cached static assets."""

from __future__ import annotations

import hashlib
import gzip
import ipaddress
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .auth import AuthSettings, AuthStore, DeliveryError, SESSION_LIFETIME, normalize_email
from .name_auth import NameAuthStore, RateLimited, name_settings
from .session_store import SessionStore
from .symbols import SymbolIndex
from .build import normalize_snapshot
from .admin import safe_display_name, user_directory, user_reviews
from .repositories import COLLECTION_SCHEMA, current_datasets, dataset_id, dataset_info, datasets, select_dataset
# Keep the historical server imports working for existing integrations. New
# database consumers can import these modules without loading HTTP transport.
from .database import DB_SCHEMA_VERSION, MIGRATIONS, connect, database_schema_version, initialize
from .judgments import (VERDICTS, WRITE_LOCK, _card_fingerprints, _judgment_matches,
                        admin_summary, backfill_review_basis, catalog, history,
                        reviewer_export, reviewer_profile, submit, update_reviewer_profile,
                        history_page, review_state, save_draft)

MAX_BODY = 16_384
SESSION_COOKIE = "kip126_review_session"


def accepts_gzip(value: str) -> bool:
    qualities = {}
    for item in value.lower().split(','):
        name, *parameters = item.strip().split(';')
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith('q='):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        qualities[name.strip()] = quality if 0 <= quality <= 1 else 0.0
    return qualities.get('gzip', qualities.get('*', 0)) > 0


def make_handler(snapshot: dict, db_path: Path, static_dir: Path, auth: SessionStore,
                 *, preview: bool = False, admin_emails: frozenset[str] = frozenset(),
                 trust_proxy_ip: bool = False):
    name_mode = isinstance(auth, NameAuthStore)
    auth_mode = "preview" if preview else "name" if name_mode else "email"
    installed = snapshot
    contexts = {dataset_id(item): (item, {card['id']: card for card in item['cards']}, SymbolIndex(item))
                for item in datasets(installed)}
    files = {"/": ("statement.html" if snapshot.get('review_mode') == 'statement' else "index.html", "text/html; charset=utf-8"),
             "/favicon.svg": ("favicon.svg", "image/svg+xml"),
             "/admin": ("admin.html", "text/html; charset=utf-8"),
             "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
             "/admin.css": ("admin.css", "text/css; charset=utf-8"),
             "/statement.js": ("statement.js", "text/javascript; charset=utf-8"),
             "/statement-api.js": ("statement-api.js", "text/javascript; charset=utf-8"),
             "/statement-identity.js": ("statement-identity.js", "text/javascript; charset=utf-8"),
             "/statement-save.js": ("statement-save.js", "text/javascript; charset=utf-8"),
             "/statement-navigation.js": ("statement-navigation.js", "text/javascript; charset=utf-8"),
             "/statement-symbols.js": ("statement-symbols.js", "text/javascript; charset=utf-8"),
             "/statement-graph.js": ("statement-graph.js", "text/javascript; charset=utf-8"),
             "/directory-tree.js": ("directory-tree.js", "text/javascript; charset=utf-8"),
             "/review-labels.js": ("review-labels.js", "text/javascript; charset=utf-8"),
             "/statement.css": ("statement.css", "text/css; charset=utf-8"),
             "/login": ("login.html", "text/html; charset=utf-8"),
             "/login.js": ("login.js", "text/javascript; charset=utf-8"),
             "/login.css": ("login.css", "text/css; charset=utf-8"),
             "/password": ("password.html", "text/html; charset=utf-8"),
             "/password.js": ("password.js", "text/javascript; charset=utf-8"),
             "/auth-notice.js": ("auth-notice.js", "text/javascript; charset=utf-8"),
             "/auth-notice.css": ("auth-notice.css", "text/css; charset=utf-8"),
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
        server_version = "FormaliscopeReview/1"

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
                  extra_headers: dict[str, str] | None = None, compress: bool = False):
            if etag and self.headers.get("If-None-Match") == etag:
                self._headers(304, "application/json; charset=utf-8", 0, etag=etag)
                return
            data = json.dumps(payload, ensure_ascii=False, separators=(',', ':') if compress else None).encode()
            if compress:
                extra_headers = {**(extra_headers or {}), 'Vary': 'Accept-Encoding'}
                if len(data) >= 1024 and accepts_gzip(self.headers.get('Accept-Encoding', '')):
                    data = gzip.compress(data, compresslevel=5, mtime=0)
                    extra_headers['Content-Encoding'] = 'gzip'
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
            return auth.session_reviewer(self._session_token())

        def _is_global_admin(self, viewer):
            if name_mode:
                return bool(viewer and auth.is_admin(viewer))
            else:
                profile = reviewer_profile(db_path, viewer) if viewer else {}
                return bool(viewer and (viewer in admin_emails or (preview and profile.get('preview_admin'))))

        def _is_admin(self, viewer):
            if self._is_global_admin(viewer):
                return True
            from contextlib import closing
            with closing(connect(db_path)) as db:
                return bool(viewer and db.execute('SELECT 1 FROM dataset_admins WHERE dataset_id=? AND reviewer=?',
                    (dataset_id(self.review_snapshot), viewer)).fetchone())

        def _context(self):
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if len(query.get('dataset', [])) > 1:
                raise ValueError('请选择一个数据集')
            selected = select_dataset(installed, query.get('dataset', [None])[0])
            self.review_snapshot = selected
            return contexts[dataset_id(selected)]

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

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            # Only an explicitly configured local proxy may supply this value.
            # It must overwrite X-Real-IP, never append a user-supplied header.
            if not trust_proxy_ip:
                return peer
            try:
                if not ipaddress.ip_address(peer).is_loopback:
                    return peer
                values = self.headers.get_all("X-Real-IP", [])
                if len(values) != 1 or "%" in values[0]:
                    return peer
                return str(ipaddress.ip_address(values[0].strip()))
            except ValueError:
                return peer

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/healthz":
                # Static assets and the immutable snapshot have loaded before
                # the handler is created. Readiness must not read identities,
                # access card evidence, issue a login code, or mutate SQLite.
                self._json(200, {"ready": True, "snapshot_schema": installed.get("schema"),
                                 "database_schema": DB_SCHEMA_VERSION})
                return
            try:
                snapshot, cards_by_id, symbols = self._context()
            except ValueError:
                self._json(404, {'error': '仓库版本不存在，请重新选择数据集'})
                return
            if path == '/api/config':
                self._json(200, {'preview': preview, 'auth_mode': auth_mode, 'authentication_notice': 'password-only-v1' if name_mode else None, 'review_mode': snapshot.get('review_mode', 'blueprint')})
                return
            if path == "/login":
                viewer = self._viewer()
                if viewer:
                    self._redirect("./password" if name_mode and auth.must_change_password(viewer) else "./")
                else:
                    self._serve_static(path)
                return
            if path in static_payloads and path not in {"/", "/admin", "/password"}:
                self._serve_static(path)
                return
            viewer = self._viewer()
            if path == "/" and not viewer and not preview:
                self._redirect("./login")
                return
            if path == "/api/auth/me":
                profile = reviewer_profile(db_path, viewer) if viewer else {}
                self._json(200, {"user_id": viewer, "email": None if name_mode else viewer, 'display_name': profile.get('display_name', ''),
                    'is_admin': self._is_admin(viewer), 'auth_mode': auth_mode,
                    'can_view_users': self._is_global_admin(viewer),
                    'must_change_password': bool(name_mode and auth.must_change_password(viewer)),
                    'preview': preview}) if viewer else self._json(401, {"error": "请先登录"})
                return
            if path == '/password':
                if not name_mode:
                    self._json(404, {'error': '页面不存在'})
                elif not viewer:
                    self._redirect('./login')
                else:
                    self._serve_static(path)
                return
            if viewer and name_mode and auth.must_change_password(viewer):
                if path in {'/', '/admin'}:
                    self._redirect('./password')
                else:
                    self._json(403, {'error': '请先修改初始密码', 'password_change_required': True})
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
                elif not self._is_admin(viewer):
                    self._json(403, {'error': '此入口仅对管理员开放'})
                elif path == '/admin':
                    self._serve_static(path)
                else:
                    summary = admin_summary(snapshot, db_path)
                    for row in summary['judgments']:
                        row['display_name'] = safe_display_name(row.get('display_name'))
                    self._json(200, summary)
                return
            if path in {'/api/admin/users', '/api/admin/user'}:
                if not viewer:
                    self._json(401, {'error': '请先登录'})
                elif not self._is_global_admin(viewer):
                    self._json(403, {'error': '用户目录仅对全局管理员开放'})
                elif path == '/api/admin/users':
                    self._json(200, user_directory(installed, db_path, auth_mode=auth_mode, admin_emails=admin_emails))
                else:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    try:
                        if any(len(query.get(key, [])) > 1 for key in ('reviewer', 'cursor', 'limit')):
                            raise ValueError('请选择一个账号及分页参数')
                        result = user_reviews(snapshot, db_path, query.get('reviewer', [''])[0],
                            cursor=int(query.get('cursor', ['0'])[0]), limit=int(query.get('limit', ['25'])[0]),
                            auth_mode=auth_mode, admin_emails=admin_emails, installed=installed)
                    except LookupError as error:
                        self._json(404, {'error': str(error)})
                    except ValueError:
                        self._json(400, {'error': '账号或分页参数无效'})
                    else:
                        self._json(200, result)
                return
            if path.startswith("/api/") and not viewer:
                self._json(401, {"error": "登录已过期，请重新登录"})
                return
            if path == '/api/datasets':
                self._json(200, {'datasets': [dataset_info(item) for item in datasets(installed)],
                                 'current_datasets': [dataset_info(item) for item in current_datasets(installed)],
                                 'selected': dataset_id(snapshot),
                                 'repository_successors': installed.get('repository_successors', {})})
                return
            if path == "/api/catalog":
                initial = parse_qs(parsed.query).get("initial", [None])[0]
                self._json(200, catalog(snapshot, db_path, viewer, initial_id=initial), compress=True)
                return
            if path == '/api/symbol':
                values = parse_qs(parsed.query)
                try:
                    result = symbols.resolve(values.get('card_id', [''])[0], values.get('name', [''])[0],
                                             int(values.get('line', ['0'])[0]), int(values.get('column', ['0'])[0]),
                                             values.get('scope', ['declaration'])[0])
                except KeyError:
                    self._json(404, {'error': '审核对象不存在'})
                except (ValueError, TypeError) as error:
                    self._json(400, {'error': str(error)})
                else:
                    self._json(200, result)
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
                records = (history_page(db_path, card_id, viewer, dataset=dataset_id(snapshot)) if snapshot.get('review_mode') == 'statement'
                           else {"history": history(db_path, card_id, viewer, dataset=dataset_id(snapshot))})
                self._json(200, {"card": card, **records},
                           etag=None)  # History changes after a judgment.
                return
            if path == "/api/review-state":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                if card_id not in cards_by_id:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                self._json(200, review_state(snapshot, db_path, card_id, viewer))
                return
            if path == "/api/history":
                card_id = parse_qs(parsed.query).get("id", [""])[0]
                if card_id not in cards_by_id:
                    self._json(404, {"error": "审核对象不存在"})
                    return
                query = parse_qs(parsed.query)
                try:
                    result = history_page(db_path, card_id, viewer,
                                          limit=int(query.get("limit", ["25"])[0]),
                                          cursor=query.get("cursor", [None])[0], dataset=dataset_id(snapshot))
                except ValueError as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, result)
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
                mode = parse_qs(parsed.query).get("mode", ["history"])[0]
                try:
                    result = reviewer_export(snapshot, db_path, viewer, mode=mode)
                except ValueError as error:
                    self._json(400, {"error": str(error)})
                    return
                self._json(200, result)
                return
            self._json(404, {"error": "页面不存在"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {"/api/auth/register", "/api/judgments", "/api/drafts", "/api/auth/request-code",
                            "/api/auth/verify-code", "/api/auth/logout", '/api/profile', '/api/preview/session', '/api/preview/resume',
                            '/api/auth/password-login', '/api/auth/password', '/api/admin/reset-password'}:
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
            viewer = self._viewer()
            if (viewer and name_mode and auth.must_change_password(viewer)
                    and path not in {'/api/auth/password', '/api/auth/password-login', '/api/auth/logout'}):
                self._json(403, {'error': '请先修改初始密码', 'password_change_required': True})
                return
            if path in {'/api/auth/password-login', '/api/auth/password', '/api/admin/reset-password'}:
                if not name_mode:
                    self._json(404, {'error': '页面不存在'})
                    return
                try:
                    if path == '/api/auth/password-login':
                        token = auth.login_password(payload.get('account'), payload.get('password'), self._client_ip())
                        if not token:
                            self._json(401, {'error': '账号或密码不正确；忘记密码请联系管理员重置'})
                        else:
                            owner = auth.session_reviewer(token)
                            self._json(200, {'ok': True, 'must_change_password': auth.must_change_password(owner)},
                                extra_headers={'Set-Cookie': self._cookie(token, max_age=auth.session_lifetime)})
                    elif not viewer:
                        self._json(401, {'error': '请先登录'})
                    elif path == '/api/admin/reset-password':
                        if not self._is_global_admin(viewer):
                            self._json(403, {'error': '重置密码仅对全局管理员开放'})
                            return
                        auth.reset_password(viewer, payload.get('reviewer'), payload.get('current_password'), self._client_ip())
                        self._json(200, {'ok': True, 'message': '密码已重置为 12345678，该用户的所有设备已退出，下次登录须修改密码'})
                    else:
                        auth.change_password(viewer, payload.get('current_password'), payload.get('new_password'),
                                             self._session_token(), self._client_ip())
                        self._json(200, {'ok': True})
                except RateLimited:
                    self._json(429, {'error': '操作过于频繁，请稍后再试'}, extra_headers={'Retry-After': '60'})
                except ValueError as error:
                    self._json(400, {'error': str(error)})
                return
            if path == '/api/auth/register':
                if not name_mode:
                    self._json(404, {'error': '页面不存在'})
                    return
                try:
                    if self._viewer():
                        self._json(409, {'error': '已登录，请先退出后再注册'})
                        return
                    token, reviewer = auth.create_reviewer(payload.get('display_name'), self._client_ip(), password=payload.get('password'))
                    self._json(201, {'ok': True, 'user_id': reviewer}, extra_headers={
                        'Set-Cookie': self._cookie(token, max_age=auth.session_lifetime)})
                except RateLimited:
                    self._json(429, {'error': '操作过于频繁，请稍后再试'}, extra_headers={'Retry-After': '60'})
                except ValueError as error:
                    self._json(409 if '已注册' in str(error) else 400, {'error': str(error)})
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
            if (preview or name_mode) and path in {'/api/auth/request-code', '/api/auth/verify-code'}:
                self._json(404, {'error': '当前使用姓名入口'})
                return
            if path == "/api/auth/request-code":
                email = normalize_email(payload.get("email"))
                if not email:
                    self._json(400, {"error": "请输入有效邮箱地址"})
                    return
                try:
                    auth.request_code(email, self._client_ip())
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
            writer = save_draft if path == '/api/drafts' else submit
            if installed.get('schema') == COLLECTION_SCHEMA and 'dataset' not in parse_qs(urlsplit(self.path).query):
                self._json(400, {'error': '保存审阅时必须指定仓库版本，请重新打开页面'})
                return
            try:
                snapshot, _, _ = self._context()
            except ValueError:
                self._json(404, {'error': '仓库版本不存在，请重新选择数据集'})
                return
            if snapshot.get('repository', {}).get('id') in installed.get('repository_successors', {}):
                self._json(409, {'error': '该仓库已迁入新仓库，请刷新页面后继续审阅'})
                return
            code, result = writer(snapshot, db_path, viewer, payload)
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
        mode = os.environ.get('REVIEW_AUTH_MODE', 'name').strip().lower()
        if mode == 'name':
            auth = NameAuthStore(db_path, name_settings())
        elif mode == 'email':
            auth = AuthStore(db_path, AuthSettings.from_env())
        else:
            raise ValueError('REVIEW_AUTH_MODE must be name or email')
    server = ReviewHTTPServer((host, port), make_handler(snapshot, db_path, static_dir, auth,
                             preview=preview, admin_emails=admin_emails,
                             trust_proxy_ip=os.environ.get('REVIEW_TRUST_PROXY_IP', '').strip() == '1'))
    print(f"Formaliscope review: http://{host}:{server.server_port}/", flush=True)
    server.serve_forever()
