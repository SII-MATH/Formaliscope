"""Semantic extraction keeps frozen review identities and conservative history."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from .build import (CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA, _content_fingerprint,
                    calculate_snapshot_digest, validate_snapshot)
from .database import connect, initialize
from .judgments import catalog, review_state, submit
from .lean_semantics import _bind_index, _source_binding
from .repositories import dataset_id, make_collection, qualify_legacy, repository_config, select_dataset
from .reuse import context_basis, freeze_imports, reuse_snapshot
from .statements import compile_statements
from .storage import install_snapshot


class SemanticCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'lean-toolchain').write_text('leanprover/lean4:v4.32.2\n')
        self.file = self.source / 'Value.lean'
        self.text = ('module\npublic section\nnamespace Example\n'
                     'def value : Nat := 1\nprivate def hidden : Nat := 2\nend Example\n')
        self.file.write_text(self.text)
        self.config = repository_config({'id': 'alpha', 'name': 'Alpha', 'roots': ['.']})
        self.data = self.root / 'runtime'
        self.db = self.data / 'judgments.sqlite3'
        initialize(self.db)
        self.reviewer = 'u_' + '1' * 32
        self.public_id = 'statement::alpha::Example.value'
        self.private_id = 'statement::alpha::Example.hidden::file=Value.lean'

    def semantic_snapshot(self, commit='a'):
        text = self.file.read_text()
        declarations = []
        for name, prefix, visibility in [('value', 'def value', 'public'),
                                          ('hidden', 'private def hidden', 'private')]:
            start = text.index(prefix)
            end = text.index('\n', start)
            declarations.append({'name': 'Example.' + name,
                                 'compiler_name': ('_private.Value.0.' if visibility == 'private' else '') + 'Example.' + name,
                                 'kind': 'def', 'visibility': visibility,
                                 'start_byte': len(text[:start].encode()),
                                 'end_byte': len(text[:end].encode()),
                                 'line': text.count('\n', 0, start) + 1,
                                 'namespace': ['Example'], 'doc': '', 'fields': []})
        raw = {'schema': 'formaliscope-lean-semantic-index.v1', 'lean_version': '4.32.2',
               'modules': [{'file': 'Value.lean', 'module': 'Value',
                            'imports': [], 'declarations': declarations}], 'context_modules': []}
        index = _bind_index(raw, _source_binding(self.source, ['.']))
        return compile_statements(self.source, source_commit=commit * 40,
                                  repository=self.config, semantic_index=index)

    def historical_snapshot(self):
        """The exact pre-semantic v2 shape, independent of current extraction."""
        cards = []
        for name, line, source, private in [('value', 4, 'def value : Nat := 1', False),
                                            ('hidden', 5, 'private def hidden : Nat := 2', True)]:
            declaration = 'Example.' + name
            summary = f'{declaration} 是一个 Lean def 声明。请结合下方完整源码及引用对象核对陈述、参数和前提。'
            card = {'id': self.private_id if private else self.public_id,
                    'declaration': declaration, 'label': declaration, 'title': name,
                    'kind': 'def', 'chapter': 'Value.lean', 'statement': summary,
                    'statement_origin': 'reading-summary', 'blueprint_file': 'Value.lean',
                    'blueprint_line': line, 'reading_summary': summary,
                    'lean': {'file': 'Value.lean', 'line': line, 'source': source, 'truncated': False},
                    'source_status': 'local', 'dependencies': [],
                    'dependency_origin': 'source-reference-candidates', 'fields': [],
                    'role': '数学对象', 'priority': 50, 'risk': 'normal', 'main_target': False,
                    'review_contract': 'statement-front-end.v1', 'module_file': 'Value.lean',
                    'annotation_source_sha256': None, 'visibility': 'private' if private else 'public'}
            if private:
                card['private'] = True
            nl, lean, fingerprint = _content_fingerprint(card, None)
            card.update(nl_digest=nl, lean_digest=lean, fingerprint=fingerprint,
                        fingerprint_scheme=CURRENT_FINGERPRINT_SCHEME,
                        fingerprints={CURRENT_FINGERPRINT_SCHEME: fingerprint})
            cards.append(card)
        snapshot = {'schema': SNAPSHOT_SCHEMA, 'fingerprint_scheme': CURRENT_FINGERPRINT_SCHEME,
                    'generated_at': '2026-10-01T00:00:00+00:00', 'source_commit': 'a' * 40,
                    'source_dirty': False, 'source_origin': 'archive-unverified',
                    'dependency_lock_digest': None, 'unlinked_nodes': 0,
                    'cards': cards, 'review_mode': 'statement',
                    'review_contract': 'statement-front-end.v1', 'modules': {'Value.lean': self.text},
                    'context_modules': {}, 'repository': deepcopy(self.config),
                    'enrichment_topics': deepcopy(self.config['topics']),
                    'source_environment': {'lean-toolchain': hashlib.sha256((self.source / 'lean-toolchain').read_bytes()).hexdigest(),
                                           'lakefile.lean': None, 'lakefile.toml': None, 'path_dependencies': False}}
        snapshot['digest'] = calculate_snapshot_digest(snapshot)
        validate_snapshot(snapshot)
        return snapshot

    def generated_snapshot(self, *, repository=None, anonymous=True):
        """Two modules with Lean-generated public names, and one private card."""
        source = self.root / ('anonymous-source' if anonymous else 'named-source')
        (source / 'KIP126').mkdir(parents=True, exist_ok=True)
        (source / 'lean-toolchain').write_text('leanprover/lean4:v4.32.2\n')
        modules = []
        for module in ('One', 'Two'):
            command = ('instance : Inhabited Nat := ⟨1⟩' if anonymous else
                       'instance named : Inhabited Nat := ⟨1⟩')
            text = f'namespace Example\n{command}\nend Example\n'
            file = f'KIP126/{module}.lean'
            (source / file).write_text(text)
            first = len('namespace Example\n'.encode())
            name = 'Example.instInhabitedNat' if anonymous else 'Example.named'
            declaration = {'name': name, 'compiler_name': name, 'kind': 'instance',
                           'name_generated': anonymous, 'visibility': 'public',
                           'start_byte': first, 'end_byte': first + len(command.encode()),
                           'line': 2, 'namespace': ['Example'], 'doc': '', 'fields': []}
            modules.append({'file': file, 'module': 'KIP126.' + module,
                            'imports': [], 'declarations': [declaration]})
        file = 'KIP126/Private.lean'
        command = 'private def hidden : Nat := 2'
        (source / file).write_text('namespace Example\n' + command + '\nend Example\n')
        first = len('namespace Example\n'.encode())
        modules.append({'file': file, 'module': 'KIP126.Private', 'imports': [],
                        'declarations': [{'name': 'Example.hidden',
                                          'compiler_name': '_private.KIP126.Private.0.Example.hidden',
                                          'kind': 'def', 'visibility': 'private', 'name_generated': False,
                                          'start_byte': first, 'end_byte': first + len(command.encode()),
                                          'line': 2, 'namespace': ['Example'], 'doc': '', 'fields': []}]})
        raw = {'schema': 'formaliscope-lean-semantic-index.v1', 'lean_version': '4.32.2',
               'modules': modules, 'context_modules': []}
        index = _bind_index(raw, _source_binding(source, ['KIP126']))
        return compile_statements(source, source_commit='d' * 40,
                                  repository=repository, semantic_index=index)

    def install(self, snapshot):
        path = self.root / (uuid.uuid4().hex + '.json')
        path.write_text(json.dumps(snapshot))
        return install_snapshot(path, self.data, allow_dirty_source=True)

    def judge(self, snapshot):
        card = next(card for card in snapshot['cards'] if card['id'] == self.public_id)
        status, payload = submit(snapshot, self.db, self.reviewer,
                                 {'request_id': str(uuid.uuid4()), 'card_id': card['id'],
                                  'fingerprint': card['fingerprint'], 'verdict': 'aligned',
                                  'rationale': 'existing public review'})
        self.assertEqual(status, 201)
        return payload['judgment']

    def test_unchanged_historical_public_review_survives_semantic_install(self):
        old = self.historical_snapshot()
        frozen = deepcopy(old)
        self.install(old)
        judgment = self.judge(old)
        semantic = self.semantic_snapshot()
        old_cards = {card['id']: card for card in old['cards']}
        self.assertEqual(set(old_cards), {card['id'] for card in semantic['cards']})
        for card in semantic['cards']:
            self.assertEqual(card['fingerprint'], old_cards[card['id']]['fingerprint'])
        installed, _ = self.install(semantic)
        state = review_state(installed, self.db, self.public_id, self.reviewer)
        self.assertEqual(state['current']['id'], judgment['id'])
        self.assertEqual(state['history_count'], 1)
        recalled = catalog(installed, self.db, self.reviewer, initial_id='auto')
        self.assertEqual(recalled['initial_evidence']['id'], self.public_id)
        private = next(card for card in recalled['cards'] if card['id'] == self.private_id)
        self.assertEqual(private['visibility'], 'private')
        self.assertIsNone(private['verdict'])
        self.assertEqual(old, frozen)
        validate_snapshot(old)

    def test_changed_basis_stays_pending_without_erasing_old_history(self):
        old = self.historical_snapshot()
        self.install(old)
        judgment = self.judge(old)
        self.file.write_text(self.text.replace('value : Nat := 1', 'value : Nat := 3'))
        installed, _ = self.install(self.semantic_snapshot())
        state = review_state(installed, self.db, self.public_id, self.reviewer)
        self.assertIsNone(state['current'])
        self.assertEqual(state['history_count'], 1)
        self.assertTrue(next(card for card in catalog(installed, self.db, self.reviewer)['cards']
                             if card['id'] == self.public_id)['stale'])
        with closing(connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT rationale FROM judgments WHERE id=?',
                                        (judgment['id'],)).fetchone()[0], 'existing public review')
        rollback, _ = self.install(old)
        self.assertEqual(review_state(rollback, self.db, self.public_id, self.reviewer)['current']['id'], judgment['id'])

    def test_semantic_versions_inherit_only_unchanged_context(self):
        old = self.semantic_snapshot('a')
        self.install(old)
        original = self.judge(old)
        candidate = reuse_snapshot(self.semantic_snapshot('b'), old)
        installed, report = self.install(candidate)
        self.assertEqual(report['inherited_judgments'], 1)
        current = review_state(installed, self.db, self.public_id, self.reviewer)['current']
        self.assertEqual(current['inherited_from_id'], original['id'])
        self.assertEqual(current['inherited_from_dataset'], dataset_id(old))

    def test_compiler_import_graph_freezes_multiline_local_context(self):
        # Header layout deliberately defeats the old line-pattern import reader.
        root_source = 'public\nimport\n  Dependency\ndef value : Nat := dependency\n'
        dependency_source = 'import Transitive\ndef dependency : Nat := transitive\n'
        (self.source / 'Dependency.lean').write_text(dependency_source)
        (self.source / 'Transitive.lean').write_text('def transitive : Nat := 1\n')
        imports = {'Value.lean': ['Dependency'], 'Dependency.lean': ['Transitive'], 'Transitive.lean': []}
        modules = {'Value.lean': root_source}
        context = freeze_imports(self.source, modules, module_imports=imports)
        self.assertEqual(set(context), {'Dependency.lean', 'Transitive.lean'})
        snapshot = self.historical_snapshot()
        snapshot.update(modules=modules, context_modules=context, module_imports=imports)
        card = snapshot['cards'][0]
        before = context_basis(snapshot, card)
        snapshot['context_modules']['Transitive.lean'] = 'def transitive : Nat := 2\n'
        self.assertNotEqual(context_basis(snapshot, card), before)
        incomplete = {key: value for key, value in imports.items() if key != 'Transitive.lean'}
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            freeze_imports(self.source, modules, module_imports=incomplete)
        snapshot['module_imports'] = incomplete
        self.assertIsNone(context_basis(snapshot, card))

    def test_generated_public_collisions_qualify_and_migrate_independent_reviews(self):
        legacy = self.generated_snapshot()
        public = [card for card in legacy['cards'] if card['visibility'] == 'public']
        self.assertEqual(len(public), 2)
        self.assertEqual(len({card['id'] for card in public}), 2)
        self.assertTrue(all(card['name_generated'] and card['identity_file'] == card['module_file']
                            and card['id'].endswith('::file=' + card['module_file']) for card in public))
        self.install(legacy)
        for card, verdict in zip(public, ('aligned', 'misaligned')):
            status, _ = submit(legacy, self.db, self.reviewer,
                               {'request_id': str(uuid.uuid4()), 'card_id': card['id'],
                                'fingerprint': card['fingerprint'], 'verdict': verdict,
                                'rationale': card['module_file']})
            self.assertEqual(status, 201)
        qualified = qualify_legacy(legacy)
        self.assertEqual(qualified['repository']['roots'],
                         legacy['semantic_index']['provenance']['roots'])
        by_alias = {card['legacy_card_id']: card for card in qualified['cards']}
        for card in public:
            self.assertEqual(by_alias[card['id']]['id'],
                             card['id'].replace('statement::', 'statement::kip126::', 1))
        private = next(card for card in legacy['cards'] if card['visibility'] == 'private')
        self.assertEqual(by_alias[private['id']]['legacy_card_id'], private['id'])
        self.assertTrue(by_alias[private['id']]['id'].endswith('::file=' + private['module_file']))
        validate_snapshot(qualified)
        installed, _ = self.install(make_collection([legacy]))
        current = select_dataset(installed)
        for card, verdict in zip(public, ('aligned', 'misaligned')):
            state = review_state(current, self.db, by_alias[card['id']]['id'], self.reviewer)
            self.assertEqual(state['current']['verdict'], verdict)
            self.assertEqual(state['current']['rationale'], card['module_file'])
            self.assertEqual(review_state(legacy, self.db, card['id'], self.reviewer)['current']['verdict'], verdict)
        recalled = catalog(current, self.db, self.reviewer, initial_id='auto')
        self.assertEqual(recalled['initial_evidence']['visibility'], 'public')
        self.assertEqual(sum(card['visibility'] == 'public' for card in recalled['cards']), 2)
        validate_snapshot(legacy)

    def test_generated_public_collisions_remain_public_with_repository_configuration(self):
        config = {'id': 'alpha', 'name': 'Alpha', 'roots': ['KIP126']}
        snapshot = self.generated_snapshot(repository=config)
        public = [card for card in snapshot['cards'] if card['visibility'] == 'public']
        self.assertEqual(len(public), 2)
        self.assertTrue(all(card['id'].startswith('statement::alpha::') and
                            card.get('private') is not True for card in public))
        validate_snapshot(snapshot)

    def test_explicit_named_public_collisions_remain_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ambiguous|duplicate'):
            self.generated_snapshot(anonymous=False)

    def test_historical_private_plain_alias_survives_file_qualified_migration(self):
        legacy = self.historical_snapshot()
        legacy.pop('repository')
        for card in legacy['cards']:
            card['id'] = 'statement::' + card['declaration']
        legacy['digest'] = calculate_snapshot_digest(legacy)
        private = next(card for card in legacy['cards'] if card.get('private'))
        self.install(legacy)
        status, _ = submit(legacy, self.db, self.reviewer,
                           {'request_id': str(uuid.uuid4()), 'card_id': private['id'],
                            'fingerprint': private['fingerprint'], 'verdict': 'aligned',
                            'rationale': 'historical private decision'})
        self.assertEqual(status, 201)
        qualified = qualify_legacy(legacy)
        current = next(card for card in qualified['cards'] if card.get('private'))
        self.assertEqual(current['legacy_card_id'], 'statement::Example.hidden')
        self.assertEqual(current['id'], 'statement::kip126::Example.hidden::file=Value.lean')
        validate_snapshot(qualified)
        installed, _ = self.install(make_collection([legacy]))
        state = review_state(select_dataset(installed), self.db, current['id'], self.reviewer)
        self.assertEqual(state['current']['rationale'], 'historical private decision')
        self.assertEqual(review_state(legacy, self.db, private['id'], self.reviewer)['current']['rationale'],
                         'historical private decision')


if __name__ == '__main__':
    unittest.main()
