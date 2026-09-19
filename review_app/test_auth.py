from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .auth import AuthSettings, AuthStore, DeliveryError
from .build import CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA
from .server import ReviewHTTPServer, initialize, make_handler


class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "review.sqlite3"
        initialize(self.db)
        self.now = 1_700_000_000
        self.sent = []
        self.auth = AuthStore(
            self.db, AuthSettings(mailer="agently", allow_any_email=True),
            sender=lambda email, code: self.sent.append((email, code)),
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_single_use_expiry_and_logout(self):
        self.assertTrue(self.auth.request_code("alice@example.org", "127.0.0.1"))
        email, code = self.sent[-1]
        with sqlite3.connect(self.db) as db:
            self.assertNotIn(code, str(db.execute("SELECT * FROM login_challenges").fetchone()))
        self.assertIsNone(self.auth.verify_code(email, 12345678))
        self.assertIsNone(self.auth.verify_code(email, "00000000" if code != "00000000" else "99999999"))
        token = self.auth.verify_code(email, code)
        self.assertEqual(self.auth.session_email(token), email)
        self.assertIsNone(self.auth.verify_code(email, code))
        self.auth.logout(token)
        self.assertIsNone(self.auth.session_email(token))
        self.now += 61
        self.assertTrue(self.auth.request_code(email, "127.0.0.1"))
        second_code = self.sent[-1][1]
        second_token = self.auth.verify_code(email, second_code)
        self.assertEqual(self.auth.session_email(second_token), email)
        self.now += 61
        self.assertTrue(self.auth.request_code(email, "127.0.0.1"))
        self.now += 601
        self.assertIsNone(self.auth.verify_code(email, self.sent[-1][1]))
        self.now += 12 * 3600
        self.assertIsNone(self.auth.session_email(second_token))

    def test_attempt_and_request_limits(self):
        email = "alice@example.org"
        self.assertTrue(self.auth.request_code(email, "127.0.0.1"))
        code = self.sent[-1][1]
        self.assertFalse(self.auth.request_code(email, "127.0.0.1"))
        wrong = "00000000" if code != "00000000" else "99999999"
        for _ in range(5):
            self.assertIsNone(self.auth.verify_code(email, wrong))
        self.assertIsNone(self.auth.verify_code(email, code))
        self.now += 61
        for number in range(4):
            self.assertTrue(self.auth.request_code(email, "127.0.0.1"))
            self.now += 61
        self.assertFalse(self.auth.request_code(email, "127.0.0.1"))

    def test_delivery_failure_refunds_quota(self):
        def fail(_email, _code):
            raise RuntimeError("provider unavailable")
        self.auth.sender = fail
        with self.assertRaises(DeliveryError):
            self.auth.request_code("alice@example.org", "127.0.0.1")
        self.auth.sender = lambda email, code: self.sent.append((email, code))
        self.assertTrue(self.auth.request_code("alice@example.org", "127.0.0.1"))


class AuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "review.sqlite3"
        initialize(self.db)
        self.sent = {}
        self.auth = AuthStore(
            self.db, AuthSettings(mailer="agently", allow_any_email=True),
            sender=lambda email, code: self.sent.setdefault(email, code),
        )
        self.card = {
            "id": "def:x::KIP126.X", "fingerprint": "a" * 64,
            "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
            "fingerprints": {CURRENT_FINGERPRINT_SCHEME: "a" * 64},
            "label": "def:x", "title": "Example", "chapter": "Test",
            "kind": "definition", "declaration": "KIP126.X", "source_status": "local",
            "statement": "Example", "blueprint_file": "content.tex", "blueprint_line": 1,
            "lean": None, "dependencies": [],
        }
        snapshot = {"schema": SNAPSHOT_SCHEMA, "digest": "b" * 64,
                    "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                    "source_dirty": False, "source_commit": "c" * 40,
                    "unlinked_nodes": 0, "cards": [self.card]}
        static = Path(__file__).parent / "static"
        handler = make_handler(snapshot, self.db, static, self.auth)
        handler.log_message = lambda *args: None
        self.server = ReviewHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, *, body=None, cookie=None, origin=True):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = self.base
        if cookie:
            headers["Cookie"] = cookie
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, headers=headers)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read()

    def login(self, email):
        self.assertEqual(self.request("/api/auth/request-code", body={"email": email})[0], 200)
        status, headers, _ = self.request(
            "/api/auth/verify-code", body={"email": email, "code": self.sent[email]})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_login_authorizes_api_and_binds_judgment_to_email(self):
        self.assertEqual(self.request("/api/catalog")[0], 401)
        self.assertEqual(self.request("/api/export")[0], 401)
        status, _, _ = self.request("/api/auth/request-code", body={"email": "alice@example.org"}, origin=False)
        self.assertEqual(status, 403)
        self.assertFalse(self.sent)
        status, _, _ = self.request("/api/auth/request-code", body={"email": "alice@example.org"})
        self.assertEqual(status, 200)
        status, _, _ = self.request("/api/auth/verify-code", body={"email": "alice@example.org", "code": 12345678})
        self.assertEqual(status, 400)
        status, headers, _ = self.request("/api/auth/verify-code", body={"email": "alice@example.org", "code": self.sent["alice@example.org"]})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/catalog", cookie=cookie)[0], 200)
        payload = {"request_id": str(uuid.uuid4()), "card_id": self.card["id"],
                   "fingerprint": self.card["fingerprint"], "reviewer": "forged@example.org",
                   "verdict": "aligned", "rationale": ""}
        status, _, content = self.request("/api/judgments", body=payload, cookie=cookie)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(content)["judgment"]["reviewer"], "alice@example.org")
        self.assertEqual(self.request("/api/auth/logout", body={}, cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/export", cookie=cookie)[0], 401)

    def test_two_reviewers_have_independent_progress_history_and_exports(self):
        alice = self.login("alice@example.org")
        bob = self.login("bob@example.org")
        request_id = str(uuid.uuid4())
        payload = {"request_id": request_id, "card_id": self.card["id"],
                   "fingerprint": self.card["fingerprint"], "reviewer": "forged@example.org",
                   "verdict": "aligned", "rationale": ""}
        self.assertEqual(self.request("/api/judgments", body=payload, cookie=alice)[0], 201)

        def read(path, cookie):
            status, _, body = self.request(path, cookie=cookie)
            self.assertEqual(status, 200)
            return json.loads(body)

        card_id = self.card["id"]
        self.assertEqual(read("/api/catalog", alice)["cards"][0]["verdict"], "aligned")
        self.assertIsNone(read("/api/catalog", bob)["cards"][0]["verdict"])
        self.assertEqual(len(read(f"/api/history?id={card_id}", alice)["history"]), 1)
        self.assertEqual(read(f"/api/history?id={card_id}&reviewer=alice@example.org", bob)["history"], [])
        self.assertEqual(read(f"/api/card?id={card_id}", bob)["history"], [])
        self.assertEqual(read("/api/export?reviewer=alice@example.org", bob)["judgments"], [])

        self.assertEqual(self.request("/api/judgments", body={**payload, "verdict": "misaligned",
                              "rationale": "Different meaning"}, cookie=bob)[0], 201)
        self.assertEqual(read("/api/catalog", alice)["cards"][0]["verdict"], "aligned")
        self.assertEqual(read("/api/catalog", bob)["cards"][0]["verdict"], "misaligned")
        for email, cookie in (("alice@example.org", alice), ("bob@example.org", bob)):
            exported = read("/api/export", cookie)
            self.assertEqual(exported["reviewer"], email)
            self.assertEqual(len(exported["judgments"]), 1)
            self.assertEqual(exported["judgments"][0]["reviewer"], email)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM judgments").fetchone()[0], 2)

    def test_public_origin_and_prefixed_cookie(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.auth.settings = AuthSettings(
            mailer="agently", allow_any_email=True,
            public_origin="https://review.example.org", cookie_path="/proxy/18766/",
        )
        snapshot = {"digest": "b" * 64, "source_commit": "c" * 40,
                    "unlinked_nodes": 0, "cards": [self.card]}
        handler = make_handler(snapshot, self.db, Path(__file__).parent / "static", self.auth)
        handler.log_message = lambda *args: None
        self.server = ReviewHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

        status, headers, _ = self.request("/api/auth/request-code", body={"email": "alice@example.org"})
        self.assertEqual(status, 403)
        req = Request(self.base + "/api/auth/request-code",
                      data=b'{"email":"alice@example.org"}',
                      headers={"Content-Type": "application/json", "Origin": "https://review.example.org"})
        with urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
        req = Request(self.base + "/api/auth/verify-code",
                      data=json.dumps({"email": "alice@example.org", "code": self.sent["alice@example.org"]}).encode(),
                      headers={"Content-Type": "application/json", "Origin": "https://review.example.org"})
        with urlopen(req, timeout=5) as response:
            cookie = response.headers["Set-Cookie"]
        self.assertIn("Path=/proxy/18766/", cookie)
        self.assertIn("Secure", cookie)


if __name__ == "__main__":
    unittest.main()
