"""Review records and read models, independent of HTTP and session authorization.

Callers supply the authenticated reviewer. Source fingerprints select current
judgments without deleting old decisions or accepting a different source version.
"""

from __future__ import annotations

import base64
import json
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
from .repositories import dataset_id, datasets, dataset_info

VERDICTS = frozenset({"aligned", "partial", "misaligned", "uncertain"})
WRITE_LOCK = threading.Lock()


def backfill_review_basis(db_path: Path, snapshot: dict) -> int:
    """Attach v2 content fingerprints to judgments made against a v1 snapshot."""
    changed = 0
    with closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for source in datasets(snapshot):
                for card in source["cards"]:
                    basis = _card_fingerprints(card).get(CURRENT_FINGERPRINT_SCHEME)
                    if not basis:
                        continue
                    for scheme, fingerprint in _card_fingerprints(card).items():
                        cursor = db.execute("""UPDATE judgments
                            SET review_basis_scheme=?, review_basis_fingerprint=?
                            WHERE review_basis_fingerprint IS NULL AND card_id=? AND dataset_id=?
                              AND fingerprint_scheme=? AND fingerprint=?""",
                            (CURRENT_FINGERPRINT_SCHEME, basis, card["id"], dataset_id(source), scheme, fingerprint))
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
            **{key: enrichment[key] for key in ("schema", "classification", "priority", "provenance")
               if key in enrichment},
            "readback": {"status": enrichment.get("readback", {}).get("status", "none")},
        }
    return fields


