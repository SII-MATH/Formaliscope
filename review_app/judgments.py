"""Review records and read models, independent of HTTP and session authorization.

Callers supply the authenticated reviewer. Source fingerprints select current
judgments without deleting old decisions or accepting a different source version.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .auth import normalize_email
from .name_auth import valid_name, valid_reviewer_id
from .build import CURRENT_FINGERPRINT_SCHEME, LEGACY_FINGERPRINT_SCHEME
from .database import connect

VERDICTS = frozenset({"aligned", "partial", "misaligned", "uncertain"})
WRITE_LOCK = threading.Lock()


def backfill_review_basis(db_path: Path, snapshot: dict) -> int:
    """Attach v2 content fingerprints to judgments made against a v1 snapshot."""
    changed = 0
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for card in snapshot["cards"]:
                basis = _card_fingerprints(card).get(CURRENT_FINGERPRINT_SCHEME)
                if not basis:
                    continue
                for scheme, fingerprint in _card_fingerprints(card).items():
                    cursor = db.execute("""UPDATE judgments
                        SET review_basis_scheme=?, review_basis_fingerprint=?
                        WHERE review_basis_fingerprint IS NULL AND card_id=?
                          AND fingerprint_scheme=? AND fingerprint=?""",
                        (CURRENT_FINGERPRINT_SCHEME, basis, card["id"], scheme, fingerprint))
                    changed += cursor.rowcount
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return changed


def _card_fingerprints(card: dict) -> dict[str, str]:
    return card.get("fingerprints") or {
        card.get("fingerprint_scheme", LEGACY_FINGERPRINT_SCHEME): card["fingerprint"]
    }


def _judgment_matches(card: dict, judgment: sqlite3.Row | dict) -> bool:
    keys = judgment.keys()
    basis = judgment["review_basis_fingerprint"] if "review_basis_fingerprint" in keys else None
    basis_scheme = judgment["review_basis_scheme"] if "review_basis_scheme" in keys else None
    if basis and _card_fingerprints(card).get(basis_scheme) == basis:
        return True
    scheme = judgment["fingerprint_scheme"]
    return _card_fingerprints(card).get(scheme) == judgment["fingerprint"]


def _catalog_enrichment(card: dict) -> dict:
    """Carry display and filter metadata without duplicating source evidence."""
    fields = {key: card[key] for key in ("title_zh", "reading_summary_zh") if key in card}
    enrichment = card.get("enrichment")
    if enrichment is not None:
        fields["enrichment"] = {
            **{key: enrichment[key] for key in ("classification", "priority", "provenance")
               if key in enrichment},
            "readback": {"status": enrichment.get("readback", {}).get("status", "none")},
        }
    return fields


def catalog(snapshot: dict, db_path: Path, reviewer: str, *, initial_id: str | None = None) -> dict:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT card_id, fingerprint, fingerprint_scheme,
            review_basis_scheme, review_basis_fingerprint, verdict, created_at FROM judgments
            WHERE reviewer=? ORDER BY created_at, rowid""", (reviewer,)).fetchall()
    by_card: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_card.setdefault(row["card_id"], []).append(row)
    cards = []
    for card in snapshot["cards"]:
        judgments = by_card.get(card["id"], [])
        current = next((row for row in reversed(judgments) if _judgment_matches(card, row)), None)
        cards.append({
            "id": card["id"], "label": card["label"], "title": card["title"],
            "chapter": card["chapter"], "kind": card["kind"],
            "declaration": card["declaration"], "source_status": card["source_status"],
            "verdict": current["verdict"] if current else None,
            "stale": bool(judgments and not current),
            **{key: card[key] for key in ('dependencies', 'role', 'priority', 'risk',
                'main_target', 'statement_origin', 'module_file') if key in card},
            **_catalog_enrichment(card),
        })
    payload = {
        "snapshot_schema": snapshot["schema"],
        "fingerprint_scheme": snapshot.get("fingerprint_scheme"),
        "digest": snapshot["digest"],
        "source_commit": snapshot["source_commit"],
        "source_dirty": snapshot.get("source_dirty", False),
        "unlinked_nodes": snapshot["unlinked_nodes"],
        "cards": cards,
        "review_mode": snapshot.get("review_mode", "blueprint"),
    }
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
        rows = db.execute("""SELECT id, fingerprint, fingerprint_scheme,
            review_basis_scheme, review_basis_fingerprint, source_commit,
            snapshot_digest, reviewer, verdict, rationale, created_at
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
    allowed_verdicts = {'aligned', 'uncertain', 'misaligned'} if snapshot.get('review_mode') == 'statement' else VERDICTS
    if verdict not in allowed_verdicts:
        return 400, {"error": "请选择审核结论"}
    if not isinstance(rationale, str) or len(rationale) > 4000 or (snapshot.get('review_mode') != 'statement' and verdict != "aligned" and not rationale.strip()):
        return 400, {"error": "除“对齐”外，请填写理由（最多 4000 字）"}
    if normalize_email(reviewer) != reviewer and not valid_reviewer_id(reviewer):
        return 400, {"error": "审核人身份无效"}
    fingerprint_scheme = card.get("fingerprint_scheme", LEGACY_FINGERPRINT_SCHEME)
    review_basis_fingerprint = _card_fingerprints(card).get(CURRENT_FINGERPRINT_SCHEME)
    review_basis_scheme = CURRENT_FINGERPRINT_SCHEME if review_basis_fingerprint else fingerprint_scheme
    review_basis_fingerprint = review_basis_fingerprint or card["fingerprint"]
    canonical = (card_id, card["fingerprint"], reviewer, verdict, rationale.strip())
    # This server has one process. Queue writes briefly in Python rather than
    # sending a simultaneous burst into SQLite's busy wait loop.
    with WRITE_LOCK:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT id, card_id, fingerprint, reviewer, verdict, rationale, created_at,
                fingerprint_scheme, review_basis_scheme, review_basis_fingerprint,
                source_commit, snapshot_digest
                FROM judgments WHERE reviewer=? AND request_id=?""", (reviewer, request_id)).fetchone()
            if previous:
                old = (previous["card_id"], previous["fingerprint"], previous["reviewer"],
                       previous["verdict"], previous["rationale"])
                db.execute("COMMIT")
                return (200, {"judgment": dict(previous), "replayed": True}) if old == canonical else (409, {"error": "请求 ID 已用于另一条判断"})
            record = {
                "id": str(uuid.uuid4()), "request_id": request_id, "card_id": card_id,
                "fingerprint": card["fingerprint"], "reviewer": reviewer,
                "fingerprint_scheme": fingerprint_scheme,
                "review_basis_scheme": review_basis_scheme,
                "review_basis_fingerprint": review_basis_fingerprint,
                "source_commit": snapshot.get("source_commit"),
                "snapshot_digest": snapshot.get("digest"),
                "verdict": verdict, "rationale": rationale.strip(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute("""INSERT INTO judgments
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at,
                 fingerprint_scheme, review_basis_scheme, review_basis_fingerprint,
                 source_commit, snapshot_digest)
                VALUES (:id, :request_id, :card_id, :fingerprint, :reviewer, :verdict, :rationale, :created_at,
                        :fingerprint_scheme, :review_basis_scheme, :review_basis_fingerprint,
                        :source_commit, :snapshot_digest)""", record)
            db.execute("COMMIT")
    return 201, {"judgment": record, "replayed": False}


def reviewer_profile(db_path: Path, reviewer: str) -> dict:
    with closing(connect(db_path)) as db:
        row = db.execute('SELECT * FROM reviewer_profiles WHERE reviewer=?', (reviewer,)).fetchone()
    return dict(row) if row else {'reviewer': reviewer, 'display_name': '', 'preview_admin': 0}


def update_reviewer_profile(db_path: Path, reviewer: str, name: object) -> tuple[int, dict]:
    """Change the display name while retaining identity and admin permissions."""
    if not valid_name(name):
        return 400, {'error': '请输入 1–60 字的姓名'}
    name = name.strip()
    with closing(connect(db_path)) as db:
        db.execute('''INSERT INTO reviewer_profiles (reviewer, display_name) VALUES (?, ?)
            ON CONFLICT(reviewer) DO UPDATE SET display_name=excluded.display_name''',
            (reviewer, name))
    return 200, {'display_name': name}


def reviewer_export(snapshot: dict, db_path: Path, reviewer: str) -> dict:
    """Export one authenticated reviewer's records with their source provenance."""
    with closing(connect(db_path)) as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM judgments WHERE reviewer=? ORDER BY created_at, rowid", (reviewer,))]
    return {
        "snapshot_schema": snapshot["schema"],
        "fingerprint_scheme": snapshot.get("fingerprint_scheme"),
        "snapshot_digest": snapshot["digest"],
        "source_commit": snapshot["source_commit"],
        "source_dirty": snapshot.get("source_dirty", False),
        "reviewer": reviewer,
        "judgments": rows,
    }


def admin_summary(snapshot: dict, db_path: Path) -> dict:
    cards = {card['id']: card for card in snapshot['cards']}
    with closing(connect(db_path)) as db:
        records = [dict(row) for row in db.execute('''SELECT j.*, p.display_name FROM judgments j
            LEFT JOIN reviewer_profiles p ON p.reviewer=j.reviewer ORDER BY j.created_at DESC, j.rowid DESC''')]
    latest = {}
    for row in records:
        card = cards.get(row['card_id'])
        if card and _judgment_matches(card, row):
            latest.setdefault((row['reviewer'], row['card_id']), row)
    return {'source_commit': snapshot['source_commit'], 'snapshot_digest': snapshot['digest'],
            'total_cards': len(cards), 'judgments': list(latest.values()),
            'history_count': len(records), 'stale_count': sum(
                not cards.get(row['card_id']) or not _judgment_matches(cards[row['card_id']], row)
                for row in records)}
