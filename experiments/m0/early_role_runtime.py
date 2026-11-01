"""Early-WIP role assignment with an identical authoritative delivery contract.

Both conditions use the same native two-agent loop, full source-diff packet,
live evidence, model, publication guard, and agent1-only published artifact.
Only the explicit allocation of integration responsibility differs. Historical
workers and artifacts are immutable; temporary modules are process-local.
"""
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import math
from pathlib import Path
import types

from budget_guard import write_json
from task_specs import ROOT
from run_baselines import digest, save_state, usage
from run_solo_smoke import configure
from luna_accounting import LunaAccounting, MODEL, MAX_OUTPUT
from selector_workers import module_bindings
from wip_checkpoint import validate_checkpoint
import cross_task_packet as packet
import luna_integrator_worker as native
from conflict_runtime import read, file_hash, _provider_history, _protected, _immutable, _validate_guard

VERSION = 'luna_early_roles_v1'
CONDITIONS = ('strongshare', 'integrator')
CONDITION_VERSIONS = {c: 'early_role_' + c + '_v1' for c in CONDITIONS}
VARIANT = 'source_handoff_diff_v1'
ACTIVE = ('agent1', 'agent2')
TERMINALS = ('completed', 'budget_exhausted', 'time_budget_exhausted_before_first_query')


def get_task(key):
    if key == 'llama_index17070_f1_f3':
        from cross_task_specs import get_task as registered
    elif key == 'click2068_f4_f10':
        from selector_task_specs import get_task as registered
    else:
        raise ValueError('Unregistered early-role task')
    return registered(key)


def check_task(task):
    if task is None or task.as_dict() != get_task(task.key).as_dict():
        raise ValueError('Early-role task differs from its pinned registration')


def common_delivery_message(task, seconds):
    first, second = task.features
    return (
        'COMMON ORGANIZATION AND FINAL DELIVERY CONTRACT. Both agent1 and agent2 remain active, '
        'with separate workspaces, native messages and Git tools. Neither agent is retired. '
        f'Agent1 continues feature {first}; agent2 continues feature {second}. '
        f'Only agent1\'s published complete patch will be evaluated against BOTH feature {first} and '
        f'feature {second}. This authoritative artifact was fixed before either continuation result. '
        'Agent2\'s published patch is a proposal retained for collaboration; it will not be automatically '
        'merged, selected instead of agent1, or used as a fallback. This supersedes earlier instructions '
        'about automatic evaluation-time merging of both submissions. Needed peer work must be incorporated '
        'through your own normal Git/editing tools into agent1\'s published branch. No code is automatically '
        'copied, repaired, merged or published by the runner. Both colleagues\' task, complete history and '
        'latest source are visible read-only at /shared-evidence/agent1/latest/ and '
        '/shared-evidence/agent2/latest/. These snapshots update after completed steps, including '
        'uncommitted and untracked files in the declared source roots. Use normal messages and Git to '
        'coordinate; do not edit the evidence mount. Each agent may finish normally after publishing and '
        'communicating its contribution; do not wait indefinitely after a colleague exits. '
        f'The remaining continuation allowance at this update is {seconds:.3f} seconds, including restore '
        'and handoff. No monetary or model-turn limit applies. Leave time to commit and push your branch, '
        'open the local PR when appropriate, and issue the exact completion command separately. Publish '
        'incomplete work honestly and do not claim failed tests passed. '
    )


def role_clause(condition):
    if condition == 'strongshare':
        return ('INTEGRATION RESPONSIBILITY: Continue your original feature responsibilities. '
                'Choose the integration division of labor yourselves through ordinary communication.')
    if condition == 'integrator':
        return ('INTEGRATION RESPONSIBILITY: Agent1 is explicitly responsible for integrating, resolving '
                'any conflicts, and testing the complete final implementation on its own published branch. '
                'Agent2 remains responsible for its feature and assists agent1 by publishing proposed '
                'changes and communicating relevant commits, files and test observations.')
    raise ValueError('Unknown early-role condition')


