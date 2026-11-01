"""Fresh-Solo orchestration tests using temporary files, with no API or Docker."""

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import evaluate_solo_expansion as evaluator
import run_solo_expansion as runner
import solo_expansion_worker as worker
import task_specs
from task_specs import get_task


class SoloExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "artifacts").mkdir()
        self.comparison = self.root / "artifacts/expansion_runs/completed_comparison"
        self.suite = self.root / "artifacts/solo_expansion_runs/solo_controls_unit"
        (self.root / "worker.py").write_text("# Frozen unit fixture\n")
        source_hash = hashlib.sha256((self.root / "worker.py").read_bytes()).hexdigest()
        self.manifest = {"worker.py": source_hash}
        self.events = []
        self.score_runs = 0
        patches = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "RUNS", self.root / "artifacts/solo_expansion_runs"),
            mock.patch.object(runner, "SOURCE_SUITE", self.comparison),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
            mock.patch.object(runner, "source_manifest", return_value=self.manifest),
            mock.patch.object(runner, "verify_references", return_value={"passed": True, "model_api_calls": 0}),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": "unit-placeholder-not-a-real-key"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.original_protocol = {"offline_selftest": False, "continuation_seconds": 1200,
                                  "tasks": {key: task.as_dict() for key, task in runner.TASKS.items()}}
        self.write(self.comparison / "protocol.json", self.original_protocol)
        self.write(self.comparison / "status.json", {"status": "completed", "completed_jobs": 12})
        self.prefix_elapsed = {"click2800_f1_f2": 52.160, "dirty_equals43_f3_f7": 38.524}
        for key, elapsed in self.prefix_elapsed.items():
            self.write(self.comparison / key / "prefix/prefix/status.json", {"status": "completed", "elapsed_seconds": elapsed})
            # Large historical expenses must not become new Solo expenses.
            self.add_usage(self.comparison / key / "prefix", 100, phase="prefix:agent1")
            task = get_task(key)
            for feature in task.features:
                path = task.task_dir / f"feature{feature}/feature.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{key}: requirement for feature {feature}")
        self.protocol = runner.protocol_for(False)
        patcher = mock.patch.object(runner, "run_command", side_effect=self.command)
        self.command_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_usage(self, run_dir, cost, status="completed", phase="solo:agent1"):
        path = run_dir / "usage_ledger.json"
        value = runner.read_json(path, {"requests": []})
        value["requests"].append({"phase": phase, "status": status, "actual_cost_usd": cost,
                                  "reserved_usd": 0.05, "prompt_tokens": 100 if cost is not None else 0,
                                  "completion_tokens": 10 if cost is not None else 0})
        self.write(path, value)

    def describe(self, command):
        path = Path(command[command.index("--run-dir") + 1])
        if "--worker" in command:
            return "worker", path, get_task(command[command.index("--task") + 1])
        config = runner.read_json(path.parent / "task_config.json")
        return "evaluation", path.parent, get_task(config["task_key"])

    def command(self, command, *args, **kwargs):
        stage, run_dir, task = self.describe(command)
        self.events.append((stage, run_dir.name))
        config = runner.read_json(run_dir / "task_config.json")
        phase = run_dir / "solo"
        if stage == "worker":
            (phase / "final.patch").write_text(f"Independent fresh Solo submission for {run_dir.name}")
            self.write(phase / "status.json", {
                "status": "completed", "fresh_solo": True, "task": task.as_dict(),
                "offline_selftest": config["offline_selftest"], "elapsed_seconds": 10.5,
                "outcome": "Submitted", "final_patch_sha256": runner.file_hash(phase / "final.patch")})
            self.write(phase / "initial_state.json", {"fresh_solo": True})
            if not config["offline_selftest"]:
                self.add_usage(run_dir, 0.1)
        else:
            existing = runner.read_json(phase / "evaluation_summary.json", {})
            if not existing.get("evaluation_completed"):
                self.score_runs += 1
                self.write(phase / "evaluation_summary.json", {
                    "evaluation_completed": True, "both_passed": False, "elapsed_seconds": 2.25,
                    "fresh_solo": True, "task": task.as_dict(), "features": list(task.features)})
        return 0

    def run_suite(self, offline=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.supervise(self.suite, offline)

    def make_job(self, task=None):
        task = task or get_task("click2800_f1_f2")
        run_dir = self.suite / task.key / "jobs" / (task.key + "_r01_solo")
        runner.configure_run(run_dir, task, self.protocol)
        return run_dir, task

    def test_budgets_are_exact_prefix_elapsed_plus_execution_allowance(self):
        self.assertEqual(runner.comparison_budgets(), {"click2800_f1_f2": 1252.160,
                                                      "dirty_equals43_f3_f7": 1238.524})
        self.assertEqual(self.protocol["execution_seconds"], runner.comparison_budgets())

    def test_invalid_or_incomplete_comparison_cannot_supply_time_budget(self):
        status_path = self.comparison / "status.json"
        for status in ({"status": "running", "completed_jobs": 11},
                       {"status": "paused", "completed_jobs": 12}):
            with self.subTest(status=status):
                self.write(status_path, status)
                with self.assertRaises(RuntimeError):
                    runner.comparison_budgets()
        self.write(status_path, {"status": "completed", "completed_jobs": 12})
        prefix_path = self.comparison / "click2800_f1_f2/prefix/prefix/status.json"
        for value in (True, 0, -1, float("inf"), float("nan"), "52.16", 1501):
            with self.subTest(elapsed=value):
                self.write(prefix_path, {"status": "completed", "elapsed_seconds": value})
                with self.assertRaises(RuntimeError):
                    runner.comparison_budgets()
        self.write(prefix_path, {"status": "completed", "elapsed_seconds": 52.16})
        wrong_task = copy.deepcopy(self.original_protocol)
        wrong_task["tasks"]["dirty_equals43_f3_f7"]["features"] = [1, 6]
        self.write(self.comparison / "protocol.json", wrong_task)
        with self.assertRaisesRegex(RuntimeError, "task changed"):
            runner.comparison_budgets()

    def test_protocol_freezes_four_fresh_jobs_and_original_feature_pairs(self):
        self.assertTrue(self.protocol["fresh_solo"])
        self.assertEqual([(job["task"], job["repeat"], job["condition"]) for job in self.protocol["jobs"]], [
            ("click2800_f1_f2", 1, "solo"), ("click2800_f1_f2", 2, "solo"),
            ("dirty_equals43_f3_f7", 1, "solo"), ("dirty_equals43_f3_f7", 2, "solo")])
        self.assertEqual(self.protocol["tasks"]["click2800_f1_f2"]["features"], [1, 2])
        self.assertEqual(self.protocol["tasks"]["dirty_equals43_f3_f7"]["features"], [3, 7])
        self.assertIsNone(self.protocol["money_limit"])
        self.assertIsNone(self.protocol["request_limit"])
        self.assertEqual(self.protocol["max_output_tokens"], 16384)

    def test_four_failed_outcomes_remain_and_completed_models_and_scores_do_not_repeat(self):
        self.assertEqual(self.run_suite(), 0)
        summary = runner.read_json(self.suite / "summary.json")
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(len(summary["rows"]), 4)
        self.assertFalse(any(row["official_success"] for row in summary["rows"]))
        self.assertEqual(summary["actual_new_usage"]["requests"], 4)
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 0.4)
        self.assertEqual({row["full_task_known_cost_usd"] for row in summary["rows"]}, {0.1})
        self.assertEqual({row["comparison_seconds"] for row in summary["rows"]}, {12.75})
        self.assertFalse(list(self.suite.glob("**/checkpoint.json")))
        self.assertEqual(self.score_runs, 4)
        self.events.clear()
        with mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": ""}):
            self.assertEqual(self.run_suite(), 0)
        self.assertEqual(len(self.events), 4)
        self.assertTrue(all(stage == "evaluation" for stage, _ in self.events))
        self.assertEqual(self.score_runs, 4)
        self.assertEqual(runner.read_json(self.suite / "summary.json"), summary)

    def test_execution_watchdog_uses_task_budget_and_configuration_is_frozen(self):
        run_dir, task = self.make_job(get_task("dirty_equals43_f3_f7"))
        config = runner.read_json(run_dir / "task_config.json")
        self.assertTrue(config["fresh_solo"])
        self.assertEqual(config["execution_seconds"], 1238.524)
        with contextlib.redirect_stdout(io.StringIO()):
            runner.run_model(run_dir, task, self.protocol, -1)
        self.assertAlmostEqual(self.command_mock.call_args.args[2], 1538.524)
        self.assertEqual(self.command_mock.call_args.args[3], -1)
        with self.assertRaisesRegex(RuntimeError, "Frozen configuration changed"):
            runner.configure_run(run_dir, get_task("click2800_f1_f2"), self.protocol)

    def test_first_receipt_and_existing_receipt_detect_replaced_publication(self):
        for has_receipt in (False, True):
            with self.subTest(receipt_exists=has_receipt):
                run_dir, task = self.make_job()
                phase = run_dir / "solo"
                phase.mkdir(exist_ok=True)
                self.command(["python", "runner.py", "--worker", "--run-dir", str(run_dir), "--task", task.key])
                if has_receipt:
                    runner.verify_execution_receipt(phase)
                (phase / "final.patch").write_text("A replacement after the model finished")
                if has_receipt:
                    status = runner.read_json(phase / "status.json")
                    status["final_patch_sha256"] = runner.file_hash(phase / "final.patch")
                    self.write(phase / "status.json", status)
                self.command_mock.reset_mock()
                with self.assertRaises(RuntimeError):
                    runner.run_model(run_dir, task, self.protocol, -1)
                self.command_mock.assert_not_called()

    def test_completed_nonfresh_status_is_rejected_without_model_or_evaluator_calls(self):
        run_dir, task = self.make_job()
        phase = run_dir / "solo"
        phase.mkdir()
        self.command(["python", "runner.py", "--worker", "--run-dir", str(run_dir), "--task", task.key])
        status = runner.read_json(phase / "status.json")
        status["fresh_solo"] = False
        self.write(phase / "status.json", status)
        self.command_mock.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "fresh Solo"):
            runner.run_model(run_dir, task, self.protocol, -1)
        self.command_mock.assert_not_called()

    def test_evaluation_infrastructure_failure_retries_only_scoring_stage(self):
        failed = False

        def fail_once(command, *args, **kwargs):
            nonlocal failed
            if self.describe(command)[0] == "evaluation" and not failed:
                failed = True
                return 2
            return self.command(command, *args, **kwargs)

        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        first_worker = self.events[0]
        self.assertEqual(first_worker[0], "worker")
        self.command_mock.side_effect = self.command
        self.assertEqual(self.run_suite(), 0)
        self.assertEqual(self.events.count(first_worker), 1)
        self.assertEqual(self.score_runs, 4)
        self.assertEqual(runner.read_json(self.suite / "summary.json")["actual_new_usage"]["known_cost_usd"], 0.4)

    def test_interrupted_attempt_cost_and_unknown_usage_survive_without_entering_valid_sample(self):
        failed = False

        def fail_once(command, *args, **kwargs):
            nonlocal failed
            stage, run_dir, task = self.describe(command)
            if stage == "worker" and not failed:
                failed = True
                self.add_usage(run_dir, 0.3)
                self.add_usage(run_dir, None, "in_flight")
                self.write(run_dir / "solo/status.json", {"status": "error", "error_type": "SyntheticInterruptedAttempt"})
                return 2
            return self.command(command, *args, **kwargs)

        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        paused = runner.read_json(self.suite / "summary.json")
        self.assertEqual(paused["actual_new_usage"]["known_cost_usd"], 0.3)
        self.assertEqual(paused["actual_new_usage"]["unknown_usage_requests"], 1)
        self.command_mock.side_effect = self.command
        self.assertEqual(self.run_suite(), 0)
        summary = runner.read_json(self.suite / "summary.json")
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 0.7)
        self.assertEqual(summary["actual_new_usage"]["requests"], 6)
        self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"], 1)
        self.assertEqual({row["full_task_known_cost_usd"] for row in summary["rows"]}, {0.1})
        self.assertEqual({row["full_task_unknown_usage_requests"] for row in summary["rows"]}, {0})
        run_dir = Path(summary["rows"][0]["run_directory"])
        excluded = [r for r in runner.read_json(run_dir / "usage_ledger.json")["requests"] if r.get("excluded_from_baseline")]
        self.assertEqual(len(excluded), 2)
        unknown = next(r for r in excluded if r["actual_cost_usd"] is None)
        self.assertEqual(unknown["status"], "error")
        self.assertEqual(unknown["error_type"], "InterruptedAttemptUsageUnknown")
        self.assertEqual(unknown["reserved_usd"], 0.05)
        self.assertEqual(len(list((run_dir / "failed_attempts").iterdir())), 1)

    def test_mutation_during_scoring_is_rejected_without_replacing_original_receipt(self):
        run_dir, task = self.make_job()
        with contextlib.redirect_stdout(io.StringIO()):
            runner.run_model(run_dir, task, self.protocol, -1)
        receipt = (run_dir / "solo/execution_receipt.json").read_bytes()

        def change_submission(command, *args, **kwargs):
            self.command(command, *args, **kwargs)
            (run_dir / "solo/final.patch").write_text("Unexpected scorer mutation")
            return 0

        self.command_mock.side_effect = change_submission
        with self.assertRaisesRegex(RuntimeError, "patch differs"):
            runner.evaluate(run_dir, task, -1)
        self.assertEqual((run_dir / "solo/execution_receipt.json").read_bytes(), receipt)

    def test_offline_proof_requires_all_four_submissions_scores_and_zero_usage(self):
        self.assertEqual(self.run_suite(offline=True), 0)
        proof = runner.read_json(self.root / "artifacts/solo_expansion_offline_validation.json")
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["jobs_completed"], 4)
        self.assertEqual(proof["model_api_calls"], 0)
        self.assertEqual(proof["source_manifest"], self.manifest)
        summary = runner.read_json(self.suite / "summary.json")
        self.assertFalse(any(row["official_success"] for row in summary["rows"]))
        self.assertEqual(summary["actual_new_usage"]["requests"], 0)

    def test_offline_api_usage_prevents_successful_preflight_proof(self):
        def unexpected_usage(command, *args, **kwargs):
            code = self.command(command, *args, **kwargs)
            stage, run_dir, _ = self.describe(command)
            if stage == "worker":
                self.add_usage(run_dir, 0.001)
            return code

        self.command_mock.side_effect = unexpected_usage
        self.assertEqual(self.run_suite(offline=True), 2)
        self.assertFalse((self.root / "artifacts/solo_expansion_offline_validation.json").exists())

    def test_last_stage_source_change_prevents_offline_proof(self):
        def mutate_last_source(command, *args, **kwargs):
            code = self.command(command, *args, **kwargs)
            if self.score_runs == 4:
                runner.source_manifest.return_value = {"worker.py": "changed-after-final-score"}
            return code

        self.command_mock.side_effect = mutate_last_source
        self.assertEqual(self.run_suite(offline=True), 2)
        self.assertFalse((self.root / "artifacts/solo_expansion_offline_validation.json").exists())


class SoloEvaluatorIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runs = self.root / "artifacts/solo_expansion_runs"
        patches = [mock.patch.object(task_specs, "ROOT", self.root),
                   mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
                   mock.patch.object(evaluator, "RUNS", self.runs),
                   mock.patch.object(evaluator, "source_manifest", return_value={"evaluator.py": "frozen"}),
                   mock.patch.object(evaluator, "verify_upstream")]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.task = get_task("dirty_equals43_f3_f7")
        self.phase = self.runs / "solo_controls_unit" / self.task.key / "jobs/task_r01_solo/solo"
        self.phase.mkdir(parents=True)
        self.config = {"task_key": self.task.key, "task": self.task.as_dict(), "fresh_solo": True,
                       "offline_selftest": True, "protocol_sha256": "a" * 64, "execution_seconds": 1238.524}
        self.write(self.phase.parent / "task_config.json", self.config)
        (self.phase / "final.patch").write_text("Synthetic published final patch")
        final_hash = hashlib.sha256((self.phase / "final.patch").read_bytes()).hexdigest()
        self.write(self.phase / "status.json", {"status": "completed", "phase": "solo",
                    "fresh_solo": True, "offline_selftest": True, "task": self.task.as_dict(),
                    "final_patch_sha256": final_hash})
        self.write(self.phase / "execution_receipt.json", {"patch_sha256": {"final.patch": final_hash}})
        self.write(self.phase / "initial_state.json", {"fresh_solo": True, "initial_message_roles": ["system", "user"]})
        self.write(self.task.image_record_path, {"Id": "sha256:" + "b" * 64})
        for feature in self.task.features:
            directory = self.task.task_dir / f"feature{feature}"
            directory.mkdir(parents=True)
            (directory / "feature.md").write_text(f"Exact feature {feature} requirement")
            (directory / "tests.patch").write_text(f"Pinned feature {feature} tests")
        (self.task.task_dir / "runner.sh").write_text("# Pinned scoring runner\n")

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def completed_score(self):
        attempt = self.phase / "evaluation_attempts/attempt_one"
        attempt.mkdir(parents=True, exist_ok=True)
        inputs = evaluator.input_manifest(self.phase, self.task, evaluator.patch_info(self.phase, self.task))
        self.write(attempt / "evaluation_input.json", {"fresh_solo": True, "task": self.task.as_dict(),
                    "protocol_sha256": self.config["protocol_sha256"], "input_manifest": inputs})
        self.write(attempt / "integrity.json", {"unchanged": True, "before": inputs, "after": inputs})
        native = {"both_passed": False, "feature1": {"passed": True, "test_output": "==== 60 passed in 0.03s ====\n"},
                  "feature2": {"passed": False, "test_output": "==== 1 failed, 70 passed in 0.04s ====\n"}, "error": None}
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json", self.phase / "evaluation.json"):
            self.write(path, native)
        summary = evaluator.summarize(native, "failed", self.phase, attempt, 2.0, self.task, self.config)
        self.write(self.phase / "evaluation_summary.json", summary)
        return summary

    def test_fresh_task_accepts_only_matching_task_and_no_inherited_artifacts(self):
        self.assertEqual(evaluator.validate_run_dir(self.phase), self.phase)
        self.assertEqual(evaluator.load_task(self.phase), (self.task, self.config))
        cases = [self.phase.parent / "checkpoint.json", self.phase / "restore_proof.json",
                 self.phase.parent.parent.parent / "shared_git.tar",
                 self.phase.parent.parent.parent.parent / "checkpoint_provenance.json"]
        for path in cases:
            with self.subTest(inherited_artifact=path.name):
                path.write_text("Inherited data is forbidden")
                try:
                    with self.assertRaisesRegex(ValueError, "checkpoint/inherited"):
                        evaluator.load_task(self.phase)
                finally:
                    path.unlink()
        for modification in ({"fresh_solo": False}, {"inherited_state": {}},
                             {"task": get_task("click2800_f1_f2").as_dict()}):
            with self.subTest(config=modification):
                self.write(self.phase.parent / "task_config.json", {**self.config, **modification})
                with self.assertRaises(ValueError):
                    evaluator.load_task(self.phase)
        self.write(self.phase.parent / "task_config.json", self.config)

    def test_single_submission_uses_both_current_features_without_merge(self):
        backend = mock.Mock()
        patches = evaluator.patch_info(self.phase, self.task)
        evaluator.evaluate_saved_patches(backend, self.phase, self.task, patches)
        backend.test_solo.assert_called_once()
        call = backend.test_solo.call_args.kwargs
        self.assertEqual(call["repo_name"], self.task.repo)
        self.assertEqual(call["task_id"], 43)
        self.assertEqual((call["feature1_id"], call["feature2_id"]), (3, 7))
        self.assertEqual(call["patch"], self.phase / "final.patch")
        backend.test_merged.assert_not_called()

    def test_completed_failed_score_reuses_only_unchanged_protected_inputs(self):
        expected = self.completed_score()
        self.assertEqual(evaluator.resume_completed(self.phase, self.task, self.config), expected)
        for path in (self.task.image_record_path, self.task.task_dir / "feature7/tests.patch",
                     self.phase / "initial_state.json"):
            with self.subTest(changed=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\nModified protected input\n")
                try:
                    with self.assertRaisesRegex(ValueError, "inputs or sources changed"):
                        evaluator.resume_completed(self.phase, self.task, self.config)
                finally:
                    path.write_bytes(original)
        with mock.patch.object(evaluator, "source_manifest", return_value={"evaluator.py": "changed"}):
            with self.assertRaisesRegex(ValueError, "inputs or sources changed"):
                evaluator.resume_completed(self.phase, self.task, self.config)

    def test_completed_score_rejects_feature_summary_or_native_result_tampering(self):
        expected = self.completed_score()
        changed = copy.deepcopy(expected)
        changed["feature_results"]["7"]["passed"] = True
        self.write(self.phase / "evaluation_summary.json", changed)
        with self.assertRaises(ValueError):
            evaluator.resume_completed(self.phase, self.task, self.config)
        self.write(self.phase / "evaluation_summary.json", expected)
        self.write(self.phase / "evaluation.json", {"both_passed": True})
        with self.assertRaisesRegex(ValueError, "native result"):
            evaluator.resume_completed(self.phase, self.task, self.config)

    def test_test_counts_require_native_counts_or_actual_terminal_pytest_summary(self):
        unknown = evaluator.feature_metrics({"passed": False, "test_output": "Could not apply patch"})
        self.assertIsNone(unknown["tests_passed"])
        self.assertIsNone(unknown["tests_failed"])
        recovered = evaluator.feature_metrics({"passed": False, "test_output":
            "Example mentions 999 passed\n==== 1 failed, 70 passed in 0.04s ====\nRepository cleaned.\n"})
        self.assertEqual((recovered["tests_passed"], recovered["tests_failed"]), (70, 1))
        self.assertEqual(recovered["counts_source"], "pytest_summary")


class SoloInitialStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = mock.patch.object(task_specs, "REPO", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = get_task("dirty_equals43_f3_f7")
        for feature in self.task.features:
            file = self.task.task_dir / f"feature{feature}/feature.md"
            file.parent.mkdir(parents=True)
            file.write_text(f"Original feature {feature} specification")
        self.combined = worker.combined_task_text(self.task)

    def agent(self):
        agent = SimpleNamespace(comm=None, messages=[], n_calls=0, cost=0, sent_messages=[],
                                _compaction_count=0, _segments=[], _current_segment_messages=[],
                                config=SimpleNamespace(system_template="SYSTEM", instance_template="INSTANCE"),
                                model=SimpleNamespace(format_message=lambda **m: copy.deepcopy(m)))
        agent._render_template = lambda template: ("Native Solo system" if template == "SYSTEM"
                                                   else "Native Solo task:\n" + agent.extra_template_vars["task"])
        agent.add_messages = lambda *messages: agent.messages.extend(copy.deepcopy(messages))
        return agent

    def inspection_fixtures(self):
        agent = self.agent()
        worker.initialize_fresh_agent(agent, self.combined, 1238.524)
        proof = {
            "head": self.task.base, "status_porcelain": "", "remote": "/shared-git/team.git",
            "refs": "\n".join(f"{name} {self.task.base}" for name in (
                "refs/heads/solo", "refs/remotes/origin/solo", "refs/remotes/origin/main")),
            "private_remote_refs": "\n".join(f"{self.task.base}\t{name}" for name in ("refs/heads/main", "refs/heads/solo")),
            "credential_environment_names": [], "forbidden_paths_present": [], "source_roots_exist": True,
        }
        attributes = {"Image": "sha256:" + "a" * 64,
            "HostConfig": {"NetworkMode": "none", "Init": True, "NanoCpus": 2_000_000_000, "Memory": 2 * 1024**3},
            "Config": {"Env": ["LANG=C.UTF-8"]},
            "Mounts": [{"Type": "volume", "Name": "private-solo", "Destination": "/shared-git", "RW": True}]}
        container = SimpleNamespace(attrs=attributes, reload=mock.Mock())
        dc = SimpleNamespace(containers=SimpleNamespace(get=mock.Mock(return_value=container)))
        env = SimpleNamespace(container_id="fake-container", execute=mock.Mock(
            side_effect=lambda action: {"returncode": 0, "output": json.dumps(proof)}))
        return agent, proof, attributes, env, dc

    def test_initial_prompt_contains_both_original_requirements_without_prior_state(self):
        self.assertEqual(self.combined, "## Feature 3\n\nOriginal feature 3 specification\n\n---\n\n## Feature 7\n\nOriginal feature 7 specification")
        agent = self.agent()
        worker.initialize_fresh_agent(agent, self.combined, 1238.524)
        self.assertEqual([m["role"] for m in agent.messages], ["system", "user"])
        self.assertIn(self.combined, agent.messages[1]["content"])
        self.assertIn("1238.52", agent.messages[1]["content"])
        self.assertFalse(agent.extra_template_vars["messaging_enabled"])
        self.assertFalse(agent.extra_template_vars["git_enabled"])
        self.assertEqual(agent.extra_template_vars["agents"], [])
        self.assertIsNone(agent.comm)
        self.assertEqual((agent.n_calls, agent.cost), (0, 0))

    def test_initialization_rejects_nonempty_history_calls_cost_or_peer_before_mutation(self):
        changes = {"messages": [{"role": "user", "content": "Inherited task"}], "n_calls": 1,
                   "cost": 0.01, "comm": object(), "sent_messages": ["Old peer message"],
                   "_compaction_count": 1, "_segments": ["Earlier history"],
                   "_current_segment_messages": [{"role": "assistant", "content": "Prior solution"}]}
        for field, value in changes.items():
            with self.subTest(nonfresh_field=field):
                agent = self.agent()
                setattr(agent, field, value)
                before = copy.deepcopy(agent.messages)
                with self.assertRaisesRegex(RuntimeError, "nonempty agent state"):
                    worker.initialize_fresh_agent(agent, self.combined)
                self.assertEqual(agent.messages, before)

    def test_initial_inspection_checks_clean_base_private_git_no_peers_and_no_credentials(self):
        agent, probe, attrs, env, dc = self.inspection_fixtures()
        result = worker.inspect_initial_state(env, dc, self.task, "private-solo", attrs["Image"], agent)
        self.assertTrue(result["verified"])
        self.assertTrue(result["fresh_solo"])
        self.assertEqual(result["initial_message_count"], 2)
        self.assertEqual(result["initial_model_calls"], 0)
        for field in ("checkpoint_loaded", "history_restored", "peer_evidence_mounted",
                      "reference_patches_mounted", "credentials_forwarded", "communication_enabled"):
            self.assertFalse(result[field])
        changes = [
            (probe, "head", "other-task-base"), (probe, "status_porcelain", " M dirty_equals/_other.py"),
            (probe, "forbidden_paths_present", ["/shared-evidence"]),
            (probe, "credential_environment_names", ["API_KEY"]),
            (probe, "refs", probe["refs"] + "\nrefs/remotes/origin/agent2 " + self.task.base),
            (attrs["HostConfig"], "NetworkMode", "bridge"),
            (attrs, "Mounts", attrs["Mounts"] + [{"Type": "bind", "Destination": "/shared-evidence", "RW": False}]),
            (attrs["Config"], "Env", ["API_KEY=synthetic-value-never-a-real-key"]),
        ]
        for target, key, value in changes:
            with self.subTest(invalid=key):
                before = target[key]
                target[key] = value
                try:
                    with self.assertRaises(RuntimeError):
                        worker.inspect_initial_state(env, dc, self.task, "private-solo", attrs["Image"], agent)
                finally:
                    target[key] = before


if __name__ == "__main__":
    unittest.main()
