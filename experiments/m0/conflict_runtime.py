"""Current-conflict collection and paired organization continuations.

Only new modules implement this protocol. Frozen native loops, SDK, publisher
and publication guard remain intact. Importing this file starts no services.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import time
import types

from budget_guard import write_json
from run_baselines import digest, load_state, save_state, usage, close_interrupted_tool_calls, safe_error
from run_solo_smoke import configure
from task_specs import ROOT, REPO, COMMIT
from luna_accounting import LunaAccounting, MODEL, MAX_OUTPUT
from conflict_checkpoint import (CHECKPOINT_SCHEMA, CHECKPOINT_RULE, inspect_conflict,
    conflict_eligible, validate_conflict_checkpoint, verify_checkpoint_files, verify_restored_conflict)
import conflict_packet as packet
import cross_task_packet as early_packet
import luna_integrator_worker as native

ACTIVE = ('agent1', 'agent2')
VARIANT = packet.SCHEMA
VERSION = 'luna_conflict_v1'
COLLECTION_VERSION = 'conflict_collection_v1'
TERMINALS = ('completed', 'time_budget_exhausted_before_first_query', 'budget_exhausted')


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Required regular artifact missing: ' + str(path))
    return json.loads(path.read_text())


def file_hash(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Required regular artifact missing: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task(task):
    from conflict_evaluator import get_task
    if task.as_dict() != get_task(task.key).as_dict():
        raise ValueError('Task differs from registered conflict study')


def _config(run, task, condition, offline, seconds, collection=False):
    _task(task)
    if type(offline) is not bool or type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0 or (collection and seconds == 0):
        raise ValueError('Invalid execution allowance')
    cfg = read(run / 'task_config.json')
    expected = {'task': task.as_dict(), 'task_key': task.key, 'condition': condition,
        'offline_selftest': offline, 'execution_seconds': seconds, 'experiment_version': VERSION,
        'submission_policy': 'authoritative_agent1_only', 'terminal_policy': 'both_active',
        'condition_version': COLLECTION_VERSION if collection else 'conflict_' + condition + '_v1',
        'stage': 'conflict_collection' if collection else 'conflict_continuation',
        'model': 'offline-scripted-model' if offline else MODEL}
    if any(cfg.get(k) != v for k, v in expected.items()):
        raise ValueError('Conflict config does not match worker arguments')
    status_path = run / condition / 'status.json'
    if status_path.exists() and read(status_path).get('status') in (*TERMINALS, 'no_conflict'):
        raise ValueError('Terminal execution must be reused, not rerun')
    return cfg


def common_delivery_message(task):
    f1, f2 = task.features
    return (
        'COMMON FINAL DELIVERY CONTRACT. Both agents remain active with their original feature assignments. '
        f'The final artifact is fixed in advance: only agent1\'s published complete patch is evaluated against '
        f'BOTH feature {f1} and feature {f2}. Agent2\'s published patch is a proposal retained for collaboration; '
        'the evaluator will not automatically merge it, choose it instead of agent1, or use it as a fallback. '
        'Coordinate using your existing messages and Git to get the required combined implementation onto '
        'agent1\'s published branch. Decide your collaboration and integration responsibilities yourselves. '
        'No code is automatically copied, repaired, merged or published. This supersedes earlier instructions '
        'about automatic evaluation-time merging. Both colleagues\' latest source, task and complete tool history '
        'are visible read-only at /shared-evidence/agent1/latest/ and /shared-evidence/agent2/latest/; '
        'these snapshots update after completed steps. Preserve normal feature roles; neither agent is retired. '
        'Leave time to commit and push your branch, open your local PR when appropriate, and use the exact '
        'completion command separately. Publish incomplete work honestly and do not claim failed tests passed. '
        'No monetary or model-turn limit applies; the execution deadline remains fixed.')


def organization_message(task, condition, seconds):
    if condition not in ('strongshare', 'integrator'):
        raise ValueError('Unknown conflict condition')
    role = ('Continue your original feature responsibilities. Resolve the current conflict through ordinary '
            'communication and choose the resolution and integration division of labor yourselves. '
            if condition == 'strongshare' else
            'Agent1 is now explicitly assigned responsibility for resolving the conflict and integrating the '
            'complete implementation into its own final published branch. Agent2 remains active, continues '
            'its feature work, and assists agent1 with proposed fixes, relevant commits and test observations. ')
    return ('CURRENT-CONFLICT ORGANIZATION UPDATE. ' + role + common_delivery_message(task).replace(
        'Decide your collaboration and integration responsibilities yourselves. ', '' if condition == 'integrator' else
        'Decide your collaboration and integration responsibilities yourselves. ') +
        f' Remaining continuation allowance at this update: {seconds:.3f} seconds, including restore and handoff.')


def _provider_history(messages):
    """Native exit markers are persisted locally, never sent to the provider.

    Remove only those markers for read-only delivery verification. Do not
    normalize away arbitrary bad roles or mutate the saved trajectory.
    """
    return [copy.deepcopy(m) for m in messages if m.get('role') != 'exit']


def _append(agent, text):
    early_packet._SOURCE_VALIDATOR._validate_messages(agent.messages)
    before = copy.deepcopy(agent.messages)
    message = agent.model.format_message(role='user', content=text)
    if message.get('role') != 'user' or message.get('content') != text:
        raise RuntimeError('Formatter changed protocol message')
    agent.add_messages(message)
    if agent.messages[:-1] != before:
        raise RuntimeError('Protocol update changed inherited history')
    early_packet._SOURCE_VALIDATOR._validate_messages(agent.messages)
    return {'role': 'user', 'message_index': len(before), 'content': text,
            'message_sha256': hashlib.sha256(text.encode()).hexdigest(), 'history_preserved': True}


def assignment(agents, cp, task, condition, remaining_seconds):
    result = {'schema': 'current_conflict_assignment_v1', 'condition': condition,
        'condition_version': 'conflict_' + condition + '_v1', 'checkpoint_sha256': digest(cp),
        'active_agents': list(ACTIVE), 'authoritative_agent': 'agent1', 'peer_retired': False,
        'automatic_merge': False, 'posthoc_selection': False, 'recipients': {}}
    text = organization_message(task, condition, remaining_seconds)
    for a in ACTIVE:
        before = save_state(agents[a])
        if digest(before) != digest(cp['agents'][a]['state']):
            raise RuntimeError('Pre-assignment native state changed')
        record = _append(agents[a], text)
        record.update(before_state_sha256=digest(before), after_state_sha256=digest(save_state(agents[a])))
        result['recipients'][a] = record
    return result


def delivery_record(phase, publications, condition):
    result = native.final_delivery_record(phase, publications)
    result.update(schema='conflict_authoritative_delivery_v1', condition=condition,
        condition_version='conflict_' + condition + '_v1', submission_policy='authoritative_agent1_only')
    return result


def _scripted(commands, agent_id):
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    class Scripted(LitellmModel):
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]
        def __init__(self):
            super().__init__(model_name='offline-scripted-model', cost_tracking='ignore_errors')
            self.index = 0
        def _query(self, messages, **kwargs):
            if self.index >= len(commands):
                raise RuntimeError('Unexpected additional synthetic model query')
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({'id': f'conflict-{agent_id}-{self.index}',
                'object': 'chat.completion', 'created': 0, 'model': 'offline-scripted-model',
                'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                'content': 'Offline synthetic infrastructure validation.', 'tool_calls': [{
                    'id': f'conflict_{agent_id}_{self.index}', 'type': 'function', 'function': {
                        'name': 'bash', 'arguments': json.dumps({'command': command})}}]}}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})
        def _calculate_cost(self, response):
            return {'cost': 0.0}
    return Scripted()


def offline_collection_commands(agent_id):
    # Both commits create the same new file differently: a real add/add conflict.
    # Synthetic fixture generation is exclusive to offline=True and recorded in CP.
    peer = 'agent2' if agent_id == 'agent1' else 'agent1'
    return [
        "printf '%s\\n' " + shlex.quote(agent_id) + " > conflict_offline_fixture.txt && git add -A && "
        "git commit -m 'Synthetic conflict fixture' && git push origin HEAD:" + agent_id,
        f"git fetch origin && git merge --no-edit origin/{peer}; "
        "test -n \"$(git ls-files -u)\" && echo synthetic_current_conflict_verified",
    ]


def offline_continuation_commands(task, agent_id):
    peer = 'agent2' if agent_id == 'agent1' else 'agent1'
    probe = (f"set -e; test -r /shared-evidence/{peer}/latest/task.md && "
        f"test -d /shared-evidence/{peer}/latest/worktree && "
        f"if touch /shared-evidence/{peer}/latest/should_not_write 2>/dev/null; then exit 9; fi; "
        "echo handoff_read_only_ok")
    resolve = ("printf 'agent1\\nagent2\\n' > conflict_offline_fixture.txt && git add -A && "
        "git commit -m 'Synthetic model resolves current conflict' && git push origin HEAD:" + agent_id +
        " && echo offline_integrator_peer_incorporated")
    return [probe, resolve, 'gh pr create --title "Offline conflict delivery" --body "Synthetic zero-API validation"',
            'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT']


@contextmanager
def _continuation_context(run, condition, task):
    import baseline_runtime
    import baseline_evidence
    from cross_task_evidence import SourceScopedPublisher
    cp = verify_checkpoint_files(run, task)
    spec = importlib.util.spec_from_file_location('_private_conflict_native', ROOT / 'luna_integrator_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.VARIANT, module.CONDITION_VERSION = VARIANT, 'conflict_' + condition + '_v1'
    module.validate_checkpoint = validate_conflict_checkpoint
    module._validate_boundary = early_packet._SOURCE_VALIDATOR._validate_messages
    module.apply_integrator_assignment = lambda agents, checkpoint, task, remaining_seconds: assignment(
        agents, checkpoint, task, condition, remaining_seconds)
    module.final_delivery_record = lambda phase, pubs: delivery_record(phase, pubs, condition)
    module.make_offline_model = lambda task, a: _scripted(offline_continuation_commands(task, a), a)
    def inject(agent, value, a):
        record = _append(agent, packet.packet_message(value))
        record.pop('content')
        record.update(packet_sha256=digest(value), all_packet_content_delivered=True,
            condition=condition, agent_id=a, schema_version=3, representation='unified_diff')
        return record
    module.inject_integrator_packet = inject
    original_write = module.write_json
    def write(path, value):
        path = Path(path)
        if path.name == 'integrator_assignment.json':
            path = path.with_name('organization_assignment.json')
        original_write(path, value)
    module.write_json = write
    runtime = types.ModuleType('baseline_runtime')
    runtime.__dict__.update(baseline_runtime.__dict__)
    proofs = {}
    def verify(env, manifest):
        baseline_runtime.verify_workspace_manifest(env, manifest)
        proofs[env.agent_id] = verify_restored_conflict(env, cp['conflict_checkpoint']['observations'][env.agent_id])
        write_json(run / condition / 'conflict_restore_proof.json', {
            'checkpoint_sha256': digest(cp), 'agents': proofs})
    runtime.verify_workspace_manifest = verify
    evidence = types.ModuleType('baseline_evidence')
    evidence.__dict__.update(baseline_evidence.__dict__)
    evidence.StrongSharePublisher = lambda *args, **kwargs: SourceScopedPublisher(*args, task=task, **kwargs)
    packet_binding = types.ModuleType('cross_task_packet')
    packet_binding.__dict__.update(packet.__dict__)
    frozen = read(run / 'checkpoint_packet.json')
    frozen_text = (run / 'checkpoint_packet.txt').read_text()
    def checked_packet(*args, **kwargs):
        rebuilt = packet.build_packet(*args, **kwargs)
        if rebuilt != frozen or packet.packet_message(rebuilt) != frozen_text:
            raise RuntimeError('Restored packet differs before any model construction/query')
        return rebuilt
    packet_binding.build_packet = checked_packet
    bindings = {'cross_task_packet': packet_binding, 'baseline_runtime': runtime, 'baseline_evidence': evidence}
    previous = {key: sys.modules.get(key) for key in bindings}
    sys.modules.update(bindings)
    try:
        yield module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=1200):
    run = Path(run_dir)
    _config(run, task, condition, offline, execution_seconds)
    cp = verify_checkpoint_files(run, task)
    if cp.get('synthetic_conflict') is not offline:
        raise ValueError('Synthetic/real checkpoint protocol mismatch')
    frozen = read(run / 'checkpoint_packet.json')
    if frozen.get('checkpoint_sha256') != digest(cp) or (run / 'checkpoint_packet.txt').read_text() != packet.packet_message(frozen):
        raise ValueError('Frozen collection packet does not match checkpoint')
    if execution_seconds == 0:
        return _zero_budget(run, condition, task, cp, frozen, offline)
    configure()
    from publication_guard import publication_guard
    with publication_guard(run):
        with _continuation_context(run, condition, task) as module:
            result = module._phase_worker(run, condition, task, offline, float(execution_seconds))
    produced = run / condition / 'handoff_packet.json'
    if produced.exists() and read(produced) != frozen:
        raise RuntimeError('Restored continuation packet differs from collection packet')
    if read(run / condition / 'status.json')['status'] in TERMINALS:
        freeze_execution_receipt(run / condition, task)
    return result


def _zero_budget(run, condition, task, cp, frozen, offline):
    phase = run / condition
    phase.mkdir(parents=True, exist_ok=True)
    guard = LunaAccounting(run / 'usage_ledger.json', run.name, model=MODEL,
        enforce_limits=False, max_output_tokens=MAX_OUTPUT)
    guard._persist()
    write_json(phase / 'status.json', {'phase': condition, 'condition': condition,
        'condition_version': 'conflict_' + condition + '_v1', 'status': 'time_budget_exhausted_before_first_query',
        'task': task.as_dict(), 'offline_selftest': offline, 'handoff_variant': VARIANT,
        'handoff_packet_sha256': digest(frozen), 'continuation_budget_seconds': 0,
        'initialization_included_in_budget': True, 'active_agents': list(ACTIVE),
        'peer_retired': False, 'authoritative_agent': 'agent1', 'infrastructure_error': False,
        'continuation_model_requests': 0, 'elapsed_seconds': 0.0, 'continuation_seconds': 0.0,
        'setup_skipped_due_to_zero_budget': True, 'no_execution': True})
    write_json(phase / 'usage_summary.json', usage(run, condition))
    freeze_execution_receipt(phase, task)
    return 0


def _read_early(origin, task):
    from wip_checkpoint import validate_checkpoint
    cp, status = read(origin / 'checkpoint.json'), read(origin / 'prefix/status.json')
    validate_checkpoint(cp, task)
    if (status.get('status') != 'completed' or status.get('offline_selftest') is not False
            or file_hash(origin / 'shared_git.tar') != cp['git_archive_sha256']
            or read(origin / 'usage_ledger.json').get('model') != MODEL):
        raise ValueError('Original real Luna early checkpoint provenance failed')
    return cp, {str(origin / name): file_hash(origin / name) for name in
        ('checkpoint.json', 'shared_git.tar', 'prefix/status.json', 'task_config.json', 'usage_ledger.json')}


def collect_conflict(run_dir, task, *, early_prefix_dir, execution_seconds, offline=False):
    run, origin = Path(run_dir), Path(early_prefix_dir).resolve()
    cfg = _config(run, task, 'strongshare', offline, execution_seconds, collection=True)
    cp, early_files = _read_early(origin, task)
    if cfg['historical_checkpoint_sha256'] != digest(cp) or Path(cfg['early_prefix_directory']).resolve() != origin:
        raise ValueError('Collector original checkpoint differs from frozen config')
    configure()
    from publication_guard import publication_guard
    with publication_guard(run):
        code = _collect(run, task, origin, cp, early_files, float(execution_seconds), offline)
    if read(run / 'strongshare/status.json')['status'] in ('completed', 'no_conflict'):
        receipt = _collection_receipt(run, task)
        write_json(run / 'collection_receipt.json', receipt)
    return code


def _collect(run, task, origin, cp, early_files, seconds, offline):
    import docker
    import yaml
    from baseline_runtime import MemoryBroker, SnapshotDockerEnvironment, restore_volume, verify_workspace_manifest, backup_volume
    from cross_task_evidence import SourceScopedPublisher
    from luna_model import make_model
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
    from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
    from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow
    started = time.monotonic()
    deadline = started + seconds
    phase = run / 'strongshare'
    phase.mkdir(parents=True, exist_ok=True)
    label = run.name + '-strongshare'
    volume = 'rsi-' + label.lower().replace('_', '-')
    evidence_volume = volume + '-evidence'
    envs, agents, outcomes, progress, created, images = {}, {}, {}, [], [], []
    dc = publisher = broker = None
    cleanup_errors, exit_code = [], 2
    result = {'phase': 'strongshare', 'stage': 'conflict_collection', 'status': 'initializing',
        'task': task.as_dict(), 'condition': 'strongshare', 'condition_version': COLLECTION_VERSION,
        'offline_selftest': offline, 'synthetic_conflict': offline, 'early_checkpoint_sha256': digest(cp),
        'execution_seconds': seconds, 'initialization_included_in_budget': True}
    write_json(phase / 'status.json', result)
    try:
        expected = read(task.image_record_path)
        dc = docker.from_env(timeout=120)
        if dc.images.get(task.image).id != expected['Id']:
            raise RuntimeError('Pinned task image changed')
        if subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip() != COMMIT:
            raise RuntimeError('Pinned upstream commit changed')
        subprocess.run(['git', '-C', str(REPO), 'diff', '--exit-code', 'HEAD', '--',
            'src/cooperbench/agents/mini_swe_agent_v2', str(task.task_dir.relative_to(REPO))], check=True, capture_output=True)
        restore_volume(origin / 'shared_git.tar', volume, expected['Id'], label, expected_sha256=cp['git_archive_sha256'])
        created.append(volume)
        dc.volumes.create(name=evidence_volume, labels={'agent-rsi-run': label})
        created.append(evidence_volume)
        publisher = SourceScopedPublisher(dc, expected['Id'], evidence_volume, label, task=task)
        broker = MemoryBroker.from_snapshot(cp['broker'])
        guard = LunaAccounting(run / 'usage_ledger.json', run.name, model=MODEL, enforce_limits=False, max_output_tokens=MAX_OUTPUT)
        guard._persist()
        config = yaml.safe_load(get_config_path('coop').read_text())
        proof = {'checkpoint_hash': digest(cp), 'broker_hash': digest(broker.snapshot()), 'agents': {}}
        for a in ACTIVE:
            saved = cp['agents'][a]
            envs[a] = SnapshotDockerEnvironment(image=saved['image'], run_label=label, agent_id=a,
                shared_git_volume=volume, evidence_volume=evidence_volume, container_timeout='40m')
            verify_workspace_manifest(envs[a], saved['manifest'])
            if inspect_conflict(envs[a])['has_unmerged']:
                raise ValueError('Original early checkpoint already contains a current conflict')
            model = _scripted(offline_collection_commands(a), a) if offline else make_model(MODEL, guard, 'strongshare', a, phase)
            model.config = model.config.model_copy(update={k: config['model'][k] for k in ('observation_template', 'format_error_template')})
            agent = DefaultAgent(model, envs[a], comm=broker.connector(a), agent_id=a,
                **{**config['agent'], 'step_limit': 0, 'cost_limit': 0.0, 'compaction_enabled': False,
                   'output_path': phase / f'{a}.traj.json'})
            agents[a] = agent
            load_state(agent, saved['state'])
            if digest(save_state(agent)) != digest(saved['state']):
                raise RuntimeError('Restored native state differs')
            proof['agents'][a] = {'state_verified': True, 'filesystem_verified': True,
                'state_hash': digest(saved['state']), 'filesystem_manifest_hash': digest(saved['manifest']), 'source_image': saved['image']}
            outcomes[a] = {'exited': False, 'submitted': False, 'infrastructure_error': False, 'exit_status': None, 'published_pr': False}
        write_json(phase / 'restore_proof.json', proof)
        write_json(phase / 'early_provenance.json', {'checkpoint_sha256': digest(cp), 'files_sha256': early_files,
            'source_directory': str(origin), 'historical_model_requests_reexecuted': False})
        initial = early_packet.build_packet(cp, task,
            {a: early_packet.collect_source_changes(envs[a], task) for a in ACTIVE},
            {a: early_packet.collect_scratch_files(envs[a], cp['agents'][a]) for a in ACTIVE})
        initial_text = early_packet.packet_message(initial)
        write_json(phase / 'initial_packet.json', initial)
        (phase / 'initial_packet.txt').write_text(initial_text)
        assignments = {'recipients': {}}
        def publish(a):
            meta = publisher.publish(a, envs[a].container_id, '/workspace/repo', agents[a].messages,
                cp['agents'][a]['state']['extra_template_vars']['task'], agents[a].n_calls)
            (phase / 'evidence_manifests').mkdir(exist_ok=True)
            write_json(phase / 'evidence_manifests' / f'{a}_{agents[a].n_calls}.json', meta)
        for a in ACTIVE:
            rec = _append(agents[a], common_delivery_message(task))
            assignments['recipients'][a] = rec
            _append(agents[a], initial_text)
            native.install_first_query_check(agents[a].model, initial_text, rec,
                cp['agents'][a]['state']['messages'], a, phase)
            agents[a].save(agents[a].config.output_path)
            publish(a)
        write_json(phase / 'collection_assignment.json', assignments)
        def step(a):
            agent = agents[a]
            try:
                agent.step()
            except InterruptAgentFlow as exc:
                if any(m.get('role') == 'exit' for m in exc.messages):
                    close_interrupted_tool_calls(agent)
                agent.add_messages(*exc.messages)
            if agent.messages[-1].get('role') == 'exit':
                if agent._nudge_unsubmitted():
                    agent.messages = [m for m in agent.messages if m.get('role') != 'exit']
                else:
                    status = agent.messages[-1].get('extra', {}).get('exit_status', 'unknown')
                    if status != 'Submitted':
                        raise RuntimeError('Unexpected non-Submitted native exit: ' + status)
                    outcomes[a].update(exited=True, submitted=True, exit_status=status)
                    agent.comm.mark_exited(published=agent._opened_pr())
            outcomes[a]['published_pr'] = agent._opened_pr()
            agent.save(agent.config.output_path)
        reason = 'deadline_without_current_conflict'
        paired_round = 0
        while time.monotonic() < deadline:
            paired_round += 1
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(step, a) for a in ACTIVE]
                for future in futures:
                    future.result()
            for a in ACTIVE:
                publish(a)
            observations = {a: inspect_conflict(envs[a]) for a in ACTIVE}
            eligible = conflict_eligible(observations, outcomes, terminal_policy='both_active')
            progress.append({'paired_round': paired_round, 'observations': observations,
                'outcomes': copy.deepcopy(outcomes), 'eligible': eligible,
                'elapsed_seconds': round(time.monotonic() - started, 3)})
            write_json(phase / 'progress.json', progress)
            if any(outcomes[a]['exited'] for a in ACTIVE):
                reason = 'agent_terminal_at_paired_boundary'
                break
            if eligible:
                saved_agents = {}
                for a in ACTIVE:
                    manifest = envs[a].workspace_manifest()
                    image = envs[a].snapshot_image()
                    images.append(image)
                    saved_agents[a] = {'state': save_state(agents[a]), 'manifest': manifest, 'image': image, 'git_base': task.base}
                backup_volume(volume, run / 'shared_git.tar', expected['Id'], label)
                captured = {'schema': CHECKPOINT_SCHEMA, 'task': task.as_dict(), 'agents': saved_agents,
                    'broker': broker.snapshot(), 'git_archive_sha256': file_hash(run / 'shared_git.tar'),
                    'prefix_steps_per_agent': paired_round, 'synthetic_conflict': offline,
                    'source_early_checkpoint_sha256': digest(cp),
                    'conflict_checkpoint': {'rule': CHECKPOINT_RULE, 'paired_round': paired_round,
                        'observations': observations, 'outcomes': copy.deepcopy(outcomes),
                        'terminal_policy': 'both_active', 'eligible': True, 'selection_uses_test_results': False}}
                changes = {a: packet.collect_source_changes(envs[a], task) for a in ACTIVE}
                captured['source_changes_sha256'] = packet.source_manifest(changes)
                validate_conflict_checkpoint(captured, task)
                value = packet.build_packet(captured, task, changes,
                    {a: packet.collect_scratch_files(envs[a], saved_agents[a]) for a in ACTIVE})
                write_json(run / 'checkpoint.json', captured)
                write_json(run / 'checkpoint_packet.json', value)
                (run / 'checkpoint_packet.txt').write_text(packet.packet_message(value))
                result.update(status='completed', checkpoint_sha256=digest(captured), packet_sha256=digest(value),
                    paired_round=paired_round, reason='first_current_unresolved_conflict_both_active')
                break
            if time.monotonic() >= deadline:
                break
        if result['status'] != 'completed':
            result.update(status='no_conflict', reason=reason, paired_round=paired_round)
        exit_code = 0
    except Exception as exc:
        result.update(status='error', error_type=type(exc).__name__, error=safe_error(exc), infrastructure_error=True)
    finally:
        for a, agent in agents.items():
            try:
                agent.save(agent.config.output_path)
                if getattr(agent.model, 'close', None):
                    agent.model.close()
            except Exception as exc:
                cleanup_errors.append(safe_error(exc))
        for env in envs.values():
            try:
                env.cleanup()
            except Exception as exc:
                cleanup_errors.append(safe_error(exc))
        if publisher:
            try:
                publisher.cleanup()
            except Exception as exc:
                cleanup_errors.append(safe_error(exc))
        if dc:
            for name in reversed(created):
                try:
                    dc.volumes.get(name).remove()
                except Exception as exc:
                    cleanup_errors.append(safe_error(exc))
            if result['status'] != 'completed':
                for image in images:
                    try:
                        dc.images.remove(image)
                    except Exception as exc:
                        cleanup_errors.append(safe_error(exc))
            dc.close()
        if result['status'] != 'completed':
            for name in ('checkpoint.json', 'shared_git.tar', 'checkpoint_packet.json', 'checkpoint_packet.txt'):
                (run / name).unlink(missing_ok=True)
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if cleanup_errors:
            result.update(cleanup_errors=cleanup_errors, infrastructure_error=True)
            exit_code = 2
        result['outcomes'] = outcomes
        write_json(phase / 'agent_outcomes.json', outcomes)
        write_json(phase / 'messages.json', broker.snapshot() if broker else {})
        write_json(phase / 'progress.json', progress)
        write_json(phase / 'usage_summary.json', usage(run, 'strongshare'))
        write_json(phase / 'status.json', result)
    return exit_code


def _protected(run, phase, mandatory):
    root_names = {'source_manifest.json', 'status.json', 'publication_guard.json', 'progress.json',
        'early_provenance.json', 'restore_proof.json', 'conflict_restore_proof.json',
        'collection_assignment.json', 'organization_assignment.json', 'initial_packet.json', 'initial_packet.txt',
        'handoff_packet.json', 'handoff_packet.txt', 'handoff_delivery.json', 'final_delivery.json',
        'agent_outcomes.json', 'messages.json', 'usage_summary.json'}
    root_names |= {a + suffix for a in ACTIVE for suffix in
        ('.traj.json', '.patch', '_unsubmitted_diagnostic.patch')}
    root_names |= {prefix + a + '.json' for a in ACTIVE for prefix in ('first_request_', 'first_model_input_')}
    paths = {run / name for name in mandatory}
    paths.update(phase / name for name in root_names if (phase / name).exists())
    for directory in ('responses', 'evidence_manifests'):
        paths.update(p for p in (phase / directory).rglob('*') if p.is_file())
    return {str(p.relative_to(run)): file_hash(p) for p in sorted(paths)}


def _validate_source(phase):
    sources = read(phase / 'source_manifest.json')
    if not sources or 'conflict_runtime.py' not in sources or 'conflict_packet.py' not in sources:
        raise ValueError('Conflict production source manifest is incomplete')
    for rel, expected in sources.items():
        if Path(rel).is_absolute() or '..' in Path(rel).parts or file_hash(ROOT / rel) != expected:
            raise ValueError('Frozen source changed: ' + rel)
    return sources


def _validate_guard(run, phase, task):
    from publication_guard import GUARD_VERSION
    value = read(phase / 'publication_guard.json')
    expected = {'schema': 'publication_guard_v1', 'guard_version': GUARD_VERSION,
        'condition': phase.name, 'task_key': task.key, 'context_closed': True,
        'run_id': run.name, 'max_nudges_per_agent': 2}
    if any(value.get(k) != v for k, v in expected.items()):
        raise ValueError('Shared publication guard evidence differs')
    return value


def _immutable(receipt, run, path):
    saved = read(path)
    if saved != receipt:
        raise ValueError('Execution receipt facts or protected artifact hashes changed')
    for relative, expected in saved['protected_files_sha256'].items():
        if Path(relative).is_absolute() or '..' in Path(relative).parts or file_hash(run / relative) != expected:
            raise ValueError('Protected execution artifact changed: ' + relative)
    return saved


def _collection_receipt(run, task):
    run, phase = Path(run), Path(run) / 'strongshare'
    cfg, status = read(run / 'task_config.json'), read(phase / 'status.json')
    if (cfg.get('stage') != 'conflict_collection' or cfg.get('condition_version') != COLLECTION_VERSION
            or cfg.get('task') != task.as_dict() or status.get('task') != task.as_dict()
            or status.get('status') not in ('completed', 'no_conflict')
            or cfg.get('offline_selftest') is not status.get('offline_selftest')
            or status.get('execution_seconds') != cfg.get('execution_seconds')):
        raise ValueError('Invalid collection terminal provenance')
    _validate_source(phase)
    _validate_guard(run, phase, task)
    original, oldfiles = _read_early(Path(cfg['early_prefix_directory']).resolve(), task)
    provenance = read(phase / 'early_provenance.json')
    if (provenance['files_sha256'] != oldfiles or provenance['checkpoint_sha256'] != digest(original)
            or status['early_checkpoint_sha256'] != digest(original)
            or cfg['historical_checkpoint_sha256'] != digest(original)):
        raise ValueError('Original collection history/provenance changed')
    proof = read(phase / 'restore_proof.json')
    if proof['checkpoint_hash'] != digest(original) or proof['broker_hash'] != digest(original['broker']):
        raise ValueError('Original model/comm restoration differs')
    initial, initial_text = read(phase / 'initial_packet.json'), (phase / 'initial_packet.txt').read_text()
    if initial_text != early_packet.packet_message(initial) or initial['checkpoint_sha256'] != digest(original):
        raise ValueError('Initial common evidence changed')
    assignments = read(phase / 'collection_assignment.json')
    ledger = read(run / 'usage_ledger.json')
    if ledger.get('model') != MODEL or (cfg['offline_selftest'] and ledger.get('requests')):
        raise ValueError('Invalid collection model accounting')
    progress = read(phase / 'progress.json')
    for index, row in enumerate(progress, 1):
        if row['paired_round'] != index or row['eligible'] != conflict_eligible(row['observations'], row['outcomes']):
            raise ValueError('Collection paired conflict observations changed')
        if index < len(progress) and (row['eligible'] or any(o['exited'] for o in row['outcomes'].values())):
            raise ValueError('Collector continued beyond its first terminal paired boundary')
    for a in ACTIVE:
        rec = assignments['recipients'][a]
        if rec['content'] != common_delivery_message(task):
            raise ValueError('Collection delivery contract is not common to both agents')
        if progress:
            if not read(phase / f'first_request_{a}.json').get('verified'):
                raise ValueError('Missing actual collector first-query proof')
            messages = _provider_history(read(phase / f'{a}.traj.json')['messages'])
            native.verify_first_messages(messages, initial_text, rec, original['agents'][a]['state']['messages'])
            if not cfg['offline_selftest']:
                first = sorted((phase / 'responses' / a).glob('request_*_input.json'))
                if not first:
                    raise ValueError('Missing actual collector transport input')
                native.verify_first_messages(read(first[0])['request']['messages'], initial_text, rec,
                    original['agents'][a]['state']['messages'])
    mandatory = {'task_config.json', 'usage_ledger.json', 'strongshare/source_manifest.json',
        'strongshare/status.json', 'strongshare/publication_guard.json', 'strongshare/progress.json',
        'strongshare/early_provenance.json', 'strongshare/restore_proof.json',
        'strongshare/collection_assignment.json', 'strongshare/initial_packet.json', 'strongshare/initial_packet.txt'}
    checkpoint_hash = packet_hash = None
    if status['status'] == 'completed':
        cp = verify_checkpoint_files(run, task)
        if (not progress or not progress[-1]['eligible'] or cp['conflict_checkpoint']['paired_round'] != len(progress)
                or cp['conflict_checkpoint']['observations'] != progress[-1]['observations']
                or cp['conflict_checkpoint']['outcomes'] != progress[-1]['outcomes']
                or cp.get('synthetic_conflict') is not cfg['offline_selftest']):
            raise ValueError('Captured conflict is not the first eligible paired state')
        checkpoint_hash = digest(cp)
        value = read(run / 'checkpoint_packet.json')
        if value['checkpoint_sha256'] != checkpoint_hash or (run / 'checkpoint_packet.txt').read_text() != packet.packet_message(value):
            raise ValueError('Captured packet differs from conflict checkpoint')
        packet_hash = digest(value)
        if status['checkpoint_sha256'] != checkpoint_hash or status['packet_sha256'] != packet_hash:
            raise ValueError('Collection packet/checkpoint status hash differs')
        for a in ACTIVE:
            if cp['agents'][a]['state']['messages'] != read(phase / f'{a}.traj.json')['messages']:
                raise ValueError('Snapshot history differs from final collector trajectory')
        mandatory |= {'checkpoint.json', 'shared_git.tar', 'checkpoint_packet.json', 'checkpoint_packet.txt'}
    elif any(row['eligible'] for row in progress):
        raise ValueError('Unavailable collection discarded an eligible current-conflict boundary')
    elif any((run / n).exists() for n in ('checkpoint.json', 'shared_git.tar', 'checkpoint_packet.json', 'checkpoint_packet.txt')):
        raise ValueError('Unavailable conflict collection retained an admitted checkpoint')
    return {'schema': 'conflict_collection_receipt_v1', 'task': task.as_dict(), 'status': status['status'],
        'checkpoint_sha256': checkpoint_hash, 'handoff_packet_sha256': packet_hash,
        'elapsed_seconds': status['elapsed_seconds'], 'historical_checkpoint_sha256': digest(original),
        'synthetic_conflict': cfg['offline_selftest'], 'selection_uses_test_results': False,
        'protected_files_sha256': _protected(run, phase, mandatory)}


def verify_collection_artifacts(run_dir, task):
    run = Path(run_dir)
    return _immutable(_collection_receipt(run, task), run, run / 'collection_receipt.json')


def _worker_receipt(phase, task):
    phase, run = Path(phase), Path(phase).parent
    condition = phase.name
    if condition not in ('strongshare', 'integrator'):
        raise ValueError('Unknown continuation phase')
    cfg, status = read(run / 'task_config.json'), read(phase / 'status.json')
    if (cfg.get('experiment_version') != VERSION or cfg.get('condition') != condition
            or cfg.get('condition_version') != 'conflict_' + condition + '_v1'
            or cfg.get('task') != task.as_dict() or status.get('task') != task.as_dict()
            or status.get('condition_version') != cfg['condition_version']
            or status.get('status') not in TERMINALS or status.get('handoff_variant') != VARIANT
            or status.get('continuation_budget_seconds') != cfg['execution_seconds']
            or status.get('offline_selftest') is not cfg.get('offline_selftest')
            or status.get('active_agents') != list(ACTIVE) or status.get('peer_retired') is not False):
        raise ValueError('Conflict continuation terminal provenance differs')
    _validate_source(phase)
    cp = verify_checkpoint_files(run, task)
    if status.get('setup_skipped_due_to_zero_budget'):
        if (cfg['execution_seconds'] != 0 or status['status'] != 'time_budget_exhausted_before_first_query'
                or status.get('continuation_model_requests') != 0
                or usage(run, condition)['requests'] != 0):
            raise ValueError('Invalid zero-budget terminal execution')
        frozen = read(run / 'checkpoint_packet.json')
        if (frozen['checkpoint_sha256'] != digest(cp) or cfg.get('checkpoint_sha256') != digest(cp)
                or cp.get('synthetic_conflict') is not cfg['offline_selftest']
                or (run / 'checkpoint_packet.txt').read_text() != packet.packet_message(frozen)):
            raise ValueError('Zero-budget terminal checkpoint identity differs')
        return {'schema': 'conflict_execution_receipt_v1', 'task': task.as_dict(), 'condition': condition,
            'condition_version': cfg['condition_version'], 'status': status['status'],
            'checkpoint_sha256': digest(cp), 'handoff_packet_sha256': digest(frozen),
            'active_agents': list(ACTIVE), 'authoritative_agent': 'agent1',
            'submission_policy': 'authoritative_agent1_only', 'patch_sha256': {},
            'protected_files_sha256': _protected(run, phase, {'task_config.json', 'usage_ledger.json',
                'checkpoint.json', 'shared_git.tar', 'checkpoint_packet.json', 'checkpoint_packet.txt'})}
    _validate_guard(run, phase, task)
    if cp.get('synthetic_conflict') is not cfg['offline_selftest'] or cfg.get('checkpoint_sha256') != digest(cp):
        raise ValueError('Continuation synthetic/checkpoint provenance differs')
    frozen, actual = read(run / 'checkpoint_packet.json'), read(phase / 'handoff_packet.json')
    text = packet.packet_message(frozen)
    if (actual != frozen or frozen['checkpoint_sha256'] != digest(cp)
            or (run / 'checkpoint_packet.txt').read_text() != text or (phase / 'handoff_packet.txt').read_text() != text
            or status['handoff_packet_sha256'] != digest(frozen)):
        raise ValueError('Continuation did not receive the frozen collection packet')
    restore = read(phase / 'conflict_restore_proof.json')
    proof = read(phase / 'restore_proof.json')
    if restore['checkpoint_sha256'] != digest(cp) or proof['checkpoint_hash'] != digest(cp) or proof['broker_hash'] != digest(cp['broker']):
        raise ValueError('Restored model/comm/conflict checkpoint differs')
    assignment_record = read(phase / 'organization_assignment.json')
    delivery = read(phase / 'handoff_delivery.json')
    for a in ACTIVE:
        if (restore['agents'][a].get('verified') is not True or restore['agents'][a]['fingerprint_sha256'] !=
                cp['conflict_checkpoint']['observations'][a]['fingerprint_sha256']
                or proof['agents'][a]['state_hash'] != digest(cp['agents'][a]['state'])
                or proof['agents'][a]['filesystem_manifest_hash'] != digest(cp['agents'][a]['manifest'])):
            raise ValueError('Exact restored Git/workspace/model state not verified')
        messages = _provider_history(read(phase / f'{a}.traj.json')['messages'])
        rec = assignment_record['recipients'][a]
        native.verify_first_messages(messages, text, rec, cp['agents'][a]['state']['messages'])
        if delivery['recipients'][a]['message_sha256'] != hashlib.sha256(text.encode()).hexdigest():
            raise ValueError('Delivered message hash differs')
        if status['status'] == 'completed':
            first = read(phase / f'first_request_{a}.json')
            if not first.get('verified') or first.get('message_sha256') != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError('Missing continuation first query proof')
            if not cfg['offline_selftest']:
                inputs = sorted((phase / 'responses' / a).glob('request_*_input.json'))
                if not inputs or not first.get('actual_transport_input_verified'):
                    raise ValueError('Missing actual continuation provider input verification')
                native.verify_first_messages(read(inputs[0])['request']['messages'], text, rec, cp['agents'][a]['state']['messages'])
    ledger = read(run / 'usage_ledger.json')
    if ledger.get('model') != MODEL or (cfg['offline_selftest'] and ledger.get('requests')):
        raise ValueError('Continuation accounting differs from Luna/offline protocol')
    mandatory = {'task_config.json', 'usage_ledger.json', 'checkpoint.json', 'shared_git.tar',
        'checkpoint_packet.json', 'checkpoint_packet.txt'}
    hashes = {}
    if status['status'] == 'completed':
        outcomes = read(phase / 'agent_outcomes.json')
        if outcomes != status.get('outcomes') or any(o.get('infrastructure_error') for o in outcomes.values()):
            raise ValueError('Accepted continuation retains failed agent execution')
        declaration = read(phase / 'final_delivery.json')
        if declaration != delivery_record(phase, declaration['publications'], condition):
            raise ValueError('Final published delivery contract changed')
        hashes = {a + '.patch': file_hash(phase / (a + '.patch')) for a in ACTIVE}
        if status['final_patch_sha256'] != hashes['agent1.patch'] or status['peer_patch_sha256'] != hashes['agent2.patch']:
            raise ValueError('Final published patch hashes differ')
    return {'schema': 'conflict_execution_receipt_v1', 'task': task.as_dict(), 'condition': condition,
        'condition_version': cfg['condition_version'], 'status': status['status'],
        'checkpoint_sha256': digest(cp), 'handoff_packet_sha256': digest(frozen),
        'active_agents': list(ACTIVE), 'authoritative_agent': 'agent1',
        'submission_policy': 'authoritative_agent1_only', 'patch_sha256': hashes,
        'protected_files_sha256': _protected(run, phase, mandatory)}


def freeze_execution_receipt(phase_dir, task):
    phase = Path(phase_dir)
    receipt = _worker_receipt(phase, task)
    if (phase / 'execution_receipt.json').exists():
        return _immutable(receipt, phase.parent, phase / 'execution_receipt.json')
    write_json(phase / 'execution_receipt.json', receipt)
    return receipt


def verify_worker_artifacts(phase_dir, task):
    phase = Path(phase_dir)
    return _immutable(_worker_receipt(phase, task), phase.parent, phase / 'execution_receipt.json')
