"""Offline proofs of identical authority/scoring and immutable conflict evidence."""
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import conflict_evaluator as e
from task_specs import TaskSpec
import task_specs


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ConflictEvaluatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "artifacts/conflict_runs"
        self.tasks = {
            e.TASK_KEYS[0]: TaskSpec(e.TASK_KEYS[0], "fixture_llama", 17070, (1, 3), "fixture:llama", "a" * 40, ("src/pkg",)),
            e.TASK_KEYS[1]: TaskSpec(e.TASK_KEYS[1], "fixture_click", 2068, (4, 10), "fixture:click", "b" * 40, ("src/click",))}
        self.cp_validator = mock.Mock()
        self.worker_verifier = mock.Mock(side_effect=lambda phase, task: e.read(phase / "execution_receipt.json"))
        for patcher in (
            mock.patch.object(e, "RUNS", self.runs),
            mock.patch.object(e, "get_task", side_effect=lambda key: self.tasks[key]),
            mock.patch.object(e, "source_manifest", return_value={"conflict_evaluator.py": "c" * 64}),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
            mock.patch.dict("sys.modules", {
                "conflict_runtime": SimpleNamespace(verify_worker_artifacts=self.worker_verifier),
                "conflict_checkpoint": SimpleNamespace(validate_conflict_checkpoint=self.cp_validator)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for task in self.tasks.values():
            write(task.image_record_path, {"Id": "sha256:" + "d" * 64, "Architecture": "arm64",
                                         "expected_base": task.base, "image": task.image})
            for feature in task.features:
                for name in ("feature.md", "tests.patch"):
                    write(task.task_dir / f"feature{feature}" / name, {"fixture": name})
            write(task.task_dir / "runner.sh", {"fixture": "runner"})
        self.full = b"complete published source\n" + b"x" * 9000 + b"\nTEST_ONLY\n"
        self.filtered = self.full.replace(b"TEST_ONLY\n", b"")

    def phase(self, condition="strongshare", key=None, empty=False):
        task = self.tasks[key or e.TASK_KEYS[0]]
        phase = self.runs / "luna_conflict_v1" / task.key / "jobs" / (condition + "_r1") / condition
        phase.mkdir(parents=True)
        config = {"task_key": task.key, "task": task.as_dict(), "condition": condition,
                  "condition_version": e.CONDITION_VERSIONS[condition], "experiment_version": e.VERSION,
                  "submission_policy": e.SUBMISSION_POLICY, "offline_selftest": True, "protocol_sha256": "f" * 64}
        write(phase.parent / "task_config.json", config)
        write(phase.parent / "checkpoint.json", {"schema": "current_unmerged_checkpoint_v1", "fixture": True})
        write(phase.parent / "shared_git.tar", {"fixture": "archive"})
        write(phase / "status.json", {"status": "completed", "task": task.as_dict(), "offline_selftest": True})
        write(phase / "source_manifest.json", {"fixture": "sources"})
        (phase / "agent1.patch").write_bytes(b"" if empty else self.full)
        (phase / "agent2.patch").write_bytes(b"different peer publication must never be selected\n")
        hashes = {name: e.file_info(phase / name)["sha256"] for name in ("agent1.patch", "agent2.patch")}
        write(phase / "final_delivery.json", {"schema": e.DELIVERY_SCHEMA, "condition": condition,
              "submission_policy": e.SUBMISSION_POLICY, "authoritative_agent": "agent1",
              "scored_patch": "agent1.patch", "peer_patch": "agent2.patch", "selection": "predesignated_agent1",
              "active_agents": ["agent1", "agent2"], "automatic_merge": False, "posthoc_selection": False,
              "patch_sha256": hashes["agent1.patch"], "peer_patch_sha256": hashes["agent2.patch"],
              "publications": {"agent1": {"strategy": "published_pr"}, "agent2": {"strategy": "published_pr"}}})
        protected = {str(p.relative_to(phase.parent)): e.file_info(p)["sha256"]
                     for p in phase.parent.rglob("*") if p.is_file()}
        write(phase / "execution_receipt.json", {"patch_sha256": hashes, "protected_files_sha256": protected})
        return phase, task, config

    def capture(self, phase, task, *, empty=False, fail=False, substitute=False):
        module = e.make_evaluator(phase.name)
        owner = module._implementation
        owner.verify_upstream = mock.Mock()
        patches = module.patch_info(phase, task)
        attempt = phase / "evaluation_attempts/fixture"
        attempt.mkdir(parents=True)
        expected = b"" if empty else self.filtered
        sandbox = SimpleNamespace(exec=mock.Mock(return_value=SimpleNamespace(returncode=0,
            stdout_read=lambda: base64.b64encode(b"peer replacement" if substitute else expected).decode())))
        native = SimpleNamespace(_load_patch=lambda path: Path(path).read_text(),
            _filter_test_files=lambda text: text.replace("TEST_ONLY\n", ""),
            _run_tests=mock.Mock(side_effect=lambda *args: {"passed": not fail, "exit_code": 1 if fail else 0,
                                                          "output": "1 failed" if fail else "6 passed"}),
            test_merged=mock.Mock(side_effect=AssertionError("No synthetic merge allowed")))
        original_test = native._run_tests

        def test_solo(**kwargs):
            self.assertEqual(kwargs["patch"], phase / "agent1.patch")
            self.assertEqual((kwargs["feature1_id"], kwargs["feature2_id"]), task.features)
            self.assertEqual(kwargs["timeout"], owner.EXEC_TIMEOUT_SECONDS)
            outcomes = [native._run_tests(sandbox, f"tests{i}.patch", "solo.patch", task.base) for i in (1, 2)]
            return {"setting": "solo", "both_passed": not fail, "error": None,
                    **{f"feature{i}": {"passed": item["passed"], "test_output": item["output"]}
                       for i, item in enumerate(outcomes, 1)}}

        native.test_solo = mock.Mock(side_effect=test_solo)
        try:
            result = owner.capture_evaluation(native, phase, attempt, task, patches, False)
        finally:
            self.assertIs(native._run_tests, original_test)
            native.test_merged.assert_not_called()
        native.test_solo.assert_called_once()
        self.assertEqual(original_test.call_count, 2)
        return module, attempt, result, patches

    def cache(self, phase, task, config, **kwargs):
        module, attempt, native, patches = self.capture(phase, task, **kwargs)
        owner = module._implementation
        manifest = module.input_manifest(phase, task, patches)
        write(attempt / "evaluation_input.json", {"task": task.as_dict(), "protocol_sha256": config["protocol_sha256"],
              "submission_policy": e.SUBMISSION_POLICY, "primary_outcome": True, "input_manifest": manifest})
        write(attempt / "integrity.json", {"unchanged": True, "before": manifest, "after": manifest})
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json", phase / "evaluation.json"):
            write(path, native)
        summary = owner.summarize(native, "passed" if native["both_passed"] else "failed", phase, attempt, 1.0, task, config)
        write(attempt / "evaluation_summary.json", summary)
        write(phase / "evaluation_summary.json", summary)
        return module, summary, attempt

    def test_both_conditions_and_tasks_score_exact_same_full_published_lead(self):
        for condition in e.CONDITIONS:
            for key in e.TASK_KEYS:
                with self.subTest(condition=condition, task=key):
                    phase, task, config = self.phase(condition, key)
                    module, summary, attempt = self.cache(phase, task, config)
                    self.assertEqual((attempt / "delivered.patch").read_bytes(), self.filtered)
                    self.assertGreater(len(self.filtered), 5000)
                    self.assertEqual(module.resume_completed(phase, task, config), summary)
                    self.assertTrue(summary["common_delivery_contract"])
                    self.assertEqual(summary["condition"], condition)
                    self.assertEqual(summary["active_agent_count"], 2)
                    self.assertFalse(summary["fresh_solo"])
                    self.assertFalse(summary["contributions"]["agent2"]["graded"])

    def test_empty_lead_remains_failure_despite_nonempty_peer(self):
        for condition in e.CONDITIONS:
            phase, task, config = self.phase(condition, empty=True)
            module, summary, attempt = self.cache(phase, task, config, empty=True, fail=True)
            self.assertFalse(summary["both_passed"])
            self.assertTrue(summary["evaluation_completed"])
            self.assertEqual((attempt / "delivered.patch").read_bytes(), b"")
            self.assertEqual(module.resume_completed(phase, task, config), summary)

    def test_missing_lead_is_not_substituted(self):
        phase, task, _ = self.phase()
        (phase / "agent1.patch").unlink()
        with self.assertRaises(ValueError):
            e.patch_info(phase, task)

    def test_capture_rejects_peer_or_worktree_substitution(self):
        phase, task, _ = self.phase()
        with self.assertRaisesRegex(ValueError, "not exactly the fixed agent1"):
            self.capture(phase, task, substitute=True)

    def test_load_uses_current_conflict_validator_and_readonly_runtime_receipt(self):
        phase, task, config = self.phase()
        self.assertEqual(e.load_task(phase), (task, config))
        self.cp_validator.assert_called_once_with(e.read(phase.parent / "checkpoint.json"), task)
        self.worker_verifier.assert_called_once_with(phase, task)
        self.cp_validator.side_effect = ValueError("No current unresolved conflict")
        with self.assertRaisesRegex(ValueError, "current unresolved"):
            e.load_task(phase)

    def test_native_merge_policy_and_old_condition_version_are_rejected(self):
        phase, _, config = self.phase()
        for changes in ({"submission_policy": "native_cooperative_merge"},
                        {"condition_version": "source_handoff_diff_v1"},
                        {"experiment_version": "luna_selector_v1"}):
            write(phase.parent / "task_config.json", {**config, **changes})
            with self.assertRaisesRegex(ValueError, "identity is inconsistent"):
                e.load_task(phase)

    def test_final_contract_cannot_change_owner_or_enable_merge(self):
        phase, task, _ = self.phase()
        original = e.read(phase / "final_delivery.json")
        for key, value in (("authoritative_agent", "agent2"), ("scored_patch", "agent2.patch"),
                           ("automatic_merge", True), ("posthoc_selection", True), ("condition", "integrator")):
            write(phase / "final_delivery.json", {**original, key: value})
            with self.assertRaisesRegex(ValueError, "common predesignated"):
                e.patch_info(phase, task)

    def test_changed_peer_invalidates_cache_even_though_peer_not_graded(self):
        phase, task, config = self.phase()
        module, _, _ = self.cache(phase, task, config)
        (phase / "agent2.patch").write_text("tampered peer")
        with self.assertRaisesRegex(ValueError, "Both published patches"):
            module.resume_completed(phase, task, config)

    def test_current_conflict_evidence_and_official_tests_are_cache_protected(self):
        phase, task, config = self.phase()
        module, _, _ = self.cache(phase, task, config)
        cp = phase.parent / "checkpoint.json"
        before = cp.read_bytes()
        cp.write_text("{}")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            module.resume_completed(phase, task, config)
        cp.write_bytes(before)
        (task.task_dir / f"feature{task.features[0]}/tests.patch").write_text("changed tests")
        with self.assertRaisesRegex(ValueError, "inputs, policy or sources"):
            module.resume_completed(phase, task, config)

    def test_delivered_patch_and_summary_tampering_are_rejected(self):
        phase, task, config = self.phase()
        module, _, attempt = self.cache(phase, task, config)
        path = attempt / "delivered.patch"
        before = path.read_bytes()
        path.write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "actually tested"):
            module.resume_completed(phase, task, config)
        path.write_bytes(before)
        summary = e.read(phase / "evaluation_summary.json")
        summary["feature_results"]["1"]["passed"] = False
        write(phase / "evaluation_summary.json", summary)
        with self.assertRaisesRegex(ValueError, "summary differs"):
            module.resume_completed(phase, task, config)

    def test_cross_condition_module_and_alternate_scoring_mode_rejected(self):
        phase, task, _ = self.phase("integrator")
        with self.assertRaisesRegex(ValueError, "condition differs"):
            e.make_evaluator("strongshare").load_task(phase)
        with self.assertRaises(ValueError):
            e.validate_run_dir(phase, diagnostic=True)
        with self.assertRaises(ValueError):
            e.make_evaluator("solo")

    def test_private_adapter_does_not_mutate_old_evaluator(self):
        import luna_integrator_evaluator as original
        old = (original.__file__, original.RUNS, original.get_task, original.load_task,
               original.input_manifest, original.patch_info, original.summarize)
        e.make_evaluator("strongshare")
        e.make_evaluator("integrator")
        self.assertEqual(old, (original.__file__, original.RUNS, original.get_task, original.load_task,
                              original.input_manifest, original.patch_info, original.summarize))


if __name__ == "__main__":
    unittest.main()
