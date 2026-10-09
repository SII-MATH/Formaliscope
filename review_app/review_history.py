"""Logical user saves, excluding verified migration and inheritance copies."""
from .dataset_storage import _scoped_id


def judgment_roots(records):
    """Resolve copies to an original record without merging independent saves.

    Older namespace migration copies have no explicit parent, but their IDs
    were generated deterministically from the original ID and dataset. Check
    both that exact identity and the preserved author/content fields.
    """
    rows = {row['id']: row for row in records}
    parents = {}
    preserved = ('reviewer', 'verdict', 'rationale', 'created_at',
                 'fingerprint', 'fingerprint_scheme', 'source_commit', 'snapshot_digest')
    for row in records:
        parent = rows.get(row['inherited_from_id'])
        if parent is not None and all(row[key] == parent[key] for key in preserved):
            parents[row['id']] = parent['id']
    scopes = {row['dataset_id'] for row in records if row['dataset_id'].startswith('kip126@')}
    for original in records:
        if original['dataset_id']:
            continue
        for scope in scopes:
            copy = rows.get(_scoped_id(original['id'], scope))
            if copy is not None and all(copy[key] == original[key] for key in preserved):
                parents[copy['id']] = original['id']
    result = {}
    for identity in rows:
        root, seen = identity, set()
        while root in parents:
            if root in seen:
                root = identity  # Malformed cycles must never hide real saves.
                break
            seen.add(root)
            root = parents[root]
        result[identity] = root
    return result
