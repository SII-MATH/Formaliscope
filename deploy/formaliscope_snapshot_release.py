#!/usr/bin/env python3
"""Publish complete snapshot drafts; reuse published evidence without mutations."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_app.build import validate_snapshot

ASSETS = ('snapshot.json', 'snapshot.json.sha256', 'snapshot-manifest.json')
COMMIT = re.compile(r'[0-9a-f]{40}')


def load_bundle(directory, tag):
    """Validate the downloaded bytes, their checksum, provenance and manifest."""
    snapshot_bytes = (directory / ASSETS[0]).read_bytes()
    checksum = (directory / ASSETS[1]).read_text(encoding='ascii')
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    if checksum != f'{digest}  snapshot.json\n':
        raise ValueError('snapshot release checksum does not match the downloaded snapshot')
    snapshot = json.loads(snapshot_bytes)
    validate_snapshot(snapshot)
    manifest = json.loads((directory / ASSETS[2]).read_text(encoding='utf-8'))
    source = tag.removeprefix('snapshot-')
    if (tag != f'snapshot-{source}' or not COMMIT.fullmatch(source)
            or snapshot.get('source_commit') != source
            or snapshot.get('source_dirty') is not False
            or snapshot.get('source_origin') != 'git-checkout'
            or snapshot.get('review_mode') != 'statement'
            or manifest.get('schema') != 'formaliscope-snapshot-release.v1'
            or manifest.get('source_repository') != 'SII-MATH/KIP126'
            or manifest.get('source_commit') != source
            or manifest.get('snapshot_digest') != snapshot.get('digest')
            or manifest.get('review_mode') != 'statement'
            or manifest.get('comparison') != snapshot.get('comparison')):
        raise ValueError('snapshot release manifest or clean Statement provenance is invalid')
    return snapshot, manifest


def equivalent_snapshot(existing, candidate):
    # A rebuild changes the timestamp; reuse the original bytes in that case.
    # Every other field, including fields outside the content digest, must agree.
    return ({key: value for key, value in existing.items() if key != 'generated_at'}
            == {key: value for key, value in candidate.items() if key != 'generated_at'})


def release_assets(release, tag, *, complete=False):
    if (release.get('tag_name') != tag or not isinstance(release.get('draft'), bool)
            or release.get('prerelease') is not False
            or not isinstance(release.get('target_commitish'), str)
            or not COMMIT.fullmatch(release['target_commitish'])):
        raise ValueError('snapshot release metadata is invalid')
    assets = {}
    for asset in release.get('assets', []):
        name = asset.get('name')
        if name not in ASSETS:
            continue
        if name in assets:
            raise ValueError('snapshot release has duplicate evidence attachments')
        assets[name] = asset
    if complete and any(name not in assets or assets[name].get('state') != 'uploaded'
                        or not isinstance(assets[name].get('size'), int)
                        or assets[name]['size'] <= 0 for name in ASSETS):
        raise ValueError('published snapshot release attachments are incomplete')
    return assets


class GitHub:
    def __init__(self, repository):
        self.repository = repository
        self.release_ids = {}

    def _run(self, *arguments):
        return subprocess.check_output(['gh', *arguments], text=True)

    def find_release(self, tag):
        pages = json.loads(self._run('api', '--paginate', '--slurp',
                                    f'repos/{self.repository}/releases?per_page=100'))
        matches = [release for page in pages for release in page if release.get('tag_name') == tag]
        if len(matches) > 1:
            raise ValueError('multiple snapshot releases use the same tag')
        if not matches:
            return None
        release = matches[0]
        identity = release.get('id')
        if not isinstance(identity, int) or identity <= 0:
            raise ValueError('snapshot release ID is invalid')
        self.release_ids[tag] = identity
        return release

    def read_release(self, tag):
        # Read by ID so authenticated draft releases work before publication too.
        if tag not in self.release_ids and self.find_release(tag) is None:
            raise ValueError('snapshot release disappeared before verification')
        return json.loads(self._run('api', f'repos/{self.repository}/releases/{self.release_ids[tag]}'))

    def create_draft(self, tag, target):
        self._run('release', 'create', tag, '--repo', self.repository, '--draft',
                  '--target', target, '--title', f'KIP126 snapshot {tag[9:21]}', '--latest=false',
                  '--notes', f'Immutable evidence snapshot built from KIP126 {tag[9:]}.')

    def download(self, tag, name, directory):
        self._run('release', 'download', tag, '--repo', self.repository,
                  '--pattern', name, '--dir', str(directory))

    def upload(self, tag, file, *, clobber=False):
        arguments = ['release', 'upload', tag, str(file), '--repo', self.repository]
        if clobber:
            arguments.append('--clobber')
        self._run(*arguments)

    def publish(self, tag):
        self._run('release', 'edit', tag, '--repo', self.repository, '--draft=false', '--latest=false')


def verify_download(github, release, tag, directory, candidate):
    assets = release_assets(release, tag, complete=True)
    for name in ASSETS:
        github.download(tag, name, directory)
        if (directory / name).stat().st_size != assets[name]['size']:
            raise ValueError('snapshot release attachment size does not match GitHub metadata')
    existing = load_bundle(directory, tag)
    if not equivalent_snapshot(existing[0], candidate[0]) or existing[1] != candidate[1]:
        raise ValueError('immutable snapshot conflict: this source commit already has different evidence')


def publish_snapshot(github, tag, target, artifacts):
    if not COMMIT.fullmatch(target):
        raise ValueError('snapshot builder target must be an exact commit')
    candidate = load_bundle(artifacts, tag)
    release = github.find_release(tag)
    if release is None:
        github.create_draft(tag, target)
        release = github.read_release(tag)
    assets = release_assets(release, tag)
    with tempfile.TemporaryDirectory(prefix='snapshot-release-') as temporary:
        root = Path(temporary)
        existing_dir = root / 'existing'
        existing_dir.mkdir()
        if not release['draft']:
            verify_download(github, release, tag, existing_dir, candidate)
            return 'reused'

        # Recover a partial draft without refreshing evidence from another builder.
        # Reuse uploaded snapshot bytes so generated_at and its checksum stay paired.
        ready_dir = root / 'ready'
        ready_dir.mkdir()
        for name in ASSETS:
            asset = assets.get(name)
            if asset and asset.get('state') == 'uploaded' and asset.get('size', 0) > 0:
                github.download(tag, name, existing_dir)
                if (existing_dir / name).stat().st_size != asset['size']:
                    raise ValueError('draft attachment size does not match GitHub metadata')
        previous_snapshot = existing_dir / ASSETS[0]
        if previous_snapshot.exists():
            previous = json.loads(previous_snapshot.read_bytes())
            validate_snapshot(previous)
            if not equivalent_snapshot(previous, candidate[0]):
                raise ValueError('immutable snapshot conflict: draft contains different evidence')
        shutil.copyfile(previous_snapshot if previous_snapshot.exists() else artifacts / ASSETS[0],
                        ready_dir / ASSETS[0])
        digest = hashlib.sha256((ready_dir / ASSETS[0]).read_bytes()).hexdigest()
        (ready_dir / ASSETS[1]).write_text(f'{digest}  snapshot.json\n', encoding='ascii')
        shutil.copyfile(artifacts / ASSETS[2], ready_dir / ASSETS[2])
        for name in ASSETS[1:]:
            existing = existing_dir / name
            if existing.exists():
                if name == ASSETS[1]:
                    if existing.read_bytes() != (ready_dir / name).read_bytes():
                        raise ValueError('immutable snapshot conflict: draft checksum differs')
                elif json.loads(existing.read_bytes()) != candidate[1]:
                    raise ValueError('immutable snapshot conflict: draft manifest differs')
                shutil.copyfile(existing, ready_dir / name)
        load_bundle(ready_dir, tag)

        for name in ASSETS:
            if (existing_dir / name).exists():
                continue
            current = github.read_release(tag)
            current_assets = release_assets(current, tag)
            if not current['draft']:
                raise ValueError('snapshot was published concurrently; no attachments changed')
            if name in current_assets and current_assets[name].get('state') == 'uploaded' \
                    and current_assets[name].get('size', 0) > 0:
                raise ValueError('snapshot attachment changed concurrently; retry verification')
            # Only replace incomplete upload placeholders while the release is draft.
            github.upload(tag, ready_dir / name, clobber=name in current_assets)

        completed = github.read_release(tag)
        verified_dir = root / 'verified'
        verified_dir.mkdir()
        verify_download(github, completed, tag, verified_dir, candidate)
        if completed['draft']:
            github.publish(tag)
        final = github.read_release(tag)
        release_assets(final, tag, complete=True)
        if final['draft']:
            raise ValueError('snapshot release remains draft after publishing')
        return 'published'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
        parser.error('invalid GitHub repository')
    try:
        result = publish_snapshot(GitHub(args.repository), args.tag, args.target, args.artifacts)
        print(f'{result}: {args.tag}; immutable evidence verified')
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
