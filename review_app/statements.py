"""Build a source-backed Statement browsing snapshot (no Lean execution).

The graph is a source-reference candidate graph, not an elaborator dependency
graph. Blueprint prose stays attributed to Blueprint. Generated summaries are
reading aids, never represented as verified semantic back-translations.
"""
from __future__ import annotations

import re
import hashlib
import json
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from functools import lru_cache

from .build import (SNAPSHOT_SCHEMA, CURRENT_FINGERPRINT_SCHEME, _digest,
                    _content_fingerprint, _dependency_lock_digest, _clean_statement,
                    INPUT_RE, NODE_RE, LABEL_RE, lean_references, CHAPTER_RE,
                    calculate_snapshot_digest, _git_head, _git_dirty)

DECL = re.compile(r"^\s*(?:@\[[^]]*\]\s*)*(?:(?:private|public|protected|noncomputable|partial|unsafe|nonrec|meta)\s+)*"
                  r"(def|theorem|lemma|structure|class|abbrev|inductive|opaque|axiom|constant|instance)"
                  r"\s+([\w'.«»₀-₉]+)(?=\s|\{|\(|:|$)", re.M)
SCOPE = re.compile(r"^\s*(?:@\[[^]]*\]\s*)*(?:(?:public|noncomputable|meta)\s+)*(section|namespace)(?:\s|$)")
TOKEN = re.compile(r"[\w'₀-₉]+(?:\.[\w'₀-₉]+)*")


def mask_comments(text: str) -> str:
    """Preserve offsets while masking nested block comments, strings and line comments."""
    return _mask_source(text)[0]


