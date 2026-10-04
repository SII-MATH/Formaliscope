#!/usr/bin/env python3
"""Mechanically collect source-bound drafts; never run models or install data."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys


# Locate the repository from this script, independently of the caller's cwd.
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
from review_app.enrichment import validate_enrichment


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON object key: {key}')
        value[key] = item
    return value


def _constant(value):
    raise ValueError(f'non-finite JSON value: {value}')


def _read(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=_pairs, parse_constant=_constant)


def _fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f'{label}: fields must be exactly {", ".join(fields)}')


def _score(value):
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError('confidence must be a finite number between 0 and 1')
    return value


def _time(value):
    if not isinstance(value, str):
        raise ValueError('reviewed_at must be a timestamp with a timezone')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
    except ValueError as exc:
        raise ValueError('reviewed_at must be a timestamp with a timezone') from exc


def _validate(document, snapshot):
    try:
        return validate_enrichment(document, snapshot)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('invalid enrichment or snapshot structure') from exc


def collect(snapshot_path, manifest_path, result_paths, review_paths, output):
    """Validate every input first, then write three files to a new directory."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('output directory must not already exist')
    snapshot = _read(snapshot_path)
    if not isinstance(snapshot, dict):
        raise ValueError('snapshot must be an object')
    _validate({'schema': 'statement-enrichment.v1', 'annotations': []}, snapshot)
    manifest = _read(manifest_path)
    _fields(manifest, ('schema', 'source_commit', 'snapshot_digest', 'threshold', 'declaration_ids'), 'manifest')
    if manifest['schema'] != 'formaliscope-enrichment-batch.v1':
        raise ValueError('unsupported manifest schema')
    threshold = _score(manifest['threshold'])
    if (manifest['source_commit'] != snapshot.get('source_commit') or
            manifest['snapshot_digest'] != snapshot.get('digest')):
        raise ValueError('manifest source does not match the frozen snapshot')
    selected = manifest['declaration_ids']
    if (not isinstance(selected, list) or not selected or
            any(not isinstance(item, str) or not item.startswith('statement::') or
                len(item) == len('statement::') for item in selected) or
            len(selected) != len(set(selected))):
        raise ValueError('manifest declaration_ids must be non-empty, exact and unique')
    if not result_paths:
        raise ValueError('at least one result is required')

    originals, scores, sources = {}, {}, {}
    for path in result_paths:
        result = _read(path)
        _fields(result, ('schema', 'enrichment', 'confidence'), 'result')
        if result['schema'] != 'formaliscope-luna-batch.v1':
            raise ValueError('unsupported result schema')
        annotations = _validate(result['enrichment'], snapshot)
        ids = {row['declaration_id'] for row in annotations}
        if not annotations:
            raise ValueError('result batch must not be empty')
        if not isinstance(result['confidence'], dict) or set(result['confidence']) != ids:
            raise ValueError('confidence keys must exactly match result annotations')
        for annotation in annotations:
            identity = annotation['declaration_id']
            if identity in originals or identity not in selected:
                raise ValueError('result declaration must be selected and appear once')
            originals[identity] = annotation
            scores[identity] = _score(result['confidence'][identity])
            sources[identity] = str(Path(path).resolve())
    if set(originals) != set(selected):
        raise ValueError('results must cover exactly the manifest declarations')

    reviews = {}
    for path in review_paths:
        document = _read(path)
        _fields(document, ('schema', 'reviews'), 'review document')
        if document['schema'] != 'formaliscope-enrichment-review.v1' or not isinstance(document['reviews'], list):
            raise ValueError('unsupported review document')
        for review in document['reviews']:
            _fields(review, ('declaration_id', 'annotation', 'model', 'reviewed_at'), 'review')
            identity = review['declaration_id']
            if not isinstance(identity, str) or identity not in originals or identity in reviews:
                raise ValueError('review declaration must be known and appear once')
            if scores[identity] >= threshold:
                raise ValueError('only original low-confidence declarations may be reviewed')
            annotation = review['annotation']
            _validate({'schema': 'statement-enrichment.v1', 'annotations': [annotation]}, snapshot)
            if (annotation['declaration_id'] != identity or
                    annotation['basis'] != originals[identity]['basis']):
                raise ValueError('review annotation identity and basis must match the original')
            if not isinstance(review['model'], str) or not review['model'].strip():
                raise ValueError('review must record the actual model')
            _time(review['reviewed_at'])
            reviews[identity] = {**review, 'path': str(Path(path).resolve())}

    accepted, pending, pending_scores, entries = [], [], {}, []
    for identity in selected:
        annotation, review = originals[identity], reviews.get(identity)
        route = 'direct' if scores[identity] >= threshold else 'reviewed' if review else 'pending'
        if route == 'pending':
            pending.append(annotation)
            pending_scores[identity] = scores[identity]
        else:
            accepted.append(review['annotation'] if review else annotation)
        entries.append({'declaration_id': identity, 'confidence': scores[identity],
                        'original_model': annotation['provenance']['model'], 'route': route,
                        'review_model': review['model'] if review else None,
                        'reviewed_at': review['reviewed_at'] if review else None,
                        'result_path': sources[identity], 'review_path': review['path'] if review else None})
    enrichment = {'schema': 'statement-enrichment.v1', 'annotations': accepted}
    queue = {'schema': 'formaliscope-luna-batch.v1',
             'enrichment': {'schema': 'statement-enrichment.v1', 'annotations': pending},
             'confidence': pending_scores}
    report = {'schema': 'formaliscope-enrichment-report.v1',
              'source_commit': manifest['source_commit'], 'snapshot_digest': manifest['snapshot_digest'],
              'threshold': threshold, 'entries': entries}
    # Serialization also finishes before creating output or writing any file.
    contents = {name: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
                for name, value in (('enrichment.json', enrichment), ('review-queue.json', queue),
                                    ('report.json', report))}
    output.mkdir(parents=True, exist_ok=False)
    for name, content in contents.items():
        with (output / name).open('x', encoding='utf-8') as stream:
            stream.write(content)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--result', required=True, action='append', type=Path)
    parser.add_argument('--review', action='append', type=Path, default=[])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = collect(args.snapshot, args.manifest, args.result, args.review, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(2, f'collect: {exc}\n')
    print(json.dumps({'output': str(args.output.resolve()),
                      'routes': {route: sum(row['route'] == route for row in report['entries'])
                                 for route in ('direct', 'reviewed', 'pending')}}))


if __name__ == '__main__':
    main()
