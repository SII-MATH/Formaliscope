"""Explicit, private imports of source-bound machine expectation assessments.

Candidate snapshot generation never imports this module. Reviewer read models
only read judgments and drafts; these tables are reserved for internal analysis.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .data_lock import data_lock
from .database import DB_SCHEMA_VERSION, connect, initialize
from .enrichment import validate_enrichment


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _text_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode('utf-8')).hexdigest() if value is not None else None


def import_agent_assessments(data_dir: Path, snapshot: dict, document: dict) -> dict:
    """Validate the complete v2 batch, then atomically append immutable records.

    ``snapshot`` is the original frozen basis, not the enriched candidate. It
    need not be the currently installed snapshot: preparing internal records
    before candidate installation is allowed. This operation never installs it.
    """
    document = deepcopy(document)
    if not isinstance(document, dict) or document.get('schema') != 'statement-enrichment.v2':
        raise ValueError('agent assessment import requires statement-enrichment.v2')
    annotations = validate_enrichment(document, snapshot)
    run = document['run']
    run_json = _json(run)
    rows = []
    for annotation in annotations:
        identity = annotation['declaration_id']
        original = document['originals'][identity]
        assessment = original['expectation_assessment']
        review = document['reviews'].get(identity)
        payload = {'source_sha256': document['sources'][identity],
                   'annotation': annotation, 'original': original, 'review': review}
        rows.append((run['run_id'], identity, document['sources'][identity],
                     _text_hash(annotation['readback']['text_zh']),
                     _text_hash(original['readback']['text_zh']),
                     original['readback']['confidence'], assessment['verdict'],
                     assessment['reason_zh'], assessment['confidence'],
                     _json(original), _json(annotation), _json(review) if review is not None else None,
                     hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()))

    # Validation above has no filesystem side effects, including rejected v1,
    # malformed batches and source mismatches. Maintenance shares the same lock
    # as numbered migrations, snapshot installation and complete backups.
    data_dir = data_dir.expanduser().resolve()
    with data_lock(data_dir):
        database = data_dir / 'judgments.sqlite3'
        initialize(database)
        with closing(connect(database)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                existing = db.execute(
                    'SELECT run_json FROM agent_assessment_runs WHERE run_id=?',
                    (run['run_id'],)).fetchone()
                if existing is not None and existing['run_json'] != run_json:
                    raise ValueError('agent run_id already has different immutable runtime or source metadata')
                if existing is None:
                    db.execute("""INSERT INTO agent_assessment_runs
                        (run_id, model, reasoning_effort, created_at, policy_version,
                         source_commit, snapshot_digest, expectation_context_digest,
                         threshold, run_json, imported_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (run['run_id'], run['model'], run['reasoning_effort'], run['created_at'],
                         run['policy_version'], run['source_commit'], run['snapshot_digest'],
                         run['expectation_context_digest'], run['threshold'], run_json,
                         datetime.now(timezone.utc).isoformat()))
                inserted = unchanged = 0
                for row in rows:
                    saved = db.execute("""SELECT payload_sha256 FROM agent_assessments
                        WHERE run_id=? AND declaration_id=?""", row[:2]).fetchone()
                    if saved is not None:
                        if saved['payload_sha256'] != row[-1]:
                            raise ValueError('agent run_id and declaration_id already have a different immutable result')
                        unchanged += 1
                        continue
                    db.execute("""INSERT INTO agent_assessments
                        (run_id, declaration_id, source_sha256, readback_sha256,
                         original_readback_sha256, readback_confidence, expectation_verdict,
                         expectation_reason_zh, expectation_confidence, original_json,
                         annotation_json, review_json, payload_sha256)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", row)
                    inserted += 1
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
    return {'run_id': run['run_id'], 'annotations': len(rows), 'inserted': inserted,
            'unchanged': unchanged, 'database_schema_version': DB_SCHEMA_VERSION,
            'source_commit': run['source_commit'], 'snapshot_digest': run['snapshot_digest']}
