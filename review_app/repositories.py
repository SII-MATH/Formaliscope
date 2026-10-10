"""Repository configuration and immutable collections of independent datasets."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re

COLLECTION_SCHEMA = 'formaliscope-review-collection.v1'


def repository_config(value: dict) -> dict:
    from .enrichment_v2 import validate_topics
    if not isinstance(value, dict) or set(value) - {'id', 'name', 'roots', 'topics', 'main_targets', 'url'}:
        raise ValueError('repository config accepts id, name, roots, topics, main_targets and url')
    identity, name = value.get('id'), value.get('name')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', identity):
        raise ValueError('repository id must be a stable lowercase slug')
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError('repository name is required (at most 120 characters)')
    roots = value.get('roots')
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) or not root or
            PurePosixPath(root).is_absolute() or '..' in PurePosixPath(root).parts or
            '\\' in root or set(PurePosixPath(root).parts) & {'.git', '.lake'} for root in roots):
        raise ValueError('scan roots must be non-empty relative source paths without .git or .lake')
    targets = value.get('main_targets', [])
    if not isinstance(targets, list) or any(not isinstance(item, str) or not item for item in targets):
        raise ValueError('main_targets must contain declaration names')
    url = value.get('url', '')
    if not isinstance(url, str) or (url and not re.fullmatch(r'https://[^\s]+', url)):
        raise ValueError('repository url must be an HTTPS source link')
    return {'id': identity, 'name': name.strip(), 'roots': sorted(set(roots)),
            'topics': validate_topics(value.get('topics', [])), 'main_targets': targets, 'url': url}


def source_files(repo: Path, roots: list[str]) -> list[Path]:
    repo = repo.resolve()
    files = set()
    for root in roots:
        path = repo / root
        if not path.exists() or not path.resolve().is_relative_to(repo):
            raise ValueError(f'scan root is missing or outside the repository: {root}')
        for candidate in ([path] if path.is_file() else path.rglob('*.lean')):
            if candidate.suffix != '.lean' or candidate.name == 'lakefile.lean':
                continue
            if set(candidate.relative_to(repo).parts) & {'.git', '.lake'}:
                continue
            if not candidate.resolve().is_relative_to(repo):
                raise ValueError(f'source symlink escapes the repository: {candidate.relative_to(repo)}')
            files.add(candidate)
    return sorted(files)


def dataset_id(snapshot: dict) -> str:
    repository = snapshot.get('repository')
    return f"{repository['id']}@{snapshot['source_commit']}" if repository else ''


def datasets(snapshot: dict) -> list[dict]:
    return snapshot['datasets'] if snapshot.get('schema') == COLLECTION_SCHEMA else [snapshot]


def current_datasets(snapshot: dict) -> list[dict]:
    """One active version per repository, retaining all historical datasets."""
    def generated(item):
        try:
            value = datetime.fromisoformat(item.get('generated_at') or '')
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    selected = select_dataset(snapshot)
    successors = snapshot.get('repository_successors', {})
    current = {}
    for item in datasets(snapshot):
        identity = dataset_info(item)['repository_id']
        if identity in successors:
            continue
        previous = current.get(identity)
        if previous is None or generated(item) >= generated(previous):
            current[identity] = item
    if dataset_info(selected)['repository_id'] not in successors:
        current[dataset_info(selected)['repository_id']] = selected
    return list(current.values())


def dataset_info(snapshot: dict) -> dict:
    repository = snapshot.get('repository', {'id': 'kip126', 'name': 'KIP126', 'url': ''})
    return {'id': dataset_id(snapshot), 'repository_id': repository['id'],
            'repository_name': repository['name'], 'source_url': repository.get('url', ''),
            'source_commit': snapshot['source_commit'], 'snapshot_digest': snapshot['digest'],
            'generated_at': snapshot.get('generated_at'), 'source_origin': snapshot.get('source_origin'),
            'source_dirty': snapshot.get('source_dirty', False), 'card_count': len(snapshot['cards'])}


def qualify_legacy(snapshot: dict) -> dict:
    """Copy a historical KIP126 artifact into a namespaced dataset, without altering it."""
    from .build import normalize_snapshot, SNAPSHOT_SCHEMA, calculate_snapshot_digest
    result = normalize_snapshot(snapshot)
    if result.get('review_mode') != 'statement':
        raise ValueError('collections require Statement snapshots')
    if 'repository' in result:
        return result
    result['schema'] = SNAPSHOT_SCHEMA
    result.setdefault('source_dirty', False)
    result['repository'] = repository_config({'id': 'kip126', 'name': 'KIP126',
                                             'roots': ['KIP126', 'KIPBase'],
                                             'topics': result.get('enrichment_topics', [])})
    mapping = {card['id']: 'statement::kip126::' + card['declaration'] for card in result['cards']}
    for card in result['cards']:
        card['legacy_card_id'] = card['id']
        card['id'] = mapping[card['id']]
        card['dependencies'] = [mapping[item] for item in card.get('dependencies', [])]
        if isinstance(card.get('enrichment'), dict):
            card['enrichment']['declaration_id'] = card['id']
    result['digest'] = calculate_snapshot_digest(result)
    return result


def make_collection(snapshots: list[dict], *, default: str | None = None,
                    repository_successors: dict[str, str] | None = None) -> dict:
    from .build import CURRENT_FINGERPRINT_SCHEME, calculate_snapshot_digest, validate_snapshot
    children = [qualify_legacy(item) for item in snapshots]
    if not children:
        raise ValueError('a collection requires at least one dataset')
    keys = [dataset_id(item) for item in children]
    if len(keys) != len(set(keys)):
        raise ValueError('collection cannot contain two snapshots of the same repository commit')
    selected = default or keys[0]
    if selected not in keys:
        raise ValueError('default dataset is not in the collection')
    first = children[keys.index(selected)]
    result = {'schema': COLLECTION_SCHEMA, 'review_mode': 'statement',
              'fingerprint_scheme': CURRENT_FINGERPRINT_SCHEME,
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'source_commit': first['source_commit'], 'source_dirty': any(item['source_dirty'] for item in children),
              'cards': [], 'unlinked_nodes': 0, 'datasets': children, 'default_dataset': selected}
    if repository_successors:
        result['repository_successors'] = repository_successors
    result['digest'] = calculate_snapshot_digest(result)
    validate_snapshot(result)
    return result


def select_dataset(snapshot: dict, selected: str | None = None) -> dict:
    if snapshot.get('schema') != COLLECTION_SCHEMA:
        if selected is not None and selected != dataset_id(snapshot):
            raise ValueError('unknown dataset')
        return snapshot
    selected = snapshot['default_dataset'] if selected is None else selected
    for item in snapshot['datasets']:
        if dataset_id(item) == selected:
            return item
    raise ValueError('unknown dataset')
