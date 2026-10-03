"""Exercise the app puller's upgrade order with local assets and service stubs."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest


TOOL_STUB = r'''
import json
import os
from pathlib import Path
import shutil
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['PULL_TEST_ROOT'])
state = root / 'service-active'
restart_queue = root / 'restart-queued'

def service_state():
    return state.read_text() if state.exists() else 'inactive'

def log(event, **details):
    with (root / 'events.jsonl').open('a') as output:
        output.write(json.dumps({'event': event, **details}) + '\n')

if name == 'flock':
    sys.exit(0)
elif name == 'mv':
    paths = [value for value in args if not value.startswith('-')]
    source, destination = map(Path, paths)
    if destination.name == 'current':
        log('activate', target=os.readlink(source))
        os.replace(source, destination)
    else:
        shutil.move(str(source), str(destination))
elif name == 'systemctl':
    log('service', args=args)
    assert args[-1] == 'formaliscope-review.service', args
    if args[0] == 'show':
        if os.environ.get('PULL_FAIL_SHOW') == '1':
            sys.exit(1)
        loaded = 'not-found' if os.environ.get('PULL_UNIT_MISSING') == '1' else 'loaded'
        properties = [f'LoadState={loaded}', f'ActiveState={service_state()}']
        # Exercise both orders across the pre-stop and post-stop reads.
        if service_state() != 'inactive':
            properties.reverse()
        print('\n'.join(properties))
    elif args[0] == 'stop':
        if os.environ.get('PULL_FAIL_STOP') == '1':
            sys.exit(1)
        if os.environ.get('PULL_STILL_ACTIVE') != '1':
            state.unlink(missing_ok=True)
            restart_queue.unlink(missing_ok=True)
            after = os.environ.get('PULL_POST_STOP_STATE', 'inactive')
            if after != 'inactive':
                state.write_text(after)
    elif args[0] == 'restart':
        assert (Path(os.environ['FORMALISCOPE_ROOT']) / 'current').resolve().name == os.environ['PULL_COMMIT']
        state.write_text('active')
    else:
        raise AssertionError(args)
elif name == 'curl':
    url = args[-1]
    if url == 'http://127.0.0.1:8765/healthz':
        log('readiness')
        assert service_state() == 'active'
        print(json.dumps({'ready': True}))
    else:
        log('download', url=url)
        assets = json.loads((root / 'asset-paths.json').read_text())
        source = root / 'releases.json' if '/releases?' in url else Path(assets[url])
        shutil.copyfile(source, args[args.index('-o') + 1])
elif name == 'runuser':
    assert args[:4] == ['-u', 'formaliscope-review', '--', 'env'], args
    command = args[args.index('review_app') + 1]
    application = next(value.split('=', 1)[1] for value in args if value.startswith('PYTHONPATH='))
    data = Path(args[args.index('--data-dir') + 1])
    log(command, application=application, active=service_state() not in ('inactive', 'failed'),
        state=service_state(), args=args)
    if command == 'backup':
        assert '--keep' not in args, 'old application does not accept --keep'
        assert (Path(application) / 'review_app' / '__main__.py').is_file()
        if os.environ.get('PULL_FAIL_BACKUP') == '1':
            sys.exit(1)
        backup = Path(args[args.index('--output') + 1]) / 'pre-upgrade'
        backup.mkdir(parents=True)
        for filename in ('judgments.sqlite3', 'snapshot.json'):
            shutil.copyfile(data / filename, backup / filename)
        print('consistent backup -> ' + str(backup))
    elif command == 'migrate':
        assert service_state() in ('inactive', 'failed'), 'the old review process must stop before migration'
        assert not restart_queue.exists(), 'stop must cancel a queued automatic restart before migration'
        assert Path(application).name == os.environ['PULL_COMMIT']
        # Model committed earlier migration steps before a later failure.
        (data / 'judgments.sqlite3').write_bytes(b'schema7')
        if os.environ.get('PULL_FAIL_MIGRATION') == '1':
            sys.exit(1)
    else:
        raise AssertionError(command)
else:
    raise AssertionError(name)
'''


class AppPullLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.application = self.root / 'application'
        self.data = self.root / 'data'
        self.backups = self.root / 'backups'
        self.old = self.application / 'releases' / ('a' * 40)
        (self.old / 'review_app').mkdir(parents=True)
        (self.old / 'review_app' / '__main__.py').write_text('# older application\n')
        (self.application / 'current').symlink_to(self.old)
        self.data.mkdir()
        (self.data / 'snapshot.json').write_text('{"digest": "original-snapshot"}\n')
        (self.data / 'judgments.sqlite3').write_bytes(b'schema6')
        self.state = self.root / 'service-active'
        self.state.write_text('active')
        self.commit = 'b' * 40
        self.candidate = self.application / 'releases' / self.commit
        self.tag = 'v0.0.7'
        self.archive_name = f'formaliscope-app-{self.commit}.tar.gz'
        self.archive = self.root / self.archive_name
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, value in {'__main__.py': b'# candidate application\n', '__init__.py': b''}.items():
                member = tarfile.TarInfo(f'formaliscope-{self.commit}/review_app/{name}')
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        checksum = self.root / (self.archive_name + '.sha256')
        checksum.write_text(f'{digest}  {self.archive_name}\n')
        self.manifest = self.root / 'formaliscope-app-manifest.json'
        self.manifest.write_text(json.dumps({'schema': 'formaliscope-app-release.v1', 'version': self.tag,
                                            'archive': self.archive_name, 'commit': self.commit, 'sha256': digest}))
        urls = [f'https://api.github.com/repos/test/application/releases/assets/{index}' for index in (1, 2, 3)]
        files = [self.archive, checksum, self.manifest]
        assets = [{'name': path.name, 'url': url} for url, path in zip(urls, files)]
        (self.root / 'asset-paths.json').write_text(json.dumps({url: str(path) for url, path in zip(urls, files)}))
        (self.root / 'releases.json').write_text(json.dumps([{'tag_name': self.tag, 'assets': assets}]))
        token = self.root / 'read-token'
        token.write_text('stub-token')
        binaries = self.root / 'bin'
        binaries.mkdir()
        dispatcher = binaries / 'tools.py'
        dispatcher.write_text(f'#!{sys.executable}\n' + textwrap.dedent(TOOL_STUB).lstrip())
        dispatcher.chmod(0o755)
        for name in ('curl', 'systemctl', 'runuser', 'flock', 'mv'):
            (binaries / name).symlink_to(dispatcher)
        (binaries / 'python3').symlink_to(sys.executable)
        self.repository = Path(__file__).resolve().parents[1]
        self.env = {
            **os.environ, 'PATH': str(binaries) + os.pathsep + os.environ.get('PATH', ''),
            'FORMALISCOPE_ROOT': str(self.application), 'REVIEW_DATA_DIR': str(self.data),
            'REVIEW_BACKUP_DIR': str(self.backups), 'FORMALISCOPE_DEPLOY_LOCK': str(self.root / 'deploy.lock'),
            'FORMALISCOPE_GITHUB_TOKEN_FILE': str(token),
            'FORMALISCOPE_RELEASE_HELPER': str(self.repository / 'deploy' / 'formaliscope_release.py'),
            'PULL_TEST_ROOT': str(self.root), 'PULL_COMMIT': self.commit,
            'PYTHONPYCACHEPREFIX': str(self.root / 'pycache'),
        }

    def run_puller(self, **environment):
        return subprocess.run(['bash', str(self.repository / 'deploy' / 'formaliscope-app-pull')],
                              env={**self.env, **environment}, capture_output=True, text=True, timeout=30)

    def events(self):
        path = self.root / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def service_actions(self):
        return [event['args'][0] for event in self.events() if event['event'] == 'service']

    def test_active_upgrade_backs_up_stops_migrates_switches_then_restarts(self):
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        actions = [event['event'] if event['event'] != 'service' else event['args'][0] for event in events]
        expected = ['backup', 'stop', 'migrate', 'activate', 'restart', 'readiness']
        positions = [actions.index(action) for action in expected]
        self.assertEqual(positions, sorted(positions))
        backup = next(event for event in events if event['event'] == 'backup')
        self.assertEqual(backup['application'], str(self.old))
        self.assertTrue(backup['active'])
        self.assertFalse(next(event for event in events if event['event'] == 'migrate')['active'])
        self.assertEqual((self.backups / 'pre-upgrade' / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema7')
        self.assertEqual((self.application / 'current').resolve(), self.candidate)
        self.assertTrue(self.state.exists())

    def test_migration_failure_keeps_backup_and_does_not_restart_previous_code(self):
        result = self.run_puller(PULL_FAIL_MIGRATION='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('migration failed', result.stderr)
        self.assertIn(str(self.backups), result.stderr)
        self.assertIn('restore the matching backup', result.stderr)
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertFalse(self.state.exists())
        self.assertEqual((self.backups / 'pre-upgrade' / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema7')
        self.assertIn('stop', self.service_actions())
        self.assertNotIn('restart', self.service_actions())
        self.assertFalse({'activate', 'readiness'} & {event['event'] for event in self.events()})

    def test_backup_failure_leaves_running_release_and_database_untouched(self):
        result = self.run_puller(PULL_FAIL_BACKUP='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pre-upgrade backup failed', result.stderr)
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertTrue(self.state.exists())
        self.assertFalse(self.service_actions())
        self.assertNotIn('migrate', {event['event'] for event in self.events()})

    def test_invalid_candidate_is_rejected_before_backup_or_service_stop(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['sha256'] = '0' * 64
        self.manifest.write_text(json.dumps(manifest))
        result = self.run_puller()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertTrue(self.state.exists())
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.service_actions())
        self.assertNotIn('backup', {event['event'] for event in self.events()})

    def test_stop_failure_aborts_before_migration_or_activation(self):
        result = self.run_puller(PULL_FAIL_STOP='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not stop', result.stderr)
        self.assertTrue(self.state.exists())
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertFalse({'migrate', 'activate', 'readiness'} & {event['event'] for event in self.events()})

    def test_service_still_active_after_stop_aborts_before_migration(self):
        result = self.run_puller(PULL_STILL_ACTIVE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('service did not stop', result.stderr)
        self.assertTrue(self.state.exists())
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertNotIn('migrate', {event['event'] for event in self.events()})

    def test_stop_not_converged_to_inactive_or_failed_aborts_before_migration(self):
        result = self.run_puller(PULL_POST_STOP_STATE='deactivating')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ActiveState=deactivating', result.stderr)
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertFalse({'migrate', 'activate', 'readiness'} & {event['event'] for event in self.events()})

    def test_activating_review_service_is_stopped_before_migration_then_restarted(self):
        self.state.write_text('activating')
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show', 'restart'])
        self.assertEqual(self.state.read_text(), 'active')
        migration = next(event for event in self.events() if event['event'] == 'migrate')
        self.assertEqual(migration['state'], 'inactive')

    def test_automatic_restart_queue_is_canceled_before_migration_then_restored(self):
        self.state.write_text('activating')
        queued = self.root / 'restart-queued'
        queued.write_text('auto-restart')
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(queued.exists())
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show', 'restart'])
        self.assertEqual(self.state.read_text(), 'active')

    def test_reloading_review_service_is_stopped_then_restarted(self):
        self.state.write_text('reloading')
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show', 'restart'])
        self.assertEqual(self.state.read_text(), 'active')

    def test_deactivating_review_service_is_stopped_and_remains_inactive(self):
        self.state.write_text('deactivating')
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show'])
        self.assertFalse(self.state.exists())
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema7')
        self.assertNotIn('readiness', {event['event'] for event in self.events()})

    def test_failed_review_service_is_stopped_and_remains_inactive(self):
        self.state.write_text('failed')
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show'])
        self.assertFalse(self.state.exists())

    def test_failed_state_after_stop_is_safe_for_migration(self):
        result = self.run_puller(PULL_POST_STOP_STATE='failed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(next(event for event in self.events() if event['event'] == 'migrate')['state'], 'failed')
        self.assertEqual(self.state.read_text(), 'active')

    def test_unreadable_service_state_aborts_before_stop_or_migration(self):
        result = self.run_puller(PULL_FAIL_SHOW='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not read review service state', result.stderr)
        self.assertEqual(self.service_actions(), ['show'])
        self.assertEqual(self.state.read_text(), 'active')
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertNotIn('migrate', {event['event'] for event in self.events()})

    def test_inactive_installation_migrates_and_switches_without_starting_service(self):
        self.state.unlink()
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.application / 'current').resolve(), self.candidate)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show'])
        self.assertIn('migrate', {event['event'] for event in self.events()})
        self.assertNotIn('readiness', {event['event'] for event in self.events()})

    def test_first_install_with_no_snapshot_does_not_migrate_or_start_service(self):
        self.state.unlink()
        (self.application / 'current').unlink()
        shutil.rmtree(self.data)
        result = self.run_puller(PULL_UNIT_MISSING='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.application / 'current').resolve(), self.candidate)
        self.assertFalse(self.data.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.service_actions(), ['show'])
        self.assertFalse({'backup', 'migrate', 'readiness'} & {event['event'] for event in self.events()})

    def test_first_install_with_existing_data_uses_candidate_backup_and_stays_inactive(self):
        self.state.unlink()
        (self.application / 'current').unlink()
        result = self.run_puller()
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next(event for event in self.events() if event['event'] == 'backup')
        self.assertEqual(backup['application'], str(self.candidate))
        self.assertEqual((self.backups / 'pre-upgrade' / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertEqual((self.application / 'current').resolve(), self.candidate)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.service_actions(), ['show', 'stop', 'show'])

    def test_database_without_snapshot_refuses_upgrade_before_service_stop(self):
        (self.data / 'snapshot.json').unlink()
        result = self.run_puller()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('database has no snapshot', result.stderr)
        self.assertEqual((self.application / 'current').resolve(), self.old)
        self.assertTrue(self.state.exists())
        self.assertEqual((self.data / 'judgments.sqlite3').read_bytes(), b'schema6')
        self.assertFalse(self.service_actions())


if __name__ == '__main__':
    unittest.main()
