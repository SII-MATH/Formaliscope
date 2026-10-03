"""Build a source-backed Statement browsing snapshot (no Lean execution).

The graph is a source-reference candidate graph, not an elaborator dependency
graph. Blueprint prose stays attributed to Blueprint. Generated summaries are
reading aids, never represented as verified semantic back-translations.
"""
from __future__ import annotations

import re
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .build import (SNAPSHOT_SCHEMA, CURRENT_FINGERPRINT_SCHEME, _digest,
                    _content_fingerprint, _dependency_lock_digest, _clean_statement,
                    INPUT_RE, NODE_RE, LABEL_RE, LEAN_RE, CHAPTER_RE,
                    calculate_snapshot_digest, _git_head, _git_dirty)

DECL = re.compile(r"^\s*(?:@\[[^]]*\]\s*)*(?:(?:private|protected|noncomputable|partial|unsafe)\s+)*"
                  r"(def|theorem|lemma|structure|class|abbrev|inductive|opaque|axiom|constant|instance)"
                  r"\s+([\w'.«»₀-₉]+)(?=\s|\{|\(|:|$)")
TOKEN = re.compile(r"[\w'₀-₉]+(?:\.[\w'₀-₉]+)*")


def mask_comments(text: str) -> str:
    """Preserve offsets while masking nested block comments, strings and line comments."""
    result = list(text)
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
            result[i:i+2] = '  '
            i += 2
            continue
        elif text.startswith('--', i):
            end = text.find('\n', i)
            if end == -1:
                end = len(text)
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
    return ''.join(result)


def source_declarations(repo: Path) -> list[dict]:
    records = []
    for root in ('KIP126', 'KIPBase'):
        for path in sorted((repo / root).rglob('*.lean')):
            text = path.read_text(encoding='utf-8')
            original, masked = text.splitlines(), mask_comments(text).splitlines()
            namespace, scopes, found = [], [], []
            for number, line in enumerate(masked):
                ns = re.match(r'^\s*namespace\s+([\w.]+)', line)
                if ns:
                    namespace.extend(ns[1].split('.'))
                    scopes.append(ns[1].split('.'))
                    continue
                if re.match(r'^\s*section(?:\s|$)', line):
                    scopes.append([])
                    continue
                if re.match(r'^\s*end(?:\s|$)', line):
                    if scopes:
                        count = len(scopes.pop())
                        if count:
                            del namespace[-count:]
                    continue
                match = DECL.match(line)
                if not match:
                    continue
                kind, name = match.groups()
                # Lean's _root_ prefix explicitly escapes the current namespace.
                fqn = name.removeprefix('_root_.') if name.startswith('_root_.') else '.'.join([*namespace, name])
                found.append((fqn, kind, number, tuple(namespace)))
            for offset, (name, kind, first, ns) in enumerate(found):
                last = found[offset+1][2] if offset+1 < len(found) else len(original)
                # Do not include following namespace/section ends or the next docstring.
                for stop in range(first+1, last):
                    if re.match(r'^\s*(?:end|namespace|section)(?:\s|$)', masked[stop]) or re.match(
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
                comment_lines = []
                start = first-1
                while start >= 0 and first-start <= 30 and (not masked[start].strip()):
                    comment_lines.append(original[start])
                    start -= 1
                comment = '\n'.join(reversed(comment_lines)).strip()
                comment = re.sub(r'^.*?/\*', '', comment)
                comment = re.sub(r'^/-[!-]?\s*|\s*-/\s*$', '', comment).strip()
                records.append({'name': name, 'kind': kind, 'namespace': ns,
                                'file': str(path.relative_to(repo)), 'line': first+1,
                                'source': source, 'fields': fields, 'doc': comment[:5000],
                                'context': '\n'.join(original[:first]),
                                'full_source': text})
    return records


def blueprint_prose(repo: Path) -> dict[str, dict]:
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
            for name in LEAN_RE.findall(body):
                result.setdefault(name.strip(), {
                    'statement': _clean_statement(body), 'title': title or name,
                    'label': label[1] if label else name,
                    'chapter': chapter[1] if chapter else path.stem,
                    'blueprint_file': str(path.relative_to(repo)),
                    'blueprint_line': text.count('\n', 0, match.start())+1,
                })
    return result


def compile_statements(repo: Path, *, source_commit: str | None = None, annotations: Path | None = None) -> dict:
    records = source_declarations(repo)
    index = {row['name']: row for row in records}
    short = defaultdict(list)
    for name in index:
        short[name.rsplit('.', 1)[-1]].append(name)
    prose = blueprint_prose(repo)
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
        if token in index:
            return token
        ns = row['namespace']
        for count in range(len(ns), -1, -1):
            candidate = '.'.join([*ns[:count], token])
            if candidate in index:
                return candidate
        choices = short.get(token, [])
        return choices[0] if len(choices) == 1 else None
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
    direct = {dep for name in roots for dep in dependencies[name]}
    cards = []
    for name, row in index.items():
        bp = prose.get(name)
        annotation = translated.get(name)
        title = annotation.get('title', name) if annotation else bp['title'] if bp else name.rsplit('.', 1)[-1]
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
        summary = f"{name} 是一个 {'定理' if row['kind'] in ('theorem','lemma') else '结构' if row['fields'] else 'Lean '+row['kind']+' 声明'}。"
        if row['fields']:
            summary += '它包含以下字段：' + '、'.join(field[0] for field in row['fields']) + '。请逐项核对字段的条件与数学含义。'
        elif row['doc']:
            summary += '\n\n'+row['doc']
        else:
            summary += '请结合下方完整源码及引用对象核对陈述、参数和前提。'
        card = {
            'id': 'statement::'+name, 'declaration': name, 'label': bp['label'] if bp else name,
            'title': title, 'kind': row['kind'], 'chapter': bp['chapter'] if bp else row['file'].split('/')[0],
            'statement': annotation['statement'] if annotation else bp['statement'] if bp else summary,
            'statement_origin': 'backtranslation' if annotation else 'blueprint' if bp else 'reading-summary',
            'blueprint_file': bp['blueprint_file'] if bp else row['file'],
            'blueprint_line': bp['blueprint_line'] if bp else row['line'],
            'reading_summary': summary,
            'lean': {'file': row['file'], 'line': row['line'], 'source': row['source'], 'truncated': False},
            'source_status': 'local', 'dependencies': ['statement::'+dep for dep in dependencies[name]],
            'dependency_origin': 'source-reference-candidates',
            'fields': [{'name': field[0], 'type': field[1]} for field in row['fields']],
            'role': role, 'priority': priority, 'risk': 'high' if risky else 'medium' if row['fields'] else 'normal',
            'main_target': name in roots,
            'review_contract': 'statement-front-end.v1',
            'module_file': row['file'],
            'annotation_source_sha256': annotation['source_sha256'] if annotation else None,
        }
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
    snapshot['digest'] = calculate_snapshot_digest(snapshot)
    return snapshot
