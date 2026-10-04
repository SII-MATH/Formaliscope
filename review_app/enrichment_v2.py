"""Minimal Agent fields, automatic source binding, and public projections.

Internal judgments and original scores stay in the private batch artifact.
Only public_annotation may be copied into a reviewer snapshot.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'statement_workflow/schema/statement-enrichment.v2.schema.json'
DEFAULT_TOPICS = [
    {'id': 'spectral_sequence', 'name': '谱序列'},
    {'id': 'adams', 'name': 'Adams 理论'},
    {'id': 'stable_homotopy', 'name': '稳定同伦论'},
    {'id': 'sphere', 'name': '球谱'},
    {'id': 'synthetic_homotopy', 'name': '合成同伦论'},
    {'id': 'steenrod', 'name': 'Steenrod 代数'},
    {'id': 'homological_algebra', 'name': '分次与同调代数'},
    {'id': 'higher_algebra', 'name': '范畴与高阶代数'},
    {'id': 'kervaire', 'name': 'Kervaire 问题'},
]


@lru_cache(maxsize=1)
def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


def read_document(path):
    """Reject duplicate keys and non-finite constants before CLI validation."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON object key: {key}')
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f'non-finite JSON value: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8'),
                      object_pairs_hook=pairs, parse_constant=constant)


def _contract(value, name):
    from .enrichment import _validate
    schema = _schema()
    _validate(value, schema['$defs'][name] if name else schema, schema)


def validate_topics(topics):
    if not isinstance(topics, list) or len(topics) > 100:
        raise ValueError('project topics must be a list of at most 100 items')
    for topic in topics:
        _contract(topic, 'topic')
    if len({item['id'] for item in topics}) != len(topics):
        raise ValueError('project topic IDs must be unique')
    return topics


def validate_agent_annotations(annotations, snapshot, topics=None):
    """Check model supplied fields without asking the model for provenance."""
    from .build import validate_snapshot
    validate_snapshot(snapshot)
    if snapshot.get('review_mode') != 'statement':
        raise ValueError('enrichment requires a Statement snapshot')
    return _annotations(annotations, snapshot, DEFAULT_TOPICS if topics is None else topics)


def _annotations(annotations, snapshot, topics):
    allowed_topics = {item['id'] for item in validate_topics(topics)}
    if not isinstance(annotations, list):
        raise ValueError('annotations must be a list')
    cards = {card['id']: card for card in snapshot['cards']}
    seen = set()
    for annotation in annotations:
        _contract(annotation, 'annotation')
        identity = annotation['declaration_id']
        if identity not in cards or identity in seen:
            raise ValueError('annotation declaration must be known and unique')
        seen.add(identity)
        lean = cards[identity].get('lean')
        if not isinstance(lean, dict) or not isinstance(lean.get('source'), str) or not lean['source'].strip():
            raise ValueError('selected declaration must have non-empty frozen Lean source text')
        if set(annotation['classification']['topics']) - allowed_topics:
            raise ValueError('annotation uses an unregistered project topic')
    return annotations


def validate_document(document, snapshot):
    """Validate a collected artifact, including its immutable originals."""
    _contract(document, None)
    run = document['run']
    if (run['source_commit'] != snapshot['source_commit'] or
            run['snapshot_digest'] != snapshot['digest']):
        raise ValueError('run source does not match this frozen snapshot')
    rows = _annotations(document['annotations'], snapshot, run['topics'])
    identities = {row['declaration_id'] for row in rows}
    if set(document['sources']) != identities or set(document['originals']) != identities:
        raise ValueError('sources and originals must exactly match annotations')
    if set(document['reviews']) - identities:
        raise ValueError('reviews must refer to accepted annotations')
    original_rows = list(document['originals'].values())
    _annotations(original_rows, snapshot, run['topics'])
    cards = {card['id']: card for card in snapshot['cards']}
    for row in rows:
        identity = row['declaration_id']
        original = document['originals'][identity]
        if original['declaration_id'] != identity:
            raise ValueError('original declaration ID does not match its key')
        source = cards[identity]['lean']['source']
        if document['sources'][identity] != hashlib.sha256(source.encode('utf-8')).hexdigest():
            raise ValueError('collected source hash does not match this frozen snapshot')
        if row['readback']['text_zh'] is None or original['readback']['text_zh'] is None:
            raise ValueError('a missing readback cannot be accepted as successful')
        if (row['readback']['confidence'] != original['readback']['confidence'] or
                row['expectation_assessment'] != original['expectation_assessment']):
            raise ValueError('original scores and internal assessment must be preserved')
        reviewed = identity in document['reviews']
        if reviewed:
            if original['readback']['confidence'] >= run['threshold']:
                raise ValueError('only original low-confidence declarations may be reviewed')
        elif row != original or original['readback']['confidence'] < run['threshold']:
            raise ValueError('unreviewed accepted entries must be unchanged high-confidence originals')
        if (run['expectation_context_digest'] is None and
                original['expectation_assessment']['verdict'] != 'undetermined'):
            raise ValueError('an assessment without expectation context must be undetermined')
    return rows


def public_annotation(annotation):
    """Explicit allowlist; never spread the worker or internal run record."""
    return {
        'schema': 'statement-enrichment.v2',
        'declaration_id': annotation['declaration_id'],
        'title_zh': annotation['title_zh'],
        'readback': {'status': 'draft' if annotation['readback']['text_zh'] is not None else 'none',
                     'text_zh': annotation['readback']['text_zh']},
        'classification': deepcopy(annotation['classification']),
        'priority': annotation['priority'],
    }


def validate_public_annotation(annotation, topics):
    _contract(annotation, 'public_annotation')
    if set(annotation['classification']['topics']) - {item['id'] for item in topics}:
        raise ValueError('public annotation uses an unregistered project topic')
