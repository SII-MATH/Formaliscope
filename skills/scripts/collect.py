#!/usr/bin/env python3
"""Mechanically collect source-bound drafts; never run models or install data."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
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


STAGED_SCHEMA = 'formaliscope-enrichment-batch.v3'
_UNVERIFIED = object()


def _batch_inputs(snapshot_path, manifest_path, executed_model=_UNVERIFIED):
    snapshot, manifest = _read(snapshot_path), _read(manifest_path)
    schema = manifest.get('schema') if isinstance(manifest, dict) else None
    fields = ('schema', 'source_commit', 'snapshot_digest', 'threshold', 'declaration_ids', 'run')
    if schema == STAGED_SCHEMA:
        fields += ('result_protocol', 'harness')
    elif schema != 'formaliscope-enrichment-batch.v2':
        raise ValueError('unsupported manifest schema')
    _fields(manifest, fields, 'manifest')
    if schema == STAGED_SCHEMA:
        if manifest['result_protocol'] != 'formaliscope-stage-results.v1':
            raise ValueError('unsupported stage result protocol')
        harness = manifest['harness']
        if harness is not None and (not isinstance(harness, str) or not harness.strip()):
            raise ValueError('invalid frozen harness')
    threshold = _score(manifest['threshold'])
    run = manifest['run']
    _fields(run, ('run_id', 'model', 'reasoning_effort', 'created_at', 'policy_version',
                  'source_commit', 'snapshot_digest', 'expectation_context_digest',
                  'topics', 'threshold'), 'run')
    if (run['source_commit'] != manifest['source_commit'] or
            run['snapshot_digest'] != manifest['snapshot_digest'] or
            run['threshold'] != threshold):
        raise ValueError('manifest and run source/threshold must agree')
    if executed_model is not _UNVERIFIED and (not executed_model or executed_model != run['model']):
        raise ValueError('executed model must be confirmed and match the frozen run model; start a new batch to change models')
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
    return snapshot, manifest


def _collect_v2(snapshot_path, manifest_path, result_paths, review_paths, output,
                executed_model, readback_paths):
    from review_app.enrichment_v2 import validate_agent_annotations

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('output directory must not already exist')
    snapshot, manifest = _batch_inputs(snapshot_path, manifest_path, executed_model)
    run, threshold, selected = manifest['run'], manifest['threshold'], manifest['declaration_ids']
    card_by_id = {card['id']: card for card in snapshot['cards']}

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

    if manifest['schema'] == STAGED_SCHEMA:
        originals, paths = _stage_originals(snapshot, manifest, manifest_path, result_paths, readback_paths)
    else:
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
    if readback_paths and manifest['schema'] == 'formaliscope-enrichment-batch.v2':
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


def _stage_rows(raw, stage, snapshot, manifest, identities):
    from review_app.enrichment_v2 import validate_stage_batch
    rows = validate_stage_batch(json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                           parse_constant=_constant),
                                stage, snapshot, topics=manifest['run']['topics'])
    if {row['declaration_id'] for row in rows} != set(identities):
        raise ValueError(f'{stage} results must exactly match the assigned group IDs and count')
    return rows


def _group_ids(identities, manifest):
    if (not isinstance(identities, list) or not identities or
            any(not isinstance(item, str) or item not in manifest['declaration_ids'] for item in identities) or
            len(set(identities)) != len(identities)):
        raise ValueError('group IDs must be non-empty, selected and unique')
    if manifest['harness'] == 'claude-code' and len(identities) != 1:
        raise ValueError('Claude Code groups must contain exactly one declaration')


def _needs_expectation(manifest):
    return manifest['harness'] == 'claude-code' or manifest['run']['expectation_context_digest'] is not None


def _baseline_path(readback_path):
    return Path(str(readback_path) + '.baseline.json')


def _new_file(path, raw):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(raw)


def _new_paths(paths):
    if len(set(paths)) != len(paths):
        raise ValueError('stage output paths must be distinct')
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError('stage output paths must not already exist; allocate new paths')


def deliver_readback(snapshot_path, manifest_path, input_path, result_path, declaration_ids,
                     expectation_path=None):
    snapshot, manifest = _batch_inputs(snapshot_path, manifest_path)
    if manifest['schema'] != STAGED_SCHEMA:
        raise ValueError('stage delivery requires an explicit v3 manifest')
    _group_ids(declaration_ids, manifest)
    if (expectation_path is not None) != _needs_expectation(manifest):
        raise ValueError('expectation output path must follow the frozen harness/context rule')
    raw = Path(input_path).read_bytes()
    rows = _stage_rows(raw, 'readback', snapshot, manifest, declaration_ids)
    result_path = Path(result_path).absolute()
    baseline_path = _baseline_path(result_path)
    next_path = Path(expectation_path).absolute() if expectation_path is not None else None
    _new_paths([result_path, baseline_path] + ([next_path] if next_path is not None else []))
    baseline = {'schema': 'formaliscope-readback-baseline.v1',
                'manifest_sha256': hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
                'run_id': manifest['run']['run_id'],
                'source_commit': manifest['source_commit'], 'snapshot_digest': manifest['snapshot_digest'],
                'declaration_ids': declaration_ids, 'readback_path': str(result_path),
                'readback_sha256': hashlib.sha256(raw).hexdigest(),
                'expectation_path': str(next_path) if next_path is not None else None}
    baseline_raw = (json.dumps(baseline, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    _new_file(result_path, raw)
    _new_file(baseline_path, baseline_raw)
    return {'result_path': str(result_path), 'count': len(rows)}


def _baseline(snapshot, manifest, manifest_path, readback_path):
    readback_path = Path(readback_path).absolute()
    baseline_path = _baseline_path(readback_path)
    if not baseline_path.is_file() or baseline_path.is_symlink() or readback_path.is_symlink():
        raise ValueError('saved readback baseline is missing or is a symlink')
    baseline = _read(baseline_path)
    _fields(baseline, ('schema', 'manifest_sha256', 'run_id', 'source_commit', 'snapshot_digest',
                       'declaration_ids', 'readback_path', 'readback_sha256', 'expectation_path'), 'readback baseline')
    if (baseline['schema'] != 'formaliscope-readback-baseline.v1' or
            baseline['manifest_sha256'] != hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest() or
            baseline['run_id'] != manifest['run']['run_id'] or
            baseline['source_commit'] != manifest['source_commit'] or
            baseline['snapshot_digest'] != manifest['snapshot_digest'] or
            baseline['readback_path'] != str(readback_path)):
        raise ValueError('readback baseline does not match the frozen batch/path')
    _group_ids(baseline['declaration_ids'], manifest)
    next_path = baseline['expectation_path']
    if next_path is not None and (not isinstance(next_path, str) or not Path(next_path).is_absolute()):
        raise ValueError('invalid saved expectation output path')
    if (next_path is not None) != _needs_expectation(manifest):
        raise ValueError('saved expectation output path violates the frozen harness/context rule')
    raw = readback_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != baseline['readback_sha256']:
        raise ValueError('readback baseline digest changed; do not modify or reseal original results')
    rows = _stage_rows(raw, 'readback', snapshot, manifest, baseline['declaration_ids'])
    return baseline, rows


def check_readback(snapshot_path, manifest_path, readback_path):
    snapshot, manifest = _batch_inputs(snapshot_path, manifest_path)
    if manifest['schema'] != STAGED_SCHEMA:
        raise ValueError('stage delivery requires an explicit v3 manifest')
    _, rows = _baseline(snapshot, manifest, manifest_path, readback_path)
    return {'result_path': str(Path(readback_path).absolute()), 'count': len(rows)}


def deliver_expectation(snapshot_path, manifest_path, input_path, result_path, readback_path):
    snapshot, manifest = _batch_inputs(snapshot_path, manifest_path)
    if manifest['schema'] != STAGED_SCHEMA:
        raise ValueError('stage delivery requires an explicit v3 manifest')
    baseline, _ = _baseline(snapshot, manifest, manifest_path, readback_path)
    result_path = Path(result_path).absolute()
    if str(result_path) != baseline['expectation_path']:
        raise ValueError('expectation output path must match the saved group allocation')
    raw = Path(input_path).read_bytes()
    rows = _stage_rows(raw, 'expectation', snapshot, manifest, baseline['declaration_ids'])
    if manifest['run']['expectation_context_digest'] is None and any(
            row['expectation_assessment']['verdict'] != 'undetermined' for row in rows):
        raise ValueError('without expectation context, verdict must be undetermined')
    _new_paths([result_path])
    _new_file(result_path, raw)
    return {'result_path': str(result_path), 'count': len(rows)}


def _stage_originals(snapshot, manifest, manifest_path, result_paths, readback_paths):
    if not readback_paths:
        raise ValueError('staged results require saved first-stage readback files and baselines')
    result_paths = [str(Path(path).absolute()) for path in result_paths]
    if len(result_paths) != len(set(result_paths)):
        raise ValueError('duplicate expectation result path')
    originals, origins, expected_paths = {}, {}, set()
    for path in readback_paths:
        baseline, first = _baseline(snapshot, manifest, manifest_path, path)
        next_path = baseline['expectation_path']
        if next_path is not None:
            if next_path in expected_paths or next_path not in result_paths:
                raise ValueError('missing or duplicate assigned expectation result path')
            expected_paths.add(next_path)
            if Path(next_path).is_symlink():
                raise ValueError('expectation result must not be a symlink')
            assessments = {row['declaration_id']: row['expectation_assessment'] for row in
                           _stage_rows(Path(next_path).read_bytes(), 'expectation', snapshot, manifest,
                                       baseline['declaration_ids'])}
        else:
            assessments = {row['declaration_id']: {
                'verdict': 'undetermined', 'reason_zh': '未提供独立预期材料；本 harness 跳过第二阶段。',
                'confidence': 1.0} for row in first}
        for row in first:
            identity = row['declaration_id']
            if identity in originals:
                raise ValueError('readback declaration must be selected and appear once')
            originals[identity] = {**row, 'expectation_assessment': assessments[identity]}
            origins[identity] = next_path or str(Path(path).absolute())
    if expected_paths != set(result_paths) or set(originals) != set(manifest['declaration_ids']):
        raise ValueError('stage results must cover exactly the manifest declarations and allocated paths')
    return originals, origins


def collect(snapshot_path, manifest_path, result_paths, review_paths, output,
            *, executed_model=None, readback_paths=None):
    """Dispatch by frozen manifest; retain v1 validation for historical batches."""
    manifest = _read(manifest_path)
    if isinstance(manifest, dict):
        if manifest.get('schema') in ('formaliscope-enrichment-batch.v2', STAGED_SCHEMA):
            return _collect_v2(snapshot_path, manifest_path, result_paths, review_paths, output,
                               executed_model, readback_paths or [])
        if manifest.get('schema') == 'formaliscope-enrichment-batch.v1':
            return _collect_v1(snapshot_path, manifest_path, result_paths, review_paths, output)
    raise ValueError('unsupported manifest schema')


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
    delivery = parser.add_mutually_exclusive_group()
    delivery.add_argument('--deliver-readback', action='store_true')
    delivery.add_argument('--deliver-expectation', action='store_true')
    delivery.add_argument('--check-readback', action='store_true')
    parser.add_argument('--input', type=Path, help='new stage draft to validate and deliver without overwriting')
    parser.add_argument('--declaration-id', action='append', default=[])
    parser.add_argument('--next-result', type=Path, help='allocated second-stage path, omitted only when skipped')
    parser.add_argument('--config', type=Path, help='task configuration with collection receipts')
    parser.add_argument('--snapshot', type=Path, help='frozen snapshot used for delivery or collection')
    parser.add_argument('--manifest', type=Path, help='frozen batch manifest')
    parser.add_argument('--result', action='append', type=Path, help='allocated stage output or collection input')
    parser.add_argument('--review', action='append', type=Path, default=[], help=argparse.SUPPRESS)
    parser.add_argument('--executed-model', help=argparse.SUPPRESS)
    parser.add_argument('--readback-result', action='append', type=Path, default=[],
                        help='first-stage result; its .baseline.json record is located automatically')
    parser.add_argument('--output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.deliver_readback or args.deliver_expectation or args.check_readback:
            if (not args.snapshot or not args.manifest or
                    any((args.config, args.review, args.output, args.executed_model))):
                raise ValueError('stage delivery requires snapshot and manifest, without collection settings')
            if args.check_readback:
                if len(args.readback_result) != 1 or any((args.input, args.result, args.next_result, args.declaration_id)):
                    raise ValueError('baseline check requires exactly one readback-result')
                receipt = check_readback(args.snapshot, args.manifest, args.readback_result[0])
            else:
                if not args.input or not args.result or len(args.result) != 1:
                    raise ValueError('stage delivery requires an input draft and exactly one result path')
                if args.deliver_readback:
                    if args.readback_result:
                        raise ValueError('readback delivery does not accept a prior readback-result')
                    receipt = deliver_readback(args.snapshot, args.manifest, args.input, args.result[0],
                                               args.declaration_id, args.next_result)
                else:
                    if len(args.readback_result) != 1 or args.next_result or args.declaration_id:
                        raise ValueError('expectation delivery requires exactly one readback-result')
                    receipt = deliver_expectation(args.snapshot, args.manifest, args.input,
                                                  args.result[0], args.readback_result[0])
            print(json.dumps(receipt))
            return
        if any((args.input, args.next_result, args.declaration_id)):
            raise ValueError('stage arguments require an explicit delivery operation')
        if args.config is not None:
            if any((args.snapshot, args.manifest, args.result, args.review, args.output,
                    args.executed_model, args.readback_result)):
                raise ValueError('put collection settings in the configuration file')
            output, report = collect_from_config(args.config)
        else:
            if not all((args.snapshot, args.manifest, args.output)) or not (args.result or args.readback_result):
                raise ValueError('provide config or snapshot, manifest, output and result inputs')
            output = args.output
            report = collect(args.snapshot, args.manifest, args.result or [], args.review, output,
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
