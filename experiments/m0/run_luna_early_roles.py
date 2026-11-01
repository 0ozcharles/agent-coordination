"""Fixed early-checkpoint role comparison with identical agent1-only scoring."""
import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time

from budget_guard import write_json
from conflict_evaluator import get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import archive_incomplete, incomplete_seconds, validate_checkpoint
from run_luna_conflict import early_references
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed
from task_specs import ROOT, COMMIT

RUNS = ROOT / 'artifacts/early_role_runs'
VERSION = 'luna_early_roles_v1'
MODEL = 'gpt-6-luna'
TASKS = {k: get_task(k) for k in ('llama_index17070_f1_f3', 'click2068_f4_f10')}
CONDITIONS = ('strongshare', 'integrator')
VARIANTS = {c: 'early_role_' + c + '_v1' for c in CONDITIONS}
TOTAL_SECONDS = 1800
ORIGIN = ROOT / 'artifacts/conflict_runs/luna_conflict_v1'
PROOF = ROOT / 'artifacts/early_roles_offline_validation.json'
NEW_SOURCES = ('run_luna_early_roles.py', 'early_role_runtime.py', 'early_role_evaluator.py')
TERMINAL = ('completed', 'budget_exhausted', 'time_budget_exhausted_before_first_query')


def source_manifest():
    historical = read_json(ORIGIN / 'protocol.json')['source_manifest']
    for name, expected in historical.items():
        if file_hash(ROOT / name) != expected or file_hash(ORIGIN / 'source_snapshot' / name) != expected:
            raise RuntimeError('Historical frozen source changed: ' + name)
    return {**historical, **{name: file_hash(ROOT / name) for name in NEW_SOURCES}}


def schedule(offline=False):
    if offline:
        return [{'task': key, 'repeat': 1, 'condition': condition}
                for key, condition in zip(TASKS, CONDITIONS)]
    orders = (CONDITIONS, tuple(reversed(CONDITIONS)))
    return [{'task': key, 'repeat': r, 'condition': c}
            for key in TASKS for r, order in enumerate(orders, 1) for c in order]


