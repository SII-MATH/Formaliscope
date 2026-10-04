#!/usr/bin/env python3
"""Release versioning and metadata shared by Actions and the VPS puller."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
COMMIT = re.compile(r'[0-9a-f]{40}')
ARCHIVE = re.compile(r'formaliscope-app-([0-9a-f]{40})\.tar\.gz')
MANIFEST = 'formaliscope-app-manifest.json'
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+')


def version(tag):
    match = VERSION.fullmatch(tag) if isinstance(tag, str) else None
    return tuple(map(int, match.groups())) if match else None


def pages(value):
    if not isinstance(value, list):
        raise ValueError('GitHub response must be an array')
    return [item for page in value for item in page] if value and isinstance(value[0], list) else value


def next_tag(releases, tags, commit):
    if not COMMIT.fullmatch(commit):
        raise ValueError('release requires an exact commit')
    releases, tags = pages(releases), pages(tags)
    reserved = {item['tag_name'] for item in releases if version(item.get('tag_name'))}
    reserved.update(item['name'] for item in tags if version(item.get('name')))
    same_commit = {item['tag_name'] for item in releases
                   if version(item.get('tag_name')) and item.get('target_commitish') == commit}
    same_commit.update(item['name'] for item in tags
                       if version(item.get('name')) and item.get('commit', {}).get('sha') == commit)
    if len(same_commit) > 1:
        raise ValueError('commit already has multiple versions; operator review required')
    if same_commit:
        return same_commit.pop()
    major, minor, patch = max((version(tag) for tag in reserved), default=(0, 0, 0))
    return f'v{major}.{minor}.{patch+1}'


def select_release(releases):
    candidates = [item for item in pages(releases) if not item.get('draft') and not item.get('prerelease')]
    numbered = [item for item in candidates if version(item.get('tag_name'))]
    legacy = [item for item in candidates if re.fullmatch(r'app-[0-9a-f]{40}', item.get('tag_name', ''))]
    if numbered:
        chosen = max(numbered, key=lambda item: version(item['tag_name']))
    elif legacy:
        chosen = max(legacy, key=lambda item: item.get('published_at') or item.get('created_at') or '')
    else:
        raise ValueError('no stable application release found')
    names = [item['name'] for item in chosen.get('assets', [])]
    archives = [name for name in names if ARCHIVE.fullmatch(name)]
    if len(archives) != 1:
        raise ValueError('release requires exactly one application archive')
    archive = archives[0]
    commit = ARCHIVE.fullmatch(archive).group(1)
    tag = chosen['tag_name']
    if tag.startswith('app-') and tag[4:] != commit:
        raise ValueError('legacy tag does not match application archive')
    assets = {item['name']: item['url'] for item in chosen['assets']}
    required = [archive, archive+'.sha256', MANIFEST]
    if any(name not in assets for name in required):
        raise ValueError('release assets are incomplete')
    for name in required:
        if not re.fullmatch(r'https://api\.github\.com/repos/[^/\s]+/[^/\s]+/releases/assets/[0-9]+', assets[name]):
            raise ValueError('invalid GitHub release asset URL')
    return tag, commit, archive, *(assets[name] for name in required)


def validate_manifest(manifest, tag, archive, commit, actual_digest):
    if (manifest.get('schema') != 'formaliscope-app-release.v1'
            or not COMMIT.fullmatch(commit) or archive != f'formaliscope-app-{commit}.tar.gz'
            or manifest.get('archive') != archive or manifest.get('commit') != commit
            or manifest.get('sha256') != actual_digest):
        raise ValueError('application manifest does not match archive, commit or checksum')
    if version(tag):
        if manifest.get('version') != tag:
            raise ValueError('application manifest does not match release version')
    elif tag != f'app-{commit}':
        raise ValueError('invalid application release tag')


def package(commit, tag, output, repo=Path('.')):
    if not COMMIT.fullmatch(commit) or not version(tag):
        raise ValueError('package requires an exact commit and a vX.Y.Z version')
    output.mkdir(parents=True, exist_ok=True)
    archive_name = f'formaliscope-app-{commit}.tar.gz'
    archive = output / archive_name
    subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar.gz',
                    f'--prefix=formaliscope-{commit}/', f'--output={archive.resolve()}', commit], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive_name+'.sha256')).write_text(f'{digest}  {archive_name}\n', encoding='utf-8')
    (output / MANIFEST).write_text(json.dumps({'schema': 'formaliscope-app-release.v1',
        'version': tag, 'commit': commit, 'archive': archive_name, 'sha256': digest}, indent=2)+'\n', encoding='utf-8')
    return archive_name


def previous_release(releases, tag):
    """Use only published numbered versions below this run's version."""
    current = version(tag)
    if current is None:
        raise ValueError('release notes require a vX.Y.Z version')
    candidates = []
    for item in pages(releases):
        if not isinstance(item, dict):
            raise ValueError('GitHub releases must be objects')
        numbered = version(item.get('tag_name'))
        if (numbered is not None and numbered < current and item.get('published_at')
                and not item.get('draft') and not item.get('prerelease')):
            candidates.append(item['tag_name'])
    return max(candidates, key=version) if candidates else None


