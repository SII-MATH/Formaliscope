"""Read-only deployment checks; never initialize data or contact a mail service."""

from __future__ import annotations

import ipaddress
from contextlib import closing
import json
import os
import re
import shutil
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import unquote, urlsplit

from .auth import normalize_email
from .build import LEGACY_SNAPSHOT_SCHEMA, validate_snapshot


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Resources(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.paths.append(values["src"])
        if tag == "link" and values.get("rel") in {"stylesheet", "icon"} and values.get("href"):
            self.paths.append(values["href"])


def _static_resources(static_dir: Path, statement: bool) -> bool:
    root = static_dir.resolve()
    # The server loads every route's resources at startup in both modes.
    pages = ["login.html", "statement.html", "admin.html", "index.html"]
    paths = {"mathjax-tex-svg.js", "MATHJAX-LICENSE.txt"}
    try:
        for page in pages:
            parser = _Resources()
            parser.feed((root / page).read_text(encoding="utf-8"))
            paths.add(page)
            paths.update(parser.paths)
        for resource in paths:
            url = urlsplit(resource)
            if url.scheme or url.netloc or url.query or url.fragment:
                return False
            path = (root / unquote(url.path)).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
                return False
    except (OSError, ValueError, UnicodeError):
        return False
    return True


def _smtp_password_available(env: Mapping[str, str]) -> bool:
    password_file = env.get("REVIEW_SMTP_PASSWORD_FILE", "")
    if not password_file:
        return bool(env.get("REVIEW_SMTP_PASSWORD", "").strip())
    try:
        path = Path(password_file).expanduser()
        # 0600 or 0640 allows a service group, but no world-readable secret.
        return (path.is_file() and path.stat().st_mode & 0o007 == 0
                and bool(path.read_text(encoding="utf-8").strip()))
    except (OSError, UnicodeError):
        return False


def run_preflight(data_dir: Path, *, preview: bool = False, host: str = "127.0.0.1",
                  admin_emails: Iterable[str] = (), env: Mapping[str, str] | None = None,
                  static_dir: Path | None = None, schema_path: Path | None = None,
                  require_statements: bool = True) -> dict:
    """Report readiness without changing files or exposing configured values.

    ``require_statements=False`` retains an explicit check path for existing
    Blueprint installations. New Statement deployments use the default.
    Passing an environment mapping makes checks independent of process state.
    """
    settings = os.environ if env is None else env
    app_root = Path(__file__).resolve().parents[1]
    static_dir = static_dir or app_root / "review_app" / "static"
    schema_path = schema_path or app_root / "statement_workflow" / "schema" / "statement-enrichment.v1.schema.json"
    checks: list[dict] = []

    def check(key: str, ok: bool, success: str, failure: str) -> None:
        checks.append({"id": key, "ok": bool(ok), "message": success if ok else failure})

    check("bind_host", _loopback(host), "Loopback binding configured.",
          "Bind to a loopback address; expose production through an HTTPS proxy.")
    snapshot = None
    try:
        candidate = json.loads((data_dir / "snapshot.json").read_text(encoding="utf-8"))
        if not isinstance(candidate, dict):
            raise ValueError("invalid snapshot")
        validate_snapshot(candidate)
        snapshot = candidate
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        pass
    check("snapshot_integrity", snapshot is not None, "Snapshot digest and card fingerprints verified.",
          "Install a readable snapshot with valid digest and card fingerprints.")
    statement = bool(snapshot and snapshot.get("review_mode") == "statement")
    check("review_mode", bool(snapshot) and (statement or not require_statements) and (statement or not preview),
          "Requested review mode is available.", "Build a Statement snapshot with --statements.")
    check("source_commit", bool(snapshot and re.fullmatch(r"[0-9a-f]{40}", str(snapshot.get("source_commit", "")))),
          "Source commit recorded.", "Snapshot must record an exact source commit.")
    legacy_unattested = bool(snapshot and not require_statements and not statement
                             and snapshot.get("schema") == LEGACY_SNAPSHOT_SCHEMA
                             and "source_dirty" not in snapshot
                             and snapshot.get("source_origin") != "archive-unverified")
    check("clean_source", bool(snapshot and (preview or legacy_unattested or (
        snapshot.get("source_dirty") is False and snapshot.get("source_origin") != "archive-unverified"))),
          "Existing legacy snapshot retained; clean provenance is unavailable." if legacy_unattested
          else "Source provenance is acceptable for this mode.",
          "Production requires a clean Git source checkout; rebuild with --require-clean.")
    check("static_assets", _static_resources(static_dir, statement or require_statements),
          "Page scripts, styles, MathJax and license are present.",
          "Restore all local resources referenced by the review pages.")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_ok = (schema.get("properties", {}).get("schema", {}).get("const") == "statement-enrichment.v1"
                     and set(schema.get("required", [])) == {"schema", "annotations"})
    except (OSError, ValueError, AttributeError, UnicodeError):
        schema_ok = False
    check("enrichment_schema", schema_ok, "Statement enrichment v1 contract is present.",
          "Restore the Statement enrichment v1 schema from the application release.")
    if not preview:
        origin_ok = False
        origin_text = settings.get("REVIEW_PUBLIC_ORIGIN", "").strip().rstrip("/")
        try:
            origin = urlsplit(origin_text)
            origin_ok = (origin.scheme == "https" and bool(origin.hostname) and not _loopback(origin.hostname)
                         and not origin.username and not origin.password and not origin.path
                         and not origin.query and not origin.fragment and not origin.netloc.endswith(":")
                         and origin_text == f"https://{origin.netloc}")
            if origin.port is not None and not 1 <= origin.port <= 65535:
                origin_ok = False
        except ValueError:
            pass
        check("public_origin", origin_ok, "Public HTTPS origin configured.",
              "Set REVIEW_PUBLIC_ORIGIN to the HTTPS origin without an application path.")
        cookie = settings.get("REVIEW_COOKIE_PATH", "/").strip()
        check("cookie_path", bool(re.fullmatch(r"/(?:[^\s;?#\\]*\/)?", cookie)),
              "Cookie path configured.", "Set REVIEW_COOKIE_PATH to / or an absolute prefix ending with /.")
        mode = settings.get('REVIEW_AUTH_MODE', 'name').strip().lower()
        check('auth_mode', mode in {'name', 'email'}, 'Authentication mode configured.',
              'Set REVIEW_AUTH_MODE to name or email.')
        if mode == 'name':
            admin_ok = False
            db_path = data_dir / 'judgments.sqlite3'
            try:
                with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    admin_ok = bool(db.execute('SELECT 1 FROM name_identities WHERE is_admin=1 AND disabled=0 LIMIT 1').fetchone())
            except (sqlite3.Error, OSError):
                pass
            check('admin_identity', admin_ok, 'An active operator-created administrator is available.',
                  'Run create-admin with a private --output file before starting the service.')
        elif mode == 'email':
            mailer = settings.get("REVIEW_MAILER", "smtp").strip().lower()
            if mailer == "smtp":
                try:
                    port_ok = 1 <= int(settings.get("REVIEW_SMTP_PORT", "465")) <= 65535
                except ValueError:
                    port_ok = False
                mail_ok = (bool(settings.get("REVIEW_SMTP_HOST", "").strip())
                           and bool(settings.get("REVIEW_SMTP_USER", "").strip())
                           and bool(normalize_email(settings.get("REVIEW_SMTP_FROM", "")))
                           and settings.get("REVIEW_SMTP_SECURITY", "ssl").strip().lower() in {"ssl", "starttls"}
                           and port_ok and _smtp_password_available(settings))
            elif mailer == "agently":
                mail_ok = shutil.which("agently-cli", path=settings.get("PATH")) is not None
            else:
                mail_ok = False
            check("mailer", mail_ok, "Mailer configuration available; no delivery attempted.",
                  "Configure a valid SMTP account and readable protected password, or an installed agently-cli.")
            raw_emails = [value.strip() for value in settings.get("REVIEW_ALLOWED_EMAILS", "").split(",") if value.strip()]
            emails = {normalize_email(value) for value in raw_emails}
            domains = {value.strip().lower().lstrip("@") for value in settings.get("REVIEW_ALLOWED_DOMAINS", "").split(",") if value.strip()}
            domains_ok = all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) for domain in domains)
            any_email = settings.get("REVIEW_ALLOW_ANY_EMAIL", "").strip() == "1"
            policy_ok = (bool(any_email or emails or domains) and None not in emails and domains_ok)
            check("email_policy", policy_ok, "Verified-email admission policy configured.",
                  "Set REVIEW_ALLOW_ANY_EMAIL=1 or valid allowed email/domain lists.")
            raw_admins = list(admin_emails) + settings.get("REVIEW_ADMIN_EMAILS", "").split(",")
            admins = [normalize_email(value) for value in raw_admins if value.strip()]
            admin_ok = (bool(admins) and all(email and (any_email or email in emails or email.rsplit("@", 1)[-1] in domains)
                                               for email in admins))
            check("admin_emails", admin_ok or not statement, "Administrative access configured for this mode.",
                  "Set valid REVIEW_ADMIN_EMAILS or --admin-email values admitted by the email policy.")
    limitations = ["Does not test proxy routing, database migrations or legacy mail delivery.",
                   "Start the service and probe /healthz after installation."]
    if legacy_unattested:
        limitations.append("Legacy v1 does not attest source cleanliness; rebuild before publishing new evidence.")
    return {"mode": "preview" if preview else "production", "auth_mode": "preview" if preview else settings.get("REVIEW_AUTH_MODE", "name"), "ready": all(item["ok"] for item in checks),
            "checks": checks, "limitations": limitations}
