"""Cross-task adapter contracts; no Docker, model API or evaluator subprocess."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cross_task_workers as workers
import evaluate_cross_task as evaluator
import task_specs
from task_specs import TaskSpec


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.task = TaskSpec("fixture_f2_f7", "fixture", 10, (2, 7), "fixture:10", "a" * 40, ("src/pkg",))

    def test_prefix_uses_parameterized_original_entry(self):
        with mock.patch("run_baselines.phase_worker", return_value=17) as call:
            self.assertEqual(workers.phase_worker(Path("/tmp/job"), "prefix", self.task, True), 17)
        call.assert_called_once_with(Path("/tmp/job"), "prefix", offline=True, task=self.task)

    def test_fresh_solo_receives_full_time_allowance_without_checkpoint(self):
        with mock.patch("solo_expansion_worker.phase_worker", return_value=0) as call:
            workers.phase_worker(Path("/tmp/job"), "solo", self.task, True, 1252.15)
        call.assert_called_once_with(Path("/tmp/job"), self.task, offline=True, execution_seconds=1252.15)

    def test_budget_changes_rejected_before_dispatch(self):
        with mock.patch.object(workers, "load_isolated") as call:
            for seconds in (1199, float("nan"), float("inf"), -1, True):
                with self.assertRaises(ValueError):
                    workers.phase_worker("/tmp/job", "strongshare", self.task, execution_seconds=seconds)
            call.assert_not_called()

    def test_offline_probe_uses_task_source_and_preserves_publication(self):
        commands = workers.offline_commands(self.task, "agent1")
        self.assertIn("/agent2/latest/worktree/src/pkg", commands[0])
        self.assertNotIn("dirty_equals", commands[0])
        self.assertIn("handoff_read_only_ok", commands[0])
        self.assertIn("git add -A", commands[1])
        self.assertEqual(commands[3], "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")

    def test_packet_module_and_old_worker_are_restored_after_error(self):
        old_packet = SimpleNamespace(old=True)
        replacement = SimpleNamespace(schema_version=2)
        module = SimpleNamespace()

        def invoke(*args, **kwargs):
            self.assertIs(sys.modules["handoff_packet"], replacement)
            self.assertEqual(module.VARIANT, "source_handoff_diff_v1")
            raise RuntimeError("fixture failure")

        module.phase_worker = invoke
        with mock.patch.dict(sys.modules, {"handoff_packet": old_packet}), \
                mock.patch.object(workers, "load_isolated", return_value=module), \
                mock.patch.object(workers.importlib, "import_module", return_value=replacement):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                workers.phase_worker("/tmp/job", "consolidate", self.task, offline=True)
            self.assertIs(sys.modules["handoff_packet"], old_packet)

    def test_isolated_module_does_not_modify_existing_handoff_globals(self):
        import handoff_worker
        original = handoff_worker.VARIANT
        clone = workers.load_isolated("handoff_worker.py")
        clone.VARIANT = "unit-only"
        self.assertEqual(handoff_worker.VARIANT, original)
        self.assertIsNot(clone.phase_worker.__globals__, handoff_worker.phase_worker.__globals__)


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        self.task = TaskSpec("fixture_f2_f7", "fixture", 10, (2, 7), "fixture:10", "a" * 40, ("src/pkg",))
        for patcher in (mock.patch.object(evaluator, "RUNS", self.runs),
                        mock.patch.object(evaluator, "get_task", return_value=self.task),
                        mock.patch.object(task_specs, "ROOT", self.root),
                        mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
                        mock.patch.object(evaluator, "source_manifest", return_value={"frozen.py": "b" * 64})):
            patcher.start()
            self.addCleanup(patcher.stop)
        write(self.task.image_record_path, {"Id": "sha256:" + "c" * 64,
              "Architecture": "arm64", "expected_base": self.task.base, "image": self.task.image})
        for feature in self.task.features:
            for name in ("feature.md", "tests.patch"):
                p = self.task.task_dir / f"feature{feature}" / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"fixture {feature} {name}")
        (self.task.task_dir / "runner.sh").write_text("fixture runner")

    def job(self, condition):
        group = "solo_jobs" if condition == "solo" else "jobs"
        phase = self.runs / "suite" / self.task.key / group / ("job_" + condition) / condition
        phase.mkdir(parents=True)
        config = {"task_key": self.task.key, "task": self.task.as_dict(), "condition": condition,
                  "offline_selftest": True, "protocol_sha256": "d" * 64,
                  "condition_version": "cross_repo_handoff_v1", "execution_seconds": 1200}
        status = {"phase": condition, "status": "completed", "task": self.task.as_dict(), "offline_selftest": True}
        names = ["final.patch"] if condition != "strongshare" else ["agent2.patch", "agent7.patch"]
        hashes = {}
        for name in names:
            p = phase / name
            p.write_text("fixture submitted patch\n")
            hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        if condition == "solo":
            config["fresh_solo"] = status["fresh_solo"] = True
            write(phase / "initial_state.json", {"fresh_solo": True, "checkpoint_loaded": False})
        else:
            for name in ("checkpoint.json", "checkpoint_provenance.json"):
                write(phase.parent / name, {"fixture": name})
            (phase.parent / "shared_git.tar").write_bytes(b"fixture")
            for name in ("handoff_packet.json", "handoff_delivery.json"):
                write(phase / name, {"fixture": name})
            (phase / "handoff_packet.txt").write_text("fixture evidence")
            for agent in (("agent1", "agent2") if condition == "strongshare" else ("agent1",)):
                write(phase / f"first_request_{agent}.json", {"verified": True})
        if condition != "strongshare":
            status["final_patch_sha256"] = hashes["final.patch"]
        write(phase.parent / "task_config.json", config)
        write(phase / "status.json", status)
        write(phase / "execution_receipt.json", {"patch_sha256": hashes})
        return phase, config

    def test_layout_and_solo_sibling_prefix_are_allowed(self):
        p, _ = self.job("solo")
        write(p.parents[2] / "prefix/checkpoint.json", {"never_inherited": True})
        self.assertEqual(evaluator.validate_run_dir(p), p)
        evaluator.assert_fresh_job(p)
        module = evaluator.make_evaluator("solo")
        task, _ = module.load_task(p)
        self.assertEqual(task, self.task)
        invalid = p.parents[2] / "jobs/bad/solo"
        invalid.mkdir(parents=True)
        with self.assertRaises(ValueError):
            evaluator.validate_run_dir(invalid)

    def test_solo_job_checkpoint_or_symlink_is_rejected(self):
        p, _ = self.job("solo")
        q = p.parent / "checkpoint.json"
        write(q, {"not_allowed": True})
        with self.assertRaises(ValueError):
            evaluator.assert_fresh_job(p)
        q.unlink()
        (p / "linked").symlink_to(self.task.image_record_path)
        with self.assertRaises(ValueError):
            evaluator.assert_fresh_job(p)

    def test_dynamic_feature_order_and_native_merge_are_preserved(self):
        for condition in ("strongshare", "consolidate", "solo"):
            phase, _ = self.job(condition)
            module = evaluator.make_evaluator(condition)
            patches = module.patch_info(phase, self.task)
            native = SimpleNamespace(test_solo=mock.Mock(), test_merged=mock.Mock())
            module.evaluate_saved_patches(native, phase, self.task, patches)
            called = native.test_merged if condition == "strongshare" else native.test_solo
            self.assertEqual(called.call_args.kwargs["feature1_id"], 2)
            self.assertEqual(called.call_args.kwargs["feature2_id"], 7)
            if condition == "strongshare":
                self.assertEqual(called.call_args.kwargs["patch1"].name, "agent2.patch")
                native.test_solo.assert_not_called()

    def test_patch_mutation_and_new_condition_mismatch_rejected(self):
        phase, config = self.job("consolidate")
        module = evaluator.make_evaluator("consolidate")
        module.patch_info(phase, self.task)
        (phase / "final.patch").write_text("changed after submission")
        with self.assertRaises(ValueError):
            module.patch_info(phase, self.task)
        config["condition"] = "strongshare"
        write(phase.parent / "task_config.json", config)
        with self.assertRaises(ValueError):
            module.load_task(phase)

    def test_cooperative_input_manifest_protects_packet_and_checkpoint(self):
        phase, _ = self.job("strongshare")
        module = evaluator.make_evaluator("strongshare")
        manifest = module.input_manifest(phase, self.task, module.patch_info(phase, self.task))
        self.assertIn(str(phase / "handoff_packet.txt"), manifest["protected_files"])
        self.assertIn(str(phase.parent / "checkpoint.json"), manifest["protected_files"])

    def test_completed_failed_evaluation_reused_but_tamper_rejected(self):
        phase, config = self.job("strongshare")
        module = evaluator.make_evaluator("strongshare")
        module.verify_upstream = mock.Mock()
        attempt = phase / "evaluation_attempts/unit"
        attempt.mkdir(parents=True)
        native = {"both_passed": False, "error": None,
                  "merge": {"status": "conflicts", "strategy": None},
                  "feature1": {"passed": False}, "feature2": {"passed": False}}
        manifest = module.input_manifest(phase, self.task, module.patch_info(phase, self.task))
        write(attempt / "evaluation_input.json", {"task": self.task.as_dict(),
              "protocol_sha256": config["protocol_sha256"], "input_manifest": manifest})
        write(attempt / "integrity.json", {"unchanged": True, "before": manifest, "after": manifest})
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json", phase / "evaluation.json"):
            write(path, native)
        summary = module.summarize(native, "failed", phase, attempt, 2, self.task, config)
        write(phase / "evaluation_summary.json", summary)
        self.assertEqual(evaluator.resume_completed(module, phase, self.task, config), summary)
        (phase / "handoff_packet.txt").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "inputs or sources changed"):
            evaluator.resume_completed(module, phase, self.task, config)

    def test_native_solo_count_summary_is_parsed_for_consolidate(self):
        phase, config = self.job("consolidate")
        module = evaluator.make_evaluator("consolidate")
        native = {"both_passed": True, "feature1": {"passed": True,
                  "test_output": "================ 9 passed in 0.42s ================\n"},
                  "feature2": {"passed": True, "test_output": "=== 12 passed in 1.01s ===\n"}}
        out = module.summarize(native, "passed", phase, phase / "evaluation_attempts/unit", 2, self.task, config)
        self.assertEqual(out["feature_results"]["2"]["tests_passed"], 9)
        self.assertEqual(out["feature_results"]["7"]["tests_passed"], 12)
        self.assertTrue(out["single_patch_scoring_adapter"])


if __name__ == "__main__":
    unittest.main()
