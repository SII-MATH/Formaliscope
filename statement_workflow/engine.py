"""Prepare isolated agent jobs, validate responses and publish analysis artifacts.

The initial adapter reads source-candidate snapshots. It does not execute Lean,
certify semantics, modify source files, or write human judgments.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from review_app.build import validate_snapshot
from review_app.statements import mask_comments

ROOT = Path(__file__).resolve().parent
STAGES = ('readback', 'audit')


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def config(path: Path | None = None) -> dict:
    value = read(path or ROOT / 'workflow.json')
    if value.get('schema') != 'statement-workflow-config.v1' or value.get('stages') != ['readback', 'audit', 'triage']:
        raise ValueError('unsupported workflow configuration')
    for field in ('max_context_nodes', 'timeout_seconds', 'max_attempts_per_stage', 'max_response_bytes'):
        if type(value.get(field)) is not int or value[field] < 1:
            raise ValueError(f'{field} must be a positive integer')
    command = value.get('agent_command')
    if command is not None and (not isinstance(command, list) or not command or
                                any(not isinstance(item, str) or not item for item in command)):
        raise ValueError('agent_command must be a non-empty argv list, or null')
    if command and (not isinstance(value.get('agent_revision'), str) or not value['agent_revision'].strip()):
        raise ValueError('configured agent needs an explicit agent_revision')
    return value


def without_comments(source: str) -> str:
    """Remove nested Lean comments, retaining string contents and positions."""
    out = list(source)
    i, depth, string = 0, 0, False
    while i < len(source):
        if string:
            if source[i] == '\\':
                i += 2
                continue
            if source[i] == '"':
                string = False
            i += 1
            continue
        if not depth and source[i] == '"':
            string = True
            i += 1
            continue
        if source.startswith('/-', i):
            depth += 1
            out[i:i+2] = '  '
            i += 2
            continue
        if depth and source.startswith('-/', i):
            depth -= 1
            out[i:i+2] = '  '
            i += 2
            continue
        if not depth and source.startswith('--', i):
            end = source.find('\n', i)
            end = len(source) if end < 0 else end
            out[i:end] = ' ' * (end-i)
            i = end
            continue
        if depth and source[i] != '\n':
            out[i] = ' '
        i += 1
    return ''.join(out).strip()


def statement_source(card: dict) -> str:
    clean = without_comments((card.get('lean') or {}).get('source', ''))
    if card['kind'] not in ('theorem', 'lemma'):
        return clean
    masked = mask_comments(clean)
    depth = 0
    for i, char in enumerate(masked):
        if char in '({[':
            depth += 1
        elif char in ')}]':
            depth -= 1
        elif depth == 0 and (masked.startswith(':=', i) or
                             (masked.startswith('where', i) and (i == 0 or not re.match(r'[\w\']', masked[i-1])) and
                              re.match(r'\bwhere\b', masked[i:]))):
            return clean[:i].rstrip()
    raise ValueError(f'cannot safely isolate theorem statement: {card["declaration"]}')


def closure(start: str, index: dict, limit: int) -> tuple[list[str], list[str]]:
    todo, seen, found, missing = [start], set(), [], set()
    while todo:
        name = todo.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name not in index or len(found) >= limit:
            missing.add(name)
            continue
        found.append(name)
        todo.extend(sorted(index[name].get('dependencies', [])))
    return found, sorted(missing)


def prepare(snapshot_path: Path, output: Path, cfg: dict, declarations: list[str],
            references_path: Path | None = None) -> dict:
    snapshot = read(snapshot_path)
    validate_snapshot(snapshot)
    if snapshot.get('review_mode') != 'statement':
        raise ValueError('a Statement snapshot is required')
    index = {c['id']: c for c in snapshot['cards']}
    named = {c['declaration']: c['id'] for c in snapshot['cards']}
    goals = sorted(c['id'] for c in snapshot['cards'] if c.get('main_target'))
    if declarations:
        selected = [name if name in index else named.get(name) for name in declarations]
        if any(name is None for name in selected):
            raise ValueError('requested declaration was not found')
        selected = sorted(set(selected))
    else:
        selected = sorted(set(goals + [dep for goal in goals for dep in index[goal].get('dependencies', [])]))
    if not selected:
        raise ValueError('no main targets; specify --declaration explicitly')
    references = read(references_path) if references_path else {}
    if not isinstance(references, dict) or any(key not in index for key in references):
        raise ValueError('references must map existing statement IDs to reference records')
    for key, value in references.items():
        source_hash = hashlib.sha256(index[key]['lean']['source'].encode()).hexdigest()
        if not isinstance(value, dict) or value.get('source_sha256') != source_hash or not value.get('text') or not value.get('source'):
            raise ValueError(f'reference must be non-empty, attributed and bound to source: {key}')
    if (output / 'manifest.json').exists():
        raise ValueError('batch already exists; resume it, or prepare a new batch directory')
    tasks = []
    for selected_id in selected:
        card = index[selected_id]
        names, omitted = closure(selected_id, index, cfg['max_context_nodes'])
        evidence = []
        for name in names:
            row = index[name]
            evidence.append({'id': 'lean:'+digest(name)[:20], 'declaration_id': name,
                             'declaration': row['declaration'], 'kind': row['kind'],
                             'source': statement_source(row),
                             'location': {'file': row['lean']['file'], 'line': row['lean']['line']}})
        # Candidate graph may include proof references and miss instances or
        # field projections. Always expose this limitation to both stages.
        context = {'schema': 'statement-agent-context.v1', 'declaration_id': selected_id,
                   'evidence': evidence, 'origin': 'source-reference-candidates',
                   'completeness': 'unknown', 'omitted': omitted,
                   'limitations': ['not_elaborated', 'implicit_binders_and_instances_not_verified',
                                   'source_graph_may_include_proof_references'],
                   'expected_toolchain': cfg.get('expected_toolchain'),
                   'dependency_lock_digest': snapshot.get('dependency_lock_digest')}
        context_hash = digest(context)
        folder = digest(selected_id)[:24]
        task = {'id': folder, 'declaration_id': selected_id, 'context_fingerprint': context_hash,
                'context': context, 'reference': references.get(selected_id),
                'main_target': selected_id in goals,
                'direct_goal_dependency': any(selected_id in index[g].get('dependencies', []) for g in goals),
                'role_hint': card.get('role'),
                'proof_source_status': {'contains_sorry': bool(re.search(r'\b(sorry|admit)\b', mask_comments(card['lean']['source']))),
                                        'kind': card['kind'], 'verified': False},
                'source_commit': snapshot['source_commit']}
        write(output / 'tasks' / folder / 'task.json', task)
        tasks.append({'id': folder, 'declaration_id': selected_id})
    manifest = {'schema': 'statement-workflow-batch.v1', 'source_commit': snapshot['source_commit'],
                'snapshot_digest': snapshot['digest'], 'tasks': tasks, 'workflow_version': cfg['version'],
                'config': cfg, 'prompt_digests': {stage: digest((ROOT / 'prompts' / f'{stage}.txt').read_text()) for stage in STAGES}}
    write(output / 'manifest.json', manifest)
    return manifest


def job(task: dict, stage: str, prior: dict | None = None) -> dict:
    prompt = (ROOT / 'prompts' / f'{stage}.txt').read_text(encoding='utf-8')
    value = {'schema': 'statement-agent-job.v1', 'stage': stage,
             'declaration_id': task['declaration_id'], 'context_fingerprint': task['context_fingerprint'],
             'instructions': prompt + '\n返回对象须逐字回填输入 job_id，并在 model 字段记录实际模型名称。',
             'context': task['context'], 'fresh_context_required': True}
    if stage == 'audit':
        value.update(readback=prior, reference=task['reference'], proof_source_status=task['proof_source_status'])
    value['job_id'] = digest(value)
    return value


def runner_digest(cfg: dict) -> str:
    return digest({'command': cfg['agent_command'], 'revision': cfg['agent_revision']})


def checked_task(folder: Path, item: dict, manifest: dict) -> dict:
    task = read(folder / 'task.json')
    if (task['id'] != item['id'] or task['declaration_id'] != item['declaration_id'] or
            task['source_commit'] != manifest['source_commit'] or
            digest(task['context']) != task['context_fingerprint'] or
            task['context']['declaration_id'] != task['declaration_id']):
        raise ValueError('task identity or context changed; prepare a new batch')
    return task


def checked_batch(output: Path, cfg: dict) -> dict:
    manifest = read(output / 'manifest.json')
    if manifest.get('schema') != 'statement-workflow-batch.v1':
        raise ValueError('unsupported batch')
    for stage in STAGES:
        if digest((ROOT / 'prompts' / f'{stage}.txt').read_text()) != manifest['prompt_digests'][stage]:
            raise ValueError('prompts changed; prepare a new batch')
    effective = dict(cfg)
    for key in ('agent_command', 'agent_revision'):
        effective[key] = manifest['config'][key]
    if effective != manifest['config']:
        raise ValueError('workflow configuration changed; prepare a new batch')
    return manifest


def import_result(output: Path, cfg: dict, stage: str, result_path: Path) -> dict:
    """Import one separately executed agent result, under the same contract."""
    manifest = checked_batch(output, cfg)
    value = read(result_path)
    item = next((item for item in manifest['tasks'] if item['declaration_id'] == value.get('declaration_id')), None)
    if item is None or stage not in STAGES:
        raise ValueError('unknown result task or stage')
    folder = output / 'tasks' / item['id']
    task = checked_task(folder, item, manifest)
    prior = None
    if stage == 'audit':
        previous_job = job(task, 'readback')
        if read(folder / 'readback.cache.json') != {'job_id': previous_job['job_id'], 'runner_digest': runner_digest(cfg)}:
            raise ValueError('readback is missing or belongs to a different runner')
        prior = read(folder / 'readback.result.json')
        validate_result(prior, previous_job)
    request = job(task, stage, prior)
    validate_result(value, request)
    write(folder / f'{stage}.job.json', request)
    write(folder / f'{stage}.result.json', value)
    write(folder / f'{stage}.cache.json', {'job_id': request['job_id'], 'runner_digest': runner_digest(cfg)})
    write(folder / f'{stage}.state.json', {'job_id': request['job_id'], 'runner_digest': runner_digest(cfg),
                                         'attempts': 0, 'status': 'complete', 'origin': 'imported', 'error': None})
    # Revalidate all artifacts and export the next stage without running an agent.
    return run(output, cfg, export_only=True)


def validate_result(value: dict, request: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError('agent result must be an object')
    for key in ('job_id', 'declaration_id', 'context_fingerprint'):
        if value.get(key) != request[key]:
            raise ValueError(f'agent result {key} mismatch')
    if not isinstance(value.get('model'), str) or not value['model'].strip():
        raise ValueError('agent result must name its model')
    allowed = {e['id'] for e in request['context']['evidence']}
    def check_evidence(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key == 'evidence_ids' and (not isinstance(child, list) or
                                             any(e not in allowed for e in child)):
                    raise ValueError('unknown evidence ID')
                check_evidence(child)
        elif isinstance(item, list):
            for child in item:
                check_evidence(child)
    check_evidence(value)
    if request['stage'] == 'readback':
        if value.get('status') not in ('draft', 'incomplete', 'blocked') or not isinstance(value.get('text'), str) or not value['text'].strip():
            raise ValueError('readback needs text and a draft/incomplete/blocked status')
        for field in ('binder_coverage', 'definition_expansions', 'edge_cases', 'unresolved', 'risk_candidates', 'evidence_ids'):
            if not isinstance(value.get(field), list):
                raise ValueError(f'readback must include {field}')
    else:
        if value.get('coverage') not in ('complete', 'incomplete', 'unknown'):
            raise ValueError('invalid audit coverage')
        if value.get('alignment') not in ('no_reference', 'not_assessed', 'no_discrepancy_detected', 'suspected_mismatch', 'confirmed_mismatch'):
            raise ValueError('invalid audit alignment')
        if not request['reference'] and value['alignment'] != 'no_reference':
            raise ValueError('cannot claim alignment without a reference')
        if not isinstance(value.get('findings'), list) or not isinstance(value.get('unresolved'), list):
            raise ValueError('audit must include findings and unresolved')
    for field in ('risk_candidates', 'findings'):
        for finding in value.get(field, []):
            if not isinstance(finding, dict) or not finding.get('evidence_ids'):
                raise ValueError('findings require specific evidence IDs')
    if 'verdict' in value or value.get('status') == 'human-approved':
        raise ValueError('agent output cannot contain a human verdict')


def triage(task: dict, readback: dict, audit: dict) -> dict:
    incomplete = task['context']['completeness'] != 'complete' or readback['status'] != 'draft' or audit['coverage'] != 'complete'
    findings = readback['risk_candidates'] + audit['findings']
    # Model-only accusations remain review candidates. R3 will require a
    # verified-check adapter, intentionally absent from this initial version.
    risk = 'R2' if findings else 'unknown' if incomplete else 'R0'
    reasons = [{'code': 'agent_finding_requires_review', 'finding': f} for f in findings]
    if incomplete:
        reasons.append({'code': 'context_or_coverage_incomplete'})
    priority = 'P0' if task['main_target'] else 'P1' if task['direct_goal_dependency'] else 'P2'
    priority_reasons = ['configured_main_target' if task['main_target'] else
                        'direct_source_candidate_of_main_target' if task['direct_goal_dependency'] else 'selected_for_review']
    return {'schema': 'statement-analysis.v1', 'declaration_id': task['declaration_id'],
            'source_commit': task['source_commit'], 'context_fingerprint': task['context_fingerprint'],
            'readback': readback, 'audit': audit, 'proof_status': task['proof_source_status'],
            'risk': {'level': risk, 'reasons': reasons, 'analysis_incomplete': incomplete},
            'priority': {'level': priority, 'reasons': priority_reasons},
            'human_review': 'not_assessed', 'policy_version': 'triage-scaffold.v1'}


def run(output: Path, cfg: dict, *, limit: int = 1, export_only: bool = False) -> dict:
    manifest = checked_batch(output, cfg)
    # Policy settings belong to the batch; only the runner can be attached
    # after preparation. Agent identity/runner changes invalidate stage cache.
    command = cfg['agent_command']
    if not export_only and not command:
        raise ValueError('agent_command is not configured; use --export-only to prepare jobs')
    if limit < 1:
        raise ValueError('limit must be positive')
    report = {'completed': 0, 'exported': 0, 'cached': 0, 'failed': 0}
    processed = 0
    for item in manifest['tasks']:
        folder = output / 'tasks' / item['id']
        task = checked_task(folder, item, manifest)
        prior, done, touched = None, True, False
        for stage in STAGES:
            request = job(task, stage, prior)
            write(folder / f'{stage}.job.json', request)
            response_path = folder / f'{stage}.result.json'
            cache_path = folder / f'{stage}.cache.json'
            runner_hash = runner_digest(cfg)
            cached = False
            if response_path.exists() and cache_path.exists():
                metadata = read(cache_path)
                if metadata == {'job_id': request['job_id'], 'runner_digest': runner_hash}:
                    prior = read(response_path)
                    validate_result(prior, request)
                    cached = True
                    report['cached'] += 1
            if cached:
                continue
            if export_only:
                report['exported'] += 1
                done = False
                break
            if processed >= limit:
                done = False
                break
            state_path = folder / f'{stage}.state.json'
            state = read(state_path) if state_path.exists() else {}
            if state.get('job_id') != request['job_id'] or state.get('runner_digest') != runner_hash:
                state = {'job_id': request['job_id'], 'runner_digest': runner_hash, 'attempts': 0}
            if state['attempts'] >= cfg['max_attempts_per_stage']:
                report['failed'] += 1
                done = False
                break
            state.update(attempts=state['attempts']+1, status='running')
            write(state_path, state)
            touched = True
            try:
                # A fresh process per item/stage; adapter must also start a
                # fresh agent session and return exactly one JSON object.
                result = subprocess.run(command, input=json.dumps(request, ensure_ascii=False),
                                        text=True, encoding='utf-8', stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=cfg['timeout_seconds'],
                                        cwd=ROOT, shell=False)
                if result.returncode:
                    raise ValueError(f'agent process exited {result.returncode}')
                if len(result.stdout.encode()) > cfg['max_response_bytes']:
                    raise ValueError('agent response exceeded configured size')
                prior = json.loads(result.stdout)
                validate_result(prior, request)
                write(response_path, prior)
                write(cache_path, {'job_id': request['job_id'], 'runner_digest': runner_hash})
                state.update(status='complete', error=None)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                state.update(status='failed', error=str(error))
                report['failed'] += 1
                done = False
            write(state_path, state)
            if not done:
                break
        if done:
            result = triage(task, read(folder / 'readback.result.json'), read(folder / 'audit.result.json'))
            write(folder / 'analysis.json', result)
            report['completed'] += 1
        if not export_only and not done and processed >= limit:
            break
        if touched:
            processed += 1
    # Publish only current, fully validated analyses. An old response cannot
    # remain in the current export after a runner change or failed rerun.
    analyses = []
    for item in manifest['tasks']:
        folder = output / 'tasks' / item['id']
        task = checked_task(folder, item, manifest)
        try:
            previous = None
            for stage in STAGES:
                request = job(task, stage, previous)
                if read(folder / f'{stage}.cache.json') != {'job_id': request['job_id'], 'runner_digest': runner_digest(cfg)}:
                    break
                previous = read(folder / f'{stage}.result.json')
                validate_result(previous, request)
            else:
                analyses.append(triage(task, read(folder / 'readback.result.json'), previous))
        except (FileNotFoundError, ValueError):
            continue
    write(output / 'analysis.json', {'schema': 'statement-analysis-batch.v1',
                                   'source_commit': manifest['source_commit'],
                                   'snapshot_digest': manifest['snapshot_digest'], 'analyses': analyses})
    write(output / 'status.json', report)
    return report
