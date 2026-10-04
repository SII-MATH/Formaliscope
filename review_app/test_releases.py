"""Actual release metadata, version reservation and immutable packaging checks."""
import copy
import hashlib
import json
import runpy
import subprocess
import sys
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


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Release test')
        self.git('config', 'user.email', 'release@example.test')
        self.counter = 0
        self.initial = self.commit('Initial application')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, subject):
        self.counter += 1
        (self.repo / 'code.txt').write_text(f'change {self.counter}\n', encoding='utf-8')
        self.git('add', 'code.txt')
        self.git('commit', '-m', subject)
        return self.git('rev-parse', 'HEAD')

    def notes(self, releases, tag, commit):
        return release['release_notes'](releases, tag, commit, 'SII-MATH/Formaliscope', self.repo)

    def invoke(self, releases, tag, commit, output, repository='SII-MATH/Formaliscope'):
        metadata = self.root / 'releases.json'
        metadata.write_text(json.dumps(releases), encoding='utf-8')
        return subprocess.run([sys.executable, str(HELPER), 'release-notes',
                               '--releases', str(metadata), '--version', tag, '--commit', commit,
                               '--repository', repository, '--output', str(output)],
                              cwd=self.repo, text=True, capture_output=True)

    def test_direct_commits_and_merge_include_changes_without_merge_wrappers(self):
        self.git('tag', 'v0.0.1')
        self.git('checkout', '-b', 'feature')
        side = self.commit('Improve review navigation')
        self.git('checkout', 'main')
        (self.repo / 'direct.txt').write_text('direct change\n')
        self.git('add', 'direct.txt')
        self.git('commit', '-m', 'Fix backup retention')
        direct = self.git('rev-parse', 'HEAD')
        self.git('merge', '--no-ff', 'feature', '-m', 'Merge wrapper should not be listed')
        current = self.git('rev-parse', 'HEAD')
        notes = self.notes([app('v0.0.1')], 'v0.0.2', current)
        self.assertIn('## Changelog\n', notes)
        self.assertIn('Improve review navigation', notes)
        self.assertIn('Fix backup retention', notes)
        self.assertNotIn('Merge wrapper', notes)
        self.assertNotIn('Initial application', notes)
        for sha in (side, direct):
            self.assertIn(f'[{sha[:7]}](https://github.com/SII-MATH/Formaliscope/commit/{sha})', notes)
        self.assertIn(f'/compare/v0.0.1...{current}', notes)
        self.assertIn(f'来源提交：[{current}]', notes)
        self.assertEqual(notes, self.notes([app('v0.0.1')], 'v0.0.2', current))

    def test_numeric_previous_version_ignores_unpublished_legacy_and_prereleases(self):
        self.git('tag', 'v0.0.9')
        boundary = self.commit('Already released in patch ten')
        self.git('tag', 'v0.0.10')
        current = self.commit('Change after patch ten')
        releases = [[app('v0.0.9'), app('v0.0.10', target_commitish='main')],
                    [app('v0.0.11', draft=True), app('v0.0.11', prerelease=True),
                     app('v0.0.11', published_at=None), app('v0.1.0'),
                     app('app-'+'a'*40, published_at='2030-01-01T00:00:00Z'),
                     app('snapshot-'+'b'*40)]]
        notes = self.notes(releases, 'v0.0.12', current)
        self.assertIn(f'/compare/v0.0.10...{current}', notes)
        self.assertIn('Change after patch ten', notes)
        self.assertNotIn('Already released in patch ten', notes)
        self.assertNotIn(boundary, notes)

    def test_historical_rerun_uses_lower_version_and_immutable_tag(self):
        self.git('tag', 'v0.0.1')
        current = self.commit('Original second release')
        self.git('tag', 'v0.0.2')
        later = self.commit('Future third release')
        self.git('tag', 'v0.0.3')
        releases = [app('v0.0.1', target_commitish='main'), app('v0.0.2', current), app('v0.0.3', later)]
        notes = self.notes(releases, 'v0.0.2', current)
        self.assertIn('Original second release', notes)
        self.assertNotIn('Future third release', notes)
        self.assertIn(f'/compare/v0.0.1...{current}', notes)

    def test_first_release_ignores_old_tags_and_has_tree_link(self):
        self.git('tag', 'app-'+'a'*40)
        current = self.commit('First numbered release')
        notes = self.notes([app('app-'+'a'*40), app('snapshot-'+'b'*40)], 'v0.0.1', current)
        self.assertIn('Initial application', notes)
        self.assertIn('First numbered release', notes)
        self.assertIn(f'/tree/{current}', notes)
        self.assertNotIn('/compare/', notes)

    def test_annotated_previous_tag_resolves_to_commit(self):
        self.git('tag', '-a', 'v0.0.1', '-m', 'Release one')
        current = self.commit('Change after annotated tag')
        notes = self.notes([app('v0.0.1')], 'v0.0.2', current)
        self.assertIn('Change after annotated tag', notes)
        self.assertNotIn('Initial application', notes)

    def test_cli_writes_deterministic_utf8_notes_and_escapes_markdown_titles(self):
        self.git('tag', 'v0.0.1')
        current = self.commit('修复 *bold* [link](url) `code` <script> & _italic_ ![image] \\path')
        output = self.root / 'notes' / 'release.md'
        first = self.invoke([app('v0.0.1')], 'v0.0.2', current, output)
        self.assertEqual(first.returncode, 0, first.stderr)
        actual = output.read_bytes()
        text = actual.decode('utf-8')
        self.assertIn(r'修复 \*bold\* \[link\]\(url\) \`code\` &lt;script&gt; &amp; \_italic\_', text)
        self.assertIn(r'\!\[image\] \\path', text)
        self.assertTrue(actual.endswith(b'\n'))
        self.assertNotIn(b'\r', actual)
        again = self.invoke([app('v0.0.1')], 'v0.0.2', current, output)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(output.read_bytes(), actual)
        self.assertEqual(list(output.parent.iterdir()), [output])

    def test_non_ancestor_range_fails_without_creating_or_overwriting_notes(self):
        self.git('tag', 'v0.0.1')
        self.git('checkout', '--orphan', 'unrelated')
        current = self.commit('Unrelated application history')
        output = self.root / 'release.md'
        result = self.invoke([app('v0.0.1')], 'v0.0.2', current, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not an ancestor', result.stderr)
        self.assertFalse(output.exists())
        output.write_text('Existing notes\n')
        result = self.invoke([app('v0.0.1')], 'v0.0.2', current, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_text(), 'Existing notes\n')
        self.assertEqual(list(self.root.glob('.release.md.*')), [])

    def test_missing_previous_tag_ref_fails_without_partial_file(self):
        output = self.root / 'release.md'
        result = self.invoke([app('v0.0.1')], 'v0.0.2', self.initial, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_empty_interval_has_explicit_message_and_compare_link(self):
        self.git('tag', 'v0.0.1')
        notes = self.notes([app('v0.0.1')], 'v0.0.2', self.initial)
        self.assertIn('- 无新增的非合并提交。', notes)
        self.assertIn(f'/compare/v0.0.1...{self.initial}', notes)
        self.assertNotIn('Initial application', notes)

    def test_rejects_invalid_version_commit_and_repository(self):
        for tag in ('v01.0.1', 'v0.0.1-beta', 'v0.0.1\n'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                self.notes([], tag, self.initial)
        for commit in ('HEAD', 'a'*39, self.initial.upper()):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                self.notes([], 'v0.0.1', commit)
        for repository in ('../repo', 'owner/..', 'owner/repo/extra', 'owner/repo#fragment', 'owner/repo\n'):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                release['release_notes']([], 'v0.0.1', self.initial, repository, self.repo)
        self.git('tag', '-a', 'v0.0.1', '-m', 'Annotated tag object')
        tag_object = self.git('rev-parse', 'refs/tags/v0.0.1')
        with self.assertRaises(ValueError):
            self.notes([], 'v0.0.2', tag_object)


if __name__ == '__main__':
    unittest.main()