def protocol_for(offline=False):
    old = read_json(ORIGIN / 'protocol.json')
    fields = ('provider', 'endpoint', 'reasoning_effort', 'temperature', 'top_p',
              'max_completion_tokens', 'service_tier', 'prompt_cache_options', 'pricing_usd_per_million')
    return {'version': VERSION, 'offline_selftest': offline, 'dataset_commit': COMMIT,
        'model': 'offline-scripted-model' if offline else MODEL, **{k: old[k] for k in fields},
        'tasks': {k: t.as_dict() for k, t in TASKS.items()}, 'jobs': schedule(offline),
        'early_references': early_references(), 'condition_versions': VARIANTS,
        'submission_policy': 'authoritative_agent1_only', 'terminal_policy': 'both_active',
        'total_execution_seconds': TOTAL_SECONDS, 'publication_guard_version': old['publication_guard_version'],
        'money_limit': None, 'request_limit': None,
        'question': 'Does explicitly assigning integration responsibilities early help when both groups have the same agent1-only final delivery and scoring?',
        'scope': 'Two historical early work-in-progress checkpoints on previously explored development tasks; two fresh samples per action and checkpoint. Eight executions, two task states. No held-out or population claim.',
        'starting_states': 'Reuse the original eligible Luna early prefixes, not the later unsuccessful conflict collectors. Preserve both original histories, workspaces, Git index and shared remote, mailbox, original feature assignments and complete checkpoint evidence. No new paid prefix, outcome-dependent checkpoint selection or replacement task.',
        'common_information': 'Prepare one complete source-diff and scratch packet per task before any new continuation outcome. Both actions and both repeats receive that exact packet and checkpoint. Live read-only source-scoped peer evidence remains available to both agents.',
        'actions': 'StrongShare retains original feature responsibilities and lets the agents choose their integration workflow. Integrator explicitly assigns agent1 to review and integrate the complete implementation, with agent2 publishing and communicating its contribution. Both agents remain active; agent1 is the sole predesignated final publisher in both groups.',
        'scoring': 'Identical pinned official feature tests on agent1 actual published complete code relative to the original task base. Native test_solo is only the one-patch grading adapter, not a Solo agent condition. No automatic merge, repair, worktree fallback, peer winner selection or posthoc choice of publisher.',
        'publication_rule': 'Both groups receive the unchanged publication_parity_v1 guard, with at most two reminders per agent at attempted voluntary completion. No code changes by the harness, automatic publishing or deadline extension.',
        'time_rule': '1800 seconds total. Subtract accepted historical prefix elapsed time and one common zero-API packet preparation including restore/collection/cleanup. All four branches of a task receive the same remainder. Each branch initialization/restore/handoff consumes its remaining allowance. In-flight requests may finish after deadline. Failed infrastructure time and cost remain separately reported.',
        'order': 'Prepare both tasks before any fresh outcome. Within each task r1 StrongShare then Integrator, r2 Integrator then StrongShare. No shared random seed; actual implicit caching is measured.',
        'stopping': 'Complete these eight fixed continuations. Retain model/task failures. Infrastructure interruption pauses for inspection and only the unfinished attempt may restart from the same fixed state, retaining all cost. A scored execution is never repeated because its result is poor.',
        'cost_rule': 'New spend sums each physical job ledger exactly once, including failed attempts and unknown usage. Historical prefix is not rebilled; it is added only to full-task comparison columns. Archived copies are not counted twice. Common packet preparation has zero model requests.',
        'offline_rule': 'Two explicitly scripted engineering continuations restore the two genuine historical checkpoints and exercise both actions, actual publication and official scoring with zero provider requests. They are excluded from research outcomes.',
        'interpretation': 'Compare official two-feature success counts, costs and integration behavior. The common delivery rule already requires agent1 to hold the combined artifact; the contrast is explicit integration-role guidance on top of that rule. Repeated continuations share two checkpoints. Historical Solo is background, not a newly matched experimental control. No evidence of selector benefit or optimal intervention timing is implied.',
        'source_manifest': source_manifest()}


def verify_sources(protocol):
    if protocol['source_manifest'] != source_manifest() or protocol['early_references'] != early_references():
        raise RuntimeError('Frozen early role inputs changed')


def verify_preflight():
    proof = read_json(PROOF, {})
    guard = read_json(ROOT / 'artifacts/publication_guard_docker_validation.json', {})
    if (guard.get('passed') is not True or guard.get('model_api_calls') != 0
            or guard.get('guard_source_sha256') != file_hash(ROOT / 'publication_guard.py')):
        raise RuntimeError('Exact-source shared publication guard proof required')
    if (proof.get('passed') is not True or proof.get('model_api_calls') != 0
            or proof.get('prepared_tasks') != 2 or proof.get('completed_jobs') != 2
            or proof.get('source_manifest') != source_manifest()
            or proof.get('early_references') != early_references()):
        raise RuntimeError('Exact-source two-task restore/delivery/scoring offline proof required')


