"""Snapshot publishing protects public evidence and resumes complete drafts."""
import copy
import hashlib
import json
import runpy
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from .build import CURRENT_FINGERPRINT_SCHEME, SNAPSHOT_SCHEMA, calculate_snapshot_digest

helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/formaliscope_snapshot_release.py'))
ASSETS = helper['ASSETS']
SOURCE = 'a' * 40
TARGET = 'b' * 40
TAG = 'snapshot-' + SOURCE


def bundle(directory, *, timestamp='2026-10-03T00:00:00Z', modules=None, extra=None):
    snapshot = {'schema': SNAPSHOT_SCHEMA, 'fingerprint_scheme': CURRENT_FINGERPRINT_SCHEME,
                'source_commit': SOURCE, 'source_dirty': False, 'source_origin': 'git-checkout',
                'review_mode': 'statement', 'dependency_lock_digest': None, 'unlinked_nodes': 0,
                'cards': [], 'modules': modules or {}, 'generated_at': timestamp,
                'comparison': {'unchanged': 0, 'changed': 0, 'added': 0, 'removed': 0}, **(extra or {})}
    snapshot['digest'] = calculate_snapshot_digest(snapshot)
    manifest = {'schema': 'formaliscope-snapshot-release.v1', 'source_repository': 'SII-MATH/KIP126',
                'source_commit': SOURCE, 'snapshot_digest': snapshot['digest'],
                'review_mode': 'statement', 'comparison': snapshot['comparison']}
    directory.mkdir(exist_ok=True)
    (directory / ASSETS[0]).write_text(json.dumps(snapshot) + '\n')
    digest = hashlib.sha256((directory / ASSETS[0]).read_bytes()).hexdigest()
    (directory / ASSETS[1]).write_text(f'{digest}  snapshot.json\n')
    (directory / ASSETS[2]).write_text(json.dumps(manifest) + '\n')
    return {name: (directory / name).read_bytes() for name in ASSETS}


class FakeGitHub:
    def __init__(self, files=None, *, draft=False):
        self.release = None if files is None else {'tag_name': TAG, 'target_commitish': TARGET,
                                                  'draft': draft, 'prerelease': False, 'assets': []}
        self.files = files or {}
        self.mutations = []
        if self.release is not None:
            self.release['assets'] = [{'name': name, 'size': len(data), 'state': 'uploaded'}
                                      for name, data in self.files.items()]

    def find_release(self, tag):
        return copy.deepcopy(self.release)

    def read_release(self, tag):
        return copy.deepcopy(self.release)

    def create_draft(self, tag, target):
        self.mutations.append('create-draft')
        self.release = {'tag_name': tag, 'target_commitish': target,
                        'draft': True, 'prerelease': False, 'assets': []}

    def download(self, tag, name, directory):
        (directory / name).write_bytes(self.files[name])

    def upload(self, tag, file, *, clobber=False):
        if not self.release['draft']:
            raise AssertionError('public release was mutated')
        if file.name in self.files and not clobber:
            raise AssertionError('unexpected attachment replacement')
        self.mutations.append(('upload', file.name, clobber))
        self.files[file.name] = file.read_bytes()
        self.release['assets'] = [asset for asset in self.release['assets'] if asset['name'] != file.name]
        self.release['assets'].append({'name': file.name, 'size': len(self.files[file.name]), 'state': 'uploaded'})

    def publish(self, tag):
        if not self.release['draft'] or set(self.files) != set(ASSETS):
            raise AssertionError('incomplete draft published')
        self.mutations.append('publish')
        self.release['draft'] = False


class SnapshotReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / 'candidate'
        self.files = bundle(self.artifacts)

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, github):
        return helper['publish_snapshot'](github, TAG, TARGET, self.artifacts)

    def test_new_release_publishes_only_after_complete_download_verification(self):
        github = FakeGitHub()
        self.assertEqual(self.publish(github), 'published')
        self.assertEqual(github.mutations, ['create-draft',
            *[('upload', name, False) for name in ASSETS], 'publish'])
        self.assertEqual(github.files, self.files)

    def test_public_rerun_reuses_original_bytes_despite_new_timestamp_and_builder(self):
        github = FakeGitHub(self.files)
        bundle(self.artifacts, timestamp='2026-10-04T00:00:00Z')
        result = helper['publish_snapshot'](github, TAG, 'c'*40, self.artifacts)
        self.assertEqual(result, 'reused')
        self.assertEqual(github.mutations, [])
        self.assertEqual(github.files, self.files)

    def test_public_changed_evidence_or_extra_fields_are_immutable_conflicts(self):
        for changed in ({'modules': {'KIP126/X.lean': 'def x := 2'}}, {'extra': {'new_meaning': True}}):
            with self.subTest(changed=changed):
                github = FakeGitHub(self.files)
                bundle(self.artifacts, **changed)
                with self.assertRaisesRegex(ValueError, 'immutable snapshot conflict'):
                    self.publish(github)
                self.assertEqual(github.mutations, [])

    def test_public_missing_duplicate_or_invalid_metadata_is_never_repaired(self):
        for mutation in ('missing', 'duplicate', 'prerelease', 'target', 'size', 'state'):
            with self.subTest(mutation=mutation):
                github = FakeGitHub(self.files)
                if mutation == 'missing':
                    github.release['assets'].pop()
                elif mutation == 'duplicate':
                    github.release['assets'].append(dict(github.release['assets'][0]))
                elif mutation == 'prerelease':
                    github.release['prerelease'] = True
                elif mutation == 'target':
                    github.release['target_commitish'] = 'main'
                elif mutation == 'size':
                    github.release['assets'][0]['size'] += 1
                else:
                    github.release['assets'][0]['state'] = 'starter'
                with self.assertRaises(ValueError):
                    self.publish(github)
                self.assertEqual(github.mutations, [])

    def test_public_corrupt_checksum_or_manifest_fails_without_mutations(self):
        for name in ASSETS[1:]:
            with self.subTest(name=name):
                files = {**self.files, name: b'corrupt\n'}
                github = FakeGitHub(files)
                with self.assertRaises(ValueError):
                    self.publish(github)
                self.assertEqual(github.mutations, [])

    def test_partial_draft_resumes_and_preserves_uploaded_snapshot_timestamp(self):
        for count in range(4):
            with self.subTest(uploaded=count):
                github = FakeGitHub({name: self.files[name] for name in ASSETS[:count]}, draft=True)
                bundle(self.artifacts, timestamp='2026-10-04T00:00:00Z')
                self.assertEqual(self.publish(github), 'published')
                uploaded = [item[1] for item in github.mutations if isinstance(item, tuple)]
                self.assertEqual(uploaded, list(ASSETS[count:]))
                if count:
                    self.assertEqual(github.files, self.files)
                self.assertFalse(github.release['draft'])

    def test_draft_conflict_does_not_overwrite_previous_builder_evidence(self):
        github = FakeGitHub({ASSETS[0]: self.files[ASSETS[0]]}, draft=True)
        bundle(self.artifacts, modules={'KIP126/X.lean': 'def x := 3'})
        with self.assertRaisesRegex(ValueError, 'immutable snapshot conflict'):
            self.publish(github)
        self.assertEqual(github.mutations, [])

    def test_incomplete_draft_upload_placeholder_can_be_replaced(self):
        github = FakeGitHub({}, draft=True)
        github.files[ASSETS[0]] = b''
        github.release['assets'] = [{'name': ASSETS[0], 'size': 0, 'state': 'starter'}]
        self.assertEqual(self.publish(github), 'published')
        self.assertIn(('upload', ASSETS[0], True), github.mutations)

    def test_bad_candidate_is_rejected_before_creating_a_release(self):
        github = FakeGitHub()
        (self.artifacts / ASSETS[1]).write_text('0'*64+'  snapshot.json\n')
        with self.assertRaises(ValueError):
            self.publish(github)
        self.assertEqual(github.mutations, [])

    def test_failed_upload_keeps_a_draft_for_a_safe_retry(self):
        github = FakeGitHub()
        upload = github.upload
        def fail_manifest(tag, file, **kwargs):
            if file.name == ASSETS[2]:
                raise OSError('simulated failed upload')
            upload(tag, file, **kwargs)
        github.upload = fail_manifest
        with self.assertRaises(OSError):
            self.publish(github)
        self.assertTrue(github.release['draft'])
        self.assertNotIn('publish', github.mutations)
        github.upload = upload
        self.assertEqual(self.publish(github), 'published')

    def test_corrupted_uploaded_draft_is_not_published(self):
        github = FakeGitHub()
        download = github.download
        def corrupt(tag, name, directory):
            download(tag, name, directory)
            if name == ASSETS[0]:
                (directory / name).write_bytes(b'incomplete')
        github.download = corrupt
        with self.assertRaises(ValueError):
            self.publish(github)
        self.assertTrue(github.release['draft'])
        self.assertNotIn('publish', github.mutations)

    def test_authenticated_draft_lookup_reads_by_release_id(self):
        github = helper['GitHub']('SII-MATH/Formaliscope')
        draft = {'id': 17, 'tag_name': TAG, 'draft': True}
        with patch.object(github, '_run', side_effect=[json.dumps([[draft]]), json.dumps(draft)]) as run:
            self.assertEqual(github.read_release(TAG), draft)
        self.assertEqual(run.call_args.args,
                         ('api', 'repos/SII-MATH/Formaliscope/releases/17'))

    def test_api_lookup_failure_is_not_treated_as_a_missing_release(self):
        github = helper['GitHub']('SII-MATH/Formaliscope')
        with patch.object(github, '_run', side_effect=OSError('simulated network failure')) as run:
            with self.assertRaises(OSError):
                self.publish(github)
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
