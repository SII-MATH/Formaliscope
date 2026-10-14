"""Readback updates preserve decisions and drafts without accepting changed Lean."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from .build import (CURRENT_FINGERPRINT_SCHEME, STATEMENT_REVIEW_SCHEME,
                    _content_fingerprint, calculate_snapshot_digest)
from .database import initialize
from .enrichment import enrich_snapshot
from .judgments import (backfill_review_basis, catalog, history, review_state,
                        reviewer_export, save_draft, submit)
from .statements import review_evidence
from .storage import install_snapshot
from .test_enrichment_v2 import fixture_document
from .test_source_fixtures import compile_statements


class ReadbackInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source/KIP126'
        source.mkdir(parents=True)
        (source / 'X.lean').write_text('def X : Nat := 1\n')
        self.base = compile_statements(source.parent, source_commit='a' * 40)
        self.old = enrich_snapshot(self.base, fixture_document(self.base))
        self.card = self.old['cards'][0]
        self.data = self.root / 'runtime'
        self.db = self.data / 'judgments.sqlite3'
        initialize(self.db)
        self.reviewer = 'u_' + '1' * 32
        document = fixture_document(self.old)
        for row in (document['annotations'][0], document['originals'][self.card['id']]):
            row['readback'].update(text_zh='更新后的中文回译。', text_en='Updated English readback.')
        self.new = enrich_snapshot(self.old, document)

    def payload(self, snapshot, **extra):
        return {'request_id': str(uuid.uuid4()), 'card_id': self.card['id'],
                'fingerprint': snapshot['cards'][0]['fingerprint'], 'verdict': 'aligned',
                'rationale': '原人工意见', **extra}

    def install(self, snapshot, name):
        path = self.root / name
        path.write_text(json.dumps(snapshot))
        return install_snapshot(path, self.data, allow_dirty_source=True)

    def test_readbacks_keep_judgment_provenance_exports_and_history(self):
        code, saved = submit(self.old, self.db, self.reviewer, self.payload(self.old))
        self.assertEqual(code, 201)
        row = saved['judgment']
        self.assertEqual(row['review_basis_scheme'], STATEMENT_REVIEW_SCHEME)
        self.assertNotEqual(self.card['fingerprint'], self.new['cards'][0]['fingerprint'])
        state = review_state(self.new, self.db, self.card['id'], self.reviewer)
        self.assertEqual({key: state['current'][key] for key in row}, row)
        self.assertEqual(catalog(self.new, self.db, self.reviewer)['cards'][0]['verdict'], 'aligned')
        self.assertEqual(reviewer_export(self.new, self.db, self.reviewer, mode='latest')['judgments'][0]['id'], row['id'])
        self.assertEqual(history(self.db, self.card['id'], self.reviewer)[0]['snapshot_digest'], self.old['digest'])
        exposed = review_evidence(self.new['cards'][0], self.new)
        self.assertEqual(exposed['fingerprints'][STATEMENT_REVIEW_SCHEME], row['review_basis_fingerprint'])
        self.assertEqual(submit(self.new, self.db, self.reviewer, self.payload(self.old))[0], 409)

    def test_install_promotes_legacy_matching_records_before_switch(self):
        self.install(self.old, 'old.json')
        _, saved = submit(self.old, self.db, self.reviewer, self.payload(self.old))
        _, draft = save_draft(self.old, self.db, self.reviewer, self.payload(self.old, revision=0, rationale='未完草稿'))
        with sqlite3.connect(self.db) as db:
            for table in ('judgments', 'review_drafts'):
                db.execute(f'UPDATE {table} SET review_basis_scheme=?, review_basis_fingerprint=fingerprint',
                           (CURRENT_FINGERPRINT_SCHEME,))
        self.install(self.new, 'new.json')
        state = review_state(self.new, self.db, self.card['id'], self.reviewer)
        self.assertEqual(state['current']['id'], saved['judgment']['id'])
        self.assertEqual(state['current']['snapshot_digest'], self.old['digest'])
        self.assertEqual(state['draft']['id'], draft['draft']['id'])
        self.assertEqual(state['draft']['rationale'], '未完草稿')
        self.assertEqual(backfill_review_basis(self.db, self.new), 0)

    def test_draft_remains_editable_and_revision_conflicts_are_preserved(self):
        _, first = save_draft(self.old, self.db, self.reviewer, self.payload(self.old, revision=0))
        self.assertEqual(review_state(self.new, self.db, self.card['id'], self.reviewer)['draft_revision'], 1)
        request = self.payload(self.new, revision=1, rationale='继续填写')
        code, second = save_draft(self.new, self.db, self.reviewer, request)
        self.assertEqual(code, 200)
        self.assertEqual(second['draft']['id'], first['draft']['id'])
        self.assertEqual(second['draft']['revision'], 2)
        self.assertEqual(save_draft(self.new, self.db, self.reviewer, request)[0], 200)
        self.assertEqual(save_draft(self.new, self.db, self.reviewer, self.payload(self.new, revision=1))[0], 409)
        code, completed = submit(self.new, self.db, self.reviewer,
                                 self.payload(self.new, draft_revision=2, rationale='继续填写'))
        self.assertEqual(code, 201)
        self.assertEqual(completed['judgment']['rationale'], '继续填写')
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_drafts').fetchone()[0], 1)

    def test_unchanged_completion_does_not_duplicate_after_readback_update(self):
        _, saved = submit(self.old, self.db, self.reviewer, self.payload(self.old))
        save_draft(self.old, self.db, self.reviewer, self.payload(self.old, revision=0))
        code, result = submit(self.new, self.db, self.reviewer, self.payload(self.new, draft_revision=1))
        self.assertEqual(code, 200)
        self.assertEqual(result['judgment']['id'], saved['judgment']['id'])
        self.assertEqual(review_state(self.new, self.db, self.card['id'], self.reviewer)['history_count'], 1)

    def test_changed_lean_is_stale_and_cannot_be_rebound_on_startup(self):
        submit(self.old, self.db, self.reviewer, self.payload(self.old))
        save_draft(self.old, self.db, self.reviewer, self.payload(self.old, revision=0))
        changed = deepcopy(self.new)
        card = changed['cards'][0]
        card['lean']['source'] = 'def X : Nat := 2'
        nl, lean, fp = _content_fingerprint(card, None)
        card.update(nl_digest=nl, lean_digest=lean, fingerprint=fp,
                    fingerprints={CURRENT_FINGERPRINT_SCHEME: fp})
        changed['digest'] = calculate_snapshot_digest(changed)
        self.assertEqual(backfill_review_basis(self.db, changed), 0)
        state = review_state(changed, self.db, self.card['id'], self.reviewer)
        self.assertIsNone(state['current'])
        self.assertIsNone(state['draft'])
        self.assertEqual(state['history_count'], 1)
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE judgments SET review_basis_scheme=?, review_basis_fingerprint=fingerprint',
                       (CURRENT_FINGERPRINT_SCHEME,))
        self.assertEqual(backfill_review_basis(self.db, changed), 0)

    def test_changed_reference_invalidates_but_relocation_does_not(self):
        ref = {'title': 'Expected', 'statement': 'One.', 'declarations': ['X'],
               'blueprint_file': 'blueprint/a.tex', 'blueprint_line': 1, 'label': 'X', 'chapter': 'Test'}
        old = deepcopy(self.old)
        old['cards'][0]['blueprint_references'] = [ref]
        submit(old, self.db, self.reviewer, self.payload(old))
        moved = deepcopy(old)
        moved['cards'][0]['blueprint_references'][0].update(blueprint_file='blueprint/b.tex', blueprint_line=99)
        self.assertIsNotNone(review_state(moved, self.db, self.card['id'], self.reviewer)['current'])
        moved['cards'][0]['blueprint_references'][0]['statement'] = 'Two.'
        self.assertIsNone(review_state(moved, self.db, self.card['id'], self.reviewer)['current'])
