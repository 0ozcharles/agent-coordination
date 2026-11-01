"""Minimal supervisor regression checks; no containers or provider requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_solo_smoke import configure
configure()
import run_luna_early_roles as r


class EarlyRolesProtocolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.suite = Path(tmp.name)
        self.task = next(iter(r.TASKS.values()))
        self.reference = {'directory': '/unused/prefix', 'record': {
            'elapsed_seconds': 96.202, 'checkpoint_sha256': 'a' * 64,
            'all_attempt_usage': {'requests': 10, 'known_cost_usd': 2,
                'unknown_usage_requests': 0, 'prompt_tokens': 10, 'completion_tokens': 10}}}
        self.preparation = {'directory': '/unused/preparation', 'preparation_sha256': 'b' * 64,
            'record': {'elapsed_seconds': 3.5, 'packet_sha256': 'c' * 64}}
        self.protocol = {'early_references': {self.task.key: self.reference},
            'model': r.MODEL, 'submission_policy': 'authoritative_agent1_only',
            'terminal_policy': 'both_active', 'offline_selftest': False,
            'publication_guard_version': 'publication_parity_v1',
            'source_manifest': {}, 'interpretation': 'fixture'}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_fixed_schedule_common_budget_and_immutable_configuration(self):
        jobs = r.schedule()
        self.assertEqual(len(jobs), 8)
        self.assertEqual(len({(j['task'], j['repeat'], j['condition']) for j in jobs}), 8)
        self.assertEqual([j['condition'] for j in r.schedule(True)], ['strongshare', 'integrator'])
        self.protocol['jobs'] = jobs
        mine = [j for j in jobs if j['task'] == self.task.key]
        with patch.object(r, 'copy_inputs'):
            configs = [r.configure_job(self.suite, j, self.protocol, self.preparation) for j in mine]
            self.assertEqual({c['execution_seconds'] for c in configs}, {1700.298})
            self.assertEqual({c['checkpoint_packet_sha256'] for c in configs}, {'c' * 64})
            self.assertTrue(all(c['initialization_included_in_budget'] for c in configs))
            changed = {**self.preparation, 'record': {**self.preparation['record'], 'elapsed_seconds': 4}}
            with self.assertRaises(RuntimeError):
                r.configure_job(self.suite, mine[0], self.protocol, changed)

    def test_completed_execution_is_verified_without_restart_even_if_verification_fails(self):
        job = {'task': self.task.key, 'repeat': 1, 'condition': 'strongshare'}
        run = r.job_path(self.suite, job)
        saved = {'status': 'completed', 'elapsed_seconds': 5}
        self.write(run / 'strongshare/status.json', saved)
        with patch('early_role_runtime.verify_worker_artifacts') as verify, \
             patch.object(r, 'run_command') as command, \
             patch.object(r, 'archive_incomplete') as archive, \
             patch.object(r, 'cleanup_phase'):
            self.assertEqual(r.model_phase(run, self.task, 'strongshare', self.protocol, self.suite, 0), saved)
            verify.assert_called_once_with(run / 'strongshare', self.task)
            verify.side_effect = RuntimeError('receipt changed')
            with self.assertRaisesRegex(RuntimeError, 'receipt changed'):
                r.model_phase(run, self.task, 'strongshare', self.protocol, self.suite, 0)
            command.assert_not_called()
            archive.assert_not_called()

    def test_physical_bill_includes_interrupted_unknown_once_and_history_only_in_comparison(self):
        job = {'task': self.task.key, 'repeat': 1, 'condition': 'strongshare'}
        run = r.job_path(self.suite, job)
        requests = [
            {'phase': 'strongshare:agent1', 'actual_cost_usd': .2},
            {'phase': 'strongshare:agent2', 'actual_cost_usd': .1, 'excluded_from_baseline': True},
            {'phase': 'strongshare:agent2', 'actual_cost_usd': None, 'excluded_from_baseline': True}]
        self.write(run / 'usage_ledger.json', {'requests': requests})
        self.write(run / 'failed_attempts/strongshare_001/status.json', {'elapsed_seconds': 7})
        self.write(run / 'failed_attempts/strongshare_001/usage_ledger.json', {'requests': requests})
        self.write(run / 'task_config.json', {'execution_seconds': 1700.298})
        self.protocol['jobs'] = [job]
        row = r.build_row(run, job, {'status': 'completed', 'elapsed_seconds': 5},
                          {'both_passed': False}, self.protocol, self.preparation)
        self.assertFalse(row['official_success'])
        self.assertTrue(row['scored'])
        self.assertEqual(row['accepted_usage']['requests'], 1)
        self.assertEqual(row['all_attempt_usage']['requests'], 3)
        self.assertEqual(row['full_task_unknown_usage_requests'], 1)
        self.assertAlmostEqual(row['full_task_known_cost_usd'], 2.3)
        self.assertAlmostEqual(row['full_task_execution_seconds'], 111.702)
        summary = r.summary_for(self.suite, self.protocol, {self.task.key: self.preparation}, [row])
        self.assertTrue(summary['pipeline_completed'])
        self.assertEqual(summary['actual_new_usage']['requests'], 3)
        self.assertEqual(summary['actual_new_usage']['unknown_usage_requests'], 1)
        self.assertAlmostEqual(summary['actual_new_usage']['known_cost_usd'], .3)
        self.assertFalse(summary['historical_prefix_rebilled'])

    def test_prepared_packet_reused_and_tampered_text_rejected(self):
        folder = self.suite / self.task.key / 'preparation'
        packet = {'task_key': self.task.key, 'checkpoint_sha256': 'a' * 64}
        self.write(folder / 'packet.json', packet)
        (folder / 'packet.txt').write_text('complete packet')
        record = {'task': self.task.key, 'checkpoint_sha256': 'a' * 64,
            'packet_sha256': r.digest(packet), 'packet_text_sha256': r.file_hash(folder / 'packet.txt'),
            'elapsed_seconds': 3.5, 'model_api_calls': 0, 'before_continuation_outcomes': True}
        self.write(folder / 'preparation.json', record)
        with patch('cross_task_packet.packet_message', return_value='complete packet'), \
             patch.object(r, 'collect_packet') as collect:
            first = r.prepare_packet(self.suite, self.task, self.protocol)
            self.assertEqual(first, r.prepare_packet(self.suite, self.task, self.protocol))
            (folder / 'packet.txt').write_text('changed packet')
            with self.assertRaisesRegex(RuntimeError, 'preparation changed'):
                r.prepare_packet(self.suite, self.task, self.protocol)
            collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
