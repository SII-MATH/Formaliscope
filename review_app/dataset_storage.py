"""Copy legacy KIP126 records into dataset scopes, retaining rollback records."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import uuid

from .database import connect
from .repositories import dataset_id, datasets
from .judgments import _judgment_matches


def _scoped_id(identity: str, scope: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'formaliscope:{scope}:{identity}'))


def preserve_legacy_records(db_path: Path, previous: dict | None, current: dict) -> int:
    """Called under the installation lock, after numbered schema migration 10.

    Original rows stay in the legacy scope. Deterministic copies retain their
    provenance, completion links and ordering; retries never create duplicates.
    Only the historical KIP126 namespace can claim unqualified legacy rows.
    """
    previous_commit = ((previous or {}).get('source_commit')
                       if not ((previous or {}).get('repository') or (previous or {}).get('datasets')) else None)
    copied = 0
    with closing(connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            for source in datasets(current):
                if source.get('repository', {}).get('id') != 'kip126':
                    continue
                scope = dataset_id(source)
                mapping = {}
                for card in source['cards']:
                    mapping.setdefault(card.get('legacy_card_id', 'statement::' + card['declaration']), []).append(card)
                for table in ('judgments', 'review_drafts'):
                    rows = db.execute(f"SELECT * FROM {table} WHERE dataset_id='' ORDER BY rowid").fetchall()
                    columns = [row[1] for row in db.execute(f"PRAGMA table_info('{table}')")]
                    for old in rows:
                        if old['card_id'] not in mapping or (old['source_commit'] or previous_commit) != source['source_commit']:
                            continue
                        candidates = mapping[old['card_id']]
                        if len(candidates) > 1:
                            candidates = [card for card in candidates if _judgment_matches(card, old)]
                            if len(candidates) != 1:
                                continue  # Keep ambiguous history in its original scope.
                        row = dict(old)
                        row['id'] = _scoped_id(old['id'], scope)
                        row['request_id'] = _scoped_id(old['request_id'], scope)
                        row['card_id'], row['dataset_id'] = candidates[0]['id'], scope
                        if table == 'review_drafts':
                            for key in ('completion_request_id', 'judgment_id'):
                                if row[key]:
                                    row[key] = _scoped_id(row[key], scope)
                        copied += db.execute(f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                            f"VALUES ({','.join('?' for _ in columns)})", [row[key] for key in columns]).rowcount
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return copied
