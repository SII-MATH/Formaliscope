"""Actual release metadata, version reservation and immutable packaging checks."""
import copy
import hashlib
import json
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / 'deploy/formaliscope_release.py'
release = runpy.run_path(str(HELPER))


def app(tag, commit='a'*40, **kwargs):
    archive = f'formaliscope-app-{commit}.tar.gz'
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'target_commitish': commit,
            'published_at': '2026-10-03T00:00:00Z',
            'assets': [{'name': name, 'url': f'https://api.github.com/repos/SII-MATH/Formaliscope/releases/assets/{i}'}
                       for i, name in enumerate((archive, archive+'.sha256', release['MANIFEST']), start=1)], **kwargs}


class ReleaseTests(unittest.TestCase):
    def test_first_version_numeric_bump_and_reserved_versions(self):
        next_tag = release['next_tag']
        self.assertEqual(next_tag([app('app-'+'a'*40)], [], 'b'*40), 'v0.0.1')
        self.assertEqual(next_tag([[app('v0.0.9'), app('v0.0.10', draft=True)]], [], 'b'*40), 'v0.0.11')
        self.assertEqual(next_tag([], [{'name': 'v0.1.0', 'commit': {'sha': 'a'*40}}], 'b'*40), 'v0.1.1')
        self.assertEqual(next_tag([app('snapshot-'+'a'*40)], [], 'b'*40), 'v0.0.1')

    def test_rerun_reuses_commit_version_including_draft_or_tag_only(self):
        for draft in (False, True):
            self.assertEqual(release['next_tag']([app('v0.0.1', draft=draft), app('v0.0.2', 'b'*40)], [], 'a'*40), 'v0.0.1')
        self.assertEqual(release['next_tag']([], [{'name': 'v0.0.1', 'commit': {'sha': 'a'*40}}], 'a'*40), 'v0.0.1')
        with self.assertRaises(ValueError):
            release['next_tag']([app('v0.0.1'), app('v0.0.2')], [], 'a'*40)

    def test_deployer_selects_highest_stable_version_and_legacy_fallback(self):
        choose = release['select_release']
        data = [app('v0.0.9'), app('v0.0.10', 'b'*40), app('v0.0.11', draft=True),
                app('v0.1.0', prerelease=True), app('snapshot-'+'a'*40),
                app('app-'+'a'*40, published_at='2027-01-01T00:00:00Z')]
        self.assertEqual(choose(data)[:2], ('v0.0.10', 'b'*40))
        self.assertEqual(choose([[app('snapshot-'+'a'*40)], [app('app-'+'a'*40)]])[:2], ('app-'+'a'*40, 'a'*40))
        for tag in ('v01.2.3', 'v0.0.1-beta', 'v0.0.1\n', 'v1.2'):
            self.assertIsNone(release['version'](tag))

    def test_malformed_assets_fail_without_falling_back_to_older_version(self):
        broken = app('v0.0.2', 'b'*40)
        for index in range(3):
            value = copy.deepcopy(broken)
            value['assets'].pop(index)
            with self.assertRaises(ValueError):
                release['select_release']([app('v0.0.1'), value])
        invalid = app('v0.0.1')
        invalid['assets'][0]['url'] = 'https://untrusted.example/archive'
        with self.assertRaises(ValueError):
            release['select_release']([invalid])
        with self.assertRaises(ValueError):
            release['select_release']([app('app-'+'b'*40)])

    def test_manifest_binds_version_commit_and_archive_checksum(self):
        manifest = {'schema': 'formaliscope-app-release.v1', 'version': 'v0.0.1',
                    'commit': 'a'*40, 'archive': 'formaliscope-app-'+'a'*40+'.tar.gz', 'sha256': 'c'*64}
        verify = release['validate_manifest']
        args = ('v0.0.1', manifest['archive'], 'a'*40, 'c'*64)
        verify(manifest, *args)
        for field, value in [('version', 'v0.0.2'), ('commit', 'b'*40), ('sha256', 'd'*64), ('archive', '../app')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify({**manifest, field: value}, *args)
        legacy = {k: v for k, v in manifest.items() if k != 'version'}
        verify(legacy, 'app-'+'a'*40, *args[1:])
        with self.assertRaises(ValueError):
            verify(legacy, *args)

    def test_archive_has_exact_tracked_source_and_no_runtime_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
            git('init')
            git('config', 'user.name', 'Release test')
            git('config', 'user.email', 'release@example.test')
            (repo / 'code.txt').write_text('versioned code\n')
            git('add', 'code.txt')
            git('commit', '-m', 'release fixture')
            commit = git('rev-parse', 'HEAD')
            (repo / 'judgments.sqlite3').write_text('private runtime data')
            (repo / 'recovery.txt').write_text('private recovery')
            (repo / 'code.txt').write_text('uncommitted change')
            output = root / 'package'
            archive_name = release['package'](commit, 'v0.0.1', output, repo)
            manifest = json.loads((output / release['MANIFEST']).read_text())
            actual = hashlib.sha256((output / archive_name).read_bytes()).hexdigest()
            release['validate_manifest'](manifest, 'v0.0.1', archive_name, commit, actual)
            with tarfile.open(output / archive_name) as archive:
                files = [entry.name for entry in archive.getmembers() if entry.isfile()]
                self.assertEqual(files, [f'formaliscope-{commit}/code.txt'])
                self.assertEqual(archive.extractfile(files[0]).read(), b'versioned code\n')
            self.assertEqual((output / (archive_name+'.sha256')).read_text(), f'{actual}  {archive_name}\n')


if __name__ == '__main__':
    unittest.main()
