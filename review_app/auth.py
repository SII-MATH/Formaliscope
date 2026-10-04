"""Email-code login for the single-process review server.

The SMTP password never enters SQLite or an HTTP response. Codes are short-lived,
single-use, and stored as HMACs with a local secret separate from the database.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .session_store import SESSION_CACHE_LIMIT, SESSION_LIFETIME, SessionStore

CODE_LIFETIME = 600
MAX_CODE_ATTEMPTS = 5
REQUEST_INTERVAL = 60
EMAIL_HOURLY_LIMIT = 5
IP_HOURLY_LIMIT = 30
GLOBAL_MINUTE_LIMIT = 8
GLOBAL_DAILY_LIMIT = 40  # The currently bound mailbox allows 50 sends/day.
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class DeliveryError(Exception):
    """The configured mail server could not accept a login message."""


def normalize_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    return email if len(email) <= 254 and EMAIL_RE.fullmatch(email) else None


@dataclass(frozen=True)
class AuthSettings:
    mailer: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    allowed_emails: frozenset[str] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    allow_any_email: bool = False
    public_origin: str | None = None
    cookie_path: str = "/"

    @classmethod
    def from_env(cls) -> "AuthSettings":
        password_file = os.environ.get("REVIEW_SMTP_PASSWORD_FILE")
        password = (Path(password_file).expanduser().read_text(encoding="utf-8").rstrip("\r\n")
                    if password_file else os.environ.get("REVIEW_SMTP_PASSWORD", ""))
        emails = frozenset(filter(None, (normalize_email(x) for x in
            os.environ.get("REVIEW_ALLOWED_EMAILS", "").split(","))))
        domains = frozenset(x.strip().lower().lstrip("@") for x in
            os.environ.get("REVIEW_ALLOWED_DOMAINS", "").split(",") if x.strip())
        try:
            port = int(os.environ.get("REVIEW_SMTP_PORT", "465"))
        except ValueError as exc:
            raise ValueError("REVIEW_SMTP_PORT must be a number") from exc
        settings = cls(
            mailer=os.environ.get("REVIEW_MAILER", "smtp").strip().lower(),
            smtp_host=os.environ.get("REVIEW_SMTP_HOST", "").strip(),
            smtp_port=port,
            smtp_security=os.environ.get("REVIEW_SMTP_SECURITY", "ssl").strip().lower(),
            smtp_user=os.environ.get("REVIEW_SMTP_USER", "").strip(),
            smtp_password=password,
            smtp_from=os.environ.get("REVIEW_SMTP_FROM", "").strip(),
            allowed_emails=emails,
            allowed_domains=domains,
            allow_any_email=os.environ.get("REVIEW_ALLOW_ANY_EMAIL", "").strip() == "1",
            public_origin=os.environ.get("REVIEW_PUBLIC_ORIGIN", "").strip().rstrip("/") or None,
            cookie_path=os.environ.get("REVIEW_COOKIE_PATH", "/").strip(),
        )
        if settings.mailer == "smtp":
            if not settings.smtp_host or not settings.smtp_user or not settings.smtp_password:
                raise ValueError("configure REVIEW_SMTP_HOST, REVIEW_SMTP_USER and SMTP password")
            if not normalize_email(settings.smtp_from):
                raise ValueError("configure a valid REVIEW_SMTP_FROM address")
            if settings.smtp_security not in {"ssl", "starttls"} or not 1 <= port <= 65535:
                raise ValueError("SMTP security must be ssl or starttls, with a valid port")
        elif settings.mailer == "agently":
            if not shutil.which("agently-cli"):
                raise ValueError("agently-cli is required for REVIEW_MAILER=agently")
        else:
            raise ValueError("REVIEW_MAILER must be smtp or agently")
        if not emails and not domains and not settings.allow_any_email:
            raise ValueError("configure allowed email(s) or REVIEW_ALLOW_ANY_EMAIL=1")
        if settings.public_origin:
            origin = urlsplit(settings.public_origin)
            if (origin.scheme not in {"http", "https"} or not origin.hostname or
                    origin.username or origin.password or origin.path or origin.query or
                    origin.fragment or origin.netloc.endswith(":") or
                    settings.public_origin != f"{origin.scheme}://{origin.netloc}"):
                raise ValueError("REVIEW_PUBLIC_ORIGIN must be an http(s) origin without a path")
        if (not settings.cookie_path.startswith("/") or ";" in settings.cookie_path or
                "\r" in settings.cookie_path or "\n" in settings.cookie_path):
            raise ValueError("REVIEW_COOKIE_PATH must be an absolute URL path")
        return settings

    def permits(self, email: str) -> bool:
        return (self.allow_any_email or email in self.allowed_emails
                or email.rsplit("@", 1)[-1] in self.allowed_domains)


def smtp_send(settings: AuthSettings, email: str, code: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = email
    message["Subject"] = "Formaliscope 审核台登录验证码"
    message.set_content(f"你的 Formaliscope 审核台验证码是：{code}\n\n10 分钟内有效，只能使用一次。若非本人操作，请忽略此邮件。\n")
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=context) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)


def agently_send(email: str, code: str) -> None:
    """Use the locally authorized mailbox without placing the code in argv/logs."""
    with tempfile.TemporaryDirectory(prefix="kip126-otp-") as directory:
        body = Path(directory) / "body.txt"
        body.write_text(f"你的 Formaliscope 审核台验证码是：{code}\n\n10 分钟内有效，只能使用一次。若非本人操作，请忽略此邮件。\n",
                        encoding="utf-8")
        body.chmod(0o600)
        result = subprocess.run([
            "agently-cli", "message", "+send", "--to", email,
            "--subject", "Formaliscope 审核台登录验证码", "--body-file", "./body.txt",
            "--body-format", "plain", "--confirmed",
        ], cwd=directory, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode != 0:
        raise DeliveryError("configured mailbox rejected the message")


class AuthStore(SessionStore):
    def __init__(self, db_path: Path, settings: AuthSettings,
                 sender: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        super().__init__(db_path, clock=clock)
        self.settings = settings
        self.sender = sender or (agently_send if settings.mailer == "agently"
                                 else lambda email, code: smtp_send(settings, email, code))

    def _session_allowed(self, reviewer: str) -> bool:
        return self.settings.permits(reviewer)

    def _code_digest(self, challenge_id: str, code: str) -> str:
        return hmac.new(self.pepper, f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()

    def request_code(self, email: str, client_ip: str) -> bool:
        """Return whether a message was sent; callers give the same response either way."""
        if not self.settings.permits(email):
            return False
        now = int(self.clock())
        ip_digest = hmac.new(self.pepper, client_ip.encode(), hashlib.sha256).hexdigest()
        challenge_id = str(uuid.uuid4())
        code = f"{secrets.randbelow(100_000_000):08d}"
        with self.lock:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM login_requests WHERE created_at < ?", (now - 86400,))
                db.execute("DELETE FROM login_challenges WHERE expires_at < ?", (now - 86400,))
                db.execute("DELETE FROM login_sessions WHERE expires_at <= ?", (now,))
                recent = db.execute("SELECT MAX(created_at), COUNT(*) FROM login_requests WHERE email=? AND created_at>?",
                                    (email, now - 3600)).fetchone()
                ip_count = db.execute("SELECT COUNT(*) FROM login_requests WHERE ip_digest=? AND created_at>?",
                                      (ip_digest, now - 3600)).fetchone()[0]
                minute_count = db.execute("SELECT COUNT(*) FROM login_requests WHERE created_at>?",
                                          (now - 60,)).fetchone()[0]
                daily_count = db.execute("SELECT COUNT(*) FROM login_requests WHERE created_at>?",
                                         (now - 86400,)).fetchone()[0]
                if ((recent[0] is not None and now - recent[0] < REQUEST_INTERVAL)
                        or recent[1] >= EMAIL_HOURLY_LIMIT or ip_count >= IP_HOURLY_LIMIT
                        or minute_count >= GLOBAL_MINUTE_LIMIT or daily_count >= GLOBAL_DAILY_LIMIT):
                    db.execute("COMMIT")
                    return False
                db.execute("INSERT INTO login_requests VALUES (?, ?, ?)", (email, ip_digest, now))
                db.execute("UPDATE login_challenges SET used_at=? WHERE email=? AND used_at IS NULL",
                           (now, email))
                db.execute("""INSERT INTO login_challenges
                    (id, email, code_digest, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (challenge_id, email, self._code_digest(challenge_id, code), now, now + CODE_LIFETIME))
                db.execute("COMMIT")
        try:
            self.sender(email, code)
        except Exception as exc:
            with self.lock:
                with closing(self._connect()) as db:
                    db.execute("UPDATE login_challenges SET used_at=? WHERE id=?", (now, challenge_id))
                    db.execute("DELETE FROM login_requests WHERE email=? AND ip_digest=? AND created_at=?",
                               (email, ip_digest, now))
            raise DeliveryError from exc
        return True

    def verify_code(self, email: str, code: str) -> str | None:
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{8}", code):
            return None
        now = int(self.clock())
        with self.lock:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""SELECT id, code_digest, expires_at, attempts FROM login_challenges
                    WHERE email=? AND used_at IS NULL ORDER BY created_at DESC LIMIT 1""", (email,)).fetchone()
                if not row or row["expires_at"] <= now or row["attempts"] >= MAX_CODE_ATTEMPTS:
                    db.execute("COMMIT")
                    return None
                if not hmac.compare_digest(row["code_digest"], self._code_digest(row["id"], code)):
                    db.execute("""UPDATE login_challenges SET attempts=attempts+1,
                        used_at=CASE WHEN attempts+1>=? THEN ? ELSE used_at END WHERE id=?""",
                        (MAX_CODE_ATTEMPTS, now, row["id"]))
                    db.execute("COMMIT")
                    return None
                db.execute("UPDATE login_challenges SET used_at=? WHERE id=?", (now, row["id"]))
                token = self._insert_session(db, email, now)
                db.execute("COMMIT")
                self._remember_session(token, email, now)
                return token
