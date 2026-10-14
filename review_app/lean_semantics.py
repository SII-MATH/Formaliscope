"""Generate and consume compiler-backed Lean declaration evidence.

The exporter runs in the audited project's pinned Lake environment. A portable
index binds its declarations to that checkout, every local Lean source, the
dependency configuration and the exact exporter. Loading an index never guesses
declarations or visibility from text and does not need a Lean installation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from .repositories import source_files


SEMANTIC_INDEX_SCHEMA = 'formaliscope-lean-semantic-index.v1'
SUPPORTED_LEAN_VERSION = '4.32.2'
EXPORTER_PATH = Path(__file__).resolve().parent / 'lean' / 'ExportSemantic.lean'
_ENVIRONMENT_FILES = ('lean-toolchain', 'lakefile.lean', 'lakefile.toml', 'lake-manifest.json')
_DECLARATION_KINDS = {'def', 'theorem', 'lemma', 'structure', 'class', 'abbrev',
                      'inductive', 'coinductive', 'opaque', 'axiom', 'constant', 'instance'}


class SemanticIndexError(ValueError):
    """Semantic evidence is unavailable, stale or malformed."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _index_digest(value: dict) -> str:
    return _sha256(json.dumps({key: item for key, item in value.items() if key != 'digest'},
                             ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8'))


def _relative_path(value: object, *, label: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value or
            any(ord(char) < 32 or ord(char) == 127 for char in value) or
            PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or
            set(PurePosixPath(value).parts) & {'.git', '.lake'} or
            str(PurePosixPath(value)) != value):
        raise SemanticIndexError(f'{label} must be a confined, canonical relative source path')
    return value


def _selected_roots(repo: Path, roots: list[str] | None) -> list[str]:
    selected = roots if roots is not None else [name for name in ('KIP126', 'KIPBase')
                                               if (repo / name).is_dir()]
    if not isinstance(selected, list) or not selected:
        raise SemanticIndexError('semantic index requires at least one source root')
    return sorted({_relative_path(root, label='scan root') for root in selected})


def _file_hash(path: Path) -> str | None:
    return _sha256(path.read_bytes()) if path.is_file() else None


def _git_identity(repo: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo,
                                         text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=repo,
            text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        # Archives can be indexed, but cannot assert a commit or a clean tree.
        return 'unknown', True
    return commit, dirty


def _toolchain_version(value: str) -> str:
    match = re.fullmatch(r'leanprover/lean4:v([0-9]+\.[0-9]+\.[0-9]+)\s*', value) if isinstance(value, str) else None
    if not match or match[1] != SUPPORTED_LEAN_VERSION:
        raise SemanticIndexError(
            f'semantic exporter supports leanprover/lean4:v{SUPPORTED_LEAN_VERSION}; '
            'the repository must pin this exact toolchain in lean-toolchain')
    return match[1]


def _source_binding(repo: Path, roots: list[str] | None = None) -> dict:
    """Capture the complete local source and build-environment binding."""
    repo = repo.resolve()
    selected = _selected_roots(repo, roots)
    try:
        selected_files = source_files(repo, selected)
        local_files = source_files(repo, ['.'])
        for path in local_files:
            _relative_path(path.relative_to(repo).as_posix(), label='local source file')
        toolchain = (repo / 'lean-toolchain').read_bytes().decode('utf-8')
        _toolchain_version(toolchain)
        if not EXPORTER_PATH.is_file():
            raise SemanticIndexError(f'Lean semantic exporter is missing: {EXPORTER_PATH}')
        from .build import _dependency_lock_digest
        manifest_file = repo / 'lake-manifest.json'
        manifest = json.loads(manifest_file.read_bytes()) if manifest_file.is_file() else {}
        if (not isinstance(manifest, dict) or not isinstance(manifest.get('packages', []), list) or
                any(not isinstance(package, dict) for package in manifest.get('packages', []))):
            raise SemanticIndexError('Lake dependency lock must contain an object and a package list')
        commit, dirty = _git_identity(repo)
        return {
            'source_commit': commit, 'source_dirty': dirty,
            'lean_toolchain': toolchain, 'roots': selected,
            'source_environment': {name: _file_hash(repo / name) for name in _ENVIRONMENT_FILES},
            'dependency_lock_digest': _dependency_lock_digest(repo),
            'path_dependencies': any(package.get('type') == 'path'
                                     for package in manifest.get('packages', [])),
            'exporter_sha256': _sha256(EXPORTER_PATH.read_bytes()),
            'source_files': {path.relative_to(repo).as_posix(): _sha256(path.read_bytes())
                             for path in selected_files},
            'local_source_files': {path.relative_to(repo).as_posix(): _sha256(path.read_bytes())
                                   for path in local_files},
        }
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, SemanticIndexError):
            raise
        raise SemanticIndexError(f'cannot bind Lean semantic index to source checkout: {error}') from error


def _bind_index(raw: dict, binding: dict) -> dict:
    """Attach source evidence to freshly exported compiler JSON."""
    if (not isinstance(raw, dict) or raw.get('schema') != SEMANTIC_INDEX_SCHEMA or
            not isinstance(raw.get('modules'), list) or
            not isinstance(raw.get('context_modules'), list)):
        raise SemanticIndexError('Lean exporter returned an invalid raw semantic index')
    result = deepcopy(raw)
    result['provenance'] = deepcopy(binding)
    for group in ('modules', 'context_modules'):
        for module in result.get(group, []):
            file = module.get('file') if isinstance(module, dict) else None
            if file not in binding['local_source_files']:
                raise SemanticIndexError(f'Lean exporter returned an unknown local source: {file}')
            module['source_sha256'] = binding['local_source_files'][file]
    result['digest'] = _index_digest(result)
    return result


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise SemanticIndexError(f'semantic index {label} must be a non-empty string')
    return value


def _validate_imports(module: dict) -> None:
    imports = module.get('imports')
    if not isinstance(imports, list):
        raise SemanticIndexError('semantic index module imports must be a compiler-derived list')
    for name in imports:
        _nonempty_string(name, 'import name')
    if len(imports) != len(set(imports)):
        raise SemanticIndexError('semantic index contains duplicate module imports')


def _validate_index(repo: Path, index: dict, binding: dict) -> None:
    if not isinstance(index, dict) or index.get('schema') != SEMANTIC_INDEX_SCHEMA:
        raise SemanticIndexError('unsupported Lean semantic index schema')
    if index.get('lean_version') != _toolchain_version(binding['lean_toolchain']):
        raise SemanticIndexError('semantic index compiler version does not match the pinned Lean toolchain')
    if index.get('provenance') != binding:
        raise SemanticIndexError('Lean semantic index is stale: source, commit, toolchain, roots or exporter changed')
    if not isinstance(index.get('digest'), str) or index['digest'] != _index_digest(index):
        raise SemanticIndexError('Lean semantic index digest does not match its contents')
    modules, context = index.get('modules'), index.get('context_modules')
    if not isinstance(modules, list) or not isinstance(context, list):
        raise SemanticIndexError('semantic index requires module and context-module lists')
    paths, compiler_names, declarations = set(), set(), set()
    module_files: dict[str, str] = {}
    file_modules: dict[str, str] = {}
    graph: dict[str, list[str]] = {}
    for group, entries in (('modules', modules), ('context_modules', context)):
        group_paths = set()
        for module in entries:
            if not isinstance(module, dict):
                raise SemanticIndexError('semantic index module must be an object')
            file = _relative_path(module.get('file'), label='module file')
            if file in group_paths:
                raise SemanticIndexError(f'semantic index contains duplicate module file: {file}')
            group_paths.add(file)
            module_name = _nonempty_string(module.get('module'), 'module name')
            if file not in binding['local_source_files']:
                raise SemanticIndexError(f'semantic index module is outside the local source set: {file}')
            if module.get('source_sha256') != binding['local_source_files'][file]:
                raise SemanticIndexError(f'semantic index source digest does not match: {file}')
            if module_name in module_files and module_files[module_name] != file:
                raise SemanticIndexError(f'semantic index contains duplicate module name: {module_name}')
            if file in file_modules and file_modules[file] != module_name:
                raise SemanticIndexError(f'semantic index assigns multiple module names to {file}')
            module_files[module_name] = file
            file_modules[file] = module_name
            _validate_imports(module)
            if file in graph and graph[file] != module['imports']:
                raise SemanticIndexError(f'semantic index has conflicting imports for {file}')
            graph[file] = module['imports']
            if group == 'context_modules':
                continue
            paths.add(file)
            rows = module.get('declarations')
            if not isinstance(rows, list):
                raise SemanticIndexError('semantic index declarations must be a list')
            raw_source = (repo / file).read_bytes()
            for row in rows:
                if not isinstance(row, dict):
                    raise SemanticIndexError('semantic index declaration must be an object')
                name = _nonempty_string(row.get('name'), 'declaration name')
                compiler_name = _nonempty_string(row.get('compiler_name'), 'compiler declaration name')
                compiler_key = (file, compiler_name)
                if compiler_key in compiler_names or (file, name) in declarations:
                    raise SemanticIndexError(f'semantic index contains duplicate declaration: {name}')
                compiler_names.add(compiler_key)
                declarations.add((file, name))
                if row.get('kind') not in _DECLARATION_KINDS:
                    raise SemanticIndexError(f'unsupported semantic declaration kind: {row.get("kind")}')
                generated = row.get('name_generated', False)
                if type(generated) is not bool or generated and row['kind'] != 'instance':
                    raise SemanticIndexError('semantic generated names must identify anonymous instances')
                if row.get('visibility') not in ('public', 'private'):
                    raise SemanticIndexError(f'semantic declaration visibility is missing or invalid: {name}')
                first, last, line = row.get('start_byte'), row.get('end_byte'), row.get('line')
                if (type(first) is not int or type(last) is not int or type(line) is not int or
                        first < 0 or last <= first or last > len(raw_source) or
                        line != raw_source[:first].count(b'\n') + 1):
                    raise SemanticIndexError(f'semantic declaration has an invalid byte range or line: {name}')
                try:
                    raw_source[:first].decode('utf-8')
                    raw_source[first:last].decode('utf-8')
                    raw_source[last:].decode('utf-8')
                except UnicodeError as error:
                    raise SemanticIndexError(f'semantic declaration range splits a UTF-8 character: {name}') from error
                namespace = row.get('namespace')
                if not isinstance(namespace, list) or any(not isinstance(item, str) or not item
                                                          for item in namespace):
                    raise SemanticIndexError(f'semantic declaration namespace is invalid: {name}')
                if not isinstance(row.get('doc'), str):
                    raise SemanticIndexError(f'semantic declaration documentation is invalid: {name}')
                fields = row.get('fields')
                if not isinstance(fields, list) or any(not isinstance(field, dict) or
                        not isinstance(field.get('name'), str) or not field['name'] or
                        not isinstance(field.get('type'), str) for field in fields):
                    raise SemanticIndexError(f'semantic declaration fields are invalid: {name}')
    if paths != set(binding['source_files']):
        missing, extra = set(binding['source_files']) - paths, paths - set(binding['source_files'])
        raise SemanticIndexError(f'semantic index source coverage mismatch; missing={sorted(missing)}, '
                                 f'extra={sorted(extra)}')
    # Every compiler-reported import that resolves to local source must be in
    # the frozen graph; absence would make reuse silently ignore dependencies.
    local_by_name = {file[:-5].replace('/', '.'): file for file in binding['local_source_files']}
    for file, imports in graph.items():
        for name in imports:
            imported = local_by_name.get(name)
            if imported is not None and imported not in graph:
                raise SemanticIndexError(f'semantic index omits local imported module {name} from {file}')


def _run_lake(repo: Path, arguments: list[str]) -> str:
    command = ['lake', *arguments]
    try:
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', check=False)
    except OSError as error:
        raise SemanticIndexError('Lean semantic indexing requires Lake and the repository\'s pinned Lean '
                                 'toolchain; install them or provide a matching --semantic-index') from error
    if result.returncode:
        diagnostics = (result.stderr or result.stdout).strip()[-12000:]
        raise SemanticIndexError(f'Lean semantic indexing failed ({" ".join(command)}): {diagnostics}')
    return result.stdout


def export_semantic_index(repo: Path, *, roots: list[str] | None = None) -> dict:
    """Build fresh selected modules and export one portable semantic index."""
    repo = repo.resolve()
    before = _source_binding(repo, roots)
    version_output = _run_lake(repo, ['env', 'lean', '--version'])
    version = re.search(r'\bversion ([0-9]+\.[0-9]+\.[0-9]+)(?=[,\s)])', version_output)
    if not version or version[1] != _toolchain_version(before['lean_toolchain']):
        raise SemanticIndexError('active Lake Lean version does not match the repository lean-toolchain')
    _run_lake(repo, ['build'])
    targets = [file[:-5].replace('/', '.') for file in before['source_files']]
    for offset in range(0, len(targets), 100):
        _run_lake(repo, ['build', *targets[offset:offset + 100]])
    after_build = _source_binding(repo, before['roots'])
    if after_build != before:
        if (before['source_environment']['lake-manifest.json'] is None and
                after_build['source_environment']['lake-manifest.json'] is not None):
            raise SemanticIndexError('Lake created lake-manifest.json; review the dependency lock '
                                     'and rerun semantic export')
        raise SemanticIndexError('source checkout or dependency environment changed while Lake built it; retry')
    with tempfile.TemporaryDirectory(prefix='formaliscope-lean-index-') as temporary:
        file_list = Path(temporary) / 'sources.txt'
        file_list.write_text(''.join(file + '\n' for file in before['source_files']), encoding='utf-8')
        output = _run_lake(repo, ['env', 'lean', '--run', str(EXPORTER_PATH), str(repo), str(file_list)])
    try:
        raw = json.loads(output)
    except json.JSONDecodeError as error:
        raise SemanticIndexError('Lean exporter did not return valid semantic JSON') from error
    if not isinstance(raw, dict) or 'provenance' in raw or 'digest' in raw:
        raise SemanticIndexError('Lean exporter returned an invalid raw index object')
    final = _source_binding(repo, before['roots'])
    if final != before:
        raise SemanticIndexError('source checkout or dependency environment changed during Lean export; retry')
    result = _bind_index(raw, final)
    _validate_index(repo, result, final)
    return result


def read_semantic_index(repo: Path, *, roots: list[str] | None = None,
                        semantic_index: Path | dict | None = None) -> dict:
    """Validate an imported index, or generate one when no index was supplied."""
    repo = repo.resolve()
    if semantic_index is None:
        return export_semantic_index(repo, roots=roots)
    try:
        if isinstance(semantic_index, (Path, str)):
            index = json.loads(Path(semantic_index).read_text(encoding='utf-8'))
        elif isinstance(semantic_index, dict):
            index = deepcopy(semantic_index)
        else:
            raise SemanticIndexError('semantic index must be a JSON file or an index object')
        _validate_index(repo, index, _source_binding(repo, roots))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SemanticIndexError(f'cannot read Lean semantic index: {error}') from error
    return index


def semantic_index_metadata(index: dict) -> dict:
    """Snapshot provenance for the validated compiler evidence."""
    return {'schema': index['schema'], 'lean_version': index['lean_version'],
            'digest': index['digest'], 'provenance': deepcopy(index['provenance']),
            'module_count': len(index['modules']),
            'declaration_count': sum(len(module['declarations']) for module in index['modules']),
            'module_imports': semantic_imports(index),
            'declarations': [dict(file=module['file'], name_generated=row.get('name_generated', False), **{
                key: row[key] for key in ('name', 'compiler_name', 'kind', 'visibility',
                                          'start_byte', 'end_byte', 'line')})
                for module in index['modules'] for row in module['declarations']]}


def semantic_imports(index: dict) -> dict[str, list[str]]:
    return {module['file']: list(module['imports'])
            for module in [*index['modules'], *index['context_modules']]}


def semantic_context_modules(repo: Path, index: dict) -> dict[str, str]:
    selected = {module['file'] for module in index['modules']}
    return {module['file']: (repo / module['file']).read_bytes().decode('utf-8')
            for module in index['context_modules'] if module['file'] not in selected}


def validate_snapshot_semantics(snapshot: dict) -> None:
    """Check frozen semantic evidence without reopening the audited checkout.

    Historical snapshots have no semantic_index marker and keep their original
    validation contract. Compiler-backed snapshots must retain exact visibility,
    declaration ranges, import graphs and source/environment hashes.
    """
    metadata = snapshot.get('semantic_index')
    if metadata is None:
        return
    if not isinstance(metadata, dict) or metadata.get('schema') != SEMANTIC_INDEX_SCHEMA:
        raise SemanticIndexError('snapshot semantic index schema is invalid')
    binding = metadata.get('provenance')
    if (not isinstance(binding, dict) or metadata.get('lean_version') != SUPPORTED_LEAN_VERSION or
            _toolchain_version(binding.get('lean_toolchain', '')) != metadata['lean_version']):
        raise SemanticIndexError('snapshot semantic compiler provenance is invalid')
    for key in ('digest',):
        if not isinstance(metadata.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', metadata[key]):
            raise SemanticIndexError('snapshot semantic index digest is invalid')
    if (not isinstance(binding.get('exporter_sha256'), str) or
            not re.fullmatch(r'[0-9a-f]{64}', binding['exporter_sha256'])):
        raise SemanticIndexError('snapshot semantic exporter digest is invalid')
    if type(binding.get('source_dirty')) is not bool or not isinstance(binding.get('source_commit'), str):
        raise SemanticIndexError('snapshot semantic checkout provenance is invalid')
    if snapshot.get('repository') and snapshot['repository'].get('roots') != binding.get('roots'):
        raise SemanticIndexError('snapshot repository roots disagree with semantic provenance')
    if snapshot.get('source_origin') == 'git-checkout' and (
            snapshot.get('source_commit') != binding['source_commit'] or
            snapshot.get('source_dirty') != binding['source_dirty']):
        raise SemanticIndexError('snapshot source checkout disagrees with semantic provenance')
    modules, context = snapshot.get('modules'), snapshot.get('context_modules', {})
    hashes, local_hashes = binding.get('source_files'), binding.get('local_source_files')
    if (not isinstance(modules, dict) or not isinstance(context, dict) or set(modules) & set(context) or
            not isinstance(hashes, dict) or not isinstance(local_hashes, dict) or set(modules) != set(hashes) or
            type(metadata.get('module_count')) is not int or metadata['module_count'] != len(modules)):
        raise SemanticIndexError('snapshot frozen source coverage disagrees with semantic index')
    frozen = {**context, **modules}
    for file, text in frozen.items():
        _relative_path(file, label='snapshot module file')
        if not isinstance(text, str) or _sha256(text.encode('utf-8')) != local_hashes.get(file):
            raise SemanticIndexError(f'snapshot frozen source does not match semantic evidence: {file}')
        if file in hashes and hashes[file] != local_hashes[file]:
            raise SemanticIndexError(f'snapshot selected source does not match semantic evidence: {file}')
    imports = snapshot.get('module_imports')
    if (not isinstance(imports, dict) or imports != metadata.get('module_imports') or
            set(imports) != set(frozen)):
        raise SemanticIndexError('snapshot compiler import graph is incomplete or changed')
    for names in imports.values():
        _validate_imports({'imports': names})
    local_by_name = {file[:-5].replace('/', '.'): file for file in local_hashes}
    for names in imports.values():
        for name in names:
            local_file = local_by_name.get(name)
            if local_file is not None and local_file not in frozen:
                raise SemanticIndexError('snapshot omits a compiler-reported local imported module')
    environment = binding.get('source_environment')
    if not isinstance(environment, dict):
        raise SemanticIndexError('snapshot semantic build environment is invalid')
    if environment.get('lean-toolchain') != _sha256(binding['lean_toolchain'].encode('utf-8')):
        raise SemanticIndexError('snapshot semantic toolchain digest is invalid')
    for name in ('lean-toolchain', 'lakefile.lean', 'lakefile.toml'):
        if snapshot.get('source_environment', {}).get(name) != environment.get(name):
            raise SemanticIndexError('snapshot build environment disagrees with semantic provenance')
    if snapshot.get('dependency_lock_digest') != binding.get('dependency_lock_digest'):
        raise SemanticIndexError('snapshot dependency lock disagrees with semantic provenance')
    if (type(binding.get('path_dependencies')) is not bool or
            snapshot.get('source_environment', {}).get('path_dependencies', False) != binding['path_dependencies']):
        raise SemanticIndexError('snapshot path dependency state disagrees with semantic provenance')
    rows = metadata.get('declarations')
    if (not isinstance(rows, list) or type(metadata.get('declaration_count')) is not int or
            metadata['declaration_count'] != len(rows)):
        raise SemanticIndexError('snapshot semantic declaration coverage is invalid')
    by_compiler = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SemanticIndexError('snapshot semantic declaration must be an object')
        compiler_name = _nonempty_string(row.get('compiler_name'), 'compiler name')
        file = _relative_path(row.get('file'), label='snapshot declaration file')
        generated = row.get('name_generated', False)
        if type(generated) is not bool or generated and row.get('kind') != 'instance':
            raise SemanticIndexError('snapshot semantic generated names must identify anonymous instances')
        compiler_key = (file, compiler_name)
        if compiler_key in by_compiler:
            raise SemanticIndexError('snapshot semantic compiler names are duplicated')
        by_compiler[compiler_key] = row
    seen = set()
    for card in snapshot.get('cards', []):
        compiler_name = card.get('compiler_name')
        file = card.get('module_file')
        generated = card.get('name_generated', False)
        if not isinstance(compiler_name, str) or not isinstance(file, str):
            raise SemanticIndexError('snapshot card compiler name is missing or invalid')
        if type(generated) is not bool:
            raise SemanticIndexError('snapshot card generated name flag is invalid')
        compiler_key = (file, compiler_name)
        row = by_compiler.get(compiler_key)
        if row is None or compiler_key in seen:
            raise SemanticIndexError('snapshot card is missing or duplicates its compiler declaration')
        seen.add(compiler_key)
        lean = card.get('lean') or {}
        if (row.get('file') not in modules or lean.get('file') != row['file'] or
                card.get('module_file') != row['file'] or lean.get('line') != row.get('line') or
                card.get('declaration') != row.get('name') or card.get('kind') != row.get('kind') or
                row.get('kind') not in _DECLARATION_KINDS or
                generated != row.get('name_generated', False) or
                card.get('visibility_origin') != 'lean-compiler' or
                row.get('visibility') not in ('private', 'public') or
                card.get('visibility') != row['visibility'] or
                bool(card.get('private')) != (row['visibility'] == 'private')):
            raise SemanticIndexError('snapshot card disagrees with compiler declaration metadata')
        raw_source = modules[row['file']].encode('utf-8')
        first, last, line = row.get('start_byte'), row.get('end_byte'), row.get('line')
        if (type(first) is not int or type(last) is not int or type(line) is not int or
                first < 0 or last <= first or last > len(raw_source) or
                raw_source[:first].count(b'\n') + 1 != line):
            raise SemanticIndexError('snapshot compiler declaration range is invalid')
        try:
            source = raw_source[first:last].decode('utf-8').rstrip()
            raw_source[:first].decode('utf-8')
            raw_source[last:].decode('utf-8')
        except UnicodeError as error:
            raise SemanticIndexError('snapshot compiler declaration range splits UTF-8') from error
        if lean.get('source') != source:
            raise SemanticIndexError('snapshot declaration source differs from compiler byte range')
    if seen != set(by_compiler):
        raise SemanticIndexError('snapshot omits a compiler declaration')


def source_declarations(repo: Path, *, roots: list[str] | None = None,
                        semantic_index: Path | dict | None = None) -> list[dict]:
    """Return existing Statement rows using compiler names, ranges and visibility."""
    repo = repo.resolve()
    index = read_semantic_index(repo, roots=roots, semantic_index=semantic_index)
    records = []
    for module in index['modules']:
        raw_source = (repo / module['file']).read_bytes()
        full_source = raw_source.decode('utf-8')
        for row in module['declarations']:
            first, last = row['start_byte'], row['end_byte']
            records.append({
                'name': row['name'], 'compiler_name': row['compiler_name'], 'kind': row['kind'],
                'name_generated': row.get('name_generated', False),
                'namespace': tuple(row['namespace']), 'private': row['visibility'] == 'private',
                'file': module['file'], 'line': row['line'],
                'source': raw_source[first:last].decode('utf-8').rstrip(),
                'fields': [(field['name'], field['type']) for field in row['fields']],
                'doc': row['doc'][:5000],
                'context': raw_source[:first].decode('utf-8').rstrip('\n'),
                'full_source': full_source,
            })
    return records
