"""Bounded exploratory comparison at actual unresolved Git conflicts.

Exactly one collection trajectory for each of two preselected tasks. A first
persistent conflict with both agents still active supplies one paired starting
state. Unavailable states are retained, never replaced or forced in paid runs.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import math
import os
from pathlib import Path
import re
import shutil
import sys

from budget_guard import write_json
from conflict_evaluator import get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import archive_incomplete, incomplete_seconds
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed
from task_specs import ROOT, COMMIT

RUNS = ROOT / 'artifacts/conflict_runs'
VERSION = 'luna_conflict_v1'
MODEL = 'gpt-6-luna'
TASKS = {k: get_task(k) for k in ('llama_index17070_f1_f3', 'click2068_f4_f10')}
CONDITIONS = ('strongshare', 'integrator')
VARIANTS = {c: 'conflict_' + c + '_v1' for c in CONDITIONS}
TOTAL_SECONDS = 1800
ORIGIN = ROOT / 'artifacts/publication_runs/luna_publication_v1'
PROOF = ROOT / 'artifacts/conflict_offline_validation.json'
NEW_SOURCES = ('run_luna_conflict.py', 'conflict_checkpoint.py', 'conflict_packet.py',
               'conflict_runtime.py', 'conflict_evaluator.py')
PREFIXES = {
    'llama_index17070_f1_f3': ROOT / 'artifacts/handoff_expansion_runs/luna_collaboration_v1/llama_index17070_f1_f3/prefix',
    'click2068_f4_f10': ROOT / 'artifacts/selector_runs/luna_selector_v1/click2068_f4_f10/prefix',
}


def source_manifest():
    historical = read_json(ORIGIN / 'protocol.json')['source_manifest']
    for name, expected in historical.items():
        if file_hash(ROOT / name) != expected or file_hash(ORIGIN / 'source_snapshot' / name) != expected:
            raise RuntimeError('Historical frozen source changed: ' + name)
    return {**historical, **{name: file_hash(ROOT / name) for name in NEW_SOURCES}}


def early_references():
    from run_handoff_expansion import validate_checkpoint
    result = {}
    for key, prefix in PREFIXES.items():
        task = TASKS[key]
        record = read_json(prefix.parent / 'prefix_record.json')
        cp, config, status = [read_json(prefix / name) for name in
                              ('checkpoint.json', 'task_config.json', 'prefix/status.json')]
        validate_checkpoint(cp, task)
        if (record['task'] != task.as_dict() or record['status'] != 'completed'
                or record['checkpoint_sha256'] != digest(cp)
                or config.get('offline_selftest') is not False or status['status'] != 'completed'
                or record['elapsed_seconds'] != status['elapsed_seconds']
                or file_hash(prefix / 'shared_git.tar') != cp['git_archive_sha256']
                or record['all_attempt_usage'] != usage(prefix)
                or record['accepted_usage'] != usage(prefix, 'prefix')):
            raise RuntimeError('Historical early prefix identity changed: ' + key)
        rows = read_json(prefix / 'usage_ledger.json')['requests']
        if not rows or any(row.get('model') != MODEL for row in rows):
            raise RuntimeError('Expected original real Luna prefix: ' + key)
        for name, expected in record['files_sha256'].items():
            if file_hash(prefix / name) != expected:
                raise RuntimeError('Historical early prefix changed: ' + key + '/' + name)
        result[key] = {'directory': str(prefix), 'record': record,
            'record_sha256': file_hash(prefix.parent / 'prefix_record.json'),
            'new_billable_requests': 0, 'historical_prefix_reexecuted': False}
    return result


def schedule(offline=False):
    orders = (CONDITIONS,) if offline else (CONDITIONS, tuple(reversed(CONDITIONS)))
    return [{'task': key, 'repeat': r, 'condition': c}
        for key in TASKS for r, order in enumerate(orders, 1) for c in order]


def protocol_for(offline=False):
    old = read_json(ORIGIN / 'protocol.json')
    fields = ('provider', 'endpoint', 'reasoning_effort', 'temperature', 'top_p',
        'max_completion_tokens', 'service_tier', 'prompt_cache_options', 'pricing_usd_per_million')
    return {'version': VERSION, 'offline_selftest': offline, 'dataset_commit': COMMIT,
        'model': 'offline-scripted-model' if offline else MODEL,
        **{k: old[k] for k in fields}, 'tasks': {k: t.as_dict() for k, t in TASKS.items()},
        'jobs': schedule(offline), 'early_references': early_references(),
        'condition_versions': VARIANTS, 'submission_policy': 'authoritative_agent1_only',
        'terminal_policy': 'both_active', 'total_execution_seconds': TOTAL_SECONDS,
        'publication_guard_version': old['publication_guard_version'],
        'money_limit': None, 'request_limit': None,
        'question': 'At an actual still-unresolved Git conflict, does explicitly assigning integration to agent1 improve on continued self-coordination?',
        'scope': 'Exploratory mechanism pilot on two previously observed task IDs. These are development tasks, not held-out validation. One collection trajectory and at most one checkpoint per task; two samples of each continuation.',
        'collection': 'Restore each fixed original Luna early prefix. Before the first new query, give both agents the same predesignated agent1-only complete-code delivery contract. Retain original feature assignments and live read-only peer evidence. Inspect the current unmerged index only after both current tool steps finish. Stop at the first still-unresolved conflict; both agents must remain active. Stop as unavailable when an agent ends or the allowance expires without this state. No paid conflict injection, hidden-test selection, resampling, replacement task, or search for a more favorable conflict.',
        'common_information': 'Preserve full messages and tool state, both workspaces including Git index/operation metadata, shared Git remote, mailbox and complete allowed source/scratch evidence. Both actions receive the exact same checkpoint and packet. A conflict repaired inside one tool command is not an eligible persistent conflict.',
        'actions': 'StrongShare retains feature responsibilities and chooses how to resolve the conflict through normal communication. Integrator explicitly assigns agent1 to resolve/integrate the complete submission and agent2 to assist. Both remain active; agent1 is the predesignated publisher in both conditions.',
        'scoring': 'Identical pinned official feature tests on actual agent1 published complete code relative to original task base. Native test_solo is only the one-patch grading adapter. No flat re-merge, peer winner selection, worktree fallback, automatic repair, publication or agent retirement.',
        'time_rule': '1800 seconds total including accepted historical early prefix, new collection through checkpoint/packet preparation, and restored continuation initialization. Both actions receive the same remainder. In-flight requests can finish after deadline. Failed infrastructure attempts are retained and reported separately. Exhausted eligible continuations are failures, not eligibility exclusions.',
        'order': 'Collect both tasks before any new continuation outcomes. Per task r1 StrongShare then Integrator; r2 Integrator then StrongShare. No common random seed; implicit cache usage is measured.',
        'stopping': 'Complete the two fixed collectors and up to eight continuations. Retain real failures. Infrastructure interruptions pause for inspection and may restart only the incomplete attempt from its fixed origin, with all costs retained. No new collection after a terminal unavailable or eligible result.',
        'cost_rule': 'Actual new spend sums each physical collection ledger once plus each physical continuation ledger including failed attempts. Historical prefix is charged only in full-task comparison columns. Archived copies of ledgers are never summed. Unknown usage remains unknown.',
        'offline_rule': 'Offline-only scripted conflicts are explicit synthetic engineering fixtures restored from the same historical prefix. They prove capture, restore, delivery and scoring, supply zero model requests, and can never be admitted into the real protocol.',
        'interpretation': 'Report collection coverage, exact conflict origins and outcomes, each condition success count, remaining/full-task cost and time. No available conflict means this batch cannot estimate intervention effects. Repeated branches share a checkpoint; at most two independent task states. Existing Solo results are background only.',
        'source_manifest': source_manifest()}


def verify_sources(protocol):
    if protocol['source_manifest'] != source_manifest() or protocol['early_references'] != early_references():
        raise RuntimeError('Frozen conflict study inputs changed')


def verify_preflight():
    proof = read_json(PROOF, {})
    restoration = read_json(ROOT / 'artifacts/conflict_checkpoint_docker_validation.json', {})
    guard = read_json(ROOT / 'artifacts/publication_guard_docker_validation.json', {})
    if (restoration.get('passed') is not True or restoration.get('model_api_calls') != 0
            or restoration.get('checkpoint_source_sha256') != file_hash(ROOT / 'conflict_checkpoint.py')
            or guard.get('passed') is not True or guard.get('model_api_calls') != 0
            or guard.get('guard_source_sha256') != file_hash(ROOT / 'publication_guard.py')):
        raise RuntimeError('Exact-source Docker conflict restoration and shared guard proof required')
    if (proof.get('passed') is not True or proof.get('model_api_calls') != 0
            or proof.get('eligible_checkpoints') != 2 or proof.get('completed_jobs') != 4
            or proof.get('source_manifest') != source_manifest()
            or proof.get('early_references') != early_references()):
        raise RuntimeError('Exact-source two-task capture/restore/scoring offline proof required')


def collection_path(suite, key):
    if key not in TASKS:
        raise ValueError('Unplanned conflict task')
    return suite / key / 'collection'


def job_path(suite, job):
    if (job['task'] not in TASKS or job['condition'] not in CONDITIONS
            or type(job['repeat']) is not int or job['repeat'] not in (1, 2)):
        raise ValueError('Unplanned conflict continuation')
    return suite / job['task'] / 'jobs' / f"{job['task']}_r{job['repeat']:02d}_{job['condition']}"


def collection_config(suite, key, protocol):
    run = collection_path(suite, key)
    reference = protocol['early_references'][key]
    config = {'task_key': key, 'task': TASKS[key].as_dict(), 'condition': 'strongshare',
        'stage': 'conflict_collection', 'condition_version': 'conflict_collection_v1',
        'experiment_version': VERSION, 'model': protocol['model'],
        'submission_policy': protocol['submission_policy'], 'terminal_policy': protocol['terminal_policy'],
        'offline_selftest': protocol['offline_selftest'], 'protocol_sha256': digest(protocol),
        'execution_seconds': round(TOTAL_SECONDS - reference['record']['elapsed_seconds'], 6),
        'historical_checkpoint_sha256': reference['record']['checkpoint_sha256'],
        'early_prefix_directory': reference['directory'], 'initialization_included_in_budget': True,
        'publication_guard_version': protocol['publication_guard_version']}
    run.mkdir(parents=True, exist_ok=True)
    write_fixed(run / 'task_config.json', config)
    return config


def collection_record(run, task):
    from conflict_runtime import verify_collection_artifacts
    verify_collection_artifacts(run, task)
    status = read_json(run / 'strongshare/status.json')
    elapsed = status.get('elapsed_seconds')
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise RuntimeError('Invalid collection elapsed time')
    eligible = status['status'] == 'completed'
    cp = read_json(run / 'checkpoint.json') if eligible else None
    return {'task': task.key, 'status': status['status'], 'eligible': eligible,
        'reason': status.get('reason'), 'elapsed_seconds': elapsed,
        'checkpoint_sha256': digest(cp) if eligible else None,
        'accepted_usage': usage(run, 'strongshare'), 'all_attempt_usage': usage(run),
        'run_directory': str(run), 'execution': status}


def copy_conflict_checkpoint(source, target, task):
    from conflict_checkpoint import verify_checkpoint_files
    verify_checkpoint_files(source, task)
    for name in ('checkpoint.json', 'shared_git.tar', 'checkpoint_packet.json', 'checkpoint_packet.txt'):
        expected = file_hash(source / name)
        if (target / name).exists():
            if file_hash(target / name) != expected:
                raise RuntimeError('Copied conflict artifact changed: ' + name)
        else:
            shutil.copyfile(source / name, target / name)
    verify_checkpoint_files(target, task)


def configure_job(suite, job, protocol, collection):
    run = job_path(suite, job)
    reference = protocol['early_references'][job['task']]['record']
    seconds = round(TOTAL_SECONDS - reference['elapsed_seconds'] - collection['elapsed_seconds'], 6)
    config = {**job, 'task_key': job['task'], 'task': TASKS[job['task']].as_dict(),
        'stage': 'conflict_continuation', 'condition_version': VARIANTS[job['condition']],
        'experiment_version': VERSION, 'model': protocol['model'],
        'submission_policy': protocol['submission_policy'], 'terminal_policy': protocol['terminal_policy'],
        'offline_selftest': protocol['offline_selftest'], 'protocol_sha256': digest(protocol),
        'execution_seconds': max(0, seconds), 'remaining_seconds_before_clamp': seconds,
        'checkpoint_sha256': collection['checkpoint_sha256'],
        'collection_directory': collection['run_directory'],
        'initialization_included_in_budget': True, 'publication_guard_version': protocol['publication_guard_version']}
    run.mkdir(parents=True, exist_ok=True)
    write_fixed(run / 'task_config.json', config)
    copy_conflict_checkpoint(Path(collection['run_directory']), run, TASKS[job['task']])
    return config


def archive_attempt(run, condition, collector=False):
    prior = set((run / 'failed_attempts').glob('*'))
    archive_incomplete(run, condition)
    if collector:
        extras = [run / name for name in ('checkpoint.json', 'shared_git.tar', 'checkpoint_packet.json', 'checkpoint_packet.txt', 'collection_receipt.json') if (run / name).exists()]
        if extras:
            added = set((run / 'failed_attempts').glob('*')) - prior
            if len(added) != 1:
                raise RuntimeError('Orphaned collection evidence needs inspection')
            dest = next(iter(added)) / 'collection_artifacts'
            dest.mkdir()
            for path in extras:
                shutil.move(str(path), str(dest / path.name))


def model_phase(run, task, condition, protocol, suite, lock_fd, collector=False):
    from conflict_runtime import verify_worker_artifacts, verify_collection_artifacts
    phase = run / condition
    terminal = ('completed', 'no_conflict') if collector else ('completed', 'budget_exhausted', 'time_budget_exhausted_before_first_query')
    saved = read_json(phase / 'status.json', {})
    if saved.get('status') not in terminal:
        if not protocol['offline_selftest'] and not os.environ.get('OPENAI_API_KEY', '').strip():
            raise RuntimeError('OPENAI_API_KEY absent; no API request started')
        archive_attempt(run, condition, collector)
        phase.mkdir()
        write_json(phase / 'source_manifest.json', protocol['source_manifest'])
        config = read_json(run / 'task_config.json')
        print(f"{task.key}: {'collecting conflict' if collector else run.name}", flush=True)
        try:
            code = run_command([sys.executable, '-B', '-u', str(Path(__file__).resolve()),
                '--worker', '--run-dir', str(run), '--suite-dir', str(suite)],
                phase / 'console.log', config['execution_seconds'] + 300, lock_fd)
        finally:
            cleanup_phase(run, condition)
        saved = read_json(phase / 'status.json', {})
        if code or saved.get('status') not in terminal:
            raise RuntimeError('Conflict execution paused: ' + str(phase))
    if collector:
        verify_collection_artifacts(run, task)
    else:
        verify_worker_artifacts(phase, task)
    cleanup_phase(run, condition)
    return saved


def evaluate(run, task, condition, lock_fd):
    from conflict_runtime import verify_worker_artifacts
    from evaluate_solo_smoke import evaluator_environment
    phase = run / condition
    before = verify_worker_artifacts(phase, task)
    code = run_command([sys.executable, '-B', str(ROOT / 'conflict_evaluator.py'), '--run-dir', str(phase)],
        phase / 'evaluation_console.log', 720, lock_fd, env=evaluator_environment())
    score = read_json(phase / 'evaluation_summary.json', {})
    if code or not score.get('evaluation_completed'):
        raise RuntimeError('Scoring paused; saved execution will be reused')
    if before != verify_worker_artifacts(phase, task):
        raise RuntimeError('Completed execution changed during scoring')
    return score


def build_row(run, job, status, score, protocol, collection):
    new_usage = usage(run)
    history = protocol['early_references'][job['task']]['record']
    common = add_usage([history['all_attempt_usage'], collection['all_attempt_usage']])
    failed = incomplete_seconds(run, job['condition'])
    collected_failed = incomplete_seconds(Path(collection['run_directory']), 'strongshare')
    return {**job, 'run_directory': str(run), 'model': MODEL, 'execution': status,
        'official_success': bool(score['both_passed']) if score else False,
        'scored': score is not None, 'evaluation': score, 'checkpoint_sha256': collection['checkpoint_sha256'],
        'accepted_usage': usage(run, job['condition']), 'all_attempt_usage': new_usage,
        'common_prefix_usage': common,
        'remaining_known_cost_usd': new_usage['known_cost_usd'],
        'full_task_known_cost_usd': common['known_cost_usd'] + new_usage['known_cost_usd'],
        'full_task_unknown_usage_requests': common['unknown_usage_requests'] + new_usage['unknown_usage_requests'],
        'remaining_execution_seconds': status['elapsed_seconds'] + failed['known_seconds'],
        'full_task_execution_seconds': history['elapsed_seconds'] + collection['elapsed_seconds'] +
            collected_failed['known_seconds'] + status['elapsed_seconds'] + failed['known_seconds'],
        'unknown_time_attempts': failed['unknown_attempts'] + collected_failed['unknown_attempts'],
        'remaining_execution_allowance_seconds': read_json(run / 'task_config.json')['execution_seconds']}


def summary_for(suite, protocol, collections, rows, skipped):
    ledgers = list(suite.glob('*/collection/usage_ledger.json')) + list(suite.glob('*/jobs/*/usage_ledger.json'))
    groups = []
    for key in TASKS:
        for condition in CONDITIONS:
            data = [r for r in rows if r['task'] == key and r['condition'] == condition]
            if data:
                groups.append({'task': key, 'condition': condition, 'n': len(data),
                    'scored': sum(r['scored'] for r in data), 'successes': sum(r['official_success'] for r in data),
                    'remaining_known_cost_mean': sum(r['remaining_known_cost_usd'] for r in data) / len(data),
                    'full_task_known_cost_mean': sum(r['full_task_known_cost_usd'] for r in data) / len(data),
                    'unknown_usage_requests': sum(r['full_task_unknown_usage_requests'] for r in data)})
    return {'version': VERSION, 'pipeline_completed': len(collections) == len(TASKS) and len(rows) + len(skipped) == len(protocol['jobs']),
        'planned_checkpoints': len(TASKS), 'collected_tasks': len(collections),
        'eligible_checkpoints': sum(r['eligible'] for r in collections.values()),
        'planned_max_executions': len(protocol['jobs']), 'completed_executions': len(rows),
        'collections': collections, 'rows': rows, 'skipped_jobs': skipped, 'comparison_groups': groups,
        'actual_new_usage': add_usage([usage(p.parent) for p in ledgers]),
        'historical_prefix_rebilled': False, 'interpretation': protocol['interpretation']}


def supervise(suite, offline=False):
    configure()
    suite.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'artifacts/model_run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not offline:
            verify_preflight()
        protocol = protocol_for(offline)
        write_fixed(suite / 'protocol.json', protocol)
        for name, expected in protocol['source_manifest'].items():
            path = suite / 'source_snapshot' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and file_hash(path) != expected:
                raise RuntimeError('Frozen source snapshot differs')
            if not path.exists():
                path.write_bytes((ROOT / name).read_bytes())
        collections, rows, skipped = {}, [], []
        try:
            for key, task in TASKS.items():
                verify_sources(protocol)
                collection_config(suite, key, protocol)
                run = collection_path(suite, key)
                write_json(suite / 'status.json', {'status': 'running', 'stage': 'collection', 'task': key})
                model_phase(run, task, 'strongshare', protocol, suite, lock.fileno(), collector=True)
                collections[key] = collection_record(run, task)
                write_json(suite / 'summary.json', summary_for(suite, protocol, collections, rows, skipped))
                print(f"{key}: conflict checkpoint eligible={collections[key]['eligible']}", flush=True)
            for job in protocol['jobs']:
                verify_sources(protocol)
                collection = collections[job['task']]
                if not collection['eligible']:
                    skipped.append({**job, 'reason': collection['reason'] or collection['status'], 'eligible': False})
                else:
                    run = job_path(suite, job)
                    configure_job(suite, job, protocol, collection)
                    write_json(suite / 'status.json', {'status': 'running', 'stage': 'continuation', **job, 'completed_jobs': len(rows)})
                    status = model_phase(run, TASKS[job['task']], job['condition'], protocol, suite, lock.fileno())
                    score = evaluate(run, TASKS[job['task']], job['condition'], lock.fileno()) if status['status'] == 'completed' else None
                    row = build_row(run, job, status, score, protocol, collection)
                    rows.append(row)
                    write_json(run / 'result.json', row)
                    print(f"{job['task']} {job['condition']} r{job['repeat']}: success={row['official_success']}, known=${row['all_attempt_usage']['known_cost_usd']:.6f}", flush=True)
                write_json(suite / 'summary.json', summary_for(suite, protocol, collections, rows, skipped))
            verify_sources(protocol)
            summary = summary_for(suite, protocol, collections, rows, skipped)
            if offline:
                if summary['eligible_checkpoints'] != 2 or len(rows) != 4 or skipped or summary['actual_new_usage']['requests'] or not all(r['scored'] for r in rows):
                    raise RuntimeError('Offline proof requires two captured states, four official scored continuations, zero API requests')
                write_json(PROOF, {'passed': True, 'model_api_calls': 0, 'eligible_checkpoints': 2,
                    'completed_jobs': 4, 'source_manifest': protocol['source_manifest'],
                    'early_references': protocol['early_references'], 'suite_directory': str(suite)})
            write_json(suite / 'status.json', {'status': 'completed', 'eligible_checkpoints': summary['eligible_checkpoints'],
                'completed_jobs': len(rows), 'skipped_jobs': len(skipped), 'planned_max_jobs': len(protocol['jobs'])})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / 'summary.json', summary_for(suite, protocol, collections, rows, skipped))
            write_json(suite / 'status.json', {'status': 'paused', 'completed_jobs': len(rows), 'error': safe_error(exc)})
            print('Conflict study paused: ' + safe_error(exc), flush=True)
            return 2


def worker(run, suite):
    from conflict_runtime import collect_conflict, phase_worker
    protocol = read_json(suite / 'protocol.json')
    verify_sources(protocol)
    for key, task in TASKS.items():
        if run == collection_path(suite, key):
            config = collection_config(suite, key, protocol)
            return collect_conflict(run, task, early_prefix_dir=Path(config['early_prefix_directory']),
                execution_seconds=config['execution_seconds'], offline=protocol['offline_selftest'])
    jobs = [j for j in protocol['jobs'] if job_path(suite, j) == run]
    if len(jobs) != 1:
        raise ValueError('Unplanned worker path')
    job = jobs[0]
    collection = collection_record(collection_path(suite, job['task']), TASKS[job['task']])
    if not collection['eligible']:
        raise ValueError('No eligible checkpoint for continuation')
    config = configure_job(suite, job, protocol, collection)
    return phase_worker(run, job['condition'], TASKS[job['task']], offline=protocol['offline_selftest'],
        execution_seconds=config['execution_seconds'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-selftest', action='store_true')
    parser.add_argument('--verify-preflight', action='store_true')
    parser.add_argument('--suite-dir', type=Path)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    name = 'luna_conflict_offline_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') if args.offline_selftest else VERSION
    suite = (args.suite_dir or RUNS / name).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r'luna_conflict_[A-Za-z0-9_]+', suite.name):
        raise ValueError('Invalid conflict suite path')
    return worker(args.run_dir.resolve(strict=True), suite) if args.worker else supervise(suite, args.offline_selftest)


if __name__ == '__main__':
    raise SystemExit(main())
