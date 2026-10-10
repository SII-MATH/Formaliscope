"""Read-only account directory and submitted reviews for global administrators."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3

from .judgments import _judgment_matches
from .repositories import current_datasets, dataset_id, dataset_info, datasets
from .review_history import judgment_roots


def safe_display_name(value: str | None) -> str:
    # A recovery credential accidentally entered as a name must not be echoed.
    return re.sub(r'KIP-[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])', '（名称含恢复码，已隐藏）', value or '')


def _read_connection(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    return db


def _accounts(db, auth_mode, admin_emails):
    if auth_mode == 'name':
        rows = db.execute('''SELECT n.reviewer, p.display_name, n.is_admin, n.disabled
            FROM name_identities n LEFT JOIN reviewer_profiles p ON p.reviewer=n.reviewer''')
    else:
        rows = db.execute('''SELECT reviewer, display_name, preview_admin AS is_admin,
            0 AS disabled FROM reviewer_profiles''')
    accounts = {}
    for row in rows:
        item = dict(row)
        item['display_name'] = safe_display_name(item['display_name']) or '未填写姓名'
        item['is_admin'] = bool(item['is_admin'] if auth_mode != 'email' else item['reviewer'] in admin_emails)
        item['disabled'] = bool(item['disabled'])
        accounts[item['reviewer']] = item
    return accounts


def user_directory(installed: dict, db_path: Path, *, auth_mode='name', admin_emails=()) -> dict:
    sources = current_datasets(installed)
    cards = {dataset_id(source): {card['id']: card for card in source['cards']} for source in sources}
    with closing(_read_connection(db_path)) as db:
        users = _accounts(db, auth_mode, admin_emails)
        for user in users.values():
            user.update(current_count=0, history_count=0, last_review_at=None,
                        verdicts={'aligned': 0, 'uncertain': 0, 'misaligned': 0, 'partial': 0},
                        dataset_admin_count=0)
        for row in db.execute('SELECT reviewer, COUNT(*) AS count FROM dataset_admins GROUP BY reviewer'):
            if row['reviewer'] in users:
                users[row['reviewer']]['dataset_admin_count'] = row['count']
        records = list(db.execute('SELECT * FROM judgments ORDER BY created_at DESC, rowid DESC'))
        roots = judgment_roots(records)
        latest, saved = set(), set()
        for row in records:
            user = users.get(row['reviewer'])
            if user is None:
                continue
            origin = (row['reviewer'], roots[row['id']])
            if origin not in saved:
                saved.add(origin)
                user['history_count'] += 1
            user['last_review_at'] = user['last_review_at'] or row['created_at']
            card = cards.get(row['dataset_id'], {}).get(row['card_id'])
            key = (row['dataset_id'], row['reviewer'], row['card_id'])
            if card and _judgment_matches(card, row) and key not in latest:
                latest.add(key)
                user['current_count'] += 1
                user['verdicts'][row['verdict']] += 1
    ordered = sorted(users.values(), key=lambda user: (not user['is_admin'], user['display_name'], user['reviewer']))
    return {'users': ordered, 'datasets': [dataset_info(source) for source in sources],
            'stats': {'registered': len(ordered), 'enabled': sum(not user['disabled'] for user in ordered),
                      'admins': sum(user['is_admin'] and not user['disabled'] for user in ordered),
                      'reviewers': sum(user['history_count'] > 0 for user in ordered)},
            'auth_mode': auth_mode}


def user_reviews(snapshot: dict, db_path: Path, reviewer: str, *, cursor=0, limit=25,
                 auth_mode='name', admin_emails=(), installed=None) -> dict:
    if cursor < 0 or not 1 <= limit <= 100:
        raise ValueError('分页参数无效')
    cards = {card['id']: card for card in snapshot['cards']}
    scope = dataset_id(snapshot)
    repository = dataset_info(snapshot)['repository_id']
    predecessors = {old for old, new in (installed or {}).get('repository_successors', {}).items()
                    if new == repository}
    family = predecessors | {repository}
    sources = {dataset_id(source): {card['id']: card for card in source['cards']}
               for source in datasets(installed or snapshot)}
    with closing(_read_connection(db_path)) as db:
        user = _accounts(db, auth_mode, admin_emails).get(reviewer)
        if user is None:
            raise LookupError('账号不存在')
        records = list(db.execute('''SELECT * FROM judgments WHERE reviewer=?
            ORDER BY created_at DESC, rowid DESC''', (reviewer,)))
    roots = judgment_roots(records)
    current = {}
    for row in records:
        card = cards.get(row['card_id'])
        if row['dataset_id'] == scope and card and _judgment_matches(card, row):
            current.setdefault(row['card_id'], row['id'])
    # Prefer the current-version representation of each actual save. Historical
    # copies still participate in current validity but never inflate history.
    representatives = {}
    for row in records:
        if not (any(row['dataset_id'].startswith(member + '@') for member in family) or
                ('kip126' in family and row['dataset_id'] == '')):
            continue
        root = roots[row['id']]
        previous = representatives.get(root)
        if previous is None or (row['dataset_id'] == scope and previous['dataset_id'] != scope):
            representatives[root] = row
    history = sorted(representatives.values(), key=lambda row: row['created_at'], reverse=True)
    page = []
    for row in history[cursor:cursor + limit]:
        successor_card_id = row['card_id']
        for old in predecessors:
            prefix = 'statement::' + old + '::'
            if successor_card_id.startswith(prefix):
                successor_card_id = 'statement::' + repository + '::' + successor_card_id[len(prefix):]
                break
        card = cards.get(successor_card_id)
        original_card = sources.get(row['dataset_id'], {}).get(row['card_id'])
        display_card = original_card or card
        valid = bool(row['dataset_id'] == scope and card and _judgment_matches(card, row))
        title = (display_card.get('title_zh') or display_card.get('title') or display_card['id']) if display_card else row['card_id']
        page.append({key: row[key] for key in ('id', 'card_id', 'verdict', 'rationale', 'created_at',
                                              'inherited_from_dataset')} | {
            'title': title,
            'status': 'historical' if row['dataset_id'] != scope else 'current' if current.get(row['card_id']) == row['id'] else 'superseded' if valid else 'stale',
            'source_dataset': row['dataset_id'],
            'source_version': row['dataset_id'].split('@', 1)[-1] if row['dataset_id'] else row['source_commit'],
            'current_card_id': successor_card_id if card is not None else None,
            'card_available': card is not None})
    return {'user': user, 'dataset': dataset_info(snapshot), 'reviews': page,
            'history_count': len(history), 'current_count': len(current),
            'next_cursor': cursor + limit if cursor + limit < len(history) else None}
