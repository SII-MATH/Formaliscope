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
REPO = Path(__file__).resolve().parents[2]
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


def _collect_v1(snapshot_path, manifest_path, result_paths, review_paths, output):
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
        if result['schema'] not in ('formaliscope-agent-batch.v1', 'formaliscope-luna-batch.v1'):
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
    queue = {'schema': 'formaliscope-agent-batch.v1',
             'enrichment': {'schema': 'statement-enrichment.v1', 'annotations': pending},
             'confidence': pending_scores}
    report = {'schema': 'formaliscope-enrichment-report.v1',
              'source_commit': manifest['source_commit'], 'snapshot_digest': manifest['snapshot_digest'],
              'threshold': threshold, 'entries': entries}
    # Serialization also finishes before creating output or writing any file.
    contents = {name: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
                for name, value in (('enrichment.json', enrichment), ('review-queue.json', queue),
                                    ('report.json', report))}
    _private_write(output, {}, {name: content.encode('utf-8') for name, content in contents.items()})
    return report


def _private_write(output, documents, raw_files=None, *, symlinks=None):
    """Serialize and validate everything before publishing private batch output."""
    import os
    encoded = {name: (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
               for name, value in documents.items()}
    encoded.update(raw_files or {})
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('output directory must not already exist')
    missing = []
    parent = output.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    for name, content in encoded.items():
        path = output / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
    for name, target in (symlinks or {}).items():
        (output / name).symlink_to(target)


def _collect_v2(snapshot_path, manifest_path, result_paths, review_paths, output,
                executed_model, readback_paths):
    import hashlib
    from review_app.enrichment_v2 import validate_agent_annotations

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('output directory must not already exist')
    snapshot, manifest = _read(snapshot_path), _read(manifest_path)
    _fields(manifest, ('schema', 'source_commit', 'snapshot_digest', 'threshold',
                       'declaration_ids', 'run'), 'manifest')
    if manifest['schema'] != 'formaliscope-enrichment-batch.v2':
        raise ValueError('unsupported manifest schema')
    threshold = _score(manifest['threshold'])
    run = manifest['run']
    _fields(run, ('run_id', 'model', 'reasoning_effort', 'created_at', 'policy_version',
                  'source_commit', 'snapshot_digest', 'expectation_context_digest',
                  'topics', 'threshold'), 'run')
    if (run['source_commit'] != manifest['source_commit'] or
            run['snapshot_digest'] != manifest['snapshot_digest'] or
            run['threshold'] != threshold):
        raise ValueError('manifest and run source/threshold must agree')
    if not executed_model or executed_model != run['model']:
        raise ValueError('executed model must be confirmed and match the frozen run model; start a new batch to change models')
    # The application validator checks run metadata, digest and frozen modules.
    empty = {'schema': 'statement-enrichment.v2', 'run': run, 'annotations': [],
             'sources': {}, 'originals': {}, 'reviews': {}}
    _validate(empty, snapshot)
    config_path = Path(manifest_path).parent / 'agent-config.json'
    if not config_path.is_file():
        raise ValueError('frozen agent configuration is missing')
    config = _read(config_path)
    _fields(config, ('schema', 'worker', 'topics'), 'frozen config')
    _fields(config['worker'], ('model', 'reasoning_effort'), 'frozen worker config')
    if (config['schema'] != 'formaliscope-enrichment-config.v2' or
            config['worker']['model'] != run['model'] or
            config['worker']['reasoning_effort'] != run['reasoning_effort'] or
            config['topics'] != run['topics']):
        raise ValueError('frozen agent configuration does not match the run; start a new batch to change configuration')
    selected = manifest['declaration_ids']
    if (not isinstance(selected, list) or not selected or
            any(not isinstance(item, str) or not item.startswith('statement::') or
                len(item) == len('statement::') for item in selected) or
            len(selected) != len(set(selected))):
        raise ValueError('manifest declaration_ids must be non-empty, exact and unique')
    card_by_id = {card['id']: card for card in snapshot['cards']}
    if not set(selected).issubset(card_by_id):
        raise ValueError('manifest contains unknown declarations')

    def batches(paths, label):
        if not paths:
            raise ValueError(f'at least one {label} result is required')
        rows, origins = {}, {}
        for path in paths:
            document = _read(path)
            _fields(document, ('schema', 'annotations'), f'{label} result')
            if document['schema'] != 'formaliscope-agent-batch.v2':
                raise ValueError('unsupported result schema')
            annotations = validate_agent_annotations(document['annotations'], snapshot, topics=run['topics'])
            if not annotations:
                raise ValueError('result batch must not be empty')
            for row in annotations:
                identity = row['declaration_id']
                if identity in rows or identity not in selected:
                    raise ValueError('result declaration must be selected and appear once')
                rows[identity] = row
                origins[identity] = str(Path(path).resolve())
        if set(rows) != set(selected):
            raise ValueError('results must cover exactly the manifest declarations')
        return rows, origins

    originals, paths = batches(result_paths, 'worker')
    if run['expectation_context_digest'] is not None and not readback_paths:
        raise ValueError('expectation context requires saved first-stage readback results')
    if run['expectation_context_digest'] is not None:
        context = Path(manifest_path).parent / 'expectation-context.txt'
        if (not context.is_file() or
                hashlib.sha256(context.read_bytes()).hexdigest() != run['expectation_context_digest']):
            raise ValueError('frozen expectation context is missing or its digest changed')
        from review_app.blueprint import CONTEXT_SCHEMA, expectation_context
        try:
            material = _read(context)
        except (ValueError, UnicodeError):
            material = None  # Existing caller-provided plain text remains supported.
        if isinstance(material, dict) and material.get('schema') == CONTEXT_SCHEMA:
            if material != expectation_context(snapshot, selected, material.get('additional_context')):
                raise ValueError('Blueprint expectations do not match the selected frozen declarations')
            if material['additional_context'] is None:
                for identity in selected:
                    if not material['references'][identity] and originals[identity]['expectation_assessment']['verdict'] != 'undetermined':
                        raise ValueError('a declaration without Blueprint reference must be undetermined')
    if readback_paths:
        first_stage, _ = batches(readback_paths, 'readback')
        for identity in selected:
            if originals[identity]['readback'] != first_stage[identity]['readback']:
                raise ValueError('expectation assessment must not alter saved readback or its confidence')
    if run['expectation_context_digest'] is None:
        if any(row['expectation_assessment']['verdict'] != 'undetermined'
               for row in originals.values()):
            raise ValueError('without expectation context, verdict must be undetermined')

    reviews = {}
    for path in review_paths:
        document = _read(path)
        _fields(document, ('schema', 'reviews'), 'review document')
        if document['schema'] != 'formaliscope-enrichment-review.v2' or not isinstance(document['reviews'], list):
            raise ValueError('unsupported review document')
        for review in document['reviews']:
            _fields(review, ('declaration_id', 'annotation', 'model', 'reviewed_at'), 'review')
            identity = review['declaration_id']
            if not isinstance(identity, str) or identity not in originals or identity in reviews:
                raise ValueError('review declaration must be known and appear once')
            original = originals[identity]
            if original['readback']['text_zh'] is None:
                raise ValueError('missing readback is failed, not a semantic review task')
            if original['readback']['confidence'] >= threshold:
                raise ValueError('only original low-confidence declarations may be reviewed')
            row = review['annotation']
            validate_agent_annotations([row], snapshot, topics=run['topics'])
            if row['declaration_id'] != identity:
                raise ValueError('review annotation identity must match the original')
            if row['readback']['text_zh'] is None:
                raise ValueError('reviewed annotation must contain a readback')
            if (row['readback']['confidence'] != original['readback']['confidence'] or
                    row['expectation_assessment'] != original['expectation_assessment']):
                raise ValueError('review must preserve both original confidence scores and expectation assessment')
            if not isinstance(review['model'], str) or not review['model'].strip():
                raise ValueError('review must record the actual model')
            _time(review['reviewed_at'])
            reviews[identity] = {**review, 'path': str(Path(path).resolve())}

    accepted, accepted_originals, pending, entries, sources, review_metadata = [], {}, [], [], {}, {}
    for identity in selected:
        original, review = originals[identity], reviews.get(identity)
        score = original['readback']['confidence']
        route = ('failed' if original['readback']['text_zh'] is None else
                 'direct' if score >= threshold else 'reviewed' if review else 'pending')
        if route == 'pending':
            pending.append(original)
        elif route in ('direct', 'reviewed'):
            accepted.append(review['annotation'] if review else original)
            accepted_originals[identity] = original
            sources[identity] = hashlib.sha256(card_by_id[identity]['lean']['source'].encode('utf-8')).hexdigest()
            if review:
                review_metadata[identity] = {'model': review['model'], 'reviewed_at': review['reviewed_at']}
        entries.append({'declaration_id': identity, 'readback_confidence': score,
                        'expectation_confidence': original['expectation_assessment']['confidence'],
                        'route': route, 'original_model': run['model'],
                        'review_model': review['model'] if review else None,
                        'reviewed_at': review['reviewed_at'] if review else None,
                        'result_path': paths[identity], 'review_path': review['path'] if review else None})
    enrichment = {'schema': 'statement-enrichment.v2', 'run': run, 'annotations': accepted,
                  'sources': sources, 'originals': accepted_originals, 'reviews': review_metadata}
    _validate(enrichment, snapshot)
    queue = {'schema': 'formaliscope-agent-batch.v2', 'annotations': pending}
    report = {'schema': 'formaliscope-enrichment-report.v2', 'run_id': run['run_id'],
              'source_commit': run['source_commit'], 'snapshot_digest': run['snapshot_digest'],
              'threshold': threshold, 'entries': entries,
              'counts': {route: sum(row['route'] == route for row in entries)
                         for route in ('direct', 'reviewed', 'pending', 'failed')}}
    _private_write(output, {'enrichment.json': enrichment, 'review-queue.json': queue, 'report.json': report})
    return report


def collect(snapshot_path, manifest_path, result_paths, review_paths, output,
            *, executed_model=None, readback_paths=None):
    """Dispatch by frozen manifest; retain v1 validation for historical batches."""
    manifest = _read(manifest_path)
    if isinstance(manifest, dict) and manifest.get('schema') == 'formaliscope-enrichment-batch.v2':
        return _collect_v2(snapshot_path, manifest_path, result_paths, review_paths, output,
                           executed_model, readback_paths or [])
    return _collect_v1(snapshot_path, manifest_path, result_paths, review_paths, output)


def collect_from_config(config_path):
    from skills.scripts.config import load_config, project_path, task_output
    settings = load_config(config_path)
    batch = task_output(config_path, settings)
    collection = settings['collection']
    output = (project_path(collection['output'], 'collection.output')
              if collection['output'] is not None else batch / 'collected')
    paths = lambda key: [project_path(value, 'collection.' + key) for value in collection[key]]
    return output, collect(batch / 'snapshot.json', batch / 'manifest.json',
                           paths('results'), paths('reviews'), output,
                           executed_model=collection['executed_model'],
                           readback_paths=paths('readback_results'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='task configuration with collection receipts')
    parser.add_argument('--snapshot', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--manifest', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--result', action='append', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--review', action='append', type=Path, default=[], help=argparse.SUPPRESS)
    parser.add_argument('--executed-model', help=argparse.SUPPRESS)
    parser.add_argument('--readback-result', action='append', type=Path, default=[],
                        help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.config is not None:
            if any((args.snapshot, args.manifest, args.result, args.review, args.output,
                    args.executed_model, args.readback_result)):
                raise ValueError('put collection settings in the configuration file')
            output, report = collect_from_config(args.config)
        else:
            if not all((args.snapshot, args.manifest, args.result, args.output)):
                raise ValueError('provide config or the complete legacy collection arguments')
            output = args.output
            report = collect(args.snapshot, args.manifest, args.result, args.review, output,
                             executed_model=args.executed_model, readback_paths=args.readback_result)
    except (ValueError, OSError) as exc:
        parser.exit(2, f'collect: {exc}\n')
    print(json.dumps({'output': str(output.resolve()),
                      'routes': {route: sum(row['route'] == route for row in report['entries'])
                                 for route in (('direct', 'reviewed', 'pending', 'failed')
                                               if report['schema'].endswith('.v2') else
                                               ('direct', 'reviewed', 'pending'))}}))


if __name__ == '__main__':
    main()
