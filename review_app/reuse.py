"""Explicit, conservative reuse of frozen Statement evidence across commits."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re

from .build import (_content_fingerprint, _digest, CURRENT_FINGERPRINT_SCHEME,
                    calculate_snapshot_digest, compare_snapshots, validate_snapshot)
from .repositories import dataset_id, datasets

REUSE_SCHEMA = 'statement-reuse.v1'
MACHINE_FIELDS = ('enrichment', 'title_zh', 'reading_summary_zh', 'annotation_source_sha256')


def _repository(snapshot):
    return snapshot.get('repository', {}).get('id', 'kip126')


def predecessor(snapshot: dict, previous: dict, selected: str | None = None) -> dict:
    validate_snapshot(previous)
    choices = [item for item in datasets(previous) if _repository(item) == _repository(snapshot)
               and (selected is None or dataset_id(item) == selected)]
    if len(choices) != 1:
        raise ValueError('reuse requires exactly one previous version of the same repository; specify --reuse-dataset')
    return choices[0]


def _generated_at(snapshot):
    try:
        timestamp = datetime.fromisoformat(snapshot.get('generated_at', '').replace('Z', '+00:00'))
        return timestamp if timestamp.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def automatic_predecessor(snapshot: dict, previous: dict) -> dict | None:
    """Prefer the installed default for this repo, otherwise its latest artifact.

    Do not reinterpret an existing version or a historical artifact as a new
    update. Explicit --reuse-from remains available for deliberate branching.
    """
    from .repositories import select_dataset
    if not snapshot.get('repository'):
        return None
    children = datasets(previous)
    if any(dataset_id(item) == dataset_id(snapshot) for item in children):
        return None
    choices = [item for item in children if item.get('repository') and
               _repository(item) == _repository(snapshot)]
    if not choices:
        return None
    active = select_dataset(previous)
    if active in choices:
        source = active
    else:
        dated = [(stamp, item) for item in choices if (stamp := _generated_at(item)) is not None]
        if not dated:
            return None
        latest = max(stamp for stamp, _ in dated)
        sources = [item for stamp, item in dated if stamp == latest]
        if len(sources) != 1:
            raise ValueError('installed repository versions have ambiguous generation times; specify --reuse-from')
        source = sources[0]
    new_time, old_time = _generated_at(snapshot), _generated_at(source)
    return source if new_time is not None and old_time is not None and new_time > old_time else None


def automatic_reuse(snapshot: dict, previous: dict | None) -> tuple[dict, int]:
    """Create candidates for new repository versions; installed evidence is read-only."""
    validate_snapshot(snapshot)
    if (previous is None or snapshot.get('reuse_disabled') or
            not any(item.get('repository') for item in datasets(snapshot)) or
            not any(item.get('repository') for item in datasets(previous))):
        return snapshot, 0
    validate_snapshot(previous)
    result, count = deepcopy(snapshot), 0
    changed = False
    for index, target in enumerate(datasets(result)):
        if target.get('reuse') or target.get('reuse_disabled') or target.get('review_mode') != 'statement':
            continue
        installed = next((item for item in datasets(previous)
                          if dataset_id(item) == dataset_id(target) and
                          item.get('reuse_input_digest') == target['digest']), None)
        if installed is not None:
            # Reinstalling the same plain input preserves the exact previously
            # derived artifact, including subsequent same-version enrichment.
            candidate = deepcopy(installed)
        else:
            source = automatic_predecessor(target, previous)
            if source is None:
                continue
            candidate = reuse_snapshot(target, source)
            candidate['reuse_input_digest'] = target['digest']
            candidate['digest'] = calculate_snapshot_digest(candidate)
            count += len(candidate['reuse']['cards'])
        changed = True
        if result.get('datasets'):
            result['datasets'][index] = candidate
        else:
            result = candidate
    if changed:
        result['digest'] = calculate_snapshot_digest(result)
        validate_snapshot(result)
    return result, count


def _tokens(text):
    # Ignore empty positional lines outside strings, preserving indentation
    # (Lean layout is significant) and the exact contents of string literals.
    lines = ['']
    for part in re.findall(r'"(?:\\.|[^"\\])*"|[^"\n]+|\n', text):
        if part == '\n':
            if lines[-1].strip():
                lines.append('')
        else:
            lines[-1] += part
    return [line.rstrip() for line in lines if line.strip()]


def imported_modules(text):
    from .statements import mask_comments
    result = []
    for line in mask_comments(text).splitlines():
        match = re.match(r'^\s*(?:public\s+|private\s+)?import\s+(.+)', line)
        if match:
            result.extend(match[1].split())
    return result


def freeze_imports(repo: Path, modules: dict) -> dict:
    """Capture local imported modules even when they are outside scan roots."""
    context = {}
    pending = list(modules.values())
    while pending:
        for name in imported_modules(pending.pop()):
            if not re.fullmatch(r'[\w]+(?:\.[\w]+)*', name):
                continue
            file = name.replace('.', '/') + '.lean'
            path = repo / file
            if file in modules or file in context or not path.is_file():
                continue
            if not path.resolve().is_relative_to(repo):
                raise ValueError('imported source symlink escapes the repository')
            context[file] = path.read_text(encoding='utf-8')
            pending.append(context[file])
    return context


def _module_basis(snapshot: dict, root: str, cache: dict) -> str | None:
    if root in cache:
        return cache[root]
    modules = {**snapshot.get('context_modules', {}), **snapshot.get('modules', {})}
    if root not in modules:
        return None
    hashes = cache.setdefault('_module_hashes', {})
    visited, imported, external = set(), {}, set()
    pending = [root]
    while pending:
        file = pending.pop()
        if file in visited:
            continue
        visited.add(file)
        text = modules[file]
        if file not in hashes:
            hashes[file] = _digest(_tokens(text))
        if file != root:
            imported[file] = hashes[file]
        for name in imported_modules(text):
            if not re.fullmatch(r'[\w]+(?:\.[\w]+)*', name):
                cache[root] = None
                return None
            path = name.replace('.', '/') + '.lean'
            if path in modules:
                pending.append(path)
            else:
                external.add(name)
    environment = snapshot.get('source_environment')
    if external and (not snapshot.get('dependency_lock_digest') or not environment or
                     not environment.get('lean-toolchain') or environment.get('path_dependencies')):
        cache[root] = None
        return None  # Historical artifacts cannot prove their external environment.
    cache[root] = _digest({'module': hashes[root], 'imports': imported, 'external': sorted(external),
                           'environment': environment if environment and any(environment.values()) else None,
                           'lock': snapshot.get('dependency_lock_digest')})
    return cache[root]


def context_basis(snapshot: dict, card: dict, cache: dict | None = None) -> str | None:
    """Whole module plus transitive imports, not the incomplete candidate graph.

    Changes to an unrelated declaration in the same imported module can cause
    false negatives. This is intentional until elaborator evidence is available.
    """
    module_basis = _module_basis(snapshot, card.get('module_file'), {} if cache is None else cache)
    if module_basis is None:
        return None
    references = [{key: item[key] for key in ('title', 'statement', 'declarations')}
                  for item in card.get('blueprint_references', [])]
    return _digest({'source': card.get('lean', {}).get('source'), 'kind': card.get('kind'),
                    'module_basis': module_basis,
                    'references': references,
                    'main_targets': snapshot.get('repository', {}).get('main_targets', [])})


def reuse_snapshot(snapshot: dict, previous: dict) -> dict:
    """Generate a new candidate; no database or installed artifact is changed."""
    validate_snapshot(snapshot)
    validate_snapshot(previous)
    if any(item.get('review_mode') != 'statement' or item.get('datasets') for item in (snapshot, previous)):
        raise ValueError('reuse requires individual Statement snapshots')
    if _repository(snapshot) != _repository(previous):
        raise ValueError('cannot reuse evidence from a different repository')
    if not snapshot.get('repository') or not previous.get('repository'):
        raise ValueError('cross-version reuse requires repository-scoped snapshots; qualify legacy snapshots with bundle-snapshots first')
    if snapshot['source_commit'] == previous['source_commit']:
        raise ValueError('reuse requires a different source commit')
    result = deepcopy(snapshot)
    result.pop('reuse_disabled', None)
    result.pop('reuse_input_digest', None)
    old = {card['id']: card for card in previous['cards']}
    manifest = {'schema': REUSE_SCHEMA, 'repository_id': _repository(previous),
                'source_dataset': dataset_id(previous), 'source_commit': previous['source_commit'],
                'snapshot_digest': previous['digest'], 'cards': {}}
    topics = {item['id'] for item in result.get('enrichment_topics', [])}
    old_cache, new_cache = {}, {}
    for card in result['cards']:
        prior = old.get(card['id'])
        if prior is None:
            continue
        basis = context_basis(result, card, new_cache)
        if basis is None or context_basis(previous, prior, old_cache) != basis:
            continue
        analysis = prior.get('enrichment')
        if analysis and set(analysis.get('classification', {}).get('topics', [])) - topics:
            continue
        # An explicitly supplied fresh translation wins over a historical one.
        if not card.get('enrichment') and card.get('statement_origin') != 'backtranslation':
            for key in MACHINE_FIELDS:
                if key in prior:
                    card[key] = deepcopy(prior[key])
            if prior.get('statement_origin') == 'backtranslation':
                card['statement'], card['statement_origin'] = prior['statement'], 'backtranslation'
                card['blueprint_file'], card['blueprint_line'] = card['lean']['file'], card['lean']['line']
                if prior.get('annotation_source_sha256'):
                    card['title'] = prior['title']
        nl, lean, fp = _content_fingerprint(card, result.get('dependency_lock_digest'))
        card.update(nl_digest=nl, lean_digest=lean, fingerprint=fp,
                    fingerprint_scheme=CURRENT_FINGERPRINT_SCHEME,
                    fingerprints={CURRENT_FINGERPRINT_SCHEME: fp})
        manifest['cards'][card['id']] = basis
        if (analysis or prior.get('statement_origin') == 'backtranslation') and (
                card.get('enrichment') == analysis or card.get('annotation_source_sha256')):
            card['reuse_source_commit'] = prior.get('reuse_source_commit', previous['source_commit'])
    result['reuse'] = manifest
    result['generated_at'] = datetime.now(timezone.utc).isoformat()
    result['digest'] = calculate_snapshot_digest(result)
    result['comparison'] = compare_snapshots(previous, result)
    validate_snapshot(result)
    return result


def validate_reuse(snapshot: dict) -> None:
    if 'reuse_disabled' in snapshot and type(snapshot['reuse_disabled']) is not bool:
        raise ValueError('reuse_disabled must be a boolean')
    if 'reuse_input_digest' in snapshot and (not snapshot.get('reuse') or
            not re.fullmatch('[0-9a-f]{64}', str(snapshot['reuse_input_digest']))):
        raise ValueError('invalid original automatic reuse input digest')
    manifest = snapshot.get('reuse')
    if manifest is None:
        return
    keys = {'schema', 'repository_id', 'source_dataset', 'source_commit', 'snapshot_digest', 'cards'}
    if (not isinstance(manifest, dict) or set(manifest) != keys or manifest['schema'] != REUSE_SCHEMA or
            snapshot.get('review_mode') != 'statement' or manifest['repository_id'] != _repository(snapshot) or
            not snapshot.get('repository') or
            not isinstance(manifest['source_dataset'], str) or
            manifest['source_dataset'] != manifest['repository_id'] + '@' + str(manifest['source_commit']) or
            not re.fullmatch('[0-9a-f]{40}', str(manifest['source_commit'])) or
            manifest['source_commit'] == snapshot['source_commit'] or
            not re.fullmatch('[0-9a-f]{64}', str(manifest['snapshot_digest'])) or
            not isinstance(manifest['cards'], dict)):
        raise ValueError('invalid Statement reuse manifest')
    cards = {card['id']: card for card in snapshot['cards']}
    cache = {}
    for identity, basis in manifest['cards'].items():
        if identity not in cards or basis is None or context_basis(snapshot, cards[identity], cache) != basis:
            raise ValueError('reuse context does not match the candidate evidence')


def installation_reuse(previous: dict | None, current: dict) -> list[tuple[dict, dict]]:
    """Verify original frozen evidence before any database writes."""
    available = datasets(current) + (datasets(previous) if previous else [])
    pairs = []
    for target in datasets(current):
        manifest = target.get('reuse')
        if not manifest:
            continue
        source = next((item for item in available if item['digest'] == manifest['snapshot_digest'] and
                       dataset_id(item) == manifest['source_dataset']), None)
        if source is None:
            if previous and any(dataset_id(item) == dataset_id(target) and item.get('reuse') == manifest
                                for item in datasets(previous)):
                continue  # Reinstall or re-enrichment of an already validated lineage.
            raise ValueError('reuse predecessor is unavailable; retain its exact frozen snapshot in the installed collection')
        if (_repository(source) != _repository(target) or source['source_commit'] != manifest['source_commit']):
            raise ValueError('reuse predecessor identity does not match')
        old = {card['id']: card for card in source['cards']}
        cache = {}
        for identity, basis in manifest['cards'].items():
            if identity not in old or context_basis(source, old[identity], cache) != basis:
                raise ValueError('reuse predecessor context does not match')
        pairs.append((source, target))
    return pairs


def inherit_judgments(db_path: Path, pairs: list[tuple[dict, dict]]) -> int:
    """Copy current judgments once, preserving authors, dates and provenance."""
    from .database import connect
    from .dataset_storage import _scoped_id
    from .judgments import _judgment_matches
    copied = 0
    with closing(connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            for source, target in pairs:
                scope = dataset_id(target)
                if scope == dataset_id(source):
                    continue  # Historical unqualified KIP126 already shares its scope.
                old = {card['id']: card for card in source['cards']}
                new = {card['id']: card for card in target['cards']}
                columns = [row[1] for row in db.execute("PRAGMA table_info('judgments')")]
                rows = db.execute('SELECT * FROM judgments WHERE dataset_id=? ORDER BY created_at DESC, rowid DESC',
                                  (dataset_id(source),)).fetchall()
                seen = set()
                for prior in rows:
                    identity, reviewer = prior['card_id'], prior['reviewer']
                    if identity not in target['reuse']['cards'] or (identity, reviewer) in seen:
                        continue
                    if not _judgment_matches(old[identity], prior) or not _judgment_matches(new[identity], prior):
                        continue
                    seen.add((identity, reviewer))
                    if db.execute('SELECT 1 FROM judgments WHERE dataset_id=? AND card_id=? AND reviewer=?',
                                  (scope, identity, reviewer)).fetchone():
                        continue  # Never override a decision already made in the new version.
                    row = dict(prior)
                    row.update(id=_scoped_id(prior['id'], scope), request_id=_scoped_id(prior['request_id'], scope),
                               dataset_id=scope, inherited_from_id=prior['id'],
                               inherited_from_dataset=dataset_id(source))
                    copied += db.execute(f"INSERT OR IGNORE INTO judgments ({','.join(columns)}) "
                                         f"VALUES ({','.join('?' for _ in columns)})",
                                         [row[key] for key in columns]).rowcount
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return copied
