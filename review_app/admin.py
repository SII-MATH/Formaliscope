"""Read-only account directory and submitted reviews for global administrators."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3

from .judgments import _judgment_matches
from .repositories import current_datasets, dataset_id, dataset_info


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
        latest = set()
        for row in db.execute('SELECT * FROM judgments ORDER BY created_at DESC, rowid DESC'):
            user = users.get(row['reviewer'])
            if user is None:
                continue
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
                 auth_mode='name', admin_emails=()) -> dict:
    if cursor < 0 or not 1 <= limit <= 100:
        raise ValueError('分页参数无效')
    cards = {card['id']: card for card in snapshot['cards']}
    with closing(_read_connection(db_path)) as db:
        user = _accounts(db, auth_mode, admin_emails).get(reviewer)
        if user is None:
            raise LookupError('账号不存在')
        records = list(db.execute('''SELECT * FROM judgments WHERE reviewer=? AND dataset_id=?
            ORDER BY created_at DESC, rowid DESC''', (reviewer, dataset_id(snapshot))))
    current = {}
    for row in records:
        card = cards.get(row['card_id'])
        if card and _judgment_matches(card, row):
            current.setdefault(row['card_id'], row['id'])
    page = []
    for row in records[cursor:cursor + limit]:
        card = cards.get(row['card_id'])
        valid = bool(card and _judgment_matches(card, row))
        title = (card.get('title_zh') or card.get('title') or card['id']) if card else row['card_id']
        page.append({key: row[key] for key in ('id', 'card_id', 'verdict', 'rationale', 'created_at',
                                              'inherited_from_dataset')} | {
            'title': title,
            'status': 'current' if current.get(row['card_id']) == row['id'] else 'superseded' if valid else 'stale',
            'card_available': card is not None})
    return {'user': user, 'dataset': dataset_info(snapshot), 'reviews': page,
            'history_count': len(records), 'current_count': len(current),
            'next_cursor': cursor + limit if cursor + limit < len(records) else None}
