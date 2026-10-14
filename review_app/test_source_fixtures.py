"""Historical source fixtures for downstream snapshot tests only.

This reproduces the former source-only builder's test data, including incomplete
Lean snippets used by auth, storage and enrichment tests. Production builds must
consume a compiler semantic index; exporter regressions exercise actual Lean.
"""
from __future__ import annotations
import re
from bisect import bisect_right
from pathlib import Path

DECL = re.compile(r"^\s*(?:@\[[^]]*\]\s*)*(?:(?:private|public|protected|noncomputable|partial|unsafe|nonrec|meta)\s+)*"
                  r"(def|theorem|lemma|structure|class|abbrev|inductive|opaque|axiom|constant|instance)"
                  r"\s+([\w'.«»₀-₉]+)(?=\s|\{|\(|:|$)", re.M)


SCOPE = re.compile(r"^\s*(?:@\[[^]]*\]\s*)*(?:(?:public|noncomputable|meta)\s+)*(section|namespace)(?:\s|$)")


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



def compile_statements(repo: Path, *, source_commit=None, annotations=None, repository=None):
    """Build historical fixture payloads for non-compiler application tests."""
    from .repositories import repository_config
    from .statements import _compile_records
    repo = repo.resolve()
    config = repository_config(repository) if repository is not None else None
    rows = source_declarations(repo, roots=config['roots'] if config else None)
    return _compile_records(repo, rows, source_commit=source_commit,
                            annotations=annotations, repository=config)


def write_semantic_fixture(repo: Path, *, roots=None, output=None):
    """Write explicit compiler-output fixtures for CLI lifecycle tests.

    These historical snippets intentionally are not Lean integration tests.
    The actual exporter is exercised independently in test_lean_exporter.
    """
    import json
    import uuid
    from .lean_semantics import _bind_index, _source_binding
    from .repositories import source_files
    from .reuse import imported_modules
    repo = repo.resolve()
    binding = _source_binding(repo, roots)
    selected = set(binding['source_files'])
    sources = {str(p.relative_to(repo)): p.read_bytes().decode('utf-8')
               for p in source_files(repo, ['.'])}
    rows = source_declarations(repo, roots=binding['roots'])
    modules = []
    graph = {file: list(dict.fromkeys(imported_modules(text))) for file, text in sources.items()}
    for file in sorted(selected):
        text = sources[file]
        declarations = []
        for row in [r for r in rows if r['file'] == file]:
            start = sum(len(line) for line in text.splitlines(keepends=True)[:row['line']-1])
            end = start + len(row['source'])
            module_name = file[:-5].replace('/', '.')
            declarations.append({'name': row['name'],
                'compiler_name': ('_private.' + module_name + '.0.' if row['private'] else '') + row['name'],
                'kind': row['kind'], 'visibility': 'private' if row['private'] else 'public',
                'start_byte': len(text[:start].encode('utf-8')), 'end_byte': len(text[:end].encode('utf-8')),
                'line': row['line'], 'namespace': list(row['namespace']), 'doc': row['doc'],
                'fields': [{'name': name, 'type': typ} for name, typ in row['fields']]})
        modules.append({'file': file, 'module': file[:-5].replace('/', '.'),
                        'imports': graph[file], 'declarations': declarations})
    context = []
    pending = list(selected)
    seen = set(selected)
    while pending:
        for name in graph[pending.pop()]:
            file = name.replace('.', '/') + '.lean'
            if file in sources and file not in seen:
                seen.add(file); pending.append(file)
                context.append({'file': file, 'module': name, 'imports': graph[file]})
    raw = {'schema': 'formaliscope-lean-semantic-index.v1', 'lean_version': '4.32.2',
           'modules': modules, 'context_modules': context}
    index = _bind_index(raw, binding)
    output = output or repo.parent / ('semantic-fixture-' + uuid.uuid4().hex + '.json')
    output.write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
    return output
