"""Real compiler regressions for the semantic declaration exporter.

This is optional for ordinary source-only installations. CI requires the exact
pinned toolchain so these tests cannot accidentally become permanent skips.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from .lean_semantics import (SUPPORTED_LEAN_VERSION, export_semantic_index,
                             read_semantic_index, source_declarations)


FIXTURE = Path(__file__).resolve().parent / 'lean' / 'tests'


class LeanSemanticExporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('FORMALISCOPE_REQUIRE_LEAN_TESTS') == '1'
        if not shutil.which('lake'):
            if required:
                raise RuntimeError('Lean exporter regressions require Lake and Lean 4.32.2')
            raise unittest.SkipTest('Lake is unavailable; real compiler regressions require Lean 4.32.2')
        cls.temporary = tempfile.TemporaryDirectory(prefix='formaliscope-lean-regression-')
        cls.repo = Path(cls.temporary.name)
        shutil.copytree(FIXTURE, cls.repo, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.lake', '*.olean', '*.ilean'))
        version = subprocess.run(['lake', 'env', 'lean', '--version'], cwd=cls.repo,
                                 capture_output=True, text=True, timeout=90)
        if version.returncode or f'version {SUPPORTED_LEAN_VERSION},' not in version.stdout:
            cls.temporary.cleanup()
            if required:
                raise RuntimeError('Lean exporter regressions require the exact Lean 4.32.2 toolchain: '
                                   + version.stdout + version.stderr)
            raise unittest.SkipTest('Exact Lean 4.32.2 toolchain is unavailable')
        # Lake creates its package manifest on the first build. Establish that
        # build environment before the index begins its strict source binding.
        build = subprocess.run(['lake', 'build'], cwd=cls.repo, capture_output=True,
                               text=True, timeout=180)
        if build.returncode:
            cls.temporary.cleanup()
            raise RuntimeError('Lean semantic fixture failed to compile: ' + build.stdout + build.stderr)
        subprocess.run(['git', 'init', '--initial-branch=dev'], cwd=cls.repo,
                       check=True, capture_output=True)
        subprocess.run(['git', 'add', '.'], cwd=cls.repo, check=True, capture_output=True)
        subprocess.run(['git', '-c', 'user.name=Formaliscope compiler test',
                        '-c', 'user.email=compiler-test@example.invalid',
                        'commit', '-m', 'Compile synthetic declaration fixtures'],
                       cwd=cls.repo, check=True, capture_output=True)
        cls.index = export_semantic_index(cls.repo, roots=['SemanticFixture'])
        cls.records = source_declarations(cls.repo, roots=['SemanticFixture'], semantic_index=cls.index)
        cls.rows = {row['name']: row for row in cls.records}

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'temporary'):
            cls.temporary.cleanup()

    def test_compiler_user_declaration_set_excludes_generated_and_quoted(self):
        self.assertEqual(set(self.rows), {
            'Audit.hidden', 'Audit.unexposed', 'Audit.visible',
            'Audit.secret', 'Audit.referringToPrivate', 'Audit.universeValue',
            'Audit.assumed', 'Audit.verified', 'Audit.Record', 'Audit.Property',
            'Audit.Flag', 'Audit.Tree', 'Audit.instInhabitedRecord',
            'Audit.namedInstance', 'Audit.instDecidableEqHAddOfNat_semanticFixture', 'Audit.even', 'Audit.odd', 'rootValue',
            'Audit.hiddenAfterSection', 'Old.ordinary', 'Old.hidden', 'Old.aLemma',
            'Shared.instNeZeroNatOfNat_semanticFixture',
        })
        self.assertEqual(len(self.records), 24)
        self.assertNotIn('Audit.phantom', self.rows)
        self.assertNotIn('Audit.Record.amount', self.rows)
        self.assertNotIn('Audit.Tree.rec', self.rows)
        self.assertEqual(self.rows['Old.aLemma']['kind'], 'lemma')

    def test_private_names_and_module_defaults_are_authoritative(self):
        private = {name for name, row in self.rows.items() if row['private']}
        self.assertEqual(private, {'Audit.hidden', 'Audit.secret',
                                   'Audit.hiddenAfterSection', 'Old.hidden'})
        self.assertFalse(self.rows['Audit.unexposed']['private'])
        self.assertFalse(self.rows['Audit.referringToPrivate']['private'])
        self.assertTrue(self.rows['Audit.secret']['compiler_name'].startswith('_private.'))
        self.assertTrue(self.rows['Audit.visible']['source'].startswith('def visible'))

    def test_unicode_docs_universe_mutual_ranges_and_structure_fields(self):
        self.assertIn('跨行公开区域', self.rows['Audit.visible']['doc'])
        self.assertNotIn('/--', self.rows['Audit.visible']['source'])
        self.assertEqual(self.rows['Audit.universeValue']['source'],
                         'public def universeValue.{u} (α : Type u) (x : α) : α := x')
        self.assertIn('| n + 1 => odd n', self.rows['Audit.even']['source'])
        self.assertNotIn('def odd', self.rows['Audit.even']['source'])
        self.assertEqual([field[0] for field in self.rows['Audit.Record']['fields']],
                         ['amount', 'bounded'])
        self.assertEqual(self.rows['Audit.Record']['fields'],
                         [('amount', 'Nat'), ('bounded', 'Fin (amount + 1)')])
        self.assertEqual(self.rows['Audit.Property']['kind'], 'class')
        self.assertEqual(self.rows['rootValue']['namespace'], ())

    def test_anonymous_local_instances_keep_module_identity(self):
        same = [row for row in self.records
                if row['name'] == 'Shared.instNeZeroNatOfNat_semanticFixture']
        self.assertEqual(len(same), 2)
        self.assertEqual({row['file'] for row in same},
                         {'SemanticFixture/SameA.lean', 'SemanticFixture/SameB.lean'})
        self.assertTrue(all(row['name_generated'] and not row['private'] for row in same))
        self.assertTrue(self.rows['Audit.instInhabitedRecord']['name_generated'])
        self.assertFalse(self.rows['Audit.namedInstance']['name_generated'])
        self.assertFalse(self.rows['Audit.visible']['name_generated'])

    def test_compiled_snapshot_private_entries_never_auto_select(self):
        from .build import validate_snapshot
        from .server import initialize, catalog
        from .statements import compile_statements
        repository = {'id': 'compilerfixture', 'name': 'Compiler fixture',
                      'roots': ['SemanticFixture']}
        snapshot = compile_statements(self.repo, repository=repository,
                                      semantic_index=self.index)
        validate_snapshot(snapshot)
        cards = {card['declaration']: card for card in snapshot['cards']}
        self.assertEqual(set(cards), set(self.rows))
        self.assertEqual(len(snapshot['cards']), len(self.records))
        same = [card for card in snapshot['cards']
                if card['declaration'] == 'Shared.instNeZeroNatOfNat_semanticFixture']
        self.assertEqual(len(same), 2)
        self.assertEqual(len({card['id'] for card in same}), 2)
        self.assertTrue(all(card['identity_file'] == card['module_file'] for card in same))
        self.assertTrue(all(card['visibility'] == 'public' for card in same))
        self.assertEqual({name for name, card in cards.items()
                          if card['visibility'] == 'private'},
                         {name for name, row in self.rows.items() if row['private']})
        with tempfile.TemporaryDirectory(prefix='formaliscope-compiler-review-') as directory:
            database = Path(directory) / 'reviews.sqlite'
            initialize(database)
            initial = catalog(snapshot, database, 'compiler-test', initial_id='auto')['initial_evidence']
            self.assertIsNotNone(initial)
            self.assertEqual(initial['visibility'], 'public')

    def test_compiler_import_graph_and_frozen_index_roundtrip(self):
        modules = {module['file']: module for module in self.index['modules']}
        self.assertIn('Support.Context',
                      modules['SemanticFixture/Basic.lean']['imports'])
        self.assertEqual({m['file'] for m in self.index['context_modules']},
                         {'Support/Context.lean'})
        self.assertEqual(read_semantic_index(self.repo, roots=['SemanticFixture'],
                                             semantic_index=self.index), self.index)


if __name__ == '__main__':
    unittest.main()