def release_notes(releases, tag, commit, repository, repo=Path('.')):
    """Render notes from the immutable Git range, including direct commits."""
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ValueError('release notes require an exact commit')
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or repository.split('/')[1] in ('.', '..')):
        raise ValueError('release notes require a GitHub owner/repository')
    previous = previous_release(releases, tag)

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding='utf-8').stdout.strip()

    resolved = git('rev-parse', '--verify', f'{commit}^{{commit}}')
    if resolved != commit:
        raise ValueError('release notes require the SHA of a commit object')
    base = None
    if previous:
        base = git('rev-parse', '--verify', f'refs/tags/{previous}^{{commit}}')
        ancestor = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', base, commit],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if ancestor.returncode == 1:
            raise ValueError(f'previous release {previous} is not an ancestor of {commit}')
        ancestor.check_returncode()
    interval = f'{base}..{commit}' if base else commit
    history = git('-c', 'log.showSignature=false', '-c', 'i18n.logOutputEncoding=UTF-8',
                  'log', '--no-merges', '--topo-order', '--reverse',
                  '--format=%H%x00%s', interval, '--')
    url = f'https://github.com/{repository}'
    lines = [f'Formaliscope {tag}', '', f'来源提交：[{commit}]({url}/commit/{commit})', '',
             '## Changelog', '']
    if previous:
        lines.extend([f'自 [{previous}]({url}/releases/tag/{previous}) 以来的变更：', ''])
    else:
        lines.extend(['首个正式版本包含的变更：', ''])
    for record in history.split('\n') if history else []:
        sha, subject = record.split('\0', 1)
        if not COMMIT.fullmatch(sha):
            raise ValueError('invalid commit in release notes history')
        subject = html.escape(' '.join(subject.split()), quote=False)
        subject = re.sub(r'([\\`*_{}\[\]()#+.!|~@-])', r'\\\1', subject)
        lines.append(f'- {subject} ([{sha[:7]}]({url}/commit/{sha}))')
    if not history:
        lines.append('- 无新增的非合并提交。')
    full = f'{url}/compare/{previous}...{commit}' if previous else f'{url}/tree/{commit}'
    lines.extend(['', f'[完整变更]({full})', ''])
    return '\n'.join(lines)


def write_release_notes(releases, tag, commit, repository, output, repo=Path('.')):
    content = release_notes(releases, tag, commit, repository, repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         prefix=f'.{output.name}.', dir=output.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    choose = commands.add_parser('next-tag')
    choose.add_argument('releases', type=Path)
    choose.add_argument('tags', type=Path)
    choose.add_argument('commit')
    select = commands.add_parser('select')
    select.add_argument('files', type=Path, nargs='+', help='paginated GitHub release response files')
    verify = commands.add_parser('validate')
    verify.add_argument('manifest', type=Path)
    verify.add_argument('tag')
    verify.add_argument('archive_name')
    verify.add_argument('commit')
    verify.add_argument('archive_file', type=Path)
    build = commands.add_parser('package')
    build.add_argument('--commit', required=True)
    build.add_argument('--version', required=True)
    build.add_argument('--output', type=Path, required=True)
    notes = commands.add_parser('release-notes')
    notes.add_argument('--releases', type=Path, required=True)
    notes.add_argument('--version', required=True)
    notes.add_argument('--commit', required=True)
    notes.add_argument('--repository', required=True)
    notes.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'next-tag':
            print(next_tag(json.loads(args.releases.read_text()), json.loads(args.tags.read_text()), args.commit))
        elif args.command == 'select':
            releases = [item for file in args.files for item in pages(json.loads(file.read_text()))]
            print('\n'.join(select_release(releases)))
        elif args.command == 'validate':
            validate_manifest(json.loads(args.manifest.read_text()), args.tag, args.archive_name,
                              args.commit, hashlib.sha256(args.archive_file.read_bytes()).hexdigest())
        elif args.command == 'package':
            print(package(args.commit, args.version, args.output))
        else:
            print(write_release_notes(json.loads(args.releases.read_text()), args.version,
                                      args.commit, args.repository, args.output))
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
