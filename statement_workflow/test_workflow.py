import json
from pathlib import Path
import sys
import tempfile
import unittest

from review_app.statements import compile_statements
from .engine import config, digest, import_result, job, prepare, read, run, statement_source, validate_result, write


def answer(request):
    common = {key: request[key] for key in ('job_id', 'declaration_id', 'context_fingerprint')}
    common['model'] = 'fixture-only-no-model-call'
    if request['stage'] == 'readback':
        common.update(status='incomplete', text='[测试夹具] 待补充语义上下文，不是数学回译。',
                      binder_coverage=[], definition_expansions=[], edge_cases=[], unresolved=['not elaborated'],
                      risk_candidates=[], evidence_ids=[])
    else:
        common.update(coverage='unknown', alignment='no_reference', findings=[], unresolved=['not elaborated'])
    return common


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'KIP126/Main/Solution/Final').mkdir(parents=True)
        self.source = self.repo / 'KIP126/Main/Solution/Final/Main.lean'
        self.source.write_text('namespace Demo\n/-- INTENT_SECRET --/\ndef custom : Nat := 2\n'
                               'theorem main : custom = 2 := by sorry\nend Demo\n')
        self.snapshot_path = self.root / 'snapshot.json'
        write(self.snapshot_path, compile_statements(self.repo, source_commit='a'*40))
        self.cfg = config()
        self.batch = self.root / 'batch'

    def prepare(self, **kwargs):
        return prepare(self.snapshot_path, self.batch, self.cfg, [], **kwargs)

    def runner(self, *, bad_audit=False):
        path = self.root / 'fixture.py'
        log = self.root / 'calls.jsonl'
        # This is a subprocess protocol fixture, not a simulated Lean or LLM.
        path.write_text('import json, sys\n'
                        'from pathlib import Path\n'
                        'request=json.load(sys.stdin)\n'
                        'with Path(sys.argv[1]).open("a") as f: f.write(json.dumps(request)+"\\n")\n'
                        + ('if request["stage"] == "audit": raise SystemExit(2)\n' if bad_audit else '') +
                        'common={k:request[k] for k in ("job_id","declaration_id","context_fingerprint")}\n'
                        'common["model"]="fixture-only-no-model-call"\n'
                        'if request["stage"]=="readback":\n'
                        ' common.update(status="incomplete",text="[TEST] no mathematical translation",'
                        'binder_coverage=[],definition_expansions=[],edge_cases=[],unresolved=["not elaborated"],'
                        'risk_candidates=[],evidence_ids=[])\n'
                        'else:\n'
                        ' common.update(coverage="unknown",alignment="no_reference",findings=[],unresolved=[])\n'
                        'print(json.dumps(common))\n')
        self.cfg.update(agent_command=[sys.executable, str(path), str(log)], agent_revision='fixture.v1')
        return log

    def test_blind_packet_and_statement_body_isolation(self):
        manifest = self.prepare()
        folder = self.batch / 'tasks' / manifest['tasks'][0]['id']
        task = read(folder / 'task.json')
        task['reference'] = {'text': 'REFERENCE_SECRET', 'source': 'example'}
        request = job(task, 'readback')
        encoded = json.dumps(request)
        self.assertNotIn('INTENT_SECRET', encoded)
        self.assertNotIn('REFERENCE_SECRET', encoded)
        self.assertNotIn('by sorry', encoded)
        self.assertNotIn('proof_source_status', request)
        self.assertNotIn('role_hint', request)
        self.assertTrue(request['fresh_context_required'])
        row = {'kind':'theorem','declaration':'X','lean':{'source':'theorem X (h : (let x := 1; x = 1)) : True := by sorry'}}
        self.assertIn('let x := 1', statement_source(row))
        self.assertNotIn('by sorry', statement_source(row))
        row['lean']['source'] = 'theorem somewhere : True := by sorry'
        self.assertEqual(statement_source(row), 'theorem somewhere : True')
        row['kind'] = 'def'
        row['lean']['source'] = 'def s := "-- literal /-" /- outer /- inner -/ SECRET -/'
        self.assertEqual(statement_source(row), 'def s := "-- literal /-"')

    def test_resume_limit_does_not_get_stuck_on_cached_tasks(self):
        log = self.runner()
        self.prepare()
        first = run(self.batch, self.cfg, limit=1)
        self.assertEqual(first['completed'], 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        second = run(self.batch, self.cfg, limit=1)
        self.assertEqual(second['completed'], 2)
        self.assertEqual(len(log.read_text().splitlines()), 4)
        run(self.batch, self.cfg, limit=1)
        self.assertEqual(len(log.read_text().splitlines()), 4)
        self.cfg['agent_revision'] = 'fixture.v2'
        run(self.batch, self.cfg, limit=1)
        self.assertEqual(len(log.read_text().splitlines()), 6)
        analyses = read(self.batch / 'analysis.json')['analyses']
        self.assertEqual(len(analyses), 1)
        self.assertEqual(analyses[0]['risk']['level'], 'unknown')
        self.assertTrue(analyses[0]['risk']['analysis_incomplete'])
        self.assertEqual(analyses[0]['human_review'], 'not_assessed')

    def test_dependencies_invalidate_context_but_proof_only_change_does_not(self):
        first = self.prepare()
        main_id = next(t['id'] for t in first['tasks'] if t['declaration_id'] == 'statement::Demo.main')
        original = read(self.batch / 'tasks' / main_id / 'task.json')['context_fingerprint']
        self.source.write_text(self.source.read_text().replace('by sorry', 'by rfl'))
        write(self.snapshot_path, compile_statements(self.repo, source_commit='b'*40))
        self.batch = self.root / 'proof-change'
        self.prepare()
        proof_hash = read(self.batch / 'tasks' / main_id / 'task.json')['context_fingerprint']
        self.assertEqual(original, proof_hash)
        self.source.write_text(self.source.read_text().replace('Nat := 2', 'Nat := 3'))
        write(self.snapshot_path, compile_statements(self.repo, source_commit='c'*40))
        self.batch = self.root / 'definition-change'
        self.prepare()
        new_hash = read(self.batch / 'tasks' / main_id / 'task.json')['context_fingerprint']
        self.assertNotEqual(original, new_hash)

    def test_results_reject_stale_identity_unattributed_findings_and_human_verdicts(self):
        manifest = self.prepare()
        task = read(self.batch / 'tasks' / manifest['tasks'][0]['id'] / 'task.json')
        request = job(task, 'readback')
        for field in ('job_id', 'context_fingerprint', 'declaration_id'):
            value = answer(request)
            value[field] = 'wrong'
            with self.assertRaises(ValueError): validate_result(value, request)
        value = answer(request)
        value['risk_candidates'] = [{'evidence_ids':['unseen']}]
        with self.assertRaises(ValueError): validate_result(value, request)
        value['risk_candidates'] = [{'evidence_ids':[]}]
        with self.assertRaises(ValueError): validate_result(value, request)
        value = answer(request)
        value['verdict'] = 'aligned'
        with self.assertRaises(ValueError): validate_result(value, request)
        audit = job(task, 'audit', answer(request))
        value = answer(audit)
        value['alignment'] = 'no_discrepancy_detected'
        with self.assertRaises(ValueError): validate_result(value, audit)

    def test_bounded_failure_keeps_readback_and_drops_stale_exports(self):
        log = self.runner(bad_audit=True)
        self.prepare()
        for _ in range(4): run(self.batch, self.cfg, limit=1)
        requests = [json.loads(line) for line in log.read_text().splitlines()]
        first_id = requests[0]['declaration_id']
        first_requests = [r for r in requests if r['declaration_id']==first_id]
        self.assertEqual(sum(r['stage']=='readback' for r in first_requests), 1)
        self.assertEqual(sum(r['stage']=='audit' for r in first_requests), 3)
        self.assertTrue(any(r['declaration_id'] != first_id for r in requests))
        self.assertEqual(read(self.batch / 'analysis.json')['analyses'], [])

    def test_manual_export_import_progresses_to_audit_and_analysis(self):
        manifest = self.prepare()
        run(self.batch, self.cfg, export_only=True)
        folder = self.batch / 'tasks' / manifest['tasks'][0]['id']
        request = read(folder / 'readback.job.json')
        response = self.root / 'response.json'
        write(response, answer(request))
        import_result(self.batch, self.cfg, 'readback', response)
        self.assertTrue((folder / 'audit.job.json').exists())
        write(response, answer(read(folder / 'audit.job.json')))
        import_result(self.batch, self.cfg, 'audit', response)
        self.assertEqual(len(read(self.batch / 'analysis.json')['analyses']), 1)
        self.assertEqual(read(folder / 'analysis.json')['risk']['level'], 'unknown')

    def test_reference_is_source_bound_and_only_in_audit(self):
        snapshot = read(self.snapshot_path)
        card = next(c for c in snapshot['cards'] if c['declaration']=='Demo.main')
        path = self.root / 'reference.json'
        write(path, {card['id']:{'source_sha256':'stale','text':'SECRET','source':'notes'}})
        with self.assertRaises(ValueError): self.prepare(references_path=path)
        import hashlib
        write(path, {card['id']:{'source_sha256':hashlib.sha256(card['lean']['source'].encode()).hexdigest(),
                                'text':'REFERENCE_SECRET','source':'notes'}})
        manifest = self.prepare(references_path=path)
        item = next(t for t in manifest['tasks'] if t['declaration_id']==card['id'])
        task = read(self.batch / 'tasks' / item['id'] / 'task.json')
        self.assertNotIn('REFERENCE_SECRET', json.dumps(job(task, 'readback')))
        self.assertIn('REFERENCE_SECRET', json.dumps(job(task, 'audit', {})))

    def test_tampered_context_and_changed_batch_settings_are_rejected(self):
        manifest = self.prepare()
        folder = self.batch / 'tasks' / manifest['tasks'][0]['id']
        task = read(folder / 'task.json')
        task['context']['evidence'][0]['source'] += '\n-- changed'
        write(folder / 'task.json', task)
        with self.assertRaises(ValueError): run(self.batch, self.cfg, export_only=True)
        self.cfg['max_context_nodes'] += 1
        with self.assertRaises(ValueError): run(self.batch, self.cfg, export_only=True)


if __name__ == '__main__':
    unittest.main()
