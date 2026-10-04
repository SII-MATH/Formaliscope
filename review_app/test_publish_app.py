"""Execute the real publishing step without contacting GitHub."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublishAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.git('init')
        self.git('config','user.name','Release test')
        self.git('config','user.email','release@example.test')
        (self.repo/'feature.txt').write_text('original\n')
        self.git('add','feature.txt')
        self.git('commit','-m','original feature')
        self.git('tag','v0.0.1')
        (self.repo/'feature.txt').write_text('updated\n')
        self.git('commit','-am','Save reviews automatically')
        self.commit = self.git('rev-parse','HEAD')
        (self.repo/'deploy').mkdir()
        shutil.copyfile(ROOT/'deploy/formaliscope_release.py',self.repo/'deploy/formaliscope_release.py')
        self.runner = self.root/'runner'
        self.runner.mkdir()
        (self.runner/'releases.json').write_text(json.dumps([[{
            'tag_name':'v0.0.1','published_at':'2026-10-03T00:00:00Z',
            'draft':False,'prerelease':False}]]))
        self.assets = self.runner/'app-release'
        self.assets.mkdir()
        archive = f'formaliscope-app-{self.commit}.tar.gz'
        for name in (archive,archive+'.sha256','formaliscope-app-manifest.json'):
            (self.assets/name).write_text('verified fixture\n')
        self.binary = self.root/'bin'
        self.binary.mkdir()
        stub = self.binary/'gh'
        stub.write_text('''#!'''+sys.executable+'''
import json, os
from pathlib import Path
import sys
state_file=Path(os.environ['FAKE_GH_STATE'])
state=json.loads(state_file.read_text())
args=sys.argv[1:]
state['calls'].append(args)
state_file.write_text(json.dumps(state))
operation=args[1]
if operation=='view':
    if state['draft'] is None:raise SystemExit(1)
    print(json.dumps({'isDraft':state['draft']}))
elif operation=='create' or (operation=='edit' and '--notes-file' in args):
    notes=Path(args[args.index('--notes-file')+1]).read_text()
    state['notes']=notes
    state['draft']=True
elif operation=='upload':
    for filename in args[3:]:
        if filename.startswith('--'):continue
        assert Path(filename).is_file()
    state['uploaded']=True
elif operation=='edit' and '--draft=false' in args:
    assert state['uploaded']
    assert '## Changelog' in state['notes']
    state['draft']=False
else:raise SystemExit('Unexpected gh operation')
state_file.write_text(json.dumps(state))
''')
        stub.chmod(0o755)
        workflow = (ROOT/'.github/workflows/publish-app.yml').read_text()
        step = workflow.split('      - name: Publish only after all assets are uploaded\n',1)[1]
        source = step.split('        run: |\n',1)[1]
        self.script = '\n'.join(line[10:] for line in source.splitlines() if line.strip())+'\n'
        self.state_file = self.root/'github-state.json'

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def publish(self,draft):
        self.state_file.write_text(json.dumps({'draft':draft,'notes':'original published notes',
                                               'calls':[],'uploaded':False}))
        env = {**os.environ,'PATH':str(self.binary)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
               'FAKE_GH_STATE':str(self.state_file),'TAG':'v0.0.2','GITHUB_SHA':self.commit,
               'GITHUB_REPOSITORY':'SII-MATH/Formaliscope','RUNNER_TEMP':str(self.runner),
               'GITHUB_STEP_SUMMARY':str(self.runner/'summary.md')}
        result = subprocess.run(['bash','-c',self.script],cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(self.state_file.read_text())

    def test_new_draft_and_retry_include_notes_before_publication(self):
        for draft in (None,True):
            with self.subTest(existing_draft=draft):
                state = self.publish(draft)
                self.assertFalse(state['draft'])
                self.assertTrue(state['uploaded'])
                self.assertIn('Save reviews automatically',state['notes'])
                self.assertIn(f'/compare/v0.0.1...{self.commit}',state['notes'])
                operations = [call[1] for call in state['calls']]
                self.assertEqual(operations,['view','create' if draft is None else 'edit','upload','edit'])
                self.assertIn(state['notes'],(self.runner/'summary.md').read_text())

    def test_published_rerun_leaves_notes_and_assets_unchanged(self):
        state = self.publish(False)
        self.assertEqual(state['notes'],'original published notes')
        self.assertFalse(state['uploaded'])
        self.assertEqual([call[1] for call in state['calls']],['view'])
        self.assertFalse((self.runner/'release-notes.md').exists())


if __name__ == '__main__':
    unittest.main()
