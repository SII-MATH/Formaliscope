"""Explicit repository succession without changing old snapshots or review rows."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path

from .build import calculate_snapshot_digest, validate_snapshot
from .database import connect
from .dataset_storage import _scoped_id
from .repositories import dataset_id, datasets


def successor_snapshot(source: dict, *, identity: str, name: str, url: str) -> dict:
    """Create a separate repository object for exactly the same source evidence."""
    if source.get('reuse') or any('legacy_card_id' in card for card in source['cards']):
        raise ValueError('source must be an independent repository snapshot')
    result = deepcopy(source)
    old = source['repository']['id']
    if old == identity:
        raise ValueError('successor needs a distinct repository ID')
    result['repository'].update(id=identity, name=name, url=url)
    mapping = {card['id']: card['id'].replace(f'statement::{old}::', f'statement::{identity}::', 1)
               for card in result['cards']}
    for card in result['cards']:
        card['id'] = mapping[card['id']]
        card['dependencies'] = [mapping[dependency] for dependency in card.get('dependencies', [])]
        if isinstance(card.get('enrichment'), dict):
            card['enrichment']['declaration_id'] = card['id']
    result.pop('comparison', None)
    result['digest'] = calculate_snapshot_digest(result)
    validate_snapshot(result)
    return result


def transfer_successor_records(db_path: Path, previous: dict | None, candidate: dict) -> dict[str, int]:
    """Copy an exact installed predecessor's records during install's data lock.

    Source records and scopes stay untouched. The caller must prevent live writes
    during the cutover; subsequent installs are idempotent.
    """
    successors = candidate.get('repository_successors', {})
    if not successors:
        return {'judgments': 0, 'drafts': 0, 'admins': 0}
    if previous is None or not db_path.is_file():
        raise ValueError('repository succession requires an installed database and predecessor')
    old_by_id = {dataset_id(item): item for item in datasets(previous)}
    new_by_id = {dataset_id(item): item for item in datasets(candidate)}
    pairs = []
    for old, new in successors.items():
        target = next((item for item in datasets(candidate)
                       if item['repository']['id'] == new and dataset_id(item) == candidate['default_dataset']), None)
        if target is None:
            raise ValueError('repository successor must be the default dataset')
        source_id = f"{old}@{target['source_commit']}"
        source = old_by_id.get(source_id)
        if source is None or source_id not in new_by_id or source['digest'] != new_by_id[source_id]['digest']:
            raise ValueError('repository predecessor must match the exact installed snapshot')
        target_cards = {card['id']: card for card in target['cards']}
        mapping = {}
        for card in source['cards']:
            new_id = card['id'].replace(f'statement::{old}::', f'statement::{new}::', 1)
            successor_card = target_cards.get(new_id)
            if successor_card is None or any(card[key] != successor_card[key] for key in
                                             ('fingerprint', 'fingerprints', 'declaration', 'module_file')):
                raise ValueError('repository successor differs from installed review evidence')
            mapping[card['id']] = new_id
        if len(mapping) != len(target_cards):
            raise ValueError('repository successor has a different card set')
        pairs.append((source_id, dataset_id(target), mapping))
    counts = {'judgments': 0, 'drafts': 0, 'admins': 0}
    with closing(connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            for source_id, target_id, mapping in pairs:
                for table, label in (('judgments', 'judgments'), ('review_drafts', 'drafts')):
                    columns = [row[1] for row in db.execute(f"PRAGMA table_info('{table}')")]
                    rows = db.execute(f'SELECT * FROM {table} WHERE dataset_id=? ORDER BY rowid', (source_id,)).fetchall()
                    for old_row in rows:
                        if old_row['card_id'] not in mapping:
                            raise ValueError('predecessor review references an unknown card')
                        row = dict(old_row)
                        row['id'] = _scoped_id(row['id'], target_id)
                        row['request_id'] = _scoped_id(row['request_id'], target_id)
                        row['card_id'] = mapping[row['card_id']]
                        row['dataset_id'] = target_id
                        if table == 'judgments':
                            row['inherited_from_id'] = old_row['id']
                            row['inherited_from_dataset'] = source_id
                        else:
                            for key in ('completion_request_id', 'judgment_id'):
                                if row[key]:
                                    row[key] = _scoped_id(row[key], target_id)
                        counts[label] += db.execute(
                            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                            f"VALUES ({','.join('?' for _ in columns)})",
                            [row[key] for key in columns]).rowcount
                counts['admins'] += db.execute('INSERT OR IGNORE INTO dataset_admins '
                    'SELECT ?, reviewer FROM dataset_admins WHERE dataset_id=?',
                    (target_id, source_id)).rowcount
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return counts