def catalog(snapshot: dict, db_path: Path, reviewer: str, *, initial_id: str | None = None) -> dict:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT card_id, fingerprint, fingerprint_scheme,
            review_basis_scheme, review_basis_fingerprint, verdict, created_at FROM judgments
            WHERE reviewer=? AND dataset_id=? ORDER BY created_at, rowid""", (reviewer, dataset_id(snapshot))).fetchall()
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
        "dataset": dataset_info(snapshot),
    }
    from .enrichment_v2 import DEFAULT_TOPICS
    payload['enrichment_topics'] = snapshot.get('enrichment_topics', DEFAULT_TOPICS)
    if initial_id is not None:
        if initial_id == "auto":
            initial_id = next((row["id"] for row in cards
                               if row["source_status"] == "local" and not row["verdict"]),
                              cards[0]["id"] if cards else None)
        payload["initial_evidence"] = next(
            (card for card in snapshot["cards"] if card["id"] == initial_id), None
        )
    return payload


def history(db_path: Path, card_id: str, reviewer: str, *, dataset: str = '') -> list[dict]:
    with closing(connect(db_path)) as db:
        rows = db.execute("""SELECT id, fingerprint, fingerprint_scheme,
            review_basis_scheme, review_basis_fingerprint, source_commit,
            snapshot_digest, reviewer, verdict, rationale, created_at
            FROM judgments WHERE card_id=? AND reviewer=? AND dataset_id=? ORDER BY created_at DESC, rowid DESC""",
            (card_id, reviewer, dataset)).fetchall()
    return [dict(row) for row in rows]


def _current(db: sqlite3.Connection, card: dict, reviewer: str, dataset: str = '') -> dict | None:
    for row in db.execute("""SELECT * FROM judgments WHERE reviewer=? AND card_id=? AND dataset_id=?
        ORDER BY created_at DESC, rowid DESC""", (reviewer, card["id"], dataset)):
        if _judgment_matches(card, row):
            return dict(row)
    return None


def _draft(db: sqlite3.Connection, card: dict, reviewer: str, dataset: str = '') -> sqlite3.Row | None:
    return db.execute("""SELECT * FROM review_drafts
        WHERE reviewer=? AND card_id=? AND fingerprint=? AND dataset_id=?""",
        (reviewer, card["id"], card["fingerprint"], dataset)).fetchone()


def review_state(snapshot: dict, db_path: Path, card_id: str, reviewer: str) -> dict:
    card = next(card for card in snapshot["cards"] if card["id"] == card_id)
    with closing(connect(db_path)) as db:
        db.execute("BEGIN")
        current = _current(db, card, reviewer, dataset_id(snapshot))
        draft = _draft(db, card, reviewer, dataset_id(snapshot))
        total = db.execute("SELECT COUNT(*) FROM judgments WHERE reviewer=? AND card_id=? AND dataset_id=?",
                           (reviewer, card_id, dataset_id(snapshot))).fetchone()[0]
    pending = bool(draft and draft["revision"] != draft["completed_revision"] and
                   (not current or (draft["verdict"], draft["rationale"]) !=
                    (current["verdict"], current["rationale"])))
    return {"current": current, "draft": dict(draft) if pending else None,
            "draft_revision": draft["revision"] if draft else 0, "history_count": total}


def history_page(db_path: Path, card_id: str, reviewer: str, *, limit: int = 25,
                 cursor: str | None = None, dataset: str = '') -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("每页历史数量应为 1–100")
    args: list = [reviewer, card_id, dataset]
    before = ""
    if cursor:
        try:
            stamp, rowid = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(stamp, str) or len(stamp) > 64 or type(rowid) is not int or not 1 <= rowid < 2**63:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("历史分页位置无效") from None
        before = " AND (created_at < ? OR (created_at = ? AND rowid < ?))"
        args.extend([stamp, stamp, rowid])
    with closing(connect(db_path)) as db:
        rows = db.execute("SELECT rowid AS history_rowid, * FROM judgments "
                          "WHERE reviewer=? AND card_id=? AND dataset_id=?" + before +
                          " ORDER BY created_at DESC, rowid DESC LIMIT ?", [*args, limit + 1]).fetchall()
    page = [dict(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [page[-1]["created_at"], page[-1]["history_rowid"]]).encode()).decode()
    for row in page:
        row.pop("history_rowid")
        row.pop("request_id")
    return {"history": page, "next_cursor": next_cursor}


def _validate(snapshot: dict, reviewer: str, payload: dict, *, draft: bool = False):
    if not isinstance(payload, dict):
        return None, (400, {"error": "请求格式错误"})
    card_id = payload.get("card_id")
    card = next((item for item in snapshot["cards"] if item["id"] == card_id), None)
    if card is None:
        return None, (404, {"error": "审核对象不存在"})
    if payload.get("fingerprint") != card["fingerprint"]:
        return None, (409, {"error": "原文或 Lean 对象已更新，请重新打开卡片"})
    verdict = payload.get("verdict")
    rationale = payload.get("rationale", "")
    request_id = payload.get("request_id")
    try:
        uuid.UUID(request_id)
    except (TypeError, ValueError, AttributeError):
        return None, (400, {"error": "缺少有效请求 ID"})
    allowed_verdicts = {'aligned', 'uncertain', 'misaligned'} if snapshot.get('review_mode') == 'statement' else VERDICTS
    if draft:
        allowed_verdicts = {*allowed_verdicts, ""}
    if not isinstance(verdict, str) or verdict not in allowed_verdicts:
        return None, (400, {"error": "请选择审核结论"})
    if not isinstance(rationale, str) or len(rationale) > 4000 or (not draft and snapshot.get('review_mode') != 'statement' and verdict != "aligned" and not rationale.strip()):
        return None, (400, {"error": "审阅意见格式无效或超过 4000 字"})
    if normalize_email(reviewer) != reviewer and not valid_reviewer_id(reviewer):
        return None, (400, {"error": "审核人身份无效"})
    return card, None


def _record(snapshot: dict, card: dict, reviewer: str, payload: dict) -> dict:
    fingerprint_scheme = card.get("fingerprint_scheme", LEGACY_FINGERPRINT_SCHEME)
    review_basis_fingerprint = _card_fingerprints(card).get(CURRENT_FINGERPRINT_SCHEME)
    review_basis_scheme = CURRENT_FINGERPRINT_SCHEME if review_basis_fingerprint else fingerprint_scheme
    review_basis_fingerprint = review_basis_fingerprint or card["fingerprint"]
    return {
        "id": str(uuid.uuid4()), "request_id": payload["request_id"], "card_id": card["id"],
        "fingerprint": card["fingerprint"], "reviewer": reviewer,
        "fingerprint_scheme": fingerprint_scheme,
        "review_basis_scheme": review_basis_scheme,
        "review_basis_fingerprint": review_basis_fingerprint,
        "source_commit": snapshot.get("source_commit"), "snapshot_digest": snapshot.get("digest"),
        "dataset_id": dataset_id(snapshot),
        "verdict": payload["verdict"], "rationale": payload.get("rationale", "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def save_draft(snapshot: dict, db_path: Path, reviewer: str, payload: dict) -> tuple[int, dict]:
    card, error = _validate(snapshot, reviewer, payload, draft=True)
    if error:
        return error
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0:
        return 400, {"error": "缺少有效草稿版本"}
    record = _record(snapshot, card, reviewer, payload)
    with WRITE_LOCK, closing(connect(db_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        old = _draft(db, card, reviewer, dataset_id(snapshot))
        if old and old["request_id"] == record["request_id"]:
            matches = (old["verdict"], old["rationale"], old["revision"]) == (
                record["verdict"], record["rationale"], revision + 1)
            return (200, {"draft": dict(old), "replayed": True}) if matches else (
                409, {"error": "请求 ID 已用于另一份草稿"})
        if revision != (old["revision"] if old else 0):
            return 409, {"error": "草稿已在其他页面更新，请保留当前输入并重新打开条目"}
        record["id"] = old["id"] if old else record["id"]
        record["revision"] = revision + 1
        db.execute("""INSERT INTO review_drafts
            (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at,
             fingerprint_scheme, review_basis_scheme, review_basis_fingerprint, source_commit,
             snapshot_digest, revision, dataset_id)
            VALUES (:id, :request_id, :card_id, :fingerprint, :reviewer, :verdict, :rationale, :created_at,
             :fingerprint_scheme, :review_basis_scheme, :review_basis_fingerprint, :source_commit,
             :snapshot_digest, :revision, :dataset_id)
            ON CONFLICT(dataset_id, reviewer, card_id, fingerprint) DO UPDATE SET
             request_id=excluded.request_id, verdict=excluded.verdict, rationale=excluded.rationale,
             created_at=excluded.created_at, source_commit=excluded.source_commit,
             snapshot_digest=excluded.snapshot_digest, revision=excluded.revision""", record)
        db.execute("COMMIT")
    return 200, {"draft": record, "replayed": False}


def submit(snapshot: dict, db_path: Path, reviewer: str, payload: dict) -> tuple[int, dict]:
    card, error = _validate(snapshot, reviewer, payload)
    if error:
        return error
    card_id, request_id = card["id"], payload["request_id"]
    verdict, rationale = payload["verdict"], payload.get("rationale", "").strip()
    canonical = (card_id, card["fingerprint"], reviewer, verdict, rationale, dataset_id(snapshot))
    # This server has one process. Queue writes briefly in Python rather than
    # sending a simultaneous burst into SQLite's busy wait loop.
    with WRITE_LOCK:
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT id, card_id, fingerprint, reviewer, verdict, rationale, created_at,
                fingerprint_scheme, review_basis_scheme, review_basis_fingerprint,
                source_commit, snapshot_digest, dataset_id
                FROM judgments WHERE reviewer=? AND request_id=?""", (reviewer, request_id)).fetchone()
            if previous:
                old = (previous["card_id"], previous["fingerprint"], previous["reviewer"],
                       previous["verdict"], previous["rationale"], previous['dataset_id'])
                db.execute("COMMIT")
                return (200, {"judgment": dict(previous), "replayed": True}) if old == canonical else (409, {"error": "请求 ID 已用于另一条判断"})
            guarded = "draft_revision" in payload
            draft = _draft(db, card, reviewer, dataset_id(snapshot)) if guarded else None
            if guarded:
                revision = payload["draft_revision"]
                if type(revision) is not int or revision < 1:
                    return 400, {"error": "缺少有效草稿版本"}
                if draft and draft["completion_request_id"] == request_id:
                    previous = db.execute("SELECT * FROM judgments WHERE id=?", (draft["judgment_id"],)).fetchone()
                    if previous and (previous["verdict"], previous["rationale"]) == (verdict, rationale):
                        return 200, {"judgment": dict(previous), "replayed": True}
                    return 409, {"error": "请求 ID 已用于另一条判断"}
                if not draft or draft["revision"] != revision or (draft["verdict"], draft["rationale"]) != (verdict, rationale):
                    return 409, {"error": "草稿已更新，请保留当前输入并重新打开条目"}
                current = _current(db, card, reviewer, dataset_id(snapshot))
                if current and current["fingerprint"] == card["fingerprint"] and (current["verdict"], current["rationale"]) == (verdict, rationale):
                    db.execute("""UPDATE review_drafts SET completed_revision=revision,
                        completion_request_id=?, judgment_id=? WHERE id=?""",
                        (request_id, current["id"], draft["id"]))
                    db.execute("COMMIT")
                    return 200, {"judgment": current, "replayed": False}
            record = _record(snapshot, card, reviewer, payload)
            db.execute("""INSERT INTO judgments
                (id, request_id, card_id, fingerprint, reviewer, verdict, rationale, created_at,
                 fingerprint_scheme, review_basis_scheme, review_basis_fingerprint,
                 source_commit, snapshot_digest, dataset_id)
                VALUES (:id, :request_id, :card_id, :fingerprint, :reviewer, :verdict, :rationale, :created_at,
                        :fingerprint_scheme, :review_basis_scheme, :review_basis_fingerprint,
                        :source_commit, :snapshot_digest, :dataset_id)""", record)
            if guarded:
                db.execute("""UPDATE review_drafts SET completed_revision=revision,
                    completion_request_id=?, judgment_id=? WHERE id=?""",
                    (request_id, record["id"], draft["id"]))
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