def collect_packet(prefix, task, output):
    # Private registry binding widens only this already-frozen zero-model collector.
    spec = importlib.util.spec_from_file_location('_early_role_packet_collector', ROOT / 'selector_workers.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def check_task(value):
        if value.key not in TASKS or value.as_dict() != TASKS[value.key].as_dict():
            raise ValueError('Unregistered early role task')
    module.check_task = check_task
    return module.collect_checkpoint_packet(prefix, task, output)


def prepare_packet(suite, task, protocol):
    from cross_task_packet import packet_message
    folder = suite / task.key / 'preparation'
    folder.mkdir(parents=True, exist_ok=True)
    reference = protocol['early_references'][task.key]
    path = folder / 'preparation.json'
    if not path.exists():
        if list(suite.glob('*/jobs/*')):
            raise RuntimeError('Missing common preparation after branches were created')
        started = time.monotonic()
        packet = collect_packet(Path(reference['directory']), task, folder / 'packet.json')
        message = packet_message(packet)
        (folder / 'packet.txt').write_text(message)
        write_fixed(path, {'task': task.key, 'checkpoint_sha256': reference['record']['checkpoint_sha256'],
            'packet_sha256': digest(packet), 'packet_text_sha256': file_hash(folder / 'packet.txt'),
            'elapsed_seconds': round(time.monotonic() - started, 6), 'model_api_calls': 0,
            'before_continuation_outcomes': True, 'created_at': datetime.now(timezone.utc).isoformat()})
    packet, record = read_json(folder / 'packet.json'), read_json(path)
    elapsed = record.get('elapsed_seconds')
    if (record.get('task') != task.key or packet.get('task_key') != task.key
            or packet.get('checkpoint_sha256') != reference['record']['checkpoint_sha256']
            or record.get('checkpoint_sha256') != packet['checkpoint_sha256']
            or record.get('packet_sha256') != digest(packet)
            or record.get('packet_text_sha256') != file_hash(folder / 'packet.txt')
            or (folder / 'packet.txt').read_text() != packet_message(packet)
            or record.get('model_api_calls') != 0 or record.get('before_continuation_outcomes') is not True
            or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0):
        raise RuntimeError('Common early checkpoint preparation changed')
    return {'directory': str(folder), 'record': record, 'preparation_sha256': digest(record)}


def remaining_seconds(reference, preparation):
    return round(TOTAL_SECONDS - reference['record']['elapsed_seconds'] - preparation['record']['elapsed_seconds'], 6)


def job_path(suite, job):
    if (job['task'] not in TASKS or job['condition'] not in CONDITIONS
            or type(job['repeat']) is not int or job['repeat'] not in (1, 2)):
        raise ValueError('Unplanned early role job')
    return suite / job['task'] / 'jobs' / f"{job['task']}_r{job['repeat']:02d}_{job['condition']}"


def copy_inputs(reference, preparation, target, task):
    prefix, folder = Path(reference['directory']), Path(preparation['directory'])
    checkpoint = read_json(prefix / 'checkpoint.json')
    validate_checkpoint(checkpoint, task)
    if digest(checkpoint) != reference['record']['checkpoint_sha256'] or file_hash(prefix / 'shared_git.tar') != checkpoint['git_archive_sha256']:
        raise RuntimeError('Early checkpoint identity changed')
    for source, name in ((prefix / 'checkpoint.json', 'checkpoint.json'), (prefix / 'shared_git.tar', 'shared_git.tar'),
                         (folder / 'packet.json', 'checkpoint_packet.json'), (folder / 'packet.txt', 'checkpoint_packet.txt')):
        if (target / name).exists():
            if file_hash(target / name) != file_hash(source):
                raise RuntimeError('Copied early role input changed: ' + name)
        else:
            shutil.copyfile(source, target / name)


def configure_job(suite, job, protocol, preparation):
    run = job_path(suite, job)
    reference = protocol['early_references'][job['task']]
    seconds = remaining_seconds(reference, preparation)
    config = {**job, 'task_key': job['task'], 'task': TASKS[job['task']].as_dict(),
        'stage': 'early_role_continuation', 'condition_version': VARIANTS[job['condition']],
        'experiment_version': VERSION, 'model': protocol['model'],
        'submission_policy': protocol['submission_policy'], 'terminal_policy': protocol['terminal_policy'],
        'offline_selftest': protocol['offline_selftest'], 'protocol_sha256': digest(protocol),
        'execution_seconds': max(0, seconds), 'remaining_seconds_before_clamp': seconds,
        'checkpoint_sha256': reference['record']['checkpoint_sha256'],
        'checkpoint_packet_sha256': preparation['record']['packet_sha256'],
        'preparation_sha256': preparation['preparation_sha256'], 'preparation_directory': preparation['directory'],
        'prefix_directory': reference['directory'], 'initialization_included_in_budget': True,
        'publication_guard_version': protocol['publication_guard_version']}
    run.mkdir(parents=True, exist_ok=True)
    write_fixed(run / 'task_config.json', config)
    copy_inputs(reference, preparation, run, TASKS[job['task']])
    return config


def model_phase(run, task, condition, protocol, suite, lock_fd):
    from early_role_runtime import verify_worker_artifacts
    phase = run / condition
    saved = read_json(phase / 'status.json', {})
    if saved.get('status') not in TERMINAL:
        if not protocol['offline_selftest'] and not os.environ.get('OPENAI_API_KEY', '').strip():
            raise RuntimeError('OPENAI_API_KEY absent; no API request started')
        archive_incomplete(run, condition)
        phase.mkdir()
        write_json(phase / 'source_manifest.json', protocol['source_manifest'])
        config = read_json(run / 'task_config.json')
        print(f'{task.key}: {run.name}', flush=True)
        try:
            code = run_command([sys.executable, '-B', '-u', str(Path(__file__).resolve()),
                '--worker', '--run-dir', str(run), '--suite-dir', str(suite)],
                phase / 'console.log', config['execution_seconds'] + 300, lock_fd)
        finally:
            cleanup_phase(run, condition)
        saved = read_json(phase / 'status.json', {})
        if code or saved.get('status') not in TERMINAL:
            raise RuntimeError('Early role execution paused: ' + str(phase))
    verify_worker_artifacts(phase, task)
    cleanup_phase(run, condition)
    return saved


def evaluate(run, task, condition, lock_fd):
    from early_role_runtime import verify_worker_artifacts
    from evaluate_solo_smoke import evaluator_environment
    phase = run / condition
    before = verify_worker_artifacts(phase, task)
    code = run_command([sys.executable, '-B', str(ROOT / 'early_role_evaluator.py'), '--run-dir', str(phase)],
        phase / 'evaluation_console.log', 720, lock_fd, env=evaluator_environment())
    score = read_json(phase / 'evaluation_summary.json', {})
    if code or not score.get('evaluation_completed'):
        raise RuntimeError('Scoring paused; saved execution will be reused')
    if before != verify_worker_artifacts(phase, task):
        raise RuntimeError('Completed execution changed during scoring')
    return score


def build_row(run, job, status, score, protocol, preparation):
    new_usage = usage(run)
    history = protocol['early_references'][job['task']]['record']
    failed = incomplete_seconds(run, job['condition'])
    return {**job, 'run_directory': str(run), 'model': protocol['model'], 'execution': status,
        'official_success': bool(score['both_passed']) if score else False, 'scored': score is not None,
        'evaluation': score, 'checkpoint_sha256': history['checkpoint_sha256'],
        'checkpoint_packet_sha256': preparation['record']['packet_sha256'],
        'accepted_usage': usage(run, job['condition']), 'all_attempt_usage': new_usage,
        'common_prefix_usage': history['all_attempt_usage'],
        'remaining_known_cost_usd': new_usage['known_cost_usd'],
        'full_task_known_cost_usd': history['all_attempt_usage']['known_cost_usd'] + new_usage['known_cost_usd'],
        'full_task_unknown_usage_requests': history['all_attempt_usage']['unknown_usage_requests'] + new_usage['unknown_usage_requests'],
        'common_preparation_seconds': preparation['record']['elapsed_seconds'],
        'remaining_execution_seconds': status['elapsed_seconds'] + failed['known_seconds'],
        'full_task_execution_seconds': history['elapsed_seconds'] + preparation['record']['elapsed_seconds'] + status['elapsed_seconds'] + failed['known_seconds'],
        'unknown_time_attempts': failed['unknown_attempts'],
        'remaining_execution_allowance_seconds': read_json(run / 'task_config.json')['execution_seconds']}


def summary_for(suite, protocol, preparations, rows):
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
    return {'version': VERSION, 'pipeline_completed': len(rows) == len(protocol['jobs']),
        'prepared_tasks': len(preparations), 'planned_executions': len(protocol['jobs']),
        'completed_executions': len(rows), 'preparations': preparations, 'rows': rows,
        'comparison_groups': groups,
        'actual_new_usage': add_usage([usage(p.parent) for p in suite.glob('*/jobs/*/usage_ledger.json')]),
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
        preparations, rows = {}, []
        try:
            for key, task in TASKS.items():
                verify_sources(protocol)
                preparations[key] = prepare_packet(suite, task, protocol)
            write_json(suite / 'summary.json', summary_for(suite, protocol, preparations, rows))
            for job in protocol['jobs']:
                verify_sources(protocol)
                preparation = prepare_packet(suite, TASKS[job['task']], protocol)
                if preparation != preparations[job['task']]:
                    raise RuntimeError('Common preparation changed after first branch')
                run = job_path(suite, job)
                configure_job(suite, job, protocol, preparation)
                write_json(suite / 'status.json', {'status': 'running', **job, 'completed_jobs': len(rows)})
                status = model_phase(run, TASKS[job['task']], job['condition'], protocol, suite, lock.fileno())
                score = evaluate(run, TASKS[job['task']], job['condition'], lock.fileno()) if status['status'] == 'completed' else None
                row = build_row(run, job, status, score, protocol, preparation)
                rows.append(row)
                write_json(run / 'result.json', row)
                write_json(suite / 'summary.json', summary_for(suite, protocol, preparations, rows))
                print(f"{job['task']} {job['condition']} r{job['repeat']}: success={row['official_success']}, known=${row['all_attempt_usage']['known_cost_usd']:.6f}", flush=True)
            verify_sources(protocol)
            summary = summary_for(suite, protocol, preparations, rows)
            if offline:
                if len(preparations) != 2 or len(rows) != 2 or summary['actual_new_usage']['requests'] or not all(r['scored'] for r in rows):
                    raise RuntimeError('Offline proof requires two original restored states, two official scored continuations and zero model requests')
                write_json(PROOF, {'passed': True, 'model_api_calls': 0, 'prepared_tasks': 2,
                    'completed_jobs': 2, 'source_manifest': protocol['source_manifest'],
                    'early_references': protocol['early_references'], 'suite_directory': str(suite)})
            write_json(suite / 'status.json', {'status': 'completed', 'completed_jobs': len(rows), 'planned_jobs': len(protocol['jobs'])})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / 'summary.json', summary_for(suite, protocol, preparations, rows))
            write_json(suite / 'status.json', {'status': 'paused', 'completed_jobs': len(rows), 'error': safe_error(exc)})
            print('Early role study paused: ' + safe_error(exc), flush=True)
            return 2


def worker(run, suite):
    from early_role_runtime import phase_worker
    protocol = read_json(suite / 'protocol.json')
    verify_sources(protocol)
    jobs = [j for j in protocol['jobs'] if job_path(suite, j) == run]
    if len(jobs) != 1:
        raise ValueError('Unplanned worker path')
    job = jobs[0]
    preparation = prepare_packet(suite, TASKS[job['task']], protocol)
    config = configure_job(suite, job, protocol, preparation)
    return phase_worker(run, job['condition'], TASKS[job['task']], offline=protocol['offline_selftest'], execution_seconds=config['execution_seconds'])


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
    name = 'luna_early_roles_offline_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') if args.offline_selftest else VERSION
    suite = (args.suite_dir or RUNS / name).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r'luna_early_roles_[A-Za-z0-9_]+', suite.name):
        raise ValueError('Invalid early roles suite path')
    return worker(args.run_dir.resolve(strict=True), suite) if args.worker else supervise(suite, args.offline_selftest)


if __name__ == '__main__':
    raise SystemExit(main())
