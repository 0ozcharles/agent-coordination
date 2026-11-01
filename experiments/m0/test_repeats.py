"""No-provider, no-Docker tests for fixed-checkpoint repeat orchestration."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_repeats as runner


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class RepeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "artifacts").mkdir()
        (self.root / "supplemental_probe_v1.py").write_text("# Frozen synthetic supplemental suite\n")
        self.suite = self.root / "artifacts/repeat_runs/repeat_smoke_unit"
        self.suite.mkdir(parents=True)
        self.provenance = {
            "prefix_usage": {"requests": 2, "known_cost_usd": 0.02, "unknown_usage_requests": 0},
            "prefix_elapsed_seconds": 5,
        }
        patches = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "source_manifest", return_value={"source.py": "frozen-hash"}),
            mock.patch.object(runner, "prepare_checkpoint", return_value=({}, self.provenance)),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": "unit-test-placeholder-not-a-real-key"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        command_patch = mock.patch.object(runner, "run_command", side_effect=self.successful_command)
        self.command = command_patch.start()
        self.addCleanup(command_patch.stop)
        self.protocol = runner.protocol_for(self.suite)
        self.job = self.protocol["jobs"][0]
        self.stages = []

    def append_usage(self, job, cost, status="completed"):
        path = Path(job["run_directory"]) / "usage_ledger.json"
        ledger = json.loads(path.read_text()) if path.exists() else {"requests": []}
        ledger["requests"].append({
            "phase": job["condition"] + ":agent1", "request_number": len(ledger["requests"]) + 1,
            "status": status, "actual_cost_usd": cost, "reserved_usd": 0.05,
            "prompt_tokens": 100 if cost is not None else 0,
            "completion_tokens": 20 if cost is not None else 0,
        })
        write(path, ledger)

    def completed_execution(self, job):
        phase = Path(job["condition_directory"])
        phase.mkdir(parents=True, exist_ok=True)
        condition = job["condition"]
        names = ["final.patch"] if condition == "consolidate" else ["agent1.patch", "agent6.patch"]
        for name in names:
            (phase / name).write_text("synthetic submission for " + job["job_id"] + ":" + name)
        status = {"status": "completed", "elapsed_seconds": 10, "outcome": "deadline"}
        if condition == "consolidate":
            status["final_patch_sha256"] = hashlib.sha256((phase / "final.patch").read_bytes()).hexdigest()
        write(phase / "status.json", status)
        return status

    def completed_official(self, phase, success=False):
        result = {"evaluation_completed": True, "both_passed": success, "elapsed_seconds": 2}
        write(phase / "evaluation_summary.json", result)
        return result

    def completed_supplemental(self, phase, success=False):
        result = {"supplemental_evaluation_completed": True, "supplemental_passed": success,
                  "status": "passed" if success else "failed", "elapsed_seconds": 1,
                  "suite_sha256": self.protocol["scoring"]["supplemental_suite_sha256"]}
        write(phase / "supplemental_summary.json", result)
        return result

    def command_stage(self, command):
        if "--worker" in command:
            run_dir = command[command.index("--run-dir") + 1]
            job = next(job for job in self.protocol["jobs"] if job["run_directory"] == run_dir)
            return "worker", job, Path(job["condition_directory"])
        option = "--condition-dir" if "--condition-dir" in command else "--run-dir"
        phase = Path(command[command.index(option) + 1])
        job = next(job for job in self.protocol["jobs"] if Path(job["condition_directory"]) == phase)
        stage = "supplemental" if "evaluate_supplemental.py" in str(command[1]) else "official"
        return stage, job, phase

    def successful_command(self, command, *args, **kwargs):
        stage, job, phase = self.command_stage(command)
        self.stages.append((job["job_id"], stage))
        if stage == "worker":
            self.completed_execution(job)
            self.append_usage(job, 0.1)
        elif stage == "official":
            self.completed_official(phase)
        elif stage == "supplemental":
            self.completed_supplemental(phase)
        return 0

    def run_one(self, job=None):
        return runner.run_job(job or self.job, self.suite, self.protocol, lock_fd=-1)

    def test_predeclared_six_jobs_have_fixed_order_independent_dirs_and_common_checkpoint(self):
        jobs = self.protocol["jobs"]
        self.assertEqual([(j["repeat"], j["condition"]) for j in jobs], [
            (2, "strongshare"), (2, "consolidate"), (2, "keep"),
            (3, "consolidate"), (3, "keep"), (3, "strongshare"),
        ])
        self.assertEqual([j["position"] for j in jobs], [1, 2, 3, 1, 2, 3])
        self.assertEqual(len({j["run_directory"] for j in jobs}), 6)
        self.assertEqual(len({j["condition_directory"] for j in jobs}), 6)
        self.assertEqual(self.protocol["checkpoint_sha256"], runner.SOURCE_CHECKPOINT)
        self.assertFalse(self.protocol["scoring"]["supplemental_feedback_to_agents"])
        self.assertFalse(self.protocol["accounting"]["prefix_reexecuted_or_rebilled"])
        for job in jobs:
            self.assertNotEqual(Path(job["run_directory"]), runner.SOURCE_RUN)

    def test_completed_task_failure_is_kept_and_no_stage_repeats_on_resume(self):
        first = self.run_one()
        self.assertEqual([stage for _, stage in self.stages], ["worker", "official", "supplemental"])
        self.assertFalse(first["official_success"])
        self.assertFalse(first["enhanced_success"])
        self.command.reset_mock()
        self.command.side_effect = AssertionError("No completed stage should repeat")
        with mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": ""}):
            resumed = self.run_one()
        self.assertEqual(resumed, first)
        self.command.assert_not_called()
        self.assertEqual(first["new_actual_usage"]["requests"], 1)

    def test_official_infrastructure_failure_resumes_official_then_supplemental_only(self):
        def fail_official(command, *args, **kwargs):
            if self.command_stage(command)[0] == "official":
                return 2
            return self.successful_command(command, *args, **kwargs)
        self.command.side_effect = fail_official
        with self.assertRaisesRegex(RuntimeError, "official evaluator paused"):
            self.run_one()
        self.stages.clear()
        self.command.side_effect = self.successful_command
        with mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": ""}):
            row = self.run_one()
        self.assertEqual([stage for _, stage in self.stages], ["official", "supplemental"])
        self.assertEqual(row["new_actual_usage"]["requests"], 1)

    def test_supplemental_infrastructure_failure_resumes_only_supplemental(self):
        def fail_supplemental(command, *args, **kwargs):
            if self.command_stage(command)[0] == "supplemental":
                return 2
            return self.successful_command(command, *args, **kwargs)
        self.command.side_effect = fail_supplemental
        with self.assertRaisesRegex(RuntimeError, "supplemental evaluator paused"):
            self.run_one()
        self.stages.clear()
        self.command.side_effect = self.successful_command
        row = self.run_one()
        self.assertEqual([stage for _, stage in self.stages], ["supplemental"])
        self.assertEqual(row["new_actual_usage"]["requests"], 1)

    def test_batch_runs_all_six_completed_task_failures_once(self):
        self.assertEqual(runner.supervise(self.suite), 0)
        self.assertEqual(len(self.stages), 18)
        self.assertEqual([job for job, stage in self.stages if stage == "worker"],
                         [job["job_id"] for job in self.protocol["jobs"]])
        summary = json.loads((self.suite / "repeat_summary.json").read_text())
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(summary["new_actual_collection_usage"]["requests"], 6)
        self.assertEqual(summary["new_actual_collection_usage"]["known_cost_usd"], 0.6)
        self.assertEqual(sum(row["official_success"] for row in summary["rows"]), 0)
        self.assertTrue(all(row["full_task_equivalent_known_cost_usd"] == 0.12 for row in summary["rows"]))
        self.assertFalse(summary["exploratory_first_run"]["pooled_into_new_repeat_statistics"])

    def test_batch_pauses_on_infrastructure_error_keeps_cost_and_archives_only_failed_attempt(self):
        def failed_worker(command, *args, **kwargs):
            stage, job, phase = self.command_stage(command)
            self.assertEqual(stage, "worker")
            self.stages.append((job["job_id"], stage))
            self.append_usage(job, 0.4)
            self.append_usage(job, None, status="in_flight")
            write(phase / "status.json", {"status": "error", "infrastructure_error": True})
            return 2
        self.command.side_effect = failed_worker
        self.assertEqual(runner.supervise(self.suite), 2)
        self.assertEqual(len(self.stages), 1)
        summary = json.loads((self.suite / "repeat_summary.json").read_text())
        self.assertFalse(summary["pipeline_completed"])
        self.assertEqual(summary["rows"], [])
        self.assertEqual(summary["new_actual_collection_usage"]["known_cost_usd"], 0.4)
        self.assertEqual(summary["new_actual_collection_usage"]["unknown_usage_requests"], 1)

        self.stages.clear()
        self.command.side_effect = self.successful_command
        self.assertEqual(runner.supervise(self.suite), 0)
        summary = json.loads((self.suite / "repeat_summary.json").read_text())
        self.assertEqual(summary["new_actual_collection_usage"]["known_cost_usd"], 1.0)
        self.assertEqual(summary["new_actual_collection_usage"]["unknown_usage_requests"], 1)
        first = summary["rows"][0]
        self.assertEqual(first["continuation_usage"]["known_cost_usd"], 0.1)
        self.assertEqual(first["excluded_infrastructure_usage"]["known_cost_usd"], 0.4)
        self.assertEqual(first["excluded_infrastructure_usage"]["unknown_usage_requests"], 1)
        self.assertEqual(first["full_task_equivalent_known_cost_usd"], 0.12)
        archived = list((Path(self.job["run_directory"]) / "failed_attempts").iterdir())
        self.assertEqual(len(archived), 1)
        ledger = json.loads((Path(self.job["run_directory"]) / "usage_ledger.json").read_text())
        self.assertEqual(ledger["requests"][1]["error_type"], "InterruptedAttemptUsageUnknown")
        self.assertIsNone(ledger["requests"][1]["actual_cost_usd"])

    def test_checkpoint_rejection_prevents_any_worker_or_evaluator(self):
        with mock.patch.object(runner, "prepare_checkpoint", side_effect=RuntimeError("Changed checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "Changed checkpoint"):
                self.run_one()
        self.command.assert_not_called()

    def test_completed_patch_receipt_and_first_consolidate_hash_detect_changes(self):
        self.run_one()
        (Path(self.job["condition_directory"]) / "agent1.patch").write_text("tampered")
        self.command.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "submission changed|Submission changed"):
            self.run_one()
        self.command.assert_not_called()

        consolidate = self.protocol["jobs"][1]
        self.completed_execution(consolidate)
        phase = Path(consolidate["condition_directory"])
        (phase / "final.patch").write_text("tampered before receipt")
        with self.assertRaisesRegex(RuntimeError, "changed|mismatch|differs"):
            self.run_one(consolidate)
        self.assertFalse((phase / "execution_receipt.json").exists())
        self.command.assert_not_called()

    def test_changed_job_config_or_supplemental_suite_cannot_silently_resume(self):
        self.run_one()
        config_path = Path(self.job["run_directory"]) / "repeat_job_config.json"
        original = json.loads(config_path.read_text())
        changed = copy.deepcopy(original)
        changed["model"] = "different-model"
        write(config_path, changed)
        self.command.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "configuration differs"):
            self.run_one()
        write(config_path, original)
        supplemental_path = Path(self.job["condition_directory"]) / "supplemental_summary.json"
        supplemental = json.loads(supplemental_path.read_text())
        supplemental["suite_sha256"] = "different-suite"
        write(supplemental_path, supplemental)
        with self.assertRaisesRegex(RuntimeError, "different frozen suite"):
            self.run_one()
        self.command.assert_not_called()

    def test_changed_frozen_protocol_is_rejected_before_starting_any_job(self):
        changed = copy.deepcopy(self.protocol)
        changed["temperature"] = 1.0
        write(self.suite / "protocol.json", changed)
        with self.assertRaisesRegex(RuntimeError, "Frozen protocol/source differs"):
            runner.supervise(self.suite)
        self.command.assert_not_called()

    def test_source_change_between_jobs_pauses_batch_without_spending_on_next_job(self):
        original = self.protocol["source_manifest"]
        changed = {"source.py": "changed-hash"}
        with mock.patch.object(runner, "source_manifest", side_effect=[original, original, original, changed]):
            self.assertEqual(runner.supervise(self.suite), 2)
        self.assertEqual(self.stages, [(self.job["job_id"], stage)
                                      for stage in ("worker", "official", "supplemental")])
        summary = json.loads((self.suite / "repeat_summary.json").read_text())
        self.assertEqual(len(summary["rows"]), 1)
        self.assertEqual(summary["new_actual_collection_usage"]["known_cost_usd"], 0.1)
        self.assertFalse(summary["pipeline_completed"])

    def test_worker_exit_zero_without_completed_status_does_not_start_scoring(self):
        def incomplete_worker(command, *args, **kwargs):
            stage, job, phase = self.command_stage(command)
            self.assertEqual(stage, "worker")
            self.stages.append((job["job_id"], stage))
            write(phase / "status.json", {"status": "error", "infrastructure_error": True})
            return 0
        self.command.side_effect = incomplete_worker
        with self.assertRaisesRegex(RuntimeError, "model execution paused"):
            self.run_one()
        self.assertEqual(self.stages, [(self.job["job_id"], "worker")])

    def test_known_and_unknown_counterfactual_costs_do_not_enter_new_actual_spend(self):
        self.append_usage(self.job, 0.2)
        self.append_usage(self.job, None, status="in_flight")
        provenance = copy.deepcopy(self.provenance)
        provenance["prefix_usage"]["unknown_usage_requests"] = 2
        row = runner.build_row(self.job, provenance, {"elapsed_seconds": 10},
                               {"both_passed": True}, {"supplemental_passed": False})
        self.assertEqual(row["new_actual_usage"]["known_cost_usd"], 0.2)
        self.assertEqual(row["new_actual_usage"]["unknown_usage_requests"], 1)
        self.assertEqual(row["full_task_equivalent_known_cost_usd"], 0.22)
        self.assertEqual(row["full_task_equivalent_unknown_usage_requests"], 3)
        self.assertTrue(row["official_success"])
        self.assertFalse(row["enhanced_success"])
        # Include costs from a job with no completed result in batch totals.
        self.append_usage(self.protocol["jobs"][1], 0.3)
        summary = runner.batch_summary(self.protocol, [row])
        self.assertEqual(summary["new_actual_collection_usage"]["known_cost_usd"], 0.5)
        self.assertEqual(summary["new_actual_collection_usage"]["unknown_usage_requests"], 1)
        self.assertEqual(summary["conditions"]["strongshare"]["full_task_equivalent_known_costs_usd"], [0.22])
        self.assertFalse(summary["pipeline_completed"])


if __name__ == "__main__":
    unittest.main()