def organization_message(task, condition, seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('Invalid assignment allowance')
    return common_delivery_message(task, seconds) + role_clause(condition)


def _append(agent, text):
    packet._SOURCE_VALIDATOR._validate_messages(agent.messages)
    before = copy.deepcopy(agent.messages)
    value = agent.model.format_message(role='user', content=text)
    if value.get('role') != 'user' or value.get('content') != text:
        raise RuntimeError('Model formatter changed role/evidence content')
    agent.add_messages(value)
    if agent.messages[:-1] != before:
        raise RuntimeError('Role/evidence update altered inherited history')
    packet._SOURCE_VALIDATOR._validate_messages(agent.messages)
    return {'role': 'user', 'message_index': len(before), 'content': text,
            'message_sha256': hashlib.sha256(text.encode()).hexdigest(), 'history_preserved': True}


def apply_assignment(agents, checkpoint, task, condition, remaining_seconds):
    if set(agents) != set(ACTIVE):
        raise ValueError('Both original agents must remain active')
    record = {'schema': 'early_role_assignment_v1', 'condition': condition,
        'condition_version': CONDITION_VERSIONS[condition], 'checkpoint_sha256': digest(checkpoint),
        'active_agents': list(ACTIVE), 'authoritative_agent': 'agent1', 'peer_retired': False,
        'submission_policy': 'authoritative_agent1_only', 'automatic_merge': False, 'posthoc_selection': False,
        'remaining_seconds_at_assignment': remaining_seconds, 'recipients': {}}
    content = organization_message(task, condition, remaining_seconds)
    for a in ACTIVE:
        before = save_state(agents[a])
        if digest(before) != digest(checkpoint['agents'][a]['state']):
            raise RuntimeError('Native state changed before assignment')
        own = _append(agents[a], content)
        own.update(before_state_sha256=digest(before), after_state_sha256=digest(save_state(agents[a])))
        record['recipients'][a] = own
    return record


def final_delivery_record(phase, publications, condition):
    value = native.final_delivery_record(phase, publications)
    value.update(schema='early_role_authoritative_delivery_v1', condition=condition,
        condition_version=CONDITION_VERSIONS[condition], submission_policy='authoritative_agent1_only')
    return value


def _config(run, condition, task, offline, seconds, *, terminal=False):
    check_task(task)
    if (condition not in CONDITIONS or type(offline) is not bool or type(seconds) not in (int, float)
            or not math.isfinite(seconds) or seconds < 0):
        raise ValueError('Invalid early-role execution arguments')
    cfg = read(run / 'task_config.json')
    expected = {'experiment_version': VERSION, 'task_key': task.key, 'task': task.as_dict(),
        'condition': condition, 'condition_version': CONDITION_VERSIONS[condition],
        'offline_selftest': offline, 'execution_seconds': seconds, 'terminal_policy': 'both_active',
        'submission_policy': 'authoritative_agent1_only',
        'model': 'offline-scripted-model' if offline else MODEL}
    if any(cfg.get(k) != v for k, v in expected.items()):
        raise ValueError('Early-role configuration differs from worker arguments')
    status = run / condition / 'status.json'
    if not terminal and status.exists() and read(status).get('status') in TERMINALS:
        raise ValueError('Terminal execution must be verified and reused, not rerun')
    return cfg


def _checkpoint_and_packet(run, task, cfg):
    checkpoint = read(run / 'checkpoint.json')
    validate_checkpoint(checkpoint, task)
    if 'conflict_checkpoint' in checkpoint or checkpoint.get('synthetic_conflict'):
        raise ValueError('This experiment requires the historical real early WIP checkpoint')
    cp_hash = digest(checkpoint)
    if cp_hash != cfg.get('checkpoint_sha256') or file_hash(run / 'shared_git.tar') != checkpoint['git_archive_sha256']:
        raise ValueError('Early checkpoint/archive differs from fixed input')
    origin = Path(cfg['prefix_directory']).resolve()
    original = read(origin / 'checkpoint.json')
    original_status = read(origin / 'prefix/status.json')
    if (checkpoint != original or file_hash(origin / 'shared_git.tar') != checkpoint['git_archive_sha256']
            or original_status.get('status') != 'completed' or original_status.get('offline_selftest') is not False
            or read(origin / 'usage_ledger.json').get('model') != MODEL):
        raise ValueError('Historical real Luna early state/provenance changed')
    prepared = Path(cfg['preparation_directory']).resolve()
    preparation = read(prepared / 'preparation.json')
    frozen = read(run / 'checkpoint_packet.json')
    text = packet.packet_message(frozen)
    if (frozen != read(prepared / 'packet.json') or (run / 'checkpoint_packet.txt').read_text() != text
            or digest(frozen) != cfg.get('checkpoint_packet_sha256')
            or frozen.get('checkpoint_sha256') != cp_hash or frozen.get('schema_version') != 2
            or frozen.get('task_key') != task.key or frozen.get('checkpoint_only') is not True
            or digest(preparation) != cfg.get('preparation_sha256')
            or preparation.get('checkpoint_sha256') != cp_hash
            or preparation.get('packet_sha256') != digest(frozen)
            or preparation.get('model_api_calls') != 0 or preparation.get('task') != task.key
            or preparation.get('before_continuation_outcomes') is not True
            or (prepared / 'packet.txt').read_text() != text
            or preparation.get('packet_text_sha256') != file_hash(prepared / 'packet.txt')):
        raise ValueError('Prepared full checkpoint packet identity differs')
    provenance = {'historical_checkpoint_sha256': cp_hash, 'packet_sha256': digest(frozen),
        'preparation_sha256': digest(preparation), 'historical_prefix_reexecuted': False,
        'files_sha256': {str(p): file_hash(p) for p in (
            origin / 'checkpoint.json', origin / 'shared_git.tar', origin / 'prefix/status.json',
            origin / 'usage_ledger.json', prepared / 'preparation.json', prepared / 'packet.json', prepared / 'packet.txt')}}
    return checkpoint, frozen, text, provenance


@contextmanager
def worker_context(run, condition, task, frozen, text):
    import baseline_evidence
    from cross_task_evidence import SourceScopedPublisher
    spec = importlib.util.spec_from_file_location('_early_roles_native', ROOT / 'luna_integrator_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.VARIANT, module.CONDITION_VERSION = VARIANT, CONDITION_VERSIONS[condition]
    module.validate_checkpoint = validate_checkpoint
    module._validate_boundary = packet._SOURCE_VALIDATOR._validate_messages
    module.apply_integrator_assignment = lambda agents, cp, t, remaining_seconds: apply_assignment(
        agents, cp, t, condition, remaining_seconds)
    module.final_delivery_record = lambda phase, pubs: final_delivery_record(phase, pubs, condition)
    def inject(agent, value, a):
        record = _append(agent, packet.packet_message(value))
        record.pop('content')
        record.update(packet_sha256=digest(value), all_packet_content_delivered=True,
            condition=condition, agent_id=a, schema_version=2, representation='unified_diff')
        return record
    module.inject_integrator_packet = inject
    original_write = module.write_json
    def write(path, value):
        path = Path(path)
        if path.name == 'integrator_assignment.json':
            path = path.with_name('organization_assignment.json')
        original_write(path, value)
    module.write_json = write
    bound_packet = types.ModuleType('cross_task_packet')
    bound_packet.__dict__.update(packet.__dict__)
    def checked_build(*args, **kwargs):
        actual = packet.build_packet(*args, **kwargs)
        if actual != frozen or packet.packet_message(actual) != text:
            raise RuntimeError('Restored packet differs before any model construction or query')
        return actual
    bound_packet.build_packet = checked_build
    evidence = types.ModuleType('baseline_evidence')
    evidence.__dict__.update(baseline_evidence.__dict__)
    evidence.StrongSharePublisher = lambda *args, **kwargs: SourceScopedPublisher(*args, task=task, **kwargs)
    with module_bindings(cross_task_packet=bound_packet, baseline_evidence=evidence):
        yield module


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=1200):
    run, phase = Path(run_dir), Path(run_dir) / condition
    cfg = _config(run, condition, task, offline, execution_seconds)
    checkpoint, frozen, text, provenance = _checkpoint_and_packet(run, task, cfg)
    phase.mkdir(parents=True, exist_ok=True)
    if (phase / 'publication_guard.json').exists() or (phase / 'execution_receipt.json').exists():
        raise RuntimeError('Prior attempt must be archived before executing')
    write_json(phase / 'input_provenance.json', provenance)
    if execution_seconds == 0:
        guard = LunaAccounting(run / 'usage_ledger.json', run.name, model=MODEL,
            enforce_limits=False, max_output_tokens=MAX_OUTPUT)
        guard._persist()
        write_json(phase / 'status.json', {'phase': condition, 'condition': condition,
            'condition_version': CONDITION_VERSIONS[condition], 'status': 'time_budget_exhausted_before_first_query',
            'task': task.as_dict(), 'offline_selftest': offline, 'handoff_variant': VARIANT,
            'handoff_packet_sha256': digest(frozen), 'continuation_budget_seconds': 0,
            'initialization_included_in_budget': True, 'active_agents': list(ACTIVE),
            'authoritative_agent': 'agent1', 'peer_retired': False, 'infrastructure_error': False,
            'continuation_model_requests': 0, 'elapsed_seconds': 0.0, 'continuation_seconds': 0.0,
            'no_execution': True})
        write_json(phase / 'usage_summary.json', usage(run, condition))
        freeze_execution_receipt(phase, task)
        return 0
    configure()
    from publication_guard import publication_guard
    with publication_guard(run):
        with worker_context(run, condition, task, frozen, text) as module:
            code = module._phase_worker(run, condition, task, offline, float(execution_seconds))
    if read(phase / 'status.json')['status'] in TERMINALS:
        freeze_execution_receipt(phase, task)
    return code


def _sources(phase):
    values = read(phase / 'source_manifest.json')
    if 'early_role_runtime.py' not in values:
        raise ValueError('Missing new early-role production source')
    for relative, expected in values.items():
        if Path(relative).is_absolute() or '..' in Path(relative).parts or file_hash(ROOT / relative) != expected:
            raise ValueError('Frozen production source changed: ' + relative)


def _first_input(phase, a, checkpoint, assignment, text, offline, required):
    saved = checkpoint['agents'][a]['state']
    trajectory = read(phase / f'{a}.traj.json')['messages']
    n = len(saved['messages'])
    if trajectory[:n] != saved['messages']:
        raise ValueError('Original complete native history changed')
    native.verify_first_messages(_provider_history(trajectory), text, assignment, saved['messages'])
    first_path = phase / f'first_request_{a}.json'
    if not required and not first_path.exists():
        return
    first = read(first_path)
    checked = read(phase / f'first_model_input_{a}.json')
    count = checked.get('message_count')
    if (first.get('verified') is not True or first.get('recipient') != a
            or first.get('organization_message_sha256') != assignment['message_sha256']
            or first.get('message_sha256') != hashlib.sha256(text.encode()).hexdigest()
            or first.get('full_packet_present') is not True
            or first.get('inherited_history_verified') is not True
            or type(count) is not int or count < n + 2 or count >= len(trajectory)
            or trajectory[count].get('role') != 'assistant'
            or checked.get('recipient') != a or checked.get('contains_raw_messages') is not False):
        raise ValueError('First model request proof is incomplete')
    model_messages = native._provider_messages(trajectory[:count])
    if digest(model_messages) != checked.get('messages_sha256'):
        raise ValueError('Actual first input fingerprint differs from final history')
    native.verify_first_messages(model_messages, text, assignment, saved['messages'])
    if not offline:
        inputs = sorted((phase / 'responses' / a).glob('request_*_input.json'))
        if not inputs or first.get('actual_transport_input_verified') is not True:
            raise ValueError('Actual provider input is missing or unverified')
        actual = read(inputs[0])['request']['messages']
        native.verify_first_messages(actual, text, assignment, saved['messages'])
        if digest(native._provider_messages(actual)) != checked['messages_sha256']:
            raise ValueError('Actual provider input differs from checked first input')


def _receipt(phase, task):
    phase, run = Path(phase), Path(phase).parent
    condition = phase.name
    status = read(phase / 'status.json')
    cfg = _config(run, condition, task, status.get('offline_selftest'),
        status.get('continuation_budget_seconds'), terminal=True)
    if (status.get('status') not in TERMINALS or status.get('phase') != condition
            or status.get('condition') != condition or status.get('task') != task.as_dict()
            or status.get('condition_version') != CONDITION_VERSIONS[condition]
            or status.get('handoff_variant') != VARIANT or status.get('peer_retired') is not False
            or status.get('active_agents') != list(ACTIVE) or status.get('authoritative_agent') != 'agent1'
            or status.get('initialization_included_in_budget') is not True):
        raise ValueError('Invalid early-role terminal status')
    _sources(phase)
    checkpoint, frozen, text, provenance = _checkpoint_and_packet(run, task, cfg)
    if read(phase / 'input_provenance.json') != provenance or status.get('handoff_packet_sha256') != digest(frozen):
        raise ValueError('Original early/prepared packet provenance changed')
    ledger = read(run / 'usage_ledger.json')
    accepted = [r for r in ledger['requests'] if not r.get('excluded_from_baseline')]
    if (ledger.get('model') != MODEL or any(r.get('phase') not in {condition + ':' + a for a in ACTIVE} for r in accepted)
            or cfg['offline_selftest'] and ledger['requests']
            or read(phase / 'usage_summary.json') != usage(run, condition)):
        raise ValueError('Model/accounting/phase usage differs')
    mandatory = {'task_config.json', 'usage_ledger.json', 'checkpoint.json', 'shared_git.tar',
                 'checkpoint_packet.json', 'checkpoint_packet.txt', condition + '/input_provenance.json'}
    if (run / 'checkpoint_provenance.json').exists():
        mandatory.add('checkpoint_provenance.json')
    patches = {}
    if status.get('no_execution'):
        if (cfg['execution_seconds'] != 0 or status['status'] != 'time_budget_exhausted_before_first_query'
                or status.get('continuation_model_requests') != 0 or usage(run, condition)['requests'] != 0
                or status.get('elapsed_seconds') != 0):
            raise ValueError('Invalid zero-budget terminal; no provider call is allowed')
    else:
        _validate_guard(run, phase, task)
        if read(phase / 'handoff_packet.json') != frozen or (phase / 'handoff_packet.txt').read_text() != text:
            raise ValueError('Delivered packet is not the common frozen packet')
        proof, roles = read(phase / 'restore_proof.json'), read(phase / 'organization_assignment.json')
        delivery = read(phase / 'handoff_delivery.json')
        expected_roles = {'schema': 'early_role_assignment_v1', 'condition': condition,
            'condition_version': CONDITION_VERSIONS[condition], 'checkpoint_sha256': digest(checkpoint),
            'active_agents': list(ACTIVE), 'authoritative_agent': 'agent1', 'peer_retired': False,
            'submission_policy': 'authoritative_agent1_only', 'automatic_merge': False, 'posthoc_selection': False}
        if (any(roles.get(k) != v for k, v in expected_roles.items())
                or proof.get('checkpoint_hash') != digest(checkpoint)
                or proof.get('broker_hash') != digest(checkpoint['broker'])
                or set(proof.get('agents', {})) != set(ACTIVE)
                or delivery.get('condition') != condition or delivery.get('checkpoint_sha256') != digest(checkpoint)
                or delivery.get('packet_sha256') != digest(frozen)
                or delivery.get('source_checkpoint_verified') is not True
                or delivery.get('code_automatically_transferred') is not False
                or set(delivery.get('recipients', {})) != set(ACTIVE)):
            raise ValueError('Native state/role/evidence restoration proof differs')
        content = organization_message(task, condition, roles['remaining_seconds_at_assignment'])
        for a in ACTIVE:
            saved, restored = checkpoint['agents'][a], proof['agents'][a]
            own = roles['recipients'][a]
            expected_state = copy.deepcopy(saved['state'])
            expected_state['messages'].append({'role': 'user', 'content': content})
            if (restored.get('state_hash') != digest(saved['state'])
                    or restored.get('filesystem_manifest_hash') != digest(saved['manifest'])
                    or restored.get('source_image') != saved['image'] or restored.get('state_verified') is not True
                    or restored.get('filesystem_verified') is not True or restored.get('state_use') != 'resumed_model_loop'
                    or own.get('content') != content or own.get('before_state_sha256') != digest(saved['state'])
                    or own.get('after_state_sha256') != digest(expected_state)
                    or own.get('history_preserved') is not True or own.get('role') != 'user'):
                raise ValueError('Agent role assignment or complete restored state changed')
            recipient = delivery['recipients'][a]
            if (recipient.get('condition') != condition or recipient.get('agent_id') != a
                    or recipient.get('packet_sha256') != digest(frozen)
                    or recipient.get('message_sha256') != hashlib.sha256(text.encode()).hexdigest()
                    or recipient.get('message_index') != own['message_index'] + 1
                    or recipient.get('history_preserved') is not True
                    or recipient.get('all_packet_content_delivered') is not True or recipient.get('role') != 'user'):
                raise ValueError('Packet delivery receipt is incomplete')
            _first_input(phase, a, checkpoint, own, text, cfg['offline_selftest'], status['status'] == 'completed')
        if status['status'] == 'completed':
            outcomes, final = read(phase / 'agent_outcomes.json'), read(phase / 'final_delivery.json')
            if (outcomes != status.get('outcomes') or set(outcomes) != set(ACTIVE)
                    or any(v.get('infrastructure_error') for v in outcomes.values())
                    or delivery.get('first_request_verified') is not True):
                raise ValueError('Accepted execution has an unverified or interrupted active loop')
            if final != final_delivery_record(phase, final.get('publications', {}), condition):
                raise ValueError('Authoritative published delivery contract changed')
            patches = {a + '.patch': file_hash(phase / (a + '.patch')) for a in ACTIVE}
            if (status.get('final_patch_sha256') != patches['agent1.patch']
                    or status.get('peer_patch_sha256') != patches['agent2.patch']
                    or status.get('delivery_selection') != 'predesignated_agent1'):
                raise ValueError('Accepted status patch differs from published artifact')
            for a, pub in final['publications'].items():
                nonempty = bool((phase / (a + '.patch')).read_bytes().strip())
                expected = 'published_pr' if pub.get('pr_tag_sha') else 'published_branch_fallback' if nonempty else 'no_published_changes'
                if (pub.get('agent_id') != a or pub.get('strategy') != expected or pub.get('patch_nonempty') != nonempty
                        or pub.get('published_pr') != bool(pub.get('pr_tag_sha'))
                        or pub.get('unsubmitted_worktree_used') is not False or nonempty and not pub.get('branch_sha')):
                    raise ValueError('Native publication provenance differs')
            if not cfg['offline_selftest'] and any(not any(r['phase'] == condition + ':' + a for r in accepted) for a in ACTIVE):
                raise ValueError('An active agent has no provider request')
    return {'schema': 'early_role_execution_receipt_v1', 'task': task.as_dict(), 'condition': condition,
        'condition_version': CONDITION_VERSIONS[condition], 'status': status['status'],
        'checkpoint_sha256': digest(checkpoint), 'handoff_packet_sha256': digest(frozen),
        'preparation_sha256': cfg['preparation_sha256'], 'active_agents': list(ACTIVE),
        'authoritative_agent': 'agent1', 'submission_policy': 'authoritative_agent1_only',
        'patch_sha256': patches, 'protected_files_sha256': _protected(run, phase, mandatory)}


def freeze_execution_receipt(phase_dir, task):
    phase = Path(phase_dir)
    receipt = _receipt(phase, task)
    path = phase / 'execution_receipt.json'
    if path.exists() or path.is_symlink():
        return _immutable(receipt, phase.parent, path)
    # The producer may create once only; subsequent validation is read-only.
    import json
    with path.open('x') as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    return receipt


def verify_worker_artifacts(phase_dir, task):
    phase = Path(phase_dir)
    return _immutable(_receipt(phase, task), phase.parent, phase / 'execution_receipt.json')


def guard_summary(phase):
    status = read(Path(phase) / 'status.json')
    if status.get('no_execution'):
        return {'invoked': False, 'reason': 'zero_execution_allowance', 'counts': {}, 'events': []}
    value = read(Path(phase) / 'publication_guard.json')
    return {k: value[k] for k in ('schema', 'guard_version', 'context_closed', 'counts', 'events')}
