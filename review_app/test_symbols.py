from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .database import initialize
from .preview import PreviewAuthStore
from .server import ReviewHTTPServer, make_handler
from .statements import compile_statements
from .symbols import NAME, SymbolIndex


class SymbolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'KIP126').mkdir()
        self.source = '''namespace A
def same : Nat := 1
structure Rec where
  law : True
namespace Inner
theorem demo (x : A.Rec) (α : Type) : True := by
  let y := x
  have h : True := by trivial
  exact h
end Inner
end A
namespace B
def same : Nat := 2
end B
namespace C
open A B
def qualified := A.same
def ambiguous := same
def unknown := Nat.add
def literal := "A.same"
/- nested /- A.same -/ B.same -/
end C
namespace D
section
variable (n : Nat)
def useVariable := n
end
def outsideVariable := n
def simpleLambda := fun z => z
end D
'''
        (self.root / 'KIP126/Trace.lean').write_text(self.source)
        self.snapshot = compile_statements(self.root, source_commit='a' * 40)
        self.cards = {card['declaration']: card for card in self.snapshot['cards']}
        self.index = SymbolIndex(self.snapshot)

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self, declaration, token, *, occurrence=-1, scope='declaration'):
        card = self.cards[declaration]
        source = self.source if scope == 'module' else card['lean']['source']
        offsets = [match.start() for match in NAME.finditer(source) if match[0] == token]
        offset = offsets[occurrence]
        base = 1 if scope == 'module' else card['lean']['line']
        line = base + source.count('\n', 0, offset)
        column = offset - source.rfind('\n', 0, offset)
        return self.index.resolve(card['id'], token, line, column, scope)

    def test_exact_qualified_definition(self):
        value = self.resolve('C.qualified', 'A.same')
        self.assertEqual(value['status'], 'definition')
        self.assertEqual(value['target']['card_id'], self.cards['A.same']['id'])
        self.assertEqual(value['target']['column'], 5)

    def test_current_namespace_and_explicit_root(self):
        card = self.cards['A.same']
        value = self.index.resolve(card['id'], 'same', 2, 5)
        self.assertEqual(value['target']['declaration'], 'A.same')
        self.snapshot['cards'].append({**card, 'id': 'root', 'declaration': 'same'})
        source = 'def root := _root_.same'
        self.snapshot['cards'].append({'id':'test', 'declaration':'A.root', 'lean': {'file':'Missing', 'line':1, 'source':source}})
        value = SymbolIndex(self.snapshot).resolve('test', '_root_.same', 1, 13)
        self.assertEqual(value['target']['card_id'], 'root')

    def test_ambiguous_opened_names_never_choose_first(self):
        value = self.resolve('C.ambiguous', 'same')
        self.assertEqual(value['status'], 'ambiguous')
        self.assertEqual([item['declaration'] for item in value['candidates']], ['A.same', 'B.same'])

    def test_unqualified_unique_suffix_remains_explicit_choice(self):
        self.snapshot['cards'].append({'id':'test','declaration':'E.t', 'lean': {'file':'Missing','line':1,'source':'def t := Rec'}})
        value = SymbolIndex(self.snapshot).resolve('test','Rec',1,10)
        self.assertEqual(value['status'],'ambiguous')
        self.assertEqual(value['candidates'][0]['declaration'],'A.Rec')

    def test_parameters_unicode_and_tactic_bindings(self):
        value = self.resolve('A.Inner.demo', 'x')
        self.assertEqual(value['status'], 'local')
        self.assertEqual(value['target']['line'], 6)
        self.assertEqual(value['target']['column'], 15)
        value = self.resolve('A.Inner.demo','h')
        self.assertEqual(value['status'],'local')
        self.assertEqual(value['target']['line'],8)
        value = self.resolve('A.Inner.demo','α')
        self.assertEqual(value['status'],'local')
        self.assertEqual(value['target']['line'],6)

    def test_section_variables_and_simple_lambda(self):
        value = self.resolve('D.useVariable', 'n')
        self.assertEqual(value['status'],'local')
        self.assertEqual(value['target']['scope'],'module')
        self.assertEqual(value['target']['line'],25)
        self.assertEqual(self.resolve('D.outsideVariable','n')['status'],'unresolved')
        self.assertEqual(self.resolve('D.simpleLambda','z')['status'],'local')

    def test_shadow_binding_is_unavailable_in_its_own_rhs_and_branch_bindings_expire(self):
        source = 'theorem t (x : Nat) : True := by\n  let x := x\n  have h : True := by\n    intro branch\n    exact h\n  exact x'
        card = {'id':'local','declaration':'D.t','lean':{'file':'Missing','line':1,'source':source}}
        index = SymbolIndex({'cards':[card]})
        value = index.resolve('local','x',2,12)
        self.assertEqual(value['target']['line'],1)
        self.assertEqual(index.resolve('local','h',5,11)['status'],'unresolved')
        self.assertEqual(index.resolve('local','x',6,9)['target']['line'],2)
        source = 'theorem t (f : ∀ (hidden : Nat), Nat) : ∀ (result : Nat), Nat := by\n  exact result'
        card['lean']['source'] = source
        self.assertEqual(SymbolIndex({'cards':[card]}).resolve('local','result',2,9)['status'],'unresolved')

    def test_module_uses_clicked_declaration_not_current_card(self):
        value = self.resolve('C.unknown', 'h', scope='module')
        self.assertEqual(value['status'], 'local')
        self.assertEqual(value['target']['card_id'], self.cards['A.Inner.demo']['id'])

    def test_structure_projection(self):
        card = {'id':'test','declaration':'E.t', 'lean': {'file':'Missing','line':1,'source':'def t := A.Rec.law'}}
        self.snapshot['cards'].append(card)
        value = SymbolIndex(self.snapshot).resolve('test','A.Rec.law',1,10)
        self.assertEqual(value['target']['card_id'], self.cards['A.Rec']['id'])
        self.assertEqual(value['target']['line'],4)
        self.assertEqual(value['target']['column'],3)

    def test_global_receiver_projection_is_explicit(self):
        card = {'id':'test','declaration':'E.t', 'lean': {'file':'Missing','line':1,'source':'def t := A.same.field'}}
        self.snapshot['cards'].append(card)
        value = SymbolIndex(self.snapshot).resolve('test','A.same.field',1,10)
        self.assertEqual(value['target']['declaration'],'A.same')
        self.assertIn('投影对象 A.same',value['message'])

    def test_unindexed_external_names_and_invalid_positions(self):
        value = self.resolve('C.unknown','Nat.add')
        self.assertEqual(value['status'],'unresolved')
        self.assertIn('Mathlib',value['message'])
        with self.assertRaises(ValueError):
            self.resolve('C.literal','A.same')
        with self.assertRaises(ValueError):
            self.index.resolve(self.cards['C.qualified']['id'],'A.same',999,1)
        with self.assertRaises(ValueError):
            self.index.resolve(self.cards['C.qualified']['id'],'A.same',17,18,'../../secret')


class SymbolHTTPTests(unittest.TestCase):
    setUp = SymbolTests.setUp
    tearDown = SymbolTests.tearDown
    def test_symbol_endpoint_requires_session_and_validates_position(self):
        db = self.root / 'review.sqlite3'
        initialize(db)
        auth = PreviewAuthStore(db)
        handler = make_handler(self.snapshot, db, Path(__file__).parent/'static', auth, preview=True)
        handler.log_message = lambda *args: None
        server = ReviewHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        path = '/api/symbol?' + urlencode({'card_id':self.cards['C.qualified']['id'], 'name':'A.same', 'line':17, 'column':18})
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(base+path, timeout=5)
            self.assertEqual(error.exception.code, 401)
            token = auth.create_reviewer('测试追溯')[0]
            with urlopen(Request(base+path, headers={'Cookie': f'kip126_review_session={token}'}), timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'"status": "definition"', response.read())
            for invalid, expected in [(path.replace('line=17','line=999'),400),
                                      ('/api/symbol?card_id=missing&name=x&line=1&column=1',404)]:
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+invalid, headers={'Cookie': f'kip126_review_session={token}'}), timeout=5)
                self.assertEqual(error.exception.code, expected)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
