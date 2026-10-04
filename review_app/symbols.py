"""Conservative source-based Lean name tracing within the frozen review snapshot.

This is a navigation aid, not Lean's elaborator. Exact lexical/namespace matches
are followed; unresolved short names are offered as explicit choices. External
library symbols cannot be navigated without their source in the snapshot.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .statements import DECL, mask_comments

PART = r"(?:[^\W\d][\w'₀-₉]*|«[^»\n]+»)"
NAME = re.compile(PART + r"(?:\." + PART + r")*", re.UNICODE)
BINDER = re.compile(r"[({\[]\s*((?:" + PART + r"\s*)+):")
LOCAL = re.compile(r"\b(?:let|have|intro|intros)\s+(" + PART + r")")
LAMBDA = re.compile(r"(?:\bfun\s+|∀\s*)([^\n]*?)(?:=>|↦|,)")


def _position(text: str, offset: int, base: int = 1) -> tuple[int, int]:
    return base + text.count('\n', 0, offset), offset - text.rfind('\n', 0, offset)


def _offset(text: str, line: int, column: int, base: int) -> int:
    lines = text.split('\n')
    relative = line - base
    if relative < 0 or relative >= len(lines) or column < 1 or column > len(lines[relative]) + 1:
        raise ValueError('名称位置已变化，请重新打开条目')
    return sum(len(value) + 1 for value in lines[:relative]) + column - 1


def _scope(source: str, line: int) -> tuple[list[str], list[str]]:
    namespaces, stack, opened = [], [], []
    for text in mask_comments(source).splitlines()[:max(0, line - 1)]:
        ns = re.match(r'^\s*namespace\s+(\S+)', text)
        if ns:
            parts = ns[1].split('.')
            stack.append((len(parts), len(opened)))
            namespaces.extend(parts)
        elif re.match(r'^\s*section(?:\s|$)', text):
            stack.append((0, len(opened)))
        elif re.match(r'^\s*end(?:\s|$)', text) and stack:
            count, previous = stack.pop()
            if count:
                del namespaces[-count:]
            del opened[previous:]
        else:
            match = re.match(r'^\s*open\s+(?!scoped\b)(.*)', text)
            if match:
                # Selective/renaming opens are intentionally left unresolved.
                if not any(char in match[1] for char in '({'):
                    opened.extend(NAME.findall(match[1]))
    return namespaces, opened


def _local_bindings(source: str, clicked: int) -> list[tuple[str, int]]:
    masked = mask_comments(source)
    before = masked[:clicked + 1]
    header_end = masked.find(':=')
    header_end = len(masked) if header_end < 0 else header_end
    bindings = []
    depth = 0
    for position, char in enumerate(masked[:header_end]):
        if char == ':' and depth == 0:
            break
        if char in '({[':
            if depth == 0:
                match = BINDER.match(masked, position)
                if match:
                    for token in NAME.finditer(match[1]):
                        bindings.append((token[0], match.start(1) + token.start()))
            depth += 1
        elif char in ')}]':
            depth = max(0, depth - 1)
    # Section variables are handled separately; these forms cover the common
    # tactic and term bindings without pretending to know elaborated types.
    for match in LOCAL.finditer(before):
        start = match.start(1)
        line_start = masked.rfind('\n', 0, match.start()) + 1
        indent = len(masked[line_start:]) - len(masked[line_start:].lstrip(' \t'))
        click_line = masked.rfind('\n', 0, clicked) + 1
        # An indented tactic branch ceases to bind after its body dedents.
        first_newline = masked.find('\n', match.end())
        trailing = masked[first_newline + 1:click_line] if 0 <= first_newline < click_line else ''
        outside = any(value.strip() and len(value) - len(value.lstrip(' \t')) < indent
                      for value in trailing.splitlines())
        keyword = masked[match.start():match.start(1)].strip()
        available = not outside
        if keyword in {'let', 'have'} and clicked != start:
            # A nonrecursive let/have is not in scope inside its own RHS or
            # proof. Wait until the next command at the binding's indentation.
            available = False
            command_end = masked.find('\n', match.end())
            if command_end >= 0:
                next_start = command_end + 1
                for text in masked[next_start:].splitlines(keepends=True):
                    text_indent = len(text) - len(text.lstrip(' \t'))
                    if text.strip() and text_indent <= indent:
                        available = clicked >= next_start and not outside
                        break
                    next_start += len(text)
        if available:
            bindings.append((match[1], start))
        # intro can introduce several names in one command.
        command = masked[match.end():masked.find('\n', match.end()) if '\n' in masked[match.end():] else len(masked)]
        if masked[match.start():match.start(1)].strip() in {'intro', 'intros'} and not outside:
            for token in NAME.finditer(command):
                if command[:token.start()].strip() and any(c in command[:token.start()] for c in ':=;'):
                    break
                bindings.append((token[0], match.end() + token.start()))
    for match in LAMBDA.finditer(masked):
        end = masked.find('\n', match.end())
        if match.start() <= clicked <= (len(masked) if end < 0 else end):
            # Only simple names/typed groups are handled here; complex patterns
            # stay unresolved instead of being confused with declarations.
            header = match[1].split(':', 1)[0]
            for token in NAME.finditer(header):
                bindings.append((token[0], match.start(1) + token.start()))
    return [(name, offset) for name, offset in bindings if offset <= clicked]


def _context_bindings(source: str, clicked_line: int) -> list[tuple[str, int, int]]:
    bindings, stack = [], []
    for line, text in enumerate(mask_comments(source).splitlines()[:max(0, clicked_line - 1)], 1):
        if re.match(r'^\s*(?:namespace|section)(?:\s|$)', text):
            stack.append(len(bindings))
        elif re.match(r'^\s*end(?:\s|$)', text) and stack:
            del bindings[stack.pop():]
        elif re.match(r'^\s*variables?(?:\s|$)', text):
            for group in BINDER.finditer(text):
                for token in NAME.finditer(group[1]):
                    bindings.append((token[0], line, group.start(1) + token.start() + 1))
    return bindings


class SymbolIndex:
    def __init__(self, snapshot: dict):
        self.cards = {card['id']: card for card in snapshot.get('cards', [])}
        self.modules = snapshot.get('modules', {})
        self.names = defaultdict(list)
        for card in self.cards.values():
            lean = card.get('lean') or {}
            if not card.get('declaration') or not lean.get('source'):
                continue
            self.names[card['declaration']].append(self._target(card))
            for field in card.get('fields', []):
                token = re.search(r'^\s*(' + re.escape(field['name']) + r')\s*(?:\([^\n]*?\)\s*)?:',
                                  mask_comments(lean['source']), re.M)
                if token:
                    line, column = _position(lean['source'], token.start(1), lean.get('line', 1))
                    self.names[card['declaration'] + '.' + field['name']].append({
                        **self._target(card), 'declaration': card['declaration'] + '.' + field['name'],
                        'line': line, 'column': column})

    @staticmethod
    def _target(card: dict) -> dict:
        lean = card.get('lean') or {}
        match = DECL.match(mask_comments(lean.get('source', '')))
        line, column = _position(lean.get('source', ''), match.start(2), lean.get('line', 1)) if match else (lean.get('line', 1), 1)
        return {'card_id': card['id'], 'declaration': card['declaration'],
                'file': lean.get('file', card.get('module_file', '')),
                'line': line, 'column': column}

    def resolve(self, card_id: str, name: str, line: int, column: int, scope: str = 'declaration') -> dict:
        card = self.cards.get(card_id)
        if not card:
            raise KeyError('审核对象不存在')
        if scope not in {'declaration', 'module'} or not NAME.fullmatch(name) or len(name) > 300:
            raise ValueError('名称追溯请求无效')
        lean = card.get('lean') or {}
        file = lean.get('file', card.get('module_file', ''))
        source = self.modules.get(file) if scope == 'module' else lean.get('source')
        if source is None:
            return {'status': 'unresolved', 'name': name, 'message': '快照中尚未收录这份源码。', 'candidates': []}
        source = source.replace('\r\n', '\n').replace('\r', '\n')
        base = 1 if scope == 'module' else lean.get('line', 1)
        offset = _offset(source, line, column, base)
        actual = NAME.match(mask_comments(source), offset)
        if actual is None or actual[0] != name:
            raise ValueError('名称位置已变化，请重新打开条目')
        active = card
        if scope == 'module':
            owners = [item for item in self.cards.values() if (item.get('lean') or {}).get('file') == file
                      and (item.get('lean') or {}).get('line', 1) <= line]
            if owners:
                active = max(owners, key=lambda item: item['lean']['line'])
        active_lean = active.get('lean') or {}
        active_base = active_lean.get('line', 1)
        relative = line - active_base
        active_source = active_lean.get('source', '').replace('\r\n', '\n').replace('\r', '\n')
        if 0 <= relative < len(active_source.split('\n')):
            local_offset = _offset(active_source, line, column, active_base)
            root_name = name.split('.')[0]
            bindings = [(value, position) for value, position in _local_bindings(active_source, local_offset)
                        if value == root_name]
            if bindings:
                value, position = bindings[-1]
                target_line, target_column = _position(active_source, position, active_base)
                return {'status': 'local', 'name': name, 'message': f'局部名称 {value} 的绑定位置。',
                        'target': {'card_id': active['id'], 'file': file, 'line': target_line,
                                   'column': target_column, 'scope': scope}, 'candidates': []}
        module = self.modules.get(file, '')
        root_name = name.split('.')[0]
        variables = [item for item in _context_bindings(module, line) if item[0] == root_name]
        if variables:
            value, target_line, target_column = variables[-1]
            return {'status': 'local', 'name': name, 'message': f'局部名称 {value} 在当前文件上下文中的绑定位置。',
                    'target': {'card_id': active['id'], 'file': file, 'line': target_line,
                               'column': target_column, 'scope': 'module'}, 'candidates': []}
        namespaces, opened = _scope(module, line) if module else (active.get('declaration', '').split('.')[:-1], [])
        def scoped_choices(value: str) -> list[dict]:
            explicit = value.removeprefix('_root_.')
            search = [explicit] if value.startswith('_root_.') else [
                '.'.join([*namespaces[:count], value]) for count in range(len(namespaces), -1, -1)]
            for candidate in search:
                if candidate in self.names:
                    return self.names[candidate]
            return [target for ns in opened for target in self.names.get(ns + '.' + value, [])]
        choices = scoped_choices(name)
        if choices:
            return self._result(name, choices)
        # Dot notation on a known value (model.sequence, object.field) still
        # lets readers inspect the receiver without guessing elaborated types.
        parts = name.split('.')
        for count in range(len(parts) - 1, 0, -1):
            receiver = '.'.join(parts[:count])
            choices = scoped_choices(receiver)
            if choices:
                result = self._result(name, choices)
                result['message'] = f'已定位投影对象 {receiver} 的定义；后续字段需要结合该定义查看。'
                return result
        # Unknown elaborator context: never silently jump to a globally unique
        # short name. Suggest source-index candidates for deliberate selection.
        choices = [target for key, targets in self.names.items()
                   if key.endswith('.' + name) for target in targets]
        if choices:
            result = self._result(name, choices, certain=False)
            result['message'] = '源码索引中找到以下候选；无法确认 Lean 的名称解析，请选择要查看的定义。'
            return result
        return {'status': 'unresolved', 'name': name, 'candidates': [],
                'message': '当前快照没有可定位的定义；可能来自 Mathlib 等外部库、尚未纳入索引，或是源码解析未覆盖的局部绑定。'}

    @staticmethod
    def _result(name: str, choices: list[dict], *, certain: bool = True) -> dict:
        unique = {tuple(sorted(choice.items())): choice for choice in choices}
        targets = sorted(unique.values(), key=lambda item: (item['declaration'], item['file'], item['line']))
        if certain and len(targets) == 1:
            return {'status': 'definition', 'name': name, 'target': targets[0],
                    'message': '已定位源码定义。', 'candidates': []}
        return {'status': 'ambiguous', 'name': name, 'candidates': targets[:30], 'truncated': len(targets) > 30,
                'message': '存在多个同名定义，请选择要查看的定义。'}
