"""Regression checks for frozen decisions, task allowance and terminal reuse."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import run_luna_selector as s
from budget_guard import write_json


class SelectorSupervisorTests(unittest.TestCase):
    def test_exact_fixed_schedule(self):
        jobs = s.schedule(False)
        self.assertEqual(len(jobs), 18)
        self.assertEqual(len({(j['task'], j['repeat'], j['condition']) for j in jobs}), 18)
        for key in s.TASKS:
            actual = [j['condition'] for j in jobs if j['task'] == key]
            self.assertEqual(actual, ['strongshare', 'integrator', 'solo', 'solo', 'integrator', 'strongshare'])
        self.assertEqual(len(s.schedule(True)), 9)
        self.assertNotIn('outlines1371_f1_f2', {j['task'] for j in jobs})
        self.assertEqual(len(s.ALL_TASKS), 4)

    def test_common_allowance_deducts_prefix_and_preparation(self):
        self.assertEqual(s.remaining_seconds({'elapsed_seconds': 111.5}, {'preparation': {'elapsed_seconds': 8.25}}), 1680.25)
        self.assertLess(s.remaining_seconds({'elapsed_seconds': 1799}, {'preparation': {'elapsed_seconds': 2}}), 0)

    def test_fresh_solo_rejects_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            task = next(iter(s.TASKS.values()))
            with self.assertRaises(ValueError):
                s.configure_run(Path(d), task, 'solo', {'offline_selftest': True, 'model': 'offline-scripted-model'}, 1800, 'bad')

    def test_configuration_is_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            task = next(iter(s.TASKS.values()))
            protocol = {'offline_selftest': True, 'model': 'offline-scripted-model'}
            config = s.configure_run(Path(d), task, 'integrator', protocol, 1600, 'abc', 1)
            self.assertEqual(config['submission_policy'], 'authoritative_agent1_only')
            with self.assertRaises(RuntimeError):
                s.configure_run(Path(d), task, 'integrator', protocol, 1599, 'abc', 1)

    def test_job_paths_keep_solo_isolated(self):
        key = next(iter(s.TASKS))
        base = Path('/tmp/example')
        self.assertIn('solo_jobs', s.job_path(base, {'task': key, 'repeat': 1, 'condition': 'solo'}).parts)
        for field, value in [('repeat', True), ('repeat', 3), ('task', 'unknown'), ('condition', 'oracle')]:
            job = {'task': key, 'repeat': 1, 'condition': 'strongshare', field: value}
            with self.assertRaises(ValueError):
                s.job_path(base, job)

    def test_completed_execution_reused_without_new_model(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run/'integrator').mkdir()
            write_json(run/'integrator/status.json', {'status': 'completed'})
            with patch.object(s, 'verify_execution') as verify, patch.object(s, 'cleanup_phase'), patch.object(s, 'run_command') as call:
                result = s.model_phase(run, next(iter(s.TASKS.values())), 'integrator', {'offline_selftest': False}, run, 0)
                self.assertEqual(result['status'], 'completed')
                call.assert_not_called()
                verify.assert_called_once()

    def test_no_decision_creation_after_continuation_directory(self):
        with tempfile.TemporaryDirectory() as d:
            suite = Path(d)
            task = next(iter(s.TASKS.values()))
            (suite/task.key/'jobs/already_started').mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                s.prepare_decision(suite, task, {'checkpoint_sha256': 'x'})

    def test_preflight_rejects_old_source_proof(self):
        proof = {'passed': True, 'model_api_calls': 0, 'prefixes_completed': 3, 'completed_jobs': 9, 'source_manifest': {'code': 'old'}}
        with patch.object(s, 'read_json', return_value=proof), patch.object(s, 'source_manifest', return_value={'code': 'new'}):
            with self.assertRaises(RuntimeError):
                s.verify_preflight()


if __name__ == '__main__':
    unittest.main()
