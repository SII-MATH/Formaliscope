"""Explicit offline merges of operator-confirmed duplicate accounts."""
from contextlib import closing
from datetime import datetime, timezone
import json
import uuid

from .data_lock import data_lock
from .database import connect, initialize
from .passwords import INITIAL_PASSWORD, hash_password, name_key


def merge_accounts(data_dir, source, target):
    if not isinstance(source, str) or not isinstance(target, str) or source == target:
        raise ValueError('source and target must be different account IDs')
    path = data_dir / 'judgments.sqlite3'
    if not path.is_file():
        raise ValueError('existing database required')
    with data_lock(data_dir):
        initialize(path)
        with closing(connect(path)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                previous = db.execute('SELECT target_reviewer FROM account_merges WHERE source_reviewer=?', (source,)).fetchone()
                if previous:
                    if previous['target_reviewer'] != target:
                        raise ValueError('account already merged into a different target')
                    db.execute('COMMIT')
                    return {'source': source, 'target': target, 'already_merged': True}
                rows = {row['reviewer']: dict(row) for row in db.execute('''SELECT i.reviewer, i.is_admin, i.disabled,
                    p.display_name FROM name_identities i JOIN reviewer_profiles p ON p.reviewer=i.reviewer
                    WHERE i.reviewer IN (?, ?)''', (source, target))}
                if len(rows) != 2 or rows[target]['disabled']:
                    raise ValueError('two registered accounts and an enabled target are required')
                if name_key(rows[source]['display_name']) != name_key(rows[target]['display_name']):
                    raise ValueError('only confirmed same-name accounts can be merged')
                archive = {'source_profile': rows[source], 'drafts': [], 'request_changes': []}
                judgments = db.execute('SELECT id, request_id FROM judgments WHERE reviewer=?', (source,)).fetchall()
                for row in judgments:
                    request = row['request_id']
                    if db.execute('SELECT 1 FROM judgments WHERE reviewer=? AND request_id=?', (target, request)).fetchone():
                        request = str(uuid.uuid5(uuid.NAMESPACE_URL, f'formaliscope-merge:{source}:{row["id"]}:{request}'))
                        archive['request_changes'].append({'id': row['id'], 'original_request_id': row['request_id'], 'merged_request_id': request})
                    db.execute('''UPDATE judgments SET reviewer=?, request_id=?, original_reviewer=COALESCE(original_reviewer, ?)
                        WHERE id=?''', (target, request, source, row['id']))
                drafts = db.execute('SELECT * FROM review_drafts WHERE reviewer=? ORDER BY created_at, rowid', (source,)).fetchall()
                for draft in drafts:
                    archive['drafts'].append(dict(draft))
                    collision = db.execute('''SELECT * FROM review_drafts WHERE reviewer=? AND dataset_id=?
                        AND card_id=? AND fingerprint=?''', (target, draft['dataset_id'], draft['card_id'], draft['fingerprint'])).fetchone()
                    if collision:
                        # Preserve both originals in the private audit archive. Only
                        # one can occupy the editable slot; the newest checkpoint wins.
                        archive['drafts'].append(dict(collision))
                        if draft['created_at'] <= collision['created_at']:
                            db.execute('DELETE FROM review_drafts WHERE id=?', (draft['id'],))
                            continue
                        db.execute('DELETE FROM review_drafts WHERE id=?', (collision['id'],))
                    db.execute('UPDATE review_drafts SET reviewer=? WHERE id=?', (target, draft['id']))
                db.execute('INSERT OR IGNORE INTO dataset_admins SELECT dataset_id, ? FROM dataset_admins WHERE reviewer=?', (target, source))
                db.execute('DELETE FROM dataset_admins WHERE reviewer=?', (source,))
                db.execute('DELETE FROM login_sessions WHERE reviewer IN (?, ?)', (source, target))
                db.execute('DELETE FROM preview_identities WHERE reviewer IN (?, ?)', (source, target))
                db.execute('DELETE FROM password_credentials WHERE reviewer=?', (source,))
                db.execute('DELETE FROM name_identities WHERE reviewer=?', (source,))
                db.execute('DELETE FROM reviewer_profiles WHERE reviewer=?', (source,))
                db.execute('UPDATE name_identities SET is_admin=?, normalized_name=? WHERE reviewer=?',
                           (int(rows[target]['is_admin'] or rows[source]['is_admin']), name_key(rows[target]['display_name']), target))
                db.execute('UPDATE password_credentials SET password_hash=?, must_change=1 WHERE reviewer=?',
                           (hash_password(INITIAL_PASSWORD), target))
                # Keep aliases pointing directly at the surviving account.
                db.execute('UPDATE account_merges SET target_reviewer=? WHERE target_reviewer=?', (target, source))
                db.execute('INSERT INTO account_merges VALUES (?, ?, ?, ?)',
                           (source, target, datetime.now(timezone.utc).isoformat(), json.dumps(archive, ensure_ascii=False)))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
    return {'source': source, 'target': target, 'judgments_moved': len(judgments),
            'drafts_processed': len(drafts), 'archived_draft_rows': len(archive['drafts']), 'already_merged': False}