def reviewer_export(snapshot: dict, db_path: Path, reviewer: str, *, mode: str = "history") -> dict:
    """Export one authenticated reviewer's records with their source provenance."""
    if mode not in {"latest", "history"}:
        raise ValueError("导出类型应为 latest 或 history")
    with closing(connect(db_path)) as db:
        if mode == "latest":
            db.execute("BEGIN")
            rows = [row for card in snapshot["cards"] if (row := _current(db, card, reviewer, dataset_id(snapshot)))]
        else:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM judgments WHERE reviewer=? AND dataset_id=? ORDER BY created_at, rowid", (reviewer, dataset_id(snapshot)))]
    return {
        "snapshot_schema": snapshot["schema"],
        "fingerprint_scheme": snapshot.get("fingerprint_scheme"),
        "snapshot_digest": snapshot["digest"],
        "source_commit": snapshot["source_commit"],
        "source_dirty": snapshot.get("source_dirty", False),
        "reviewer": reviewer,
        "export_mode": mode,
        "dataset": dataset_info(snapshot),
        "judgments": rows,
    }


def admin_summary(snapshot: dict, db_path: Path) -> dict:
    cards = {card['id']: card for card in snapshot['cards']}
    with closing(connect(db_path)) as db:
        records = [dict(row) for row in db.execute('''SELECT j.*, p.display_name FROM judgments j
            LEFT JOIN reviewer_profiles p ON p.reviewer=j.reviewer WHERE j.dataset_id=?
            ORDER BY j.created_at DESC, j.rowid DESC''', (dataset_id(snapshot),))]
    latest = {}
    for row in records:
        card = cards.get(row['card_id'])
        if card and _judgment_matches(card, row):
            latest.setdefault((row['reviewer'], row['card_id']), row)
    return {'source_commit': snapshot['source_commit'], 'snapshot_digest': snapshot['digest'],
            'dataset': dataset_info(snapshot),
            'total_cards': len(cards), 'judgments': list(latest.values()),
            'history_count': len(records), 'stale_count': sum(
                not cards.get(row['card_id']) or not _judgment_matches(cards[row['card_id']], row)
                for row in records)}
