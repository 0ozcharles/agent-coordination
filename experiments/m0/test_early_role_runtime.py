"""Focused early-role protocol tests: no Docker and no provider requests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import early_role_runtime as r


class EarlyRoleRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run = Path(temp.name)
        self.task = SimpleNamespace(key='fixture', features=(4, 10), base='a' * 40,
            as_dict=lambda: {'key': 'fixture', 'base': 'a' * 40})

    def write(self, rel, value):
        path = self.run / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_only_role_clause_differs_and_common_delivery_is_same(self):
        common = r.common_delivery_message(self.task, 120)
        values = [r.organization_message(self.task, c, 120) for c in r.CONDITIONS]
        for c, value in zip(r.CONDITIONS, values):
            self.assertEqual(value, common + r.role_clause(c))
            self.assertIn("Only agent1's published complete patch", value)
            self.assertIn('Neither agent is retired', value)
            self.assertNotIn('current conflict', value)
            self.assertNotIn('CURRENT-CONFLICT', value)
        self.assertIn('yourselves', values[0])
        self.assertIn('Agent1 is explicitly responsible', values[1])

    def test_full_native_state_and_history_preserved(self):
        class Agent:
            def __init__(self):
                self.messages = [{'role': 'system', 'content': 'original'}, {'role': 'user', 'content': 'feature'}]
                self.model = SimpleNamespace(format_message=lambda **kw: kw)
                self.n_calls = 7
                self._segments = [{'messages': ['historical compaction metadata']}]
            def add_messages(self, *messages): self.messages.extend(messages)
        agents = {a: Agent() for a in r.ACTIVE}
        cp = {'agents': {a: {'state': r.save_state(agent)} for a, agent in agents.items()}}
        before = copy.deepcopy(cp)
        result = r.apply_assignment(agents, cp, self.task, 'strongshare', 90)
        self.assertEqual(cp, before)
        self.assertEqual(agents['agent1'].messages[-1], agents['agent2'].messages[-1])
        for a in r.ACTIVE:
            self.assertEqual(agents[a].n_calls, 7)
            self.assertEqual(agents[a].messages[:-1], cp['agents'][a]['state']['messages'])
            self.assertEqual(result['recipients'][a]['before_state_sha256'], r.digest(cp['agents'][a]['state']))

    def test_both_conditions_select_same_authoritative_publication(self):
        for a in r.ACTIVE: (self.run / f'{a}.patch').write_text(a)
        pubs = {a: {'patch_sha256': r.file_hash(self.run / f'{a}.patch')} for a in r.ACTIVE}
        for c in r.CONDITIONS:
            value = r.final_delivery_record(self.run, pubs, c)
            self.assertEqual(value['schema'], 'early_role_authoritative_delivery_v1')
            self.assertEqual(value['condition'], c)
            self.assertEqual(value['scored_patch'], 'agent1.patch')
            self.assertEqual(value['selection'], 'predesignated_agent1')
            self.assertFalse(value['automatic_merge'])
            self.assertFalse(value['posthoc_selection'])

    def test_zero_allowance_calls_no_initialization_guard_or_model(self):
        with patch.object(r, '_config', return_value={}), \
             patch.object(r, '_checkpoint_and_packet', return_value=({}, {}, '', {})), \
             patch.object(r, 'configure', side_effect=AssertionError('No native initialization allowed')), \
             patch.object(r, 'freeze_execution_receipt') as freeze:
            self.assertEqual(r.phase_worker(self.run, 'strongshare', self.task, True, 0), 0)
        value = r.read(self.run / 'strongshare/status.json')
        self.assertEqual(value['status'], 'time_budget_exhausted_before_first_query')
        self.assertTrue(value['no_execution'])
        self.assertEqual(r.read(self.run / 'usage_ledger.json')['requests'], [])
        self.assertFalse((self.run / 'strongshare/publication_guard.json').exists())
        freeze.assert_called_once()

    def test_terminal_reuse_and_config_mismatch(self):
        cfg = {'experiment_version': r.VERSION, 'task_key': self.task.key, 'task': self.task.as_dict(),
            'condition': 'integrator', 'condition_version': r.CONDITION_VERSIONS['integrator'],
            'offline_selftest': True, 'execution_seconds': 0, 'terminal_policy': 'both_active',
            'submission_policy': 'authoritative_agent1_only', 'model': 'offline-scripted-model'}
        self.write('task_config.json', cfg)
        with patch.object(r, 'check_task'):
            self.assertEqual(r._config(self.run, 'integrator', self.task, True, 0), cfg)
            self.write('integrator/status.json', {'status': 'completed'})
            with self.assertRaises(ValueError): r._config(self.run, 'integrator', self.task, True, 0)
            self.assertEqual(r._config(self.run, 'integrator', self.task, True, 0, terminal=True), cfg)
            with self.assertRaises(ValueError): r._config(self.run, 'strongshare', self.task, True, 0, terminal=True)

    def first_input_fixture(self, offline):
        phase = self.run / 'strongshare'; phase.mkdir(exist_ok=True)
        history = [{'role': 'system', 'content': 'native'}, {'role': 'user', 'content': 'feature'}]
        role = {'role': 'user', 'content': 'organization', 'message_index': 2,
                'message_sha256': hashlib.sha256(b'organization').hexdigest()}
        inputs = history + [{'role': 'user', 'content': 'organization'}, {'role': 'user', 'content': 'fullpacket'}]
        trajectory = inputs + [{'role': 'assistant', 'content': 'done', 'tool_calls': [
            {'id': 'finish', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'finish', 'content': 'submitted'},
            {'role': 'exit', 'extra': {'exit_status': 'Submitted'}, 'content': ''}]
        self.write('strongshare/agent1.traj.json', {'messages': trajectory})
        self.write('strongshare/first_request_agent1.json', {'verified': True, 'recipient': 'agent1',
            'organization_message_sha256': role['message_sha256'],
            'message_sha256': hashlib.sha256(b'fullpacket').hexdigest(), 'full_packet_present': True,
            'inherited_history_verified': True, 'actual_transport_input_verified': not offline})
        self.write('strongshare/first_model_input_agent1.json', {'recipient': 'agent1', 'message_count': 4,
            'contains_raw_messages': False, 'messages_sha256': r.digest(inputs)})
        self.write('strongshare/responses/agent1/request_1_input.json', {'request': {'messages': inputs}})
        cp = {'agents': {'agent1': {'state': {'messages': history}}}}
        return phase, cp, role, inputs

    def test_final_native_exit_filtered_but_provider_input_exact(self):
        phase, cp, role, inputs = self.first_input_fixture(False)
        before = (phase / 'agent1.traj.json').read_bytes()
        r._first_input(phase, 'agent1', cp, role, 'fullpacket', False, True)
        self.assertEqual((phase / 'agent1.traj.json').read_bytes(), before)
        inputs.append({'role': 'exit', 'content': 'not a provider role'})
        self.write('strongshare/responses/agent1/request_1_input.json', {'request': {'messages': inputs}})
        with self.assertRaises(ValueError): r._first_input(phase, 'agent1', cp, role, 'fullpacket', False, True)

    def test_first_input_fingerprint_and_original_history_tampering_rejected(self):
        phase, cp, role, _ = self.first_input_fixture(True)
        self.write('strongshare/first_model_input_agent1.json', {'recipient': 'agent1', 'message_count': 4,
            'contains_raw_messages': False, 'messages_sha256': '0' * 64})
        with self.assertRaises(ValueError): r._first_input(phase, 'agent1', cp, role, 'fullpacket', True, True)

    def test_pending_tool_pair_not_silently_normalized(self):
        agent = SimpleNamespace(messages=[{'role': 'system', 'content': 'native'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function',
                'function': {'name': 'bash', 'arguments': '{}'}}]}])
        with self.assertRaises(ValueError): r._append(agent, 'organization')


if __name__ == '__main__': unittest.main()
