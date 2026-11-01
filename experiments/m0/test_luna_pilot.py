"""Luna supervision, frozen fresh-Solo reuse and scoring integrity; no network/Docker."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import evaluate_cross_task as evaluator
import luna_solo_worker as wrapper
import run_handoff_expansion as shared
import run_luna_pilot as runner
import solo_expansion_worker as original_worker
import task_specs


class LunaPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "artifacts").mkdir()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        self.suite = self.runs / "luna_solo_unit"
        self.qwen = self.runs / "cross_handoff_v1"
        (self.root / "source.py").write_text("# Frozen unit fixture\n")
        self.manifest = {"source.py": runner.file_hash(self.root / "source.py")}
        self.events = []
        self.score_runs = 0
        patches = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "RUNS", self.runs),
            mock.patch.object(runner, "QWEN_SUITE", self.qwen),
            mock.patch.object(runner, "PREFLIGHT", self.root / "artifacts/offline.json"),
            mock.patch.object(runner, "source_manifest", return_value=self.manifest),
            mock.patch.object(runner, "verify_references"),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch.dict(runner.os.environ, {"OPENAI_API_KEY": "unit-placeholder-not-a-key"}),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for key, seconds in zip(runner.TASK_KEYS, (1466.261, 1269.899)):
            task = runner.get_task(key)
            run = self.qwen / key / "solo_jobs" / f"{key}_r01_solo"
            self.write(run / "task_config.json", {"task_key": key, "task": task.as_dict(),
                "condition": "solo", "fresh_solo": True, "execution_seconds": seconds})
            self.add_usage(run, 100)
        self.qwen_before = {str(p.relative_to(self.qwen)): p.read_bytes()
                            for p in self.qwen.rglob("*") if p.is_file()}
        self.protocol = runner.protocol_for(False)
        for module in (runner, shared):
            patch = mock.patch.object(module, "run_command", side_effect=self.command)
            handle = patch.start()
            self.addCleanup(patch.stop)
            if module is runner:
                self.worker_command = handle
            else:
                self.evaluator_command = handle

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_usage(self, run, cost, status="completed"):
        path = run / "usage_ledger.json"
        ledger = runner.read_json(path, {"requests": []})
        ledger["requests"].append({"phase": "solo:solo", "status": status,
            "actual_cost_usd": cost, "reserved_usd": 0.1,
            "prompt_tokens": 100 if cost is not None else 0,
            "completion_tokens": 10 if cost is not None else 0})
        self.write(path, ledger)

    def command(self, command, *args, **kwargs):
        path = Path(command[command.index("--run-dir") + 1])
        is_worker = "--worker" in command
        run = path if is_worker else path.parent
        phase = run / "solo"
        config = runner.read_json(run / "task_config.json")
        self.events.append(("model" if is_worker else "evaluation", run.name))
        if is_worker:
            (phase / "final.patch").write_text("Synthetic independent publication " + run.name)
            self.write(phase / "initial_state.json", {"fresh_solo": True,
                "initial_message_count": 2, "initial_model_calls": 0})
            self.write(phase / "solo.traj.json", {"messages": [
                {"role": "system", "content": "Fresh Solo"},
                {"role": "user", "content": "Both original requirements"}]})
            self.write(phase / "status.json", {"status": "completed", "fresh_solo": True,
                "task": config["task"], "offline_selftest": config["offline_selftest"],
                "elapsed_seconds": 20.5, "outcome": "Submitted",
                "offline_isolation_probe_passed": config["offline_selftest"],
                "final_patch_sha256": runner.file_hash(phase / "final.patch")})
            if not config["offline_selftest"]:
                self.add_usage(run, 0.1)
        elif not runner.read_json(phase / "evaluation_summary.json", {}).get("evaluation_completed"):
            self.score_runs += 1
            self.write(phase / "evaluation_summary.json", {"evaluation_completed": True,
                "both_passed": False, "elapsed_seconds": 2.25})
        return 0

    def run_suite(self, offline=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.supervise(self.suite, offline)

    def summary(self):
        return runner.read_json(self.suite / "summary.json")

    def make_job(self):
        task = runner.get_task(runner.TASK_KEYS[0])
        run = self.suite / task.key / "solo_jobs" / f"{task.key}_r01_solo"
        runner.configure_job(run, task, self.protocol)
        return run, task

    def test_fixed_four_jobs_original_allowances_and_no_historical_charge(self):
        self.assertEqual([(j["task"], j["repeat"]) for j in self.protocol["jobs"]],
                         [(k, r) for r in (1, 2) for k in runner.TASK_KEYS])
        self.assertEqual([self.protocol["time_allowances"][k]["seconds"]
                          for k in runner.TASK_KEYS], [1466.261, 1269.899])
        self.assertEqual(self.run_suite(), 0)
        summary = self.summary()
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 0.4)
        self.assertEqual(summary["actual_new_usage"]["requests"], 4)
        self.assertEqual(self.qwen_before, {str(p.relative_to(self.qwen)): p.read_bytes()
                         for p in self.qwen.rglob("*") if p.is_file()})
        self.assertFalse(list(self.suite.rglob("checkpoint*")))

    def test_completed_failures_and_cleanup_resume_do_not_repeat_models_or_scores(self):
        self.assertEqual(self.run_suite(), 0)
        expected = self.summary()
        self.assertFalse(any(row["official_success"] for row in expected["rows"]))
        self.events.clear()
        runner.cleanup_phase.reset_mock()
        with mock.patch.dict(runner.os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.run_suite(), 0)
        self.assertEqual(self.score_runs, 4)
        self.assertEqual([stage for stage, _ in self.events], ["evaluation"] * 4)
        self.assertEqual(runner.cleanup_phase.call_count, 4)
        self.assertEqual(self.summary(), expected)

    def test_incomplete_attempt_keeps_known_unknown_cost_and_time_outside_accepted_usage(self):
        def interrupt(command, *args, **kwargs):
            run = Path(command[command.index("--run-dir") + 1])
            self.add_usage(run, 0.3)
            self.add_usage(run, None, "in_flight")
            self.write(run / "solo/status.json", {"status": "error", "elapsed_seconds": 7})
            return 2
        self.worker_command.side_effect = interrupt
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.summary()["actual_new_usage"]["unknown_usage_requests"], 1)
        self.assertEqual(self.summary()["rows"], [])
        self.worker_command.side_effect = self.command
        self.assertEqual(self.run_suite(), 0)
        summary = self.summary()
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 0.7)
        self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"], 1)
        first = summary["rows"][0]
        self.assertEqual(first["accepted_usage"]["requests"], 1)
        self.assertEqual(first["all_attempt_usage"]["requests"], 3)
        self.assertEqual(first["execution_seconds_including_incomplete"], 27.5)
        ledger = runner.read_json(Path(first["run_directory"]) / "usage_ledger.json")
        excluded = [r for r in ledger["requests"] if r.get("excluded_from_baseline")]
        self.assertEqual(len(excluded), 2)
        self.assertEqual(excluded[-1]["error_type"], "InterruptedAttemptUsageUnknown")

    def test_scoring_error_resumes_scoring_without_model_repetition(self):
        self.evaluator_command.return_value = 2
        self.evaluator_command.side_effect = None
        self.assertEqual(self.run_suite(), 2)
        first = self.events[0]
        self.evaluator_command.side_effect = self.command
        self.assertEqual(self.run_suite(), 0)
        self.assertEqual(self.events.count(first), 1)
        self.assertEqual(self.summary()["actual_new_usage"]["requests"], 4)

    def test_completed_receipt_rejects_changed_patch_or_history_without_model(self):
        run, task = self.make_job()
        runner.execute(run, task, self.protocol, -1)
        for name in ("final.patch", "solo.traj.json"):
            with self.subTest(file=name):
                path = run / "solo" / name
                previous = path.read_bytes()
                path.write_bytes(previous + b"\nUnexpected change")
                try:
                    self.worker_command.reset_mock()
                    with self.assertRaises((RuntimeError, ValueError)):
                        runner.execute(run, task, self.protocol, -1)
                    self.worker_command.assert_not_called()
                finally:
                    path.write_bytes(previous)

    def test_task_and_time_config_cannot_change_after_creation(self):
        run, task = self.make_job()
        changed = copy.deepcopy(self.protocol)
        changed["time_allowances"][task.key]["seconds"] += 1
        with self.assertRaisesRegex(RuntimeError, "Frozen configuration changed"):
            runner.configure_job(run, task, changed)
        timing = self.qwen / task.key / "solo_jobs" / f"{task.key}_r01_solo/task_config.json"
        value = runner.read_json(timing)
        value["execution_seconds"] = 1200
        self.write(timing, value)
        with self.assertRaisesRegex(RuntimeError, "Historical Solo allowance changed"):
            runner.time_allowances()

    def test_offline_proof_requires_both_scored_jobs_zero_api_and_frozen_final_source(self):
        self.assertEqual(self.run_suite(offline=True), 0)
        proof = runner.read_json(runner.PREFLIGHT)
        self.assertEqual((proof["completed_jobs"], proof["model_api_calls"]), (2, 0))
        self.assertEqual(proof["source_manifest"], self.manifest)
        runner.PREFLIGHT.unlink()
        runner.source_manifest.return_value = {"source.py": "changed"}
        with self.assertRaisesRegex(RuntimeError, "Frozen Luna sources"):
            runner.verify_sources(self.protocol)

    def test_worker_cli_rejects_task_seconds_protocol_and_unplanned_job_before_execution(self):
        run, task = self.make_job()
        self.write(self.suite / "protocol.json", self.protocol)
        config = runner.read_json(run / "task_config.json")
        cases = [{"task_key": runner.TASK_KEYS[1]}, {"execution_seconds": 1200},
                 {"protocol_sha256": "0" * 64}, {"offline_selftest": True},
                 {"task": runner.get_task(runner.TASK_KEYS[1]).as_dict()}]
        with mock.patch.object(wrapper, "phase_worker") as execute:
            for change in cases:
                with self.subTest(change=change):
                    self.write(run / "task_config.json", {**config, **change})
                    with mock.patch.object(sys, "argv", ["runner", "--worker", "--run-dir", str(run)]):
                        with self.assertRaises(ValueError):
                            runner.main()
                    execute.assert_not_called()
            self.write(run / "task_config.json", config)
            unplanned = run.with_name(task.key + "_r99_solo")
            run.rename(unplanned)
            with mock.patch.object(sys, "argv", ["runner", "--worker", "--run-dir", str(unplanned)]):
                with self.assertRaises(ValueError):
                    runner.main()
            execute.assert_not_called()


class LunaWrapperIsolationTests(unittest.TestCase):
    def test_independent_clone_forwards_arguments_and_restores_import_even_on_error(self):
        original = (original_worker.MODEL, original_worker.MAX_OUTPUT, original_worker.BudgetGuard)
        fake_model = SimpleNamespace()
        accounting = type("Accounting", (), {})
        original_spec = wrapper.importlib.util.spec_from_file_location
        clones = []
        for had_prior in (False, True):
            for fail in (False, True):
                with self.subTest(prior_module=had_prior, exception=fail):
                    previous = object()
                    def load_spec(*args, **kwargs):
                        spec = original_spec(*args, **kwargs)
                        original_load = spec.loader.exec_module
                        def load(module):
                            original_load(module)
                            clones.append(module)
                            def phase(run, task, offline, execution_seconds):
                                self.assertIs(sys.modules["baseline_model"], fake_model)
                                self.assertEqual((module.MODEL, module.MAX_OUTPUT), ("gpt-6-luna", 16384))
                                self.assertIs(module.BudgetGuard, accounting)
                                self.assertEqual((str(run), task, offline, execution_seconds),
                                                 ("unit-run", "task-sentinel", True, 1466.261))
                                if fail:
                                    raise RuntimeError("synthetic worker failure")
                                return 17
                            module.phase_worker = phase
                        spec.loader.exec_module = load
                        return spec
                    with mock.patch.dict(sys.modules, {"luna_model": fake_model,
                            "luna_accounting": SimpleNamespace(LunaAccounting=accounting)}):
                        sys.modules.pop("baseline_model", None)
                        if had_prior:
                            sys.modules["baseline_model"] = previous
                        with mock.patch.object(wrapper.importlib.util, "spec_from_file_location", side_effect=load_spec):
                            if fail:
                                with self.assertRaisesRegex(RuntimeError, "synthetic worker failure"):
                                    wrapper.phase_worker("unit-run", "task-sentinel", True, 1466.261)
                            else:
                                self.assertEqual(wrapper.phase_worker("unit-run", "task-sentinel", True, 1466.261), 17)
                        if had_prior:
                            self.assertIs(sys.modules["baseline_model"], previous)
                        else:
                            self.assertNotIn("baseline_model", sys.modules)
        self.assertEqual((original_worker.MODEL, original_worker.MAX_OUTPUT, original_worker.BudgetGuard), original)
        self.assertEqual(len({id(clone) for clone in clones}), 4)


class LunaEvaluatorCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        for patch in (mock.patch.object(task_specs, "ROOT", self.root),
                      mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
                      mock.patch.object(evaluator, "RUNS", self.runs),
                      mock.patch.object(evaluator, "source_manifest", return_value={"evaluator.py": "frozen"})):
            patch.start()
            self.addCleanup(patch.stop)
        self.module = evaluator.make_evaluator("solo")
        self.module.verify_upstream = mock.Mock()
        self.task = runner.get_task("pillow68_f1_f2")
        self.phase = self.runs / "luna_solo_unit" / self.task.key / "solo_jobs" / (self.task.key + "_r01_solo") / "solo"
        self.phase.mkdir(parents=True)
        self.config = {"task_key": self.task.key, "task": self.task.as_dict(), "condition": "solo",
            "condition_version": runner.VARIANT, "fresh_solo": True, "offline_selftest": True,
            "protocol_sha256": "a" * 64, "execution_seconds": 1269.899}
        self.write(self.phase.parent / "task_config.json", self.config)
        (self.phase / "final.patch").write_text("Synthetic fresh Luna publication")
        self.write(self.phase / "status.json", {"status": "completed", "phase": "solo",
            "fresh_solo": True, "task": self.task.as_dict(), "offline_selftest": True,
            "final_patch_sha256": runner.file_hash(self.phase / "final.patch")})
        self.write(self.phase / "source_manifest.json", {"luna_model.py": "frozen"})
        self.write(self.phase / "initial_state.json", {"fresh_solo": True, "initial_message_count": 2})
        self.write(self.phase / "solo.traj.json", {"messages": []})
        runner.verify_execution_receipt(self.phase, self.task, "solo")
        self.write(self.task.image_record_path, {"Id": "sha256:" + "b" * 64,
            "image": self.task.image, "expected_base": self.task.base})
        for feature in self.task.features:
            directory = self.task.task_dir / f"feature{feature}"
            directory.mkdir(parents=True)
            (directory / "feature.md").write_text(f"Original feature {feature}")
            (directory / "tests.patch").write_text(f"Official tests {feature}")
        (self.task.task_dir / "runner.sh").write_text("# Pinned test runner\n")

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def completed_score(self):
        attempt = self.phase / "evaluation_attempts/attempt_one"
        attempt.mkdir(parents=True)
        inputs = self.module.input_manifest(self.phase, self.task, self.module.patch_info(self.phase, self.task))
        self.write(attempt / "evaluation_input.json", {"task": self.task.as_dict(),
            "protocol_sha256": self.config["protocol_sha256"], "input_manifest": inputs})
        self.write(attempt / "integrity.json", {"unchanged": True, "before": inputs, "after": inputs})
        native = {"both_passed": False, "feature1": {"passed": True},
                  "feature2": {"passed": False}, "error": None}
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json", self.phase / "evaluation.json"):
            self.write(path, native)
        summary = self.module.summarize(native, "failed", self.phase, attempt, 2, self.task, self.config)
        self.write(self.phase / "evaluation_summary.json", summary)
        return summary

    def test_luna_solo_config_receipt_and_completed_failure_are_accepted(self):
        self.assertEqual(self.module.load_task(self.phase), (self.task, self.config))
        summary = self.completed_score()
        self.assertFalse(summary["both_passed"])
        self.assertEqual(self.module.resume_completed(self.phase, self.task, self.config), summary)
        backend = mock.Mock()
        patches = self.module.patch_info(self.phase, self.task)
        self.module.evaluate_saved_patches(backend, self.phase, self.task, patches)
        kwargs = backend.test_solo.call_args.kwargs
        self.assertEqual((kwargs["task_id"], kwargs["feature1_id"], kwargs["feature2_id"]), (68, 1, 2))
        backend.test_merged.assert_not_called()

    def test_cache_rejects_changed_freshness_source_or_native_score(self):
        self.completed_score()
        for name in ("initial_state.json", "source_manifest.json", "execution_receipt.json"):
            with self.subTest(changed=name):
                path = self.phase / name
                original = path.read_bytes()
                value = json.loads(original)
                value["unexpected_change"] = True
                self.write(path, value)
                try:
                    with self.assertRaises(ValueError):
                        self.module.resume_completed(self.phase, self.task, self.config)
                finally:
                    path.write_bytes(original)
        self.write(self.phase / "evaluation.json", {"both_passed": True})
        with self.assertRaisesRegex(ValueError, "native evaluation"):
            self.module.resume_completed(self.phase, self.task, self.config)


if __name__ == "__main__":
    unittest.main()
