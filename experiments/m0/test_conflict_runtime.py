"""Protocol boundary tests with local artifacts only; no Docker or provider calls."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import conflict_runtime as runtime
import conflict_packet as packet
from run_baselines import digest


class ConflictRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.task = SimpleNamespace(key='fixture', features=(1, 2), base='a' * 40,
            as_dict=lambda: {'key': 'fixture', 'base': 'a' * 40})

    def write(self, rel, value):
        path = self.run / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_same_common_delivery_contract_both_live(self):
        text = runtime.common_delivery_message(self.task)
        self.assertIn("only agent1's published complete patch", text)
        self.assertIn('neither agent is retired', text)
        self.assertNotIn('agent1 is now explicitly', text.lower())
        ss = runtime.organization_message(self.task, 'strongshare', 100)
        integrator = runtime.organization_message(self.task, 'integrator', 100)
        self.assertIn('choose the resolution', ss)
        self.assertIn('Agent1 is now explicitly assigned responsibility', integrator)
        self.assertNotIn('Decide your collaboration', integrator)
        for text in (ss, integrator):
            self.assertIn("only agent1's published complete patch", text)
            self.assertIn('not automatically merge', text)

    def test_assignment_preserves_original_state_and_same_text(self):
        class Agent:
            def __init__(self):
                self.messages = [{'role': 'system', 'content': 'native'}, {'role': 'user', 'content': 'task'}]
                self.model = SimpleNamespace(format_message=lambda **kwargs: kwargs)
            def add_messages(self, *messages): self.messages.extend(messages)
        agents = {a: Agent() for a in runtime.ACTIVE}
        cp = {'agents': {a: {'state': runtime.save_state(agent)} for a, agent in agents.items()}}
        old = copy.deepcopy(cp)
        value = runtime.assignment(agents, cp, self.task, 'strongshare', 60)
        self.assertEqual(cp, old)
        self.assertEqual(agents['agent1'].messages[-1], agents['agent2'].messages[-1])
        self.assertFalse(value['peer_retired'])
        for a in runtime.ACTIVE:
            self.assertEqual(agents[a].messages[:-1], cp['agents'][a]['state']['messages'])

    def test_terminal_native_marker_not_a_provider_role(self):
        original = [{'role': 'system', 'content': 'native'}, {'role': 'user', 'content': 'task'}]
        assignment = {'message_index': 2, 'content': 'organization',
                      'message_sha256': runtime.hashlib.sha256(b'organization').hexdigest()}
        messages = original + [{'role': 'user', 'content': 'organization'},
            {'role': 'user', 'content': 'full packet'},
            {'role': 'exit', 'content': '', 'extra': {'exit_status': 'Submitted'}}]
        before = copy.deepcopy(messages)
        runtime.native.verify_first_messages(runtime._provider_history(messages), 'full packet', assignment, original)
        self.assertEqual(messages, before)
        self.assertEqual(len(runtime._provider_history(messages)), 4)
        with self.assertRaises(ValueError):
            runtime.native.verify_first_messages(runtime._provider_history(messages + [{'role': 'invalid'}]),
                'full packet', assignment, original)

    def test_pending_tool_result_rejected(self):
        agent = SimpleNamespace(messages=[{'role': 'system', 'content': 'x'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function',
            'function': {'name': 'bash', 'arguments': '{}'}}]}])
        with self.assertRaises(ValueError):
            runtime._append(agent, 'contract')

    def test_publication_selection_identical_conditions(self):
        for a in runtime.ACTIVE:
            (self.run / (a + '.patch')).write_text(a + ' published patch')
        pubs = {a: {'patch_sha256': runtime.file_hash(self.run / (a + '.patch'))} for a in runtime.ACTIVE}
        ss = runtime.delivery_record(self.run, pubs, 'strongshare')
        integrator = runtime.delivery_record(self.run, pubs, 'integrator')
        for value in (ss, integrator):
            self.assertEqual(value['scored_patch'], 'agent1.patch')
            self.assertEqual(value['submission_policy'], 'authoritative_agent1_only')
            self.assertFalse(value['automatic_merge'])
            self.assertFalse(value['posthoc_selection'])
        self.assertEqual(ss['patch_sha256'], integrator['patch_sha256'])
        (self.run / 'agent1.patch').write_text('modified')
        with self.assertRaises(RuntimeError): runtime.delivery_record(self.run, pubs, 'strongshare')

    def test_protected_allowlist_does_not_expand_when_scored(self):
        phase = self.run / 'strongshare'; phase.mkdir()
        self.write('task_config.json', {})
        self.write('strongshare/status.json', {})
        self.write('strongshare/responses/agent1/request_1.json', {'x': 1})
        old = runtime._protected(self.run, phase, {'task_config.json'})
        for name in ('evaluation_console.log', 'evaluation_status.json', 'input_manifest.json', 'delivered.patch'):
            (phase / name).write_text('future scoring')
        self.write('strongshare/evaluation_attempts/id/results.json', {})
        self.assertEqual(old, runtime._protected(self.run, phase, {'task_config.json'}))
        self.write('strongshare/responses/agent1/request_1.json', {'x': 2})
        self.assertNotEqual(old, runtime._protected(self.run, phase, {'task_config.json'}))

    def test_continuation_zero_budget_never_starts_guard_or_container(self):
        cp = {'synthetic_conflict': True, 'task': self.task.as_dict()}
        value = {'checkpoint_sha256': digest(cp)}
        self.write('checkpoint_packet.json', value)
        (self.run / 'checkpoint_packet.txt').write_text('frozen packet')
        with patch.object(runtime, '_config'), patch.object(runtime, 'verify_checkpoint_files', return_value=cp), \
             patch.object(runtime.packet, 'packet_message', return_value='frozen packet'), \
             patch.object(runtime, 'configure', side_effect=AssertionError('No initialization permitted')), \
             patch.object(runtime, 'freeze_execution_receipt') as freeze:
            self.assertEqual(runtime.phase_worker(self.run, 'strongshare', self.task, True, 0), 0)
        status = runtime.read(self.run / 'strongshare/status.json')
        self.assertEqual(status['status'], 'time_budget_exhausted_before_first_query')
        self.assertTrue(status['no_execution'])
        self.assertEqual(runtime.read(self.run / 'usage_ledger.json')['requests'], [])
        freeze.assert_called_once()

    def test_real_worker_refuses_synthetic_checkpoint_before_setup(self):
        with patch.object(runtime, '_config'), patch.object(runtime, 'verify_checkpoint_files', return_value={'synthetic_conflict': True}), \
             patch.object(runtime, 'configure', side_effect=AssertionError('No setup permitted')):
            with self.assertRaises(ValueError): runtime.phase_worker(self.run, 'strongshare', self.task, False, 100)

    def test_config_zero_allowed_only_continuation(self):
        cfg = {'task': self.task.as_dict(), 'task_key': self.task.key, 'condition': 'strongshare',
            'offline_selftest': True, 'execution_seconds': 0, 'experiment_version': runtime.VERSION,
            'submission_policy': 'authoritative_agent1_only', 'terminal_policy': 'both_active',
            'condition_version': 'conflict_strongshare_v1', 'stage': 'conflict_continuation', 'model': 'offline-scripted-model'}
        self.write('task_config.json', cfg)
        with patch.object(runtime, '_task'):
            self.assertEqual(runtime._config(self.run, self.task, 'strongshare', True, 0), cfg)
            with self.assertRaises(ValueError): runtime._config(self.run, self.task, 'strongshare', True, 0, True)
            self.write('strongshare/status.json', {'status': 'completed'})
            with self.assertRaises(ValueError): runtime._config(self.run, self.task, 'strongshare', True, 0)

    def test_offline_commands_are_explicit_and_no_solutions(self):
        for a in runtime.ACTIVE:
            commands = runtime.offline_collection_commands(a)
            self.assertEqual(len(commands), 2)
            self.assertIn('Synthetic conflict fixture', commands[0])
            self.assertIn('git ls-files -u', commands[1])
            self.assertNotIn('tests.patch', '\n'.join(commands))
            later = runtime.offline_continuation_commands(self.task, a)
            self.assertIn('handoff_read_only_ok', later[0])
            self.assertIn('git push', later[1])
            self.assertEqual(later[-1], 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT')

    def test_packet_complete_size_failure_and_exact_source_hashes(self):
        changes = {a: [{'path': 'src/core.py', 'before': 'x = 1\n', 'after': 'x = 2\n'}] for a in runtime.ACTIVE}
        manifest = packet.source_manifest(changes)
        self.assertEqual(manifest['agent1'][0]['before_sha256'], packet.sha('x = 1\n'))
        value = {'schema': packet.SCHEMA, 'schema_version': 3, 'checkpoint_only': True,
                 'agents': {a: {'original_task': 'requirement'} for a in runtime.ACTIVE},
                 'current_conflicts': {a: {} for a in runtime.ACTIVE}}
        content = packet.packet_message(value)
        self.assertIn('snapshot DATA', content)
        value['agents']['agent1']['original_task'] = 'x' * packet.MAX_PACKET_BYTES
        with self.assertRaises(ValueError): packet.packet_message(value)


if __name__ == '__main__': unittest.main()
