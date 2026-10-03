from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .build import validate_snapshot
from .statements import compile_statements
from .server import ReviewHTTPServer, initialize, make_handler
from .preview import PreviewAuthStore


class StatementBuildTests(unittest.TestCase):
    def test_full_source_base_and_candidate_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source/'KIP126/Main/Solution/Final').mkdir(parents=True)
            (source/'KIPBase').mkdir()
            (source/'KIPBase/Base.lean').write_text('namespace KIPBase\nstructure Record where\n  law :\n    True\nend KIPBase\n')
            (source/'KIP126/Main/Solution/Final/Main.lean').write_text(
                'namespace KIP126\n/- fake\n theorem commented : False := sorry\n/- nested -/ -/\n'
                'def text := "theorem string : False := sorry"\n'
                '@[simp] theorem main (x : KIPBase.Record) : True := by\n'+
                '\n'.join('  -- line '+str(i) for i in range(140))+'\n  exact x.law\nend KIP126\n')
            snapshot = compile_statements(source, source_commit='a'*40)
            cards = {c['declaration']:c for c in snapshot['cards']}
            self.assertEqual(set(cards), {'KIPBase.Record','KIP126.text','KIP126.main'})
            main = cards['KIP126.main']
            self.assertIn('exact x.law',main['lean']['source'])
            self.assertFalse(main['lean']['truncated'])
            self.assertEqual(main['dependencies'], ['statement::KIPBase.Record'])
            self.assertEqual(main['role'],'主定理')
            self.assertEqual(cards['KIPBase.Record']['role'],'直接依赖')
            self.assertEqual(cards['KIPBase.Record']['fields'], [{'name':'law','type':'True'}])
            validate_snapshot(snapshot)
            snapshot['modules']['KIPBase/Base.lean'] += '\nchanged'
            with self.assertRaises(ValueError):
                validate_snapshot(snapshot)

    def test_source_bound_annotations_fail_when_lean_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'KIP126').mkdir()
            lean=root/'KIP126/Sample.lean';lean.write_text('def value : Nat := 1\n')
            annotations=root/'annotations.json'
            annotations.write_text(json.dumps({'value': {'source_sha256':hashlib.sha256(b'def value : Nat := 1').hexdigest(),'statement':'The value is one.'}}))
            snapshot=compile_statements(root,source_commit='a'*40,annotations=annotations)
            self.assertEqual(snapshot['cards'][0]['statement_origin'],'backtranslation')
            lean.write_text('def value : Nat := 2\n')
            with self.assertRaises(ValueError):
                compile_statements(root,source_commit='a'*40,annotations=annotations)


class StatementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'reviews.sqlite3';initialize(self.db)
        self.auth=PreviewAuthStore(self.db)
        self.card={'id':'statement::X','declaration':'X','fingerprint':'a'*64,
                   'fingerprint_scheme':'kip126-review-content.v1',
                   'fingerprints':{'kip126-review-content.v1':'a'*64},
                   'label':'X','title':'X','chapter':'Test','kind':'def','source_status':'local',
                   'dependencies':[], 'priority':50,'role':'数学对象','risk':'normal'}
        self.snapshot={'schema':'kip126-review-snapshot.v2','cards':[self.card],
                       'review_mode':'statement','digest':'b'*64,'source_commit':'c'*40,'unlinked_nodes':0}
        handler=make_handler(self.snapshot,self.db,Path(__file__).parent/'static',self.auth,preview=True)
        handler.log_message=lambda *args:None
        self.server=ReviewHTTPServer(('127.0.0.1',0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,path,body=None,cookie=None):
        headers={'Origin':self.base}
        if body is not None:headers['Content-Type']='application/json'
        if cookie:headers['Cookie']=cookie
        req=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:response=urlopen(req,timeout=5)
        except HTTPError as error:response=error
        with response:
            raw=response.read();return response.status,dict(response.headers),json.loads(raw) if response.headers['Content-Type'].startswith('application/json') else raw

    def login(self,name):
        status,headers,_=self.request('/api/preview/session',{'display_name':name})
        self.assertEqual(status,201);return headers['Set-Cookie'].split(';',1)[0]

    def test_names_are_independent_and_only_admin_sees_summary(self):
        first=self.login('同名');second=self.login('同名')
        identity_a=self.request('/api/auth/me',cookie=first)[2];identity_b=self.request('/api/auth/me',cookie=second)[2]
        self.assertNotEqual(identity_a['email'],identity_b['email'])
        self.assertTrue(identity_a['is_admin']);self.assertFalse(identity_b['is_admin'])
        payload={'request_id':str(uuid.uuid4()),'card_id':self.card['id'],'fingerprint':self.card['fingerprint'],'verdict':'uncertain','rationale':''}
        self.assertEqual(self.request('/api/judgments',payload,first)[0],201)
        self.assertEqual(self.request('/api/catalog',cookie=second)[2]['cards'][0]['verdict'],None)
        self.assertEqual(self.request('/api/history?id=statement%3A%3AX',cookie=second)[2]['history'],[])
        self.assertEqual(self.request('/api/admin/summary',cookie=second)[0],403)
        summary=self.request('/api/admin/summary',cookie=first)[2]
        self.assertEqual(len(summary['judgments']),1)
        self.assertEqual(summary['judgments'][0]['display_name'],'同名')
        self.assertEqual(self.request('/api/judgments',{**payload,'request_id':str(uuid.uuid4()),'verdict':'partial'},second)[0],400)
        self.assertEqual(self.request('/api/profile',{'display_name':'新姓名'},second)[0],200)
        self.assertEqual(self.request('/api/auth/me',cookie=second)[2]['display_name'],'新姓名')
        self.assertFalse(self.request('/api/auth/me',cookie=second)[2]['is_admin'])

    def test_name_only_preview_route_is_disabled_in_normal_server(self):
        handler=make_handler(self.snapshot,self.db,Path(__file__).parent/'static',self.auth)
        handler.log_message=lambda *args:None
        server=ReviewHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        old_base=self.base;self.base=f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual(self.request('/api/config')[2]['preview'],False)
            self.assertEqual(self.request('/api/preview/session',{'display_name':'伪造'})[0],404)
            self.assertEqual(self.request('/api/preview/resume',{'resume_key':'x'*43})[0],404)
            self.assertEqual(self.request('/api/catalog')[0],401)
        finally:
            self.base=old_base;server.shutdown();server.server_close();thread.join()

    def test_preview_identity_can_resume_after_logout_without_name_collision(self):
        _, headers, result = self.request('/api/preview/session', {'display_name':'同名'})
        cookie = headers['Set-Cookie'].split(';',1)[0]
        first = self.request('/api/auth/me',cookie=cookie)[2]
        self.request('/api/auth/logout',{},cookie)
        self.assertEqual(self.request('/api/auth/me',cookie=cookie)[0],401)
        self.login('同名')
        self.assertEqual(self.request('/api/preview/resume',{'resume_key':'bad'})[0],401)
        status, headers, _ = self.request('/api/preview/resume',{'resume_key':result['resume_key']})
        self.assertEqual(status,200)
        resumed=headers['Set-Cookie'].split(';',1)[0]
        self.assertEqual(self.request('/api/auth/me',cookie=resumed)[2]['email'],first['email'])
        self.assertTrue(self.request('/api/auth/me',cookie=resumed)[2]['is_admin'])


if __name__=='__main__':
    unittest.main()
