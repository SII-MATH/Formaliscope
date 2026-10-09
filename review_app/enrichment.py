"""Validate source-bound Agent drafts and build a candidate evidence snapshot.

This module has no database, HTTP or model dependency. Its small validator
implements only the keywords used by the committed enrichment contract, not
arbitrary JSON Schema documents. Personal judgments never enter this format.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from .build import (CURRENT_FINGERPRINT_SCHEME, _content_fingerprint,
                    calculate_snapshot_digest, compare_snapshots, validate_snapshot)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'statement_workflow/schema/statement-enrichment.v1.schema.json'


def _validate(value, rule, root, path='$'):
    if '$ref' in rule:
        _validate(value, root['$defs'][rule['$ref'].removeprefix('#/$defs/')], root, path)
        return
    if 'const' in rule and value != rule['const']:
        raise ValueError(f'{path}: unexpected constant')
    if 'enum' in rule and value not in rule['enum']:
        raise ValueError(f'{path}: unsupported value')
    if 'anyOf' in rule:
        for alternative in rule['anyOf']:
            try:
                _validate(value, alternative, root, path)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f'{path}: no allowed type matches')
    types = rule.get('type', [])
    types = [types] if isinstance(types, str) else types
    checks = {'object': isinstance(value, dict), 'array': isinstance(value, list),
              'string': isinstance(value, str), 'integer': type(value) is int,
              'number': type(value) in (int, float),
              'null': value is None}
    if types and not any(checks.get(kind, False) for kind in types):
        raise ValueError(f'{path}: expected {types}')
    if isinstance(value, dict):
        properties = rule.get('properties', {})
        if any(key not in value for key in rule.get('required', [])):
            raise ValueError(f'{path}: missing required field')
        if rule.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError(f'{path}: unexpected field')
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], root, path+'.'+key)
            elif isinstance(rule.get('additionalProperties'), dict):
                _validate(item, rule['additionalProperties'], root, path+'.'+key)
    if isinstance(value, list):
        if len(value) < rule.get('minItems', 0) or len(value) > rule.get('maxItems', float('inf')):
            raise ValueError(f'{path}: invalid item count')
        if rule.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise ValueError(f'{path}: duplicate items')
        if 'items' in rule:
            for index, item in enumerate(value):
                _validate(item, rule['items'], root, f'{path}[{index}]')
    if isinstance(value, str):
        if len(value.strip()) < rule.get('minLength', 0):
            raise ValueError(f'{path}: empty text')
        if len(value) > rule.get('maxLength', float('inf')):
            raise ValueError(f'{path}: text too long')
        if 'pattern' in rule and re.search(rule['pattern'], value) is None:
            raise ValueError(f'{path}: invalid format')
        if rule.get('format') == 'date-time':
            try:
                timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if timestamp.tzinfo is None:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError(f'{path}: timestamp needs a timezone') from exc
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise ValueError(f'{path}: value must be finite')
        if value < rule.get('minimum', float('-inf')):
            raise ValueError(f'{path}: value below minimum')
        if value > rule.get('maximum', float('inf')):
            raise ValueError(f'{path}: value above maximum')
    for part in rule.get('allOf', []):
        _validate(value, part, root, path)
    if 'if' in rule:
        try:
            _validate(value, rule['if'], root, path)
            branch = 'then'
        except ValueError:
            branch = 'else'
        if branch in rule:
            _validate(value, rule[branch], root, path)


def validate_enrichment(document: dict, snapshot: dict) -> list[dict]:
    """Check the contract plus identity, version and actual source evidence."""
    if not isinstance(document, dict):
        raise ValueError('enrichment must be an object')
    validate_snapshot(snapshot)
    if snapshot.get('review_mode') != 'statement':
        raise ValueError('enrichment requires a Statement snapshot')
    if document.get('schema') == 'statement-enrichment.v2':
        from .enrichment_v2 import validate_document
        return validate_document(document, snapshot)
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    _validate(document, schema, schema)
    cards = {card['id']: card for card in snapshot['cards']}
    seen = set()
    for annotation in document['annotations']:
        identity = annotation['declaration_id']
        if identity in seen or identity not in cards:
            raise ValueError('annotation declaration must be known and unique')
        seen.add(identity)
        basis = annotation['basis']
        source = cards[identity]['lean']['source']
        if (basis['source_commit'] != snapshot['source_commit'] or
                basis['snapshot_digest'] != snapshot['digest'] or
                basis['source_sha256'] != hashlib.sha256(source.encode()).hexdigest()):
            raise ValueError('annotation basis does not match this frozen snapshot')
        if basis['context_fingerprint'] is not None:
            raise ValueError('manual importer requires null context_fingerprint; a typed context adapter is not connected')
        if annotation['provenance']['context_completeness'] == 'complete':
            raise ValueError('manual source candidates cannot claim verified complete semantic context')
        evidence = {}
        for item in annotation['evidence']:
            if item['id'] in evidence:
                raise ValueError('evidence IDs must be unique within an annotation')
            module = snapshot.get('modules', {}).get(item['file'])
            if not isinstance(module, str):
                raise ValueError('evidence file is not in the frozen snapshot')
            lines = module.splitlines()
            start, end = item['line_start'], item['line_end']
            if start > end or end > len(lines) or item['excerpt'].rstrip('\n') != '\n'.join(lines[start-1:end]):
                raise ValueError('evidence range or excerpt does not match actual source')
            evidence[item['id']] = item
        def check_refs(value):
            if isinstance(value, dict):
                if 'evidence_ids' in value and any(key not in evidence for key in value['evidence_ids']):
                    raise ValueError('annotation refers to unknown evidence')
                for item in value.values():
                    check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)
        check_refs(annotation)
        classification = annotation['classification']
        if (classification['role'] != 'unclassified' or classification['topics']) and (
                not classification['rationale_zh'] or not classification['evidence_ids']):
            raise ValueError('a candidate classification needs a reason and source evidence')
        if annotation['readback']['status'] == 'draft' and not annotation['readback']['evidence_ids']:
            raise ValueError('a readback draft needs source evidence')
    return document['annotations']


def enrich_snapshot(snapshot: dict, document: dict) -> dict:
    """Return a new artifact; input snapshot and personal review DB are untouched."""
    annotations = validate_enrichment(document, snapshot)
    result = deepcopy(snapshot)
    cards = {card['id']: card for card in result['cards']}
    for annotation in annotations:
        card = cards[annotation['declaration_id']]
        card.pop('reuse_source_commit', None)
        from .blueprint import card_references
        references = card_references(card)
        if references:
            card['blueprint_references'] = references
        if document['schema'] == 'statement-enrichment.v2':
            from .enrichment_v2 import public_annotation
            card['enrichment'] = public_annotation(annotation)
            card.pop('reading_summary_zh', None)
        else:
            card['enrichment'] = deepcopy(annotation)
            card['reading_summary_zh'] = annotation['summary_zh']
        card['title_zh'] = annotation['title_zh']
        text = annotation['readback']['text_zh']
        if (document['schema'] == 'statement-enrichment.v2' and text is not None or
                document['schema'] == 'statement-enrichment.v1' and annotation['readback']['status'] == 'draft'):
            card['statement'] = annotation['readback']['text_zh']
            card['statement_origin'] = 'backtranslation'
            card['blueprint_file'] = card['lean']['file']
            card['blueprint_line'] = card['lean']['line']
        # Display aliases/labels do not rewrite title or review basis. A changed
        # mathematical statement does, without preserving old matching aliases.
        nl, lean, fingerprint = _content_fingerprint(card, result.get('dependency_lock_digest'))
        card.update(nl_digest=nl, lean_digest=lean, fingerprint=fingerprint,
                    fingerprint_scheme=CURRENT_FINGERPRINT_SCHEME,
                    fingerprints={CURRENT_FINGERPRINT_SCHEME: fingerprint})
    if document['schema'] == 'statement-enrichment.v2':
        result['enrichment_topics'] = deepcopy(document['run']['topics'])
    result['generated_at'] = datetime.now(timezone.utc).isoformat()
    result['digest'] = calculate_snapshot_digest(result)
    result['comparison'] = compare_snapshots(snapshot, result)
    validate_snapshot(result)
    return result
