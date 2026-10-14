"""Binding/consumption tests; declaration fixtures stand in for compiler JSON.

The production exporter is exercised separately by its Lean integration tests.
These tests deliberately specify compiler evidence instead of deriving it with
the former regular-expression declaration scanner.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import lean_semantics as semantics


class SemanticIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / 'source'
        (self.repo / 'KIP126').mkdir(parents=True)
        (self.repo / 'KIPBase').mkdir()
        self.exporter = Path(self.temporary.name) / 'ExportSemantic.lean'
        self.exporter.write_text('import Lean\n-- fixed test exporter identity\n', encoding='utf-8')
        self.exporter_patch = patch.object(semantics, 'EXPORTER_PATH', self.exporter)
        self.exporter_patch.start()
        self.addCleanup(self.exporter_patch.stop)
        (self.repo / 'lean-toolchain').write_text('leanprover/lean4:v4.32.2\n', encoding='utf-8')
        (self.repo / 'lakefile.toml').write_text('name = "fixture"\n', encoding='utf-8')
        self.text = ('module\nimport KIPBase.Context\nnamespace Test\n'
                     'private def hidden : Nat := 1\n'
                     'public\nsection\n/-- Exposed α. -/\n'
                     'def apiα : Nat := hidden\nend\nend Test\n')
        (self.repo / 'KIP126/Main.lean').write_text(self.text, encoding='utf-8')
        (self.repo / 'KIPBase/Context.lean').write_text('def context : Nat := 2\n', encoding='utf-8')
        self.raw = {
            'schema': semantics.SEMANTIC_INDEX_SCHEMA, 'lean_version': '4.32.2',
            'modules': [{
                'file': 'KIP126/Main.lean', 'module': 'KIP126.Main',
                'imports': ['Init', 'KIPBase.Context'],
                'declarations': [self.declaration('hidden', 'private'),
                                 self.declaration('apiα', 'public', doc='Exposed α.')],
            }],
            'context_modules': [{'file': 'KIPBase/Context.lean', 'module': 'KIPBase.Context',
                                 'imports': ['Init']}],
        }
        self.roots = ['KIP126']
        self.index = semantics._bind_index(self.raw, semantics._source_binding(self.repo, self.roots))

    def declaration(self, name, visibility, *, doc=''):
        statement = ('private def hidden : Nat := 1' if name == 'hidden'
                     else 'def apiα : Nat := hidden')
        first = self.text.encode('utf-8').index(statement.encode('utf-8'))
        return {'name': f'Test.{name}',
                'compiler_name': f'_private.KIP126.Main.0.Test.{name}' if visibility == 'private' else f'Test.{name}',
                'kind': 'def', 'visibility': visibility,
                'start_byte': first, 'end_byte': first + len(statement.encode('utf-8')),
                'line': self.text.encode('utf-8')[:first].count(b'\n') + 1,
                'namespace': ['Test'], 'doc': doc, 'fields': []}

    def resign(self, index):
        index['digest'] = semantics._index_digest(index)
        return index

    def read(self, index=None):
        return semantics.read_semantic_index(self.repo, roots=self.roots,
                                              semantic_index=self.index if index is None else index)

    def test_compiler_visibility_and_utf8_ranges_without_lean_runtime(self):
        with patch.object(semantics, '_run_lake', side_effect=AssertionError('must load offline')):
            rows = semantics.source_declarations(self.repo, roots=self.roots, semantic_index=self.index)
        hidden, exposed = rows
        self.assertTrue(hidden['private'])
        self.assertFalse(exposed['private'])
        self.assertEqual(exposed['name'], 'Test.apiα')
        self.assertEqual(exposed['source'], 'def apiα : Nat := hidden')
        self.assertEqual(exposed['doc'], 'Exposed α.')
        self.assertEqual(hidden['compiler_name'], '_private.KIP126.Main.0.Test.hidden')
        self.assertEqual(semantics.semantic_imports(self.index), {
            'KIP126/Main.lean': ['Init', 'KIPBase.Context'], 'KIPBase/Context.lean': ['Init']})
        self.assertEqual(semantics.semantic_context_modules(self.repo, self.index), {
            'KIPBase/Context.lean': 'def context : Nat := 2\n'})

    def test_index_file_is_portable_and_unmodified(self):
        path = Path(self.temporary.name) / 'index.json'
        path.write_text(json.dumps(self.index, ensure_ascii=False), encoding='utf-8')
        original = deepcopy(self.index)
        self.assertEqual(semantics.read_semantic_index(self.repo, roots=self.roots, semantic_index=path), self.index)
        self.assertEqual(self.index, original)
        self.assertEqual(self.index['provenance']['source_commit'], 'unknown')
        self.assertTrue(self.index['provenance']['source_dirty'])

    def test_tamper_and_missing_visibility_fail_without_guessing(self):
        tampered = deepcopy(self.index)
        tampered['modules'][0]['declarations'][0]['visibility'] = 'public'
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'digest'):
            self.read(tampered)
        tampered = deepcopy(self.index)
        del tampered['modules'][0]['declarations'][0]['visibility']
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'visibility'):
            self.read(self.resign(tampered))

    def colliding_instance_index(self):
        modules = []
        for file, value in [('KIP126/Main.lean', 1), ('KIP126/Second.lean', 2)]:
            text = f'local instance : Inhabited Nat := ⟨{value}⟩\n'
            (self.repo / file).write_text(text, encoding='utf-8')
            modules.append({'file': file, 'module': file[:-5].replace('/', '.'), 'imports': ['Init'],
                            'declarations': [{
                                'name': 'instInhabitedNat', 'compiler_name': 'instInhabitedNat',
                                'kind': 'instance', 'name_generated': True, 'visibility': 'public',
                                'start_byte': 0, 'end_byte': len(text.rstrip().encode('utf-8')),
                                'line': 1, 'namespace': [], 'doc': '', 'fields': [],
                            }]})
        raw = {'schema': semantics.SEMANTIC_INDEX_SCHEMA, 'lean_version': '4.32.2',
               'modules': modules, 'context_modules': []}
        return semantics._bind_index(raw, semantics._source_binding(self.repo, self.roots))

    def test_generated_compiler_name_collision_is_local_to_each_source_module(self):
        index = self.colliding_instance_index()
        rows = semantics.source_declarations(self.repo, roots=self.roots, semantic_index=index)
        self.assertEqual([(row['file'], row['compiler_name'], row['name_generated'], row['source'])
                          for row in rows], [
            ('KIP126/Main.lean', 'instInhabitedNat', True, 'local instance : Inhabited Nat := ⟨1⟩'),
            ('KIP126/Second.lean', 'instInhabitedNat', True, 'local instance : Inhabited Nat := ⟨2⟩'),
        ])
        cards = [{'compiler_name': row['compiler_name'], 'declaration': row['name'], 'kind': row['kind'],
                  'name_generated': True, 'visibility': 'public', 'visibility_origin': 'lean-compiler',
                  'module_file': row['file'], 'lean': {'file': row['file'], 'line': 1, 'source': row['source']}}
                 for row in rows]
        snapshot = {'semantic_index': semantics.semantic_index_metadata(index),
                    'modules': {row['file']: row['full_source'] for row in rows},
                    'context_modules': {}, 'module_imports': semantics.semantic_imports(index),
                    'dependency_lock_digest': index['provenance']['dependency_lock_digest'],
                    'source_environment': {**index['provenance']['source_environment'],
                                           'path_dependencies': index['provenance']['path_dependencies']},
                    'cards': cards}
        semantics.validate_snapshot_semantics(snapshot)
        mismatched = deepcopy(snapshot)
        mismatched['cards'][0]['lean']['source'] = rows[1]['source']
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'source differs'):
            semantics.validate_snapshot_semantics(mismatched)
        mismatched = deepcopy(snapshot)
        mismatched['cards'][0]['name_generated'] = False
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'disagrees'):
            semantics.validate_snapshot_semantics(mismatched)
        duplicate = deepcopy(index)
        duplicate['modules'][0]['declarations'].append(
            deepcopy(duplicate['modules'][0]['declarations'][0]))
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'duplicate declaration'):
            self.read(self.resign(duplicate))

    def test_generated_name_flag_is_strictly_boolean_and_only_for_instances(self):
        for flag in ('true', 1, True):
            index = deepcopy(self.index)
            index['modules'][0]['declarations'][0]['name_generated'] = flag
            with self.subTest(flag=flag), self.assertRaisesRegex(semantics.SemanticIndexError, 'generated names'):
                self.read(self.resign(index))

    def test_generated_instance_collision_builds_two_cards_with_correct_source(self):
        from .statements import compile_statements
        index = self.colliding_instance_index()
        snapshot = compile_statements(
            self.repo, source_commit='a' * 40,
            repository={'id': 'instances', 'name': 'Instances', 'roots': self.roots},
            semantic_index=index)
        self.assertEqual({card['id'] for card in snapshot['cards']}, {
            'statement::instances::instInhabitedNat::file=KIP126/Main.lean',
            'statement::instances::instInhabitedNat::file=KIP126/Second.lean',
        })
        for card in snapshot['cards']:
            self.assertTrue(card['name_generated'])
            self.assertEqual(card['identity_file'], card['module_file'])
            self.assertEqual(card['visibility'], 'public')
            self.assertEqual(card['lean']['source'],
                             (self.repo / card['module_file']).read_bytes().decode('utf-8').rstrip())
        semantics.validate_snapshot_semantics(snapshot)
        single_raw = {'schema': semantics.SEMANTIC_INDEX_SCHEMA, 'lean_version': '4.32.2',
                      'modules': [deepcopy(index['modules'][0])], 'context_modules': []}
        single_root = ['KIP126/Main.lean']
        single_index = semantics._bind_index(single_raw, semantics._source_binding(self.repo, single_root))
        single = compile_statements(self.repo, source_commit='a' * 40,
            repository={'id': 'instances', 'name': 'Instances', 'roots': single_root},
            semantic_index=single_index)
        self.assertEqual(single['cards'][0]['id'],
                         'statement::instances::instInhabitedNat::file=KIP126/Main.lean')

    def test_source_toolchain_environment_exporter_and_roots_are_bound(self):
        paths = [self.repo / 'KIP126/Main.lean', self.repo / 'KIPBase/Context.lean',
                 self.repo / 'lakefile.toml', self.exporter]
        for path in paths:
            original = path.read_bytes()
            with self.subTest(path=path.name):
                path.write_bytes(original + b'\n-- changed\n')
                with self.assertRaisesRegex(semantics.SemanticIndexError, 'stale'):
                    self.read()
                path.write_bytes(original)
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'stale'):
            semantics.read_semantic_index(self.repo, roots=['KIP126', 'KIPBase'], semantic_index=self.index)
        (self.repo / 'lean-toolchain').write_text('leanprover/lean4:v4.32.1\n')
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'supports'):
            self.read()

    def test_exact_source_and_context_coverage_is_required(self):
        missing = deepcopy(self.index)
        missing['modules'] = []
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'coverage'):
            self.read(self.resign(missing))
        missing = deepcopy(self.index)
        missing['context_modules'] = []
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'omits local imported'):
            self.read(self.resign(missing))
        (self.repo / 'KIP126/New.lean').write_text('def added : Nat := 3\n')
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'stale'):
            self.read()

    def test_bad_paths_duplicate_names_lines_and_utf8_splits_are_rejected(self):
        changes = [
            (lambda index: index['modules'][0].update(file='../escape.lean'), 'confined'),
            (lambda index: index['modules'].append(deepcopy(index['modules'][0])), 'duplicate module'),
            (lambda index: index['modules'][0]['declarations'].append(
                deepcopy(index['modules'][0]['declarations'][0])), 'duplicate declaration'),
            (lambda index: index['modules'][0]['declarations'][0].update(line=999), 'invalid byte range'),
            (lambda index: index['modules'][0]['declarations'][1].update(
                end_byte=self.text.encode('utf-8').index('apiα'.encode('utf-8')) + len(b'api') + 1), 'UTF-8'),
        ]
        for mutate, error in changes:
            with self.subTest(error=error):
                candidate = deepcopy(self.index)
                mutate(candidate)
                with self.assertRaisesRegex(semantics.SemanticIndexError, error):
                    self.read(self.resign(candidate))

    def test_git_commit_and_dirty_state_are_bound(self):
        for arguments in (['init', '-q'], ['config', 'user.email', 'fixture@example.invalid'],
                          ['config', 'user.name', 'Fixture'], ['add', '.'], ['commit', '-qm', 'fixture']):
            subprocess.run(['git', *arguments], cwd=self.repo, check=True, capture_output=True)
        index = semantics._bind_index(self.raw, semantics._source_binding(self.repo, self.roots))
        self.assertFalse(index['provenance']['source_dirty'])
        self.read(index)
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'next commit'], cwd=self.repo,
                       check=True, capture_output=True)
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'stale'):
            self.read(index)

    def test_generation_builds_each_selected_module_and_checks_runtime(self):
        calls = []
        def run(repo, arguments):
            calls.append(arguments)
            if arguments == ['env', 'lean', '--version']:
                return 'Lean (version 4.32.2, x86_64-unknown-linux-gnu, Release)\n'
            if arguments[:3] == ['env', 'lean', '--run']:
                self.assertEqual(Path(arguments[-1]).read_text(), 'KIP126/Main.lean\n')
                return json.dumps(self.raw)
            return ''
        with patch.object(semantics, '_run_lake', side_effect=run):
            index = semantics.export_semantic_index(self.repo, roots=self.roots)
        self.assertEqual(calls[:3], [['env', 'lean', '--version'], ['build'], ['build', 'KIP126.Main']])
        self.assertEqual(index, self.index)
        with patch.object(semantics, '_run_lake', return_value='Lean (version 4.31.0, Release)'):
            with self.assertRaisesRegex(semantics.SemanticIndexError, 'active Lake Lean version'):
                semantics.export_semantic_index(self.repo, roots=self.roots)

    def test_missing_runtime_and_mutation_during_build_fail(self):
        with patch.object(semantics.subprocess, 'run', side_effect=FileNotFoundError('lake')):
            with self.assertRaisesRegex(semantics.SemanticIndexError, 'requires Lake'):
                semantics.export_semantic_index(self.repo, roots=self.roots)
        def changing_build(repo, arguments):
            if arguments == ['env', 'lean', '--version']:
                return 'Lean (version 4.32.2, Release)'
            if arguments == ['build']:
                (repo / 'KIP126/Main.lean').write_text(self.text + '\n-- raced\n')
            return ''
        with patch.object(semantics, '_run_lake', side_effect=changing_build):
            with self.assertRaisesRegex(semantics.SemanticIndexError, 'changed while Lake built'):
                semantics.export_semantic_index(self.repo, roots=self.roots)

    def frozen_snapshot(self):
        rows = semantics.source_declarations(self.repo, roots=self.roots, semantic_index=self.index)
        cards = [{'compiler_name': row['compiler_name'], 'declaration': row['name'], 'kind': row['kind'],
                  'visibility': 'private' if row['private'] else 'public', 'private': row['private'],
                  'visibility_origin': 'lean-compiler', 'module_file': row['file'],
                  'lean': {'file': row['file'], 'line': row['line'], 'source': row['source']}}
                 for row in rows]
        return {'semantic_index': semantics.semantic_index_metadata(self.index),
                'modules': {'KIP126/Main.lean': self.text},
                'context_modules': semantics.semantic_context_modules(self.repo, self.index),
                'module_imports': semantics.semantic_imports(self.index),
                'dependency_lock_digest': self.index['provenance']['dependency_lock_digest'],
                'source_environment': self.index['provenance']['source_environment'], 'cards': cards}

    def test_frozen_snapshot_validates_without_exporter_or_checkout(self):
        snapshot = self.frozen_snapshot()
        semantics.validate_snapshot_semantics(snapshot)
        self.exporter.unlink()
        (self.repo / 'KIP126/Main.lean').unlink()
        semantics.validate_snapshot_semantics(snapshot)
        # Old installed evidence keeps its validation contract without Lean.
        semantics.validate_snapshot_semantics({'cards': [{'declaration': 'Old.name'}]})

    def test_frozen_snapshot_rejects_changed_visibility_range_source_and_imports(self):
        mutations = [
            lambda snapshot: snapshot['cards'][0].update(visibility='public', private=False),
            lambda snapshot: snapshot['cards'][0]['lean'].update(source='def fake : Nat := 7'),
            lambda snapshot: snapshot['semantic_index']['declarations'][0].update(start_byte=0),
            lambda snapshot: snapshot['modules'].update({'KIP126/Main.lean': self.text + '-- changed\n'}),
            lambda snapshot: snapshot['module_imports'].pop('KIPBase/Context.lean'),
            lambda snapshot: snapshot['cards'].pop(),
        ]
        for mutate in mutations:
            snapshot = self.frozen_snapshot()
            mutate(snapshot)
            with self.assertRaises(semantics.SemanticIndexError):
                semantics.validate_snapshot_semantics(snapshot)

    def test_frozen_snapshot_cannot_drop_a_reported_local_import(self):
        snapshot = self.frozen_snapshot()
        snapshot['context_modules'].pop('KIPBase/Context.lean')
        snapshot['module_imports'].pop('KIPBase/Context.lean')
        snapshot['semantic_index']['module_imports'].pop('KIPBase/Context.lean')
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'compiler-reported local'):
            semantics.validate_snapshot_semantics(snapshot)

    def test_frozen_snapshot_dependency_lock_must_match_compiler_binding(self):
        (self.repo / 'lake-manifest.json').write_text('{"version":"1.1.0","packages":[]}\n')
        self.index = semantics._bind_index(self.raw, semantics._source_binding(self.repo, self.roots))
        snapshot = self.frozen_snapshot()
        semantics.validate_snapshot_semantics(snapshot)
        snapshot['dependency_lock_digest'] = None
        with self.assertRaisesRegex(semantics.SemanticIndexError, 'dependency lock'):
            semantics.validate_snapshot_semantics(snapshot)

    def test_statement_assembly_rechecks_source_before_returning_candidate(self):
        from . import statements
        assemble = statements._compile_records

        def racing_assembly(*args, **kwargs):
            candidate = assemble(*args, **kwargs)
            (self.repo / 'KIP126/Main.lean').write_text(self.text + '\n-- changed during assembly\n')
            return candidate

        with patch.object(statements, '_compile_records', side_effect=racing_assembly):
            with self.assertRaisesRegex(semantics.SemanticIndexError, 'stale'):
                statements.compile_statements(self.repo, source_commit='a' * 40,
                    repository={'id': 'example', 'name': 'Example', 'roots': self.roots},
                    semantic_index=self.index)


if __name__ == '__main__':
    unittest.main()