def _mask_source(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Mask source and retain complete top-level block and line comment spans."""
    result = list(text)
    comments = []
    comment_start = 0
    i, depth, string = 0, 0, False
    while i < len(text):
        if depth:
            if text.startswith('/-', i):
                depth += 1
                result[i:i+2] = '  '
                i += 2
                continue
            if text.startswith('-/', i):
                depth -= 1
                if not depth:
                    comments.append((comment_start, i + 2))
                result[i:i+2] = '  '
                i += 2
                continue
        elif string:
            if text[i] == '\\' and i + 1 < len(text):
                result[i:i+2] = '  '
                i += 2
                continue
            if text[i] == '"':
                string = False
        elif text.startswith('/-', i):
            depth = 1
            comment_start = i
            result[i:i+2] = '  '
            i += 2
            continue
        elif text.startswith('--', i):
            end = text.find('\n', i)
            if end == -1:
                end = len(text)
            comments.append((i, end))
            result[i:end] = ' ' * (end-i)
            i = end
            continue
        elif text[i] == '"':
            string = True
        else:
            i += 1
            continue
        if text[i] != '\n':
            result[i] = ' '
        i += 1
    return ''.join(result), comments


def _ordinary_comment_gap(text: str, start: int, end: int,
                          comments: list[tuple[int, int]], comment_ends: list[int]) -> bool:
    """Check original source so strings and documentation stay syntax boundaries."""
    position = start
    for index in range(bisect_right(comment_ends, start), len(comments)):
        left, right = comments[index]
        if left >= end:
            break
        if right > end or text[position:left].strip() or text.startswith(('/--', '/-!'), left):
            return False
        position = right
    return not text[position:end].strip()


def _declaration_headers(masked_text: str) -> dict:
    """Locate supported headers and track name visibility in section scopes."""
    offsets = [0]
    for match in re.finditer('\n', masked_text):
        offsets.append(match.end())
    headers = {}
    for match in DECL.finditer(masked_text):
        start = match.start() + len(match[0]) - len(match[0].lstrip())
        headers[bisect_right(offsets, start) - 1] = match
    # Ordinary Lean files export declarations by default; `module` opts into
    # private defaults. An explicit public section overrides that default.
    private = bool(re.match(r'^\s*module\b', masked_text))
    scopes, result = [], {}
    for number, line in enumerate(masked_text.splitlines()):
        scope = SCOPE.match(line)
        if scope:
            scopes.append(private)
            modifiers = re.sub(r'@\[[^]]*\]', '', line[:scope.start(1)])
            if re.search(r'\bpublic\b', modifiers):
                private = False
        elif re.match(r'^\s*end(?:\s|$)', line):
            if scopes:
                private = scopes.pop()
        if number in headers:
            match = headers[number]
            modifiers = re.sub(r'@\[[^]]*\]', '', masked_text[match.start():match.start(1)])
            explicit = re.search(r'\b(private|public)\b', modifiers)
            result[number] = (match, explicit[1] == 'private' if explicit else private)
    return result


@lru_cache(maxsize=4096)
def _module_visibility(source: str) -> dict[int, str]:
    masked = mask_comments(source)
    result = {}
    position, last = 0, 0
    for first, (match, private) in _declaration_headers(masked).items():
        last += masked.count('\n', position, match.start(1))
        position = match.start(1)
        for line in range(first + 1, last + 2):
            result[line] = 'private' if private else 'public'
    return result


def review_evidence(card: dict, snapshot: dict) -> dict:
    """Add display metadata on recall without rewriting installed evidence."""
    if snapshot.get('review_mode') != 'statement':
        return card
    visibility = card.get('visibility')
    if visibility not in ('private', 'public'):
        if card.get('private') is True:
            return {**card, 'visibility': 'private'}
        lean = card.get('lean') or {}
        module = snapshot.get('modules', {}).get(card.get('module_file') or lean.get('file'))
        visibility = _module_visibility(module).get(lean.get('line')) if module else None
        if visibility is None:
            flags = _module_visibility(lean.get('source') or '')
            visibility = next(iter(flags.values()), 'public')
    return {**card, 'visibility': visibility}


def source_declarations(repo: Path, *, roots: list[str] | None = None) -> list[dict]:
    repo = repo.resolve()
    records = []
    from .repositories import source_files
    selected_roots = roots if roots is not None else [name for name in ('KIP126', 'KIPBase') if (repo / name).is_dir()]
    for path in source_files(repo, selected_roots):
        text = path.read_text(encoding='utf-8')
        masked_text, comments = _mask_source(text)
        headers = _declaration_headers(masked_text)
        original, masked = text.splitlines(), masked_text.splitlines()
        comment_ends = [end for _, end in comments]
        documentation = [(start, end) for start, end in comments
                         if text.startswith(('/--', '/-!'), start)]
        documentation_ends = [end for _, end in documentation]
        previous_documentation = -1
        line_offsets = []
        position = 0
        for line in text.splitlines(keepends=True):
            line_offsets.append(position)
            position += len(line)
        namespace, scopes, found = [], [], []
        for number, line in enumerate(masked):
            ns = re.match(r'^\s*namespace\s+([\w.]+)', line)
            if ns:
                namespace.extend(ns[1].split('.'))
                scopes.append(ns[1].split('.'))
                continue
            if SCOPE.match(line):
                scopes.append([])
                continue
            if re.match(r'^\s*end(?:\s|$)', line):
                if scopes:
                    count = len(scopes.pop())
                    if count:
                        del namespace[-count:]
                continue
            if number not in headers:
                continue
            match, private = headers[number]
            kind, name = match.groups()
            # Lean's _root_ prefix explicitly escapes the current namespace.
            fqn = name.removeprefix('_root_.') if name.startswith('_root_.') else '.'.join([*namespace, name])
            # A section heading (/-!) is a separate command, not declaration
            # documentation. Ordinary comments are whitespace; real code,
            # strings and other documentation remain attachment boundaries.
            declaration_start = line_offsets[number] + len(line) - len(line.lstrip())
            comment_index = bisect_right(documentation_ends, declaration_start) - 1
            doc = ''
            if comment_index >= 0 and comment_index != previous_documentation:
                # Only the first following declaration can consume a doc;
                # avoid repeatedly scanning growing gaps for later declarations.
                previous_documentation = comment_index
                start, end = documentation[comment_index]
                if text.startswith('/--', start) and _ordinary_comment_gap(
                        text, end, declaration_start, comments, comment_ends):
                    doc = text[start+3:end-2].strip()
            found.append((fqn, kind, number, tuple(namespace), doc, private))
        for offset, (name, kind, first, ns, doc, private) in enumerate(found):
            last = found[offset+1][2] if offset+1 < len(found) else len(original)
            # Do not include following namespace/section ends or the next docstring.
            for stop in range(first+1, last):
                if SCOPE.match(masked[stop]) or re.match(r'^\s*end(?:\s|$)', masked[stop]) or re.match(
                    r'^(?:attribute|open|variable|universe|set_option|export|#check|#eval|#print|syntax|macro|elab|initialize)(?:\s|$)', masked[stop]):
                    last = stop
                    break
            while last > first+1 and not masked[last-1].strip():
                last -= 1
            source = '\n'.join(original[first:last]).rstrip()
            fields = []
            if kind in ('structure', 'class'):
                field_re = re.compile(r'^([ \t]{2,})([\w\'₀-₉]+)\s*(?:\([^\n]*?\)\s*)?:', re.M)
                found_fields = list(field_re.finditer(source))
                for field_index, field in enumerate(found_fields):
                    end = found_fields[field_index+1].start() if field_index+1 < len(found_fields) else len(source)
                    fields.append((field[2], source[field.end():end].strip()))
            records.append({'name': name, 'kind': kind, 'namespace': ns,
                            'private': private,
                            'file': str(path.relative_to(repo)), 'line': first+1,
                            'source': source, 'fields': fields, 'doc': doc[:5000],
                            'context': '\n'.join(original[:first]),
                            'full_source': text})
    return records


def blueprint_references(repo: Path) -> dict[str, list[dict]]:
    result = {}
    content = repo / 'blueprint/src/content.tex'
    if not content.is_file():
        return result
    for value in INPUT_RE.findall(content.read_text(encoding='utf-8')):
        path = content.parent / (value + '.tex')
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        chapter = CHAPTER_RE.search(text)
        for match in NODE_RE.finditer(text):
            kind, title, body = match.groups()
            label = LABEL_RE.search(body)
            names = list(dict.fromkeys(lean_references(body)))
            statement = _clean_statement(body)
            if not statement:
                continue
            for name in names:
                result.setdefault(name, []).append({
                    'statement': statement, 'title': title or name,
                    'label': label[1] if label else name,
                    'chapter': chapter[1] if chapter else path.stem,
                    'blueprint_file': str(path.relative_to(repo)),
                    'blueprint_line': text.count('\n', 0, match.start())+1,
                    'declarations': names,
                })
    return result


def blueprint_prose(repo: Path) -> dict[str, dict]:
    return {name: references[0] for name, references in blueprint_references(repo).items()}


def compile_statements(repo: Path, *, source_commit: str | None = None, annotations: Path | None = None,
                       repository: dict | None = None) -> dict:
    repo = repo.resolve()
    from .repositories import repository_config
    repository = repository_config(repository) if repository is not None else None
    records = source_declarations(repo, roots=repository['roots'] if repository else None)
    index = {(row['name'] + '::file=' + row['file'] if repository and row['private'] else row['name']): row
             for row in records}
    if repository and len(index) != len(records):
        raise ValueError('ambiguous declaration names in configured roots; narrow roots or disambiguate source namespaces')
    if repository and not records:
        raise ValueError('configured roots contain no supported Lean declarations')
    prefix = 'statement::' + (repository['id'] + '::' if repository else '')
    short = defaultdict(list)
    full_names = defaultdict(list)
    for name in index:
        full_names[index[name]['name']].append(name)
        short[index[name]['name'].rsplit('.', 1)[-1]].append(name)
    references = blueprint_references(repo)
    translated = json.loads(annotations.read_text(encoding='utf-8')) if annotations else {}
    if not isinstance(translated, dict):
        raise ValueError('annotations must map declaration names to source-bound reading drafts')
    for name, annotation in translated.items():
        if name not in index or annotation.get('source_sha256') != hashlib.sha256(index[name]['source'].encode()).hexdigest():
            raise ValueError(f'annotation source has changed or disappeared: {name}')
        if not isinstance(annotation.get('statement'), str) or not annotation['statement'].strip():
            raise ValueError(f'annotation statement is missing: {name}')
    dependencies = {}
    def resolve(token, row):
        def visible(keys):
            local = [key for key in keys if index[key]['private'] and index[key]['file'] == row['file']]
            candidates = local or [key for key in keys if not index[key]['private']]
            return candidates[0] if len(candidates) == 1 else None
        ns = row['namespace']
        for count in range(len(ns), -1, -1):
            candidate = '.'.join([*ns[:count], token])
            result = visible(full_names.get(candidate, []))
            if result:
                return result
        choices = short.get(token, [])
        return visible(choices)
    for name, row in index.items():
        refs = set()
        for token in TOKEN.findall(mask_comments(row['source'])):
            candidate = resolve(token, row)
            if candidate and candidate != name:
                refs.add(candidate)
        dependencies[name] = sorted(refs)
    roots = [name for name, row in index.items()
             if row['kind'] in ('theorem', 'lemma') and
             (row['file'].startswith(('KIP126/Main/Solution/Endpoint', 'KIP126/Main/Solution/Final/')) or
              name.rsplit('.', 1)[-1] in ('main', 'mainTheorem', 'kervaireInvariantOne'))]
    if not roots:
        roots = [name for name, row in index.items() if row['file'] == 'KIP126/Main.lean']
    if repository:
        roots = repository['main_targets']
        if set(roots) - index.keys():
            raise ValueError('configured main_targets are missing from the selected source roots')
    direct = {dep for name in roots for dep in dependencies[name]}
    cards = []
    for name, row in index.items():
        declaration = row['name']
        blueprint = references.get(declaration, [])
        bp = blueprint[0] if blueprint else None
        annotation = translated.get(name)
        title = annotation.get('title', declaration) if annotation else bp['title'] if bp else declaration.rsplit('.', 1)[-1]
        role = ('主定理' if name in roots else '直接依赖' if name in direct else
                '开发期假设' if row['kind'] == 'axiom' else
                '文献输入' if '/Main/Axiom/Literature/' in row['file'] else
                '计算输入' if '/Axiom/LinProgram/' in row['file'] else
                '解码协议' if '/Interpretation/' in row['file'] else
                '基础设施' if row['file'].startswith('KIPBase/') else
                '接口对象' if row['kind'] in ('structure', 'class') or 'Challenge' in row['file'] else '数学对象')
        priority = {'主定理': 100, '直接依赖': 95, '开发期假设': 90, '文献输入': 80, '计算输入': 82, '解码协议': 85,
                    '接口对象': 75, '数学对象': 50, '基础设施': 40}[role]
        risky = row['kind'] == 'axiom' or '/Axiom/' in row['file'] or '/Interpretation/' in row['file']
        summary = f"{declaration} 是一个 {'定理' if row['kind'] in ('theorem','lemma') else '结构' if row['fields'] else 'Lean '+row['kind']+' 声明'}。"
        if row['fields']:
            summary += '它包含以下字段：' + '、'.join(field[0] for field in row['fields']) + '。请逐项核对字段的条件与数学含义。'
        elif row['doc']:
            summary += '\n\n'+row['doc']
        else:
            summary += '请结合下方完整源码及引用对象核对陈述、参数和前提。'
        card = {
            'id': prefix+name, 'declaration': declaration, 'label': bp['label'] if bp else declaration,
            'title': title, 'kind': row['kind'], 'chapter': bp['chapter'] if bp else row['file'].split('/')[0],
            'statement': annotation['statement'] if annotation else bp['statement'] if bp else summary,
            'statement_origin': 'backtranslation' if annotation else 'blueprint' if bp else 'reading-summary',
            'blueprint_file': bp['blueprint_file'] if bp else row['file'],
            'blueprint_line': bp['blueprint_line'] if bp else row['line'],
            'reading_summary': summary,
            'lean': {'file': row['file'], 'line': row['line'], 'source': row['source'], 'truncated': False},
            'source_status': 'local', 'dependencies': [prefix+dep for dep in dependencies[name]],
            'dependency_origin': 'source-reference-candidates',
            'fields': [{'name': field[0], 'type': field[1]} for field in row['fields']],
            'role': role, 'priority': priority, 'risk': 'high' if risky else 'medium' if row['fields'] else 'normal',
            'main_target': name in roots,
            'review_contract': 'statement-front-end.v1',
            'module_file': row['file'],
            'annotation_source_sha256': annotation['source_sha256'] if annotation else None,
            'visibility': 'private' if row['private'] else 'public',
        }
        if blueprint:
            card['blueprint_references'] = blueprint
        if row['private']:
            card['private'] = True
        nl, lean, fingerprint = _content_fingerprint(card, None)
        card.update(nl_digest=nl, lean_digest=lean, fingerprint=fingerprint,
                    fingerprint_scheme=CURRENT_FINGERPRINT_SCHEME,
                    fingerprints={CURRENT_FINGERPRINT_SCHEME: fingerprint})
        cards.append(card)
    cards.sort(key=lambda card: (-card['priority'], card['declaration']))
    snapshot = {'schema': SNAPSHOT_SCHEMA, 'fingerprint_scheme': CURRENT_FINGERPRINT_SCHEME,
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'source_commit': source_commit or _git_head(repo),
                'source_dirty': False if source_commit else _git_dirty(repo),
                'source_origin': 'archive-unverified' if source_commit else 'git-checkout',
                'dependency_lock_digest': _dependency_lock_digest(repo),
                'unlinked_nodes': 0, 'cards': cards, 'review_mode': 'statement',
                'review_contract': 'statement-front-end.v1',
                'modules': {row['file']: row['full_source'] for row in records}}
    from .repositories import source_files
    scan_roots = repository['roots'] if repository else [name for name in ('KIP126', 'KIPBase') if (repo / name).is_dir()]
    snapshot['modules'] = {str(path.relative_to(repo)): path.read_text(encoding='utf-8')
                           for path in source_files(repo, scan_roots)}
    snapshot['source_environment'] = {
        name: hashlib.sha256((repo / name).read_bytes()).hexdigest() if (repo / name).is_file() else None
        for name in ('lean-toolchain', 'lakefile.lean', 'lakefile.toml')}
    manifest_path = repo / 'lake-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
    snapshot['source_environment']['path_dependencies'] = any(
        item.get('type') == 'path' for item in manifest.get('packages', []))
    from .reuse import freeze_imports
    snapshot['context_modules'] = freeze_imports(repo, snapshot['modules'])
    if repository:
        snapshot['repository'] = repository
        snapshot['enrichment_topics'] = repository['topics']
    snapshot['digest'] = calculate_snapshot_digest(snapshot)
    return snapshot
