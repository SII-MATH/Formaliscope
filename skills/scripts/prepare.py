#!/usr/bin/env python3
"""Prepare a source-bound v2 task using a shared read-only snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import uuid


SCRIPT = Path(__file__).resolve().parent / 'collect.py'
spec = importlib.util.spec_from_file_location('formaliscope_prepare_collect', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def _relative(value, label):
    if (not isinstance(value, str) or not value.strip() or '\\' in value or
            PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or
            value.rstrip('/') in ('', '.')):
        raise ValueError(f'{label} must be a specific relative Lean path')
    return str(PurePosixPath(value))


def _shared_snapshot(snapshot, output):
    """Publish once by content hash; never overwrite an existing pool entry."""
    raw = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    pool = Path(output).absolute().parent / '.snapshots'
    # Match the private batch directories, including newly created parents.
    missing, parent = [], pool
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    target = pool / (hashlib.sha256(raw).hexdigest() + '.json')
    if not target.exists() and not target.is_symlink():
        descriptor, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=pool)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass  # Another preparation may have published the same content.
        finally:
            Path(temporary).unlink()
    if target.is_symlink() or target.read_bytes() != raw:
        raise ValueError('shared snapshot content changed; restore the pool entry before preparing tasks')
    return os.path.relpath(target, Path(output).absolute())


def prepare(snapshot_path, output, *, config_path, directories=(), files=(),
            declaration_ids=(), threshold=0.8, expectation_context=None):
    """Validate inputs, then record scope/configuration and link shared source."""
    if Path(output).exists() or Path(output).is_symlink():
        raise ValueError('output directory must not already exist')
    snapshot = helper._read(snapshot_path)
    helper._validate({'schema': 'statement-enrichment.v1', 'annotations': []}, snapshot)
    config = helper._read(config_path)
    helper._fields(config, ('schema', 'worker', 'topics'), 'config')
    if config['schema'] != 'formaliscope-enrichment-config.v2':
        raise ValueError('unsupported config schema; new batches require v2')
    worker = config['worker']
    helper._fields(worker, ('model', 'reasoning_effort'), 'worker config')
    if not isinstance(worker['model'], str) or not worker['model'].strip():
        raise ValueError('worker model must be a non-empty string')
    if worker['reasoning_effort'] not in (None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise ValueError('unsupported worker reasoning effort')
    threshold = helper._score(threshold)
    directories = [_relative(value, 'directory') for value in directories]
    files = [_relative(value, 'file') for value in files]
    if not directories and not files and not declaration_ids:
        raise ValueError('select at least one directory, file or declaration; full-library selection is not implicit')
    requested = []
    for value in declaration_ids:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('declaration ID/name must be a non-empty string')
        requested.append(value if value.startswith('statement::') else 'statement::' + value)
    by_id = {card['id']: card for card in snapshot['cards']}
    if not set(requested).issubset(by_id):
        raise ValueError('unknown selected declaration')
    # Catch mistyped selectors even when another selector finds valid declarations.
    for directory in directories:
        if not any(card['lean']['file'].startswith(directory + '/') for card in snapshot['cards']):
            raise ValueError(f'no declarations in selected directory: {directory}')
    for file in files:
        if not any(card['lean']['file'] == file for card in snapshot['cards']):
            raise ValueError(f'no declarations in selected file: {file}')
    selected = [card['id'] for card in snapshot['cards']
                if card['id'] in requested or card['lean']['file'] in files or
                any(card['lean']['file'].startswith(directory + '/') for directory in directories)]
    if not selected:
        raise ValueError('selection contains no declarations')
    raw_context = None
    if expectation_context is not None:
        raw_context = Path(expectation_context).read_bytes()
        if not raw_context.decode('utf-8').strip():
            raise ValueError('expectation context must be non-empty UTF-8 text')
    from review_app.blueprint import expectation_context as blueprint_context
    context = blueprint_context(snapshot, selected,
                                raw_context.decode('utf-8') if raw_context is not None else None)
    if any(context['references'].values()):
        raw_context = (json.dumps(context, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    now = datetime.now(timezone.utc)
    run = {'run_id': now.strftime('%Y%m%dT%H%M%SZ-') + str(uuid.uuid4()),
           'model': worker['model'], 'reasoning_effort': worker['reasoning_effort'],
           'created_at': now.isoformat(), 'policy_version': 'statement-fields.v2',
           'source_commit': snapshot['source_commit'], 'snapshot_digest': snapshot['digest'],
           'expectation_context_digest': hashlib.sha256(raw_context).hexdigest() if raw_context is not None else None,
           'topics': config['topics'], 'threshold': threshold}
    helper._validate({'schema': 'statement-enrichment.v2', 'run': run, 'annotations': [],
                      'sources': {}, 'originals': {}, 'reviews': {}}, snapshot)
    manifest = {'schema': 'formaliscope-enrichment-batch.v2',
                'source_commit': snapshot['source_commit'], 'snapshot_digest': snapshot['digest'],
                'threshold': threshold, 'declaration_ids': selected, 'run': run}
    snapshot_link = _shared_snapshot(snapshot, output)
    helper._private_write(output, {'manifest.json': manifest,
                                  'agent-config.json': config},
                          {'expectation-context.txt': raw_context} if raw_context is not None else None,
                          symlinks={'snapshot.json': snapshot_link})
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path,
                        help='explicit configuration for the selected harness')
    parser.add_argument('--directory', action='append', default=[])
    parser.add_argument('--file', action='append', default=[])
    parser.add_argument('--declaration-id', action='append', default=[])
    parser.add_argument('--threshold', type=float, default=0.8)
    parser.add_argument('--expectation-context', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = prepare(args.snapshot, args.output, config_path=args.config,
                           directories=args.directory, files=args.file,
                           declaration_ids=args.declaration_id, threshold=args.threshold,
                           expectation_context=args.expectation_context)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(2, f'prepare: {exc}\n')
    import json
    print(json.dumps({'output': str(args.output.resolve()), 'run_id': manifest['run']['run_id'],
                      'selected': len(manifest['declaration_ids']), 'model': manifest['run']['model']}))


if __name__ == '__main__':
    main()
