from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3

from .build import compare_snapshots, normalize_snapshot, write_snapshot
from .database import database_schema_version, initialize
from .judgments import backfill_review_basis
from .server import serve
from .snapshot_artifacts import candidate_output_path, write_candidate_artifact
from .storage import create_backup, install_snapshot, verify_backup


def main():
    app_root = Path(__file__).resolve().parents[1]
    default_data_dir = Path(os.environ.get("REVIEW_DATA_DIR", app_root / ".review"))
    parser = argparse.ArgumentParser(description="Formaliscope Lean Statement review")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build a new evidence candidate; install-snapshot activates it")
    build.add_argument("--source", type=Path, required=True,
                       help="path to a separate reviewed Lean source checkout")
    build_output = build.add_mutually_exclusive_group(required=True)
    build_output.add_argument("--output", type=Path,
                              help="new candidate artifact outside reviewed source and runtime data")
    build_output.add_argument("--data-dir", type=Path,
                              help="compatibility: new candidate directory; existing snapshot or review data is refused")
    build.add_argument("--require-clean", action="store_true",
                       help="refuse a reviewed-source checkout with local changes")
    build.add_argument('--statements', action='store_true', help='index all KIP126 and KIPBase declarations for Statement review')
    build.add_argument('--source-commit', help='known 40-character commit for an exact source archive (Statement preview only)')
    build.add_argument('--annotations', type=Path, help='source-hash-bound back-translation drafts for Statement review')
    build.add_argument('--repository-config', type=Path, help='repository id/name, scan roots, main targets and topics JSON')
    build.add_argument('--expect-commit', help='refuse a source checkout that does not match this exact commit')
    collection = sub.add_parser('bundle-snapshots', help='combine frozen repository versions into a new candidate')
    collection.add_argument('--snapshot', type=Path, action='append', required=True)
    collection.add_argument('--default', help='initial dataset id: repository@commit')
    collection.add_argument('--output', type=Path, required=True)
    grant = sub.add_parser('dataset-admin', help='grant or revoke administrative access to one repository version')
    grant.add_argument('--data-dir', type=Path, default=default_data_dir)
    grant.add_argument('--dataset', required=True)
    grant.add_argument('--reviewer', required=True, help='existing internal reviewer id')
    grant.add_argument('--revoke', action='store_true')
    start = sub.add_parser("serve", help="serve the local review site")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)
    start.add_argument("--data-dir", type=Path, default=default_data_dir,
                       help="persistent data directory (or REVIEW_DATA_DIR)")
    start.add_argument('--preview', action='store_true', help='explicit local name-only frontend preview, without sending email')
    start.add_argument('--admin-email', action='append', default=[], help='verified email allowed to access administrative summary')
    backup = sub.add_parser("backup", help="create a consistent online backup")
    backup.add_argument("--data-dir", type=Path, default=default_data_dir,
                        help="persistent data directory (or REVIEW_DATA_DIR)")
    backup.add_argument("--output", type=Path, required=True,
                        help="directory that will contain timestamped backups")
    backup.add_argument("--keep", type=int,
                        help="retain this many verified v2 backups for this data directory; default keeps all")
    verification = sub.add_parser("verify-backup", help="read-only v2 backup verification before transfer or restore")
    verification.add_argument("--directory", type=Path, required=True,
                              help="complete v2 timestamped backup directory")
    install = sub.add_parser("install-snapshot", help="validate and install a reviewed-source snapshot")
    install.add_argument("--file", type=Path, required=True, help="snapshot artifact to install")
    install.add_argument("--data-dir", type=Path, default=default_data_dir,
                         help="persistent data directory (or REVIEW_DATA_DIR)")
    install.add_argument("--allow-dirty-source", action="store_true",
                         help="allow a development snapshot from local changes or an unverified source archive")
    install.add_argument('--legacy-kip126-only', action='store_true', help='automatic legacy puller guard: refuse repository datasets or collections')
    migrate = sub.add_parser("migrate", help="apply pending database migrations")
    migrate.add_argument("--data-dir", type=Path, default=default_data_dir,
                         help="persistent data directory (or REVIEW_DATA_DIR)")
    check = sub.add_parser('preflight', help='read-only deployment readiness checks; never send mail or change data')
    check.add_argument('--data-dir', type=Path, default=default_data_dir)
    check.add_argument('--preview', action='store_true')
    check.add_argument('--host', default='127.0.0.1')
    check.add_argument('--admin-email', action='append', default=[])
    check.add_argument('--legacy-blueprint', action='store_true', help='explicitly check an existing Blueprint deployment')
    for command, help_text in [('validate-enrichment', 'check Agent annotations against a frozen Statement snapshot'),
                               ('enrich-snapshot', 'build a separate candidate snapshot from checked Agent annotations')]:
        operation = sub.add_parser(command, help=help_text)
        operation.add_argument('--snapshot', type=Path, required=True)
        operation.add_argument('--file', type=Path, required=True, help='statement-enrichment.v1 or v2 annotation file')
        if command == 'enrich-snapshot':
            operation.add_argument('--output', type=Path, required=True, help='new candidate artifact; input is never overwritten')
    assessments = sub.add_parser('import-agent-assessments',
        help='explicitly store private v2 machine assessments; does not install a snapshot')
    assessments.add_argument('--snapshot', type=Path, required=True,
                             help='original frozen base snapshot used by the batch, not its enriched candidate')
    assessments.add_argument('--file', type=Path, required=True,
                             help='collected statement-enrichment.v2 result with original worker records')
    assessments.add_argument('--data-dir', type=Path, required=True,
                             help='explicit target persistent data directory; requires the schema 9 application')
    for command, description in [('create-admin', 'create an administrator with a private recovery file'),
                                 ('bind-recovery', 'bind an existing reviewer to name login without moving records')]:
        operation = sub.add_parser(command, help=description)
        operation.add_argument('--data-dir', type=Path, default=default_data_dir)
        operation.add_argument('--name', required=True)
        operation.add_argument('--output', type=Path, required=True, help='new private recovery file; never overwritten')
        if command == 'bind-recovery':
            operation.add_argument('--reviewer', required=True, help='exact existing internal reviewer key')
            operation.add_argument('--admin', action='store_true', help='explicitly grant operator role')
    args = parser.parse_args()
    if args.command == 'bundle-snapshots':
        from .repositories import make_collection
        from .enrichment_v2 import read_document
        try:
            result = make_collection([read_document(path) for path in args.snapshot], default=args.default)
            output = write_candidate_artifact(result, args.output, input_artifacts=tuple(args.snapshot))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(json.dumps({'datasets': len(result['datasets']), 'snapshot_digest': result['digest'], 'output': str(output)}, ensure_ascii=False))
        return
    if args.command == "verify-backup":
        try:
            directory = args.directory.expanduser().absolute()
            manifest = verify_backup(directory)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            parser.error(str(exc))
        print(json.dumps({'valid': True, 'directory': str(directory),
                          'schema': manifest['schema'],
                          'database_schema_version': manifest['database_schema_version'],
                          'snapshot_digest': manifest['snapshot_digest']}, ensure_ascii=False))
        return
    if args.command == 'import-agent-assessments':
        from .agent_assessments import import_agent_assessments
        from .enrichment_v2 import read_document
        try:
            base = read_document(args.snapshot)
            document = read_document(args.file)
            result = import_agent_assessments(args.data_dir, base, document)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            parser.exit(1, f'{error}\n')
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command in {'validate-enrichment', 'enrich-snapshot'}:
        from .enrichment import enrich_snapshot, validate_enrichment
        from .enrichment_v2 import read_document
        try:
            base = read_document(args.snapshot)
            annotation = read_document(args.file)
            count = len(validate_enrichment(annotation, base))
            if args.command == 'enrich-snapshot':
                candidate = enrich_snapshot(base, annotation)
                output = write_candidate_artifact(candidate, args.output,
                                                  input_artifacts=(args.snapshot, args.file))
                print(json.dumps({'annotations': count, 'snapshot_digest': candidate['digest'],
                                  'comparison': candidate['comparison'], 'output': str(output),
                                  'install_command': shlex.join(['python3', '-m', 'review_app', 'install-snapshot',
                                                                 '--file', str(output), '--data-dir', '/path/to/review-data'])},
                                 ensure_ascii=False))
            else:
                print(json.dumps({'valid': True, 'annotations': count}, ensure_ascii=False))
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.exit(1, f'{error}\n')
        return
    if args.command == "build":
        source = args.source.expanduser().resolve()
        if not source.is_dir():
            parser.error(f'--source is not a source directory: {source}')
        if (not args.repository_config and not (source / "KIP126").is_dir()) or (not args.statements and not (source / "blueprint/src/content.tex").is_file()):
            parser.error(f"--source is not a KIP126 checkout: {source}")
        requested_output = args.output if args.output is not None else args.data_dir / "snapshot.json"
        try:
            output = candidate_output_path(requested_output, source_tree=source)
            if args.repository_config and not args.statements:
                parser.error('--repository-config requires --statements')
            if args.expect_commit:
                from .build import _git_head
                import re
                if not re.fullmatch('[0-9a-f]{40}', args.expect_commit) or _git_head(source) != args.expect_commit:
                    parser.error('--expect-commit does not match the reviewed checkout HEAD')
            if args.statements:
                import re
                from .statements import compile_statements
                if args.source_commit and (not re.fullmatch('[0-9a-f]{40}', args.source_commit) or args.require_clean):
                    parser.error('--source-commit requires an exact archive commit and cannot be combined with --require-clean')
                from .enrichment_v2 import read_document
                repository = read_document(args.repository_config) if args.repository_config else None
                result = compile_statements(source, source_commit=args.source_commit, annotations=args.annotations, repository=repository)
                if args.require_clean and result['source_dirty']:
                    parser.error('reviewed source has uncommitted changes')
                result['comparison'] = compare_snapshots(None, result)
                write_candidate_artifact(result, output, source_tree=source)
            else:
                if args.source_commit:
                    parser.error('--source-commit is only supported for --statements')
                result = write_snapshot(source, output, require_clean=args.require_clean)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"{len(result['cards'])} review cards, {result['unlinked_nodes']} unlinked Blueprint nodes")
        comparison = result["comparison"]
        print("candidate content: " + ", ".join(f"{name}={count}" for name, count in comparison.items()))
        print(f"candidate snapshot {result['digest']} -> {output}")
        print('Next: ' + shlex.join(['python3', '-m', 'review_app', 'install-snapshot',
                                     '--file', str(output), '--data-dir', '/path/to/review-data']))
        return
    data_dir = args.data_dir.expanduser().resolve()
    if args.command == 'dataset-admin':
        from .data_lock import data_lock
        from .repositories import select_dataset, dataset_id
        from .database import connect
        from contextlib import closing
        try:
            with data_lock(data_dir):
                installed = normalize_snapshot(json.loads((data_dir / 'snapshot.json').read_text()))
                selected = select_dataset(installed, args.dataset)
                initialize(data_dir / 'judgments.sqlite3')
                with closing(connect(data_dir / 'judgments.sqlite3')) as db:
                    known = db.execute('SELECT 1 FROM reviewer_profiles WHERE reviewer=?', (args.reviewer,)).fetchone()
                    if not known:
                        raise ValueError('reviewer must be an existing registered identity')
                    if args.revoke:
                        db.execute('DELETE FROM dataset_admins WHERE dataset_id=? AND reviewer=?', (dataset_id(selected), args.reviewer))
                    else:
                        db.execute('INSERT OR IGNORE INTO dataset_admins VALUES (?, ?)', (dataset_id(selected), args.reviewer))
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.error(str(exc))
        print(json.dumps({'dataset_id': dataset_id(selected), 'reviewer': args.reviewer, 'granted': not args.revoke}))
        return
    if args.command in {'create-admin', 'bind-recovery'}:
        from .name_auth import NameAuthStore, valid_name
        if not valid_name(args.name):
            parser.error('请输入 1–60 字的姓名，不含控制字符')
        output = args.output.expanduser().absolute()
        # Exclusive creation also refuses symlinks. Never print credentials to logs.
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            parser.exit(1, 'Cannot create a new private recovery file; choose a new --output path.\n')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as secret_file:
                auth = NameAuthStore(data_dir / 'judgments.sqlite3')
                token, recovery = auth.create_identity(args.name,
                    admin=args.command == 'create-admin' or args.admin,
                    existing_reviewer=getattr(args, 'reviewer', None))
                secret_file.write(recovery + '\n')
                secret_file.flush()
                os.fsync(secret_file.fileno())
                reviewer = auth.session_reviewer(token)
                auth.logout(token)
        except (OSError, ValueError) as error:
            output.unlink(missing_ok=True)
            parser.exit(1, str(error) + '\n')
        print(json.dumps({'reviewer': reviewer, 'name': args.name.strip(), 'recovery_file': str(output)}, ensure_ascii=False))
        return
    snapshot = data_dir / "snapshot.json"
    if args.command == "serve":
        if not snapshot.exists():
            parser.error(f"snapshot missing in {data_dir}; build a candidate and use install-snapshot first")
        if args.preview and json.loads(snapshot.read_text()).get('review_mode') != 'statement':
            parser.error('--preview requires a Statement snapshot; build with --statements')
        emails = args.admin_email + os.environ.get('REVIEW_ADMIN_EMAILS', '').split(',')
        from .auth import normalize_email
        admins = frozenset(filter(None, (normalize_email(email) for email in emails)))
        serve(snapshot, data_dir / "judgments.sqlite3",
              app_root / "review_app" / "static", args.host, args.port,
              preview=args.preview, admin_emails=admins)
    elif args.command == "backup":
        try:
            destination = create_backup(data_dir, args.output.expanduser().absolute(), keep=args.keep)
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.error(str(exc))
        print(f"consistent backup -> {destination}")
    elif args.command == "install-snapshot":
        try:
            installed, comparison = install_snapshot(
                args.file.expanduser().resolve(), data_dir,
                allow_dirty_source=args.allow_dirty_source, legacy_kip126_only=args.legacy_kip126_only)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        print("source update: " + ", ".join(f"{name}={count}" for name, count in comparison.items()))
        print(f"installed snapshot {installed['digest']} from source {installed['source_commit']}")
    elif args.command == 'preflight':
        from .preflight import run_preflight
        result = run_preflight(data_dir, preview=args.preview, host=args.host,
                               admin_emails=args.admin_email, require_statements=not args.legacy_blueprint)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['ready']:
            parser.exit(1)
    else:
        from .data_lock import data_lock
        if not snapshot.is_file():
            parser.error(f"snapshot missing in {data_dir}; install it before migrating")
        with data_lock(data_dir):
            if not snapshot.is_file():
                parser.error(f"snapshot missing in {data_dir}; install it before migrating")
            before = database_schema_version(data_dir / "judgments.sqlite3")
            initialize(data_dir / "judgments.sqlite3")
            active_snapshot = normalize_snapshot(json.loads(snapshot.read_text(encoding="utf-8")))
            backfilled = backfill_review_basis(data_dir / "judgments.sqlite3", active_snapshot)
            after = database_schema_version(data_dir / "judgments.sqlite3")
        print(f"database schema: {before} -> {after}")
        print(f"review bases backfilled: {backfilled}")


if __name__ == "__main__":
    main()
