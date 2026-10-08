"""Resolve project-relative task configuration over shared defaults."""
from copy import deepcopy
import json
from pathlib import Path
from review_app.local_paths import local_root


REPO = Path(__file__).resolve().parents[2]
DEFAULT = REPO / 'skills/default-config.json'


def project_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a non-empty path')
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def _read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate configuration key: {key}')
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f'non-finite configuration value: {value}')

    with Path(path).open(encoding='utf-8') as stream:
        document = json.load(stream, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(document, dict):
        raise ValueError('configuration must be an object')
    return document


def _merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        if key not in base:
            raise ValueError(f'unknown configuration field: {key}')
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f'{key} must be an object')
            result[key] = _merge(base[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path):
    """Objects merge recursively; lists and explicit null replace defaults."""
    base = _read(DEFAULT)

    def resolve(current, seen):
        current = Path(current).resolve()
        if current in seen:
            raise ValueError('configuration defaults contain a cycle')
        document = _read(current)
        parent = document.pop('defaults', None)
        inherited = (resolve(project_path(parent, 'defaults'), seen | {current})
                     if parent is not None else base)
        return _merge(inherited, document)

    result = resolve(project_path(str(path), 'config'), set())
    if result['schema'] != base['schema']:
        raise ValueError('unsupported configuration schema')
    for key in ('directories', 'files', 'declaration_ids'):
        values = result['selection'][key]
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError(f'selection.{key} must be a list of non-empty strings')
    for key in ('results', 'readback_results', 'reviews'):
        values = result['collection'][key]
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError(f'collection.{key} must be a list of paths')
    return result


def task_output(config_path, config):
    return project_path(config['output'], 'output') if config['output'] is not None else (
        local_root(REPO) / 'tasks' / 'batches' / Path(config_path).stem)


def snapshot_pool(output):
    """Project tasks share one cache; external task dirs stay self-contained."""
    root = local_root(REPO)
    return root / 'cache' / 'snapshots' if Path(output).resolve().is_relative_to(root.resolve()) else None
