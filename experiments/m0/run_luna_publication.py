"""Six fresh Click comparisons with one shared publication-reminder rule.

This is a runner-parity diagnostic on a previously observed task. Historical
scores are immutable. The old checkpoint/packet is reused without new API cost.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
from pathlib import Path
import re
import sys

from budget_guard import write_json
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import copy_checkpoint, incomplete_seconds, prefix_record
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed
from selector_task_specs import get_task
from task_specs import ROOT, COMMIT

RUNS = ROOT / 'artifacts/publication_runs'
VERSION = 'luna_publication_v1'
ORIGIN = ROOT / 'artifacts/selector_runs/luna_selector_v1'
TASK_KEY = 'click2068_f4_f10'
TASK = get_task(TASK_KEY)
MODEL = 'gpt-6-luna'
CONDITIONS = ('strongshare', 'integrator', 'solo')
VARIANTS = {'strongshare': 'source_handoff_diff_v1', 'integrator': 'designated_integrator_v1', 'solo': 'luna_fresh_solo_v1'}
TOTAL_SECONDS = 1800
PROOF = ROOT / 'artifacts/publication_offline_validation.json'
NEW_SOURCES = ('run_luna_publication.py', 'publication_workers.py', 'publication_evaluator.py', 'publication_guard.py')
EXHAUSTED_STATES = ('time_budget_exhausted_before_first_query', 'budget_exhausted')


def source_manifest():
    import run_luna_selector as old
    historical = read_json(ORIGIN / 'protocol.json')['source_manifest']
    if historical != old.source_manifest():
        raise RuntimeError('Historical selector sources changed')
    for name, expected in historical.items():
        if file_hash(ORIGIN / 'source_snapshot' / name) != expected:
            raise RuntimeError('Historical selector snapshot changed: ' + name)
    return {**historical, **{name: file_hash(ROOT / name) for name in NEW_SOURCES}}


def checkpoint_reference():
    protocol = read_json(ORIGIN / 'protocol.json')
    if (protocol['model'] != MODEL or protocol['offline_selftest'] is not False
            or read_json(ORIGIN / 'status.json').get('status') != 'completed'):
        raise RuntimeError('Expected the completed real-model selector pilot')
    prefix = ORIGIN / TASK_KEY / 'prefix'
    record = prefix_record(prefix, TASK)
    if record['status'] != 'completed':
        raise RuntimeError('Historical Click checkpoint unavailable')
    folder = ORIGIN / TASK_KEY / 'decision'
    packet, decision, preparation = [read_json(folder / name) for name in ('packet.json', 'decision.json', 'preparation.json')]
    from selector_rule import verify_decision
    verify_decision(packet, decision)
    if (packet['checkpoint_sha256'] != record['checkpoint_sha256']
            or preparation['packet_sha256'] != digest(packet)
            or preparation['decision_sha256'] != digest(decision)):
        raise RuntimeError('Historical packet binding changed')
    paths = [ORIGIN / name for name in ('protocol.json', 'status.json', 'summary.json')]
    paths += [folder / name for name in ('packet.json', 'decision.json', 'preparation.json')]
    paths += [prefix / name for name in record['files_sha256']]
    return {'directory': str(prefix), 'record': record, 'preparation': preparation,
            'historical_files': {str(p.relative_to(ROOT)): file_hash(p) for p in paths},
            'historical_usage_is_new_billable_usage': False}


def schedule(offline=False):
    return [{'task': TASK_KEY, 'repeat': repeat, 'condition': c}
            for repeat, order in enumerate((CONDITIONS,) if offline else (CONDITIONS, tuple(reversed(CONDITIONS))), 1)
            for c in order]


def protocol_for(offline=False):
    from publication_guard import GUARD_VERSION
    old = read_json(ORIGIN / 'protocol.json')
    fields = ('provider', 'endpoint', 'reasoning_effort', 'temperature', 'top_p', 'max_completion_tokens',
              'service_tier', 'prompt_cache_options', 'pricing_usd_per_million')
    return {'version': VERSION, 'offline_selftest': offline, 'dataset_commit': COMMIT,
        'model': 'offline-scripted-model' if offline else MODEL,
        **{k: old[k] for k in fields}, 'tasks': {TASK_KEY: TASK.as_dict()}, 'jobs': schedule(offline),
        'condition_versions': VARIANTS, 'publication_guard_version': GUARD_VERSION,
        'checkpoint_reference': checkpoint_reference(), 'total_execution_seconds': TOTAL_SECONDS,
        'money_limit': None, 'request_limit': None,
        'question': 'After all conditions use the same publication reminders, can fresh Solo complete Click and does fixed Integrator improve on StrongShare?',
        'scope': 'Development diagnostic on one previously observed task and one reused multi-agent checkpoint; two new samples per condition. Not unseen-task validation or a causal estimate from old-versus-new scores.',
        'guard': 'One process-local publication guard applies to solo, agent1 and agent2. Only voluntary Submitted exits can receive up to two reminders for local unpublished work. The model must commit/push/publish itself through normal tools. No repair, automatic publication, worktree score fallback, added deadline, or requirement that a redundant peer produce a nonempty patch.',
        'scoring': 'Pinned CooperBench unchanged: StrongShare native merge/lead fallback; Integrator predesignated agent1 only; Solo published single patch. Empty-artifact/apply failures remain official failures and are distinguished from assertion failures.',
        'time_rule': 'Fresh Solo has 1800 seconds including initialization. SS/I have 1800 minus historical accepted prefix and packet-preparation time, exactly the previous Click allowance. Guard checks and any resulting model turns consume this same allowance. In-flight calls can finish after the deadline.',
        'checkpoint_rule': 'Reuse the exact original Click source snapshots, histories, shared Git archive and complete packet. The copied historical decision only binds packet provenance; this study evaluates no new selector and makes no policy claim.',
        'order': 'Repeat 1 StrongShare/Integrator/Solo; repeat 2 Solo/Integrator/StrongShare. Independent model samples, shared implicit cache, no common random seed.',
        'stopping': 'Complete six fresh scored executions. Retain completed failures; infrastructure interruptions pause and keep all costs. Resume only unfinished execution or scoring.',
        'cost_rule': 'New spend sums six physical job ledgers including interrupted attempts. Historical prefix cost is added only to full-task comparison columns, never billed again. Unknown request usage stays unknown.',
        'interpretation': 'Inspect reminder events, actual publication, feature-test results, cost and agent coordination. Improved new Solo scores alone do not prove the reminder caused the change; a triggered reminder followed by publication supports the mechanism within that trace.',
        'source_manifest': source_manifest()}


def verify_sources(protocol):
    if protocol['source_manifest'] != source_manifest() or protocol['checkpoint_reference'] != checkpoint_reference():
        raise RuntimeError('Frozen publication study inputs changed')


def verify_preflight():
    proof = read_json(PROOF, {})
    guard_proof = read_json(ROOT / 'artifacts/publication_guard_docker_validation.json', {})
    if (guard_proof.get('passed') is not True or guard_proof.get('model_api_calls') != 0
            or guard_proof.get('guard_source_sha256') != file_hash(ROOT / 'publication_guard.py')):
        raise RuntimeError('Exact-guard Docker premature-completion validation required')
    if (proof.get('passed') is not True or proof.get('model_api_calls') != 0
            or proof.get('completed_jobs') != 3 or proof.get('source_manifest') != source_manifest()
            or proof.get('checkpoint_reference') != checkpoint_reference()):
        raise RuntimeError('Exact-source three-condition offline validation required')


def job_path(suite, job):
    if (job['task'] != TASK_KEY or job['condition'] not in CONDITIONS or type(job['repeat']) is not int
            or job['repeat'] not in (1, 2)):
        raise ValueError('Unplanned publication job')
    kind = 'solo_jobs' if job['condition'] == 'solo' else 'jobs'
    return suite / TASK_KEY / kind / f"{TASK_KEY}_r{job['repeat']:02d}_{job['condition']}"


def copy_historical_packet(suite, protocol):
    verify_sources(protocol)
    folder = suite / TASK_KEY / 'decision'
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('packet.json', 'decision.json', 'preparation.json'):
        write_fixed(folder / name, read_json(ORIGIN / TASK_KEY / 'decision' / name))
    write_fixed(suite / TASK_KEY / 'historical_reference.json', protocol['checkpoint_reference'])


def configure_run(run, job, protocol):
    condition = job['condition']
    reference = protocol['checkpoint_reference']
    seconds = TOTAL_SECONDS if condition == 'solo' else round(TOTAL_SECONDS - reference['record']['elapsed_seconds'] - reference['preparation']['elapsed_seconds'], 6)
    config = {**job, 'task_key': TASK_KEY, 'task': TASK.as_dict(), 'condition_version': VARIANTS[condition],
              'experiment_version': VERSION, 'offline_selftest': protocol['offline_selftest'],
              'model': protocol['model'], 'protocol_sha256': digest(protocol), 'execution_seconds': seconds,
              'initialization_included_in_budget': True, 'publication_guard_version': protocol['publication_guard_version']}
    if condition == 'solo':
        config['fresh_solo'] = True
    else:
        config.update(checkpoint_sha256=reference['record']['checkpoint_sha256'],
                      submission_policy='authoritative_agent1_only' if condition == 'integrator' else 'native_cooperative_merge')
    run.mkdir(parents=True, exist_ok=True)
    write_fixed(run / 'task_config.json', config)
    if condition != 'solo':
        copy_checkpoint(Path(reference['directory']), run, TASK, reference['record'])
    return config


def verify_execution(phase, task, condition, freeze=False):
    from publication_workers import verify_execution as verify
    return verify(phase, task, condition, freeze=freeze)


def model_phase(run, condition, protocol, suite, lock_fd):
    # Keep the proven archive, pause, watchdog and terminal-reuse implementation
    # in a private namespace so no historical globals or sources are modified.
    spec = importlib.util.spec_from_file_location('_publication_supervision', ROOT / 'run_luna_selector.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = __file__
    module.verify_execution = verify_execution
    return module.model_phase(run, TASK, condition, protocol, suite, lock_fd)


def evaluate(run, condition, lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase = run / condition
    before = verify_execution(phase, TASK, condition)
    code = run_command([sys.executable, '-B', str(ROOT / 'publication_evaluator.py'), '--run-dir', str(phase)],
        phase / 'evaluation_console.log', 720, lock_fd, env=evaluator_environment())
    score = read_json(phase / 'evaluation_summary.json', {})
    if code or not score.get('evaluation_completed'):
        raise RuntimeError('Scoring paused; saved execution will be reused')
    if before != verify_execution(phase, TASK, condition):
        raise RuntimeError('Execution changed during scoring')
    return score


def build_row(run, job, execution, score, protocol):
    reference = protocol['checkpoint_reference']
    current = usage(run)
    failed = incomplete_seconds(run, job['condition'])
    common = reference['record']['all_attempt_usage'] if job['condition'] != 'solo' else add_usage([])
    common_seconds = reference['record']['elapsed_seconds'] + reference['preparation']['elapsed_seconds'] if job['condition'] != 'solo' else 0
    from publication_workers import guard_summary
    return {**job, 'run_directory': str(run), 'model': MODEL, 'official_success': bool(score['both_passed']),
        'execution': execution, 'evaluation': score, 'accepted_usage': usage(run, job['condition']),
        'all_attempt_usage': current, 'historical_prefix_usage': common,
        'full_task_known_cost_usd': common['known_cost_usd'] + current['known_cost_usd'],
        'full_task_unknown_usage_requests': common['unknown_usage_requests'] + current['unknown_usage_requests'],
        'full_task_execution_seconds': round(common_seconds + execution['elapsed_seconds'] + failed['known_seconds'], 6),
        'full_task_time_unknown_attempts': failed['unknown_attempts'],
        'checkpoint_sha256': reference['record']['checkpoint_sha256'] if job['condition'] != 'solo' else None,
        'publication_guard': guard_summary(run / job['condition']),
        'remaining_execution_allowance_seconds': read_json(run / 'task_config.json')['execution_seconds']}


def summary_for(suite, protocol, rows, skipped):
    ledgers = list(suite.glob('*/jobs/*/usage_ledger.json')) + list(suite.glob('*/solo_jobs/*/usage_ledger.json'))
    groups = []
    for condition in CONDITIONS:
        data = [r for r in rows if r['condition'] == condition]
        if data:
            groups.append({'condition': condition, 'n': len(data), 'official_passes': sum(r['official_success'] for r in data),
                'full_task_known_cost_mean': sum(r['full_task_known_cost_usd'] for r in data) / len(data),
                'unknown_usage_requests': sum(r['full_task_unknown_usage_requests'] for r in data)})
    return {'version': VERSION, 'pipeline_completed': len(rows) + len(skipped) == len(protocol['jobs']),
        'planned_executions': len(protocol['jobs']), 'completed_executions': len(rows), 'rows': rows, 'skipped_jobs': skipped,
        'comparison_groups': groups, 'actual_new_usage': add_usage([usage(p.parent) for p in ledgers]),
        'new_prefix_requests': 0, 'historical_prefix_rebilled': False, 'interpretation': protocol['interpretation']}


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
        copy_historical_packet(suite, protocol)
        rows, skipped = [], []
        try:
            for job in protocol['jobs']:
                verify_sources(protocol)
                run, condition = job_path(suite, job), job['condition']
                configure_run(run, job, protocol)
                write_json(suite / 'status.json', {'status': 'running', **job, 'completed_jobs': len(rows), 'planned_jobs': len(protocol['jobs'])})
                execution = model_phase(run, condition, protocol, suite, lock.fileno())
                if execution['status'] in EXHAUSTED_STATES:
                    skipped.append({**job, 'reason': execution['status'], 'run_directory': str(run)})
                else:
                    score = evaluate(run, condition, lock.fileno())
                    row = build_row(run, job, execution, score, protocol)
                    rows.append(row)
                    write_json(run / 'result.json', row)
                    print(f"{condition} r{job['repeat']}: official={row['official_success']}, known=${row['all_attempt_usage']['known_cost_usd']:.6f}", flush=True)
                write_json(suite / 'summary.json', summary_for(suite, protocol, rows, skipped))
            verify_sources(protocol)
            summary = summary_for(suite, protocol, rows, skipped)
            write_json(suite / 'summary.json', summary)
            if offline:
                if len(rows) != 3 or skipped or summary['actual_new_usage']['requests']:
                    raise RuntimeError('Offline proof needs three scored conditions and zero API requests')
                write_json(PROOF, {'passed': True, 'model_api_calls': 0, 'completed_jobs': 3,
                    'source_manifest': protocol['source_manifest'], 'checkpoint_reference': protocol['checkpoint_reference'],
                    'suite_directory': str(suite)})
            write_json(suite / 'status.json', {'status': 'completed', 'completed_jobs': len(rows), 'skipped_jobs': len(skipped), 'planned_jobs': len(protocol['jobs'])})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / 'summary.json', summary_for(suite, protocol, rows, skipped))
            write_json(suite / 'status.json', {'status': 'paused', 'completed_jobs': len(rows), 'skipped_jobs': len(skipped), 'error': safe_error(exc)})
            print('Publication study paused: ' + safe_error(exc), flush=True)
            return 2


def worker(run, suite):
    from publication_workers import phase_worker
    protocol = read_json(suite / 'protocol.json')
    verify_sources(protocol)
    jobs = [j for j in protocol['jobs'] if job_path(suite, j) == run]
    if len(jobs) != 1:
        raise ValueError('Unplanned worker directory')
    config = configure_run(run, jobs[0], protocol)
    copy_historical_packet(suite, protocol)
    return phase_worker(run, jobs[0]['condition'], TASK, offline=protocol['offline_selftest'], execution_seconds=config['execution_seconds'])


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
    name = 'luna_publication_offline_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') if args.offline_selftest else VERSION
    suite = (args.suite_dir or RUNS / name).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r'luna_publication_[A-Za-z0-9_]+', suite.name):
        raise ValueError('Invalid publication suite path')
    return worker(args.run_dir.resolve(strict=True), suite) if args.worker else supervise(suite, args.offline_selftest)


if __name__ == '__main__':
    raise SystemExit(main())
