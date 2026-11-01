"""Exact delivery capture contracts; no Docker, model calls or network."""
import base64
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import luna_collaboration_evaluator as evaluator
from task_specs import TaskSpec


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.attempt = Path(self.temp.name).resolve()
        self.task = TaskSpec("pillow68_f1_f2", "pillow_task", 68, (1, 2), "fixture:68", "a" * 40, ("src/PIL",))
        self.data = b"complete native patch\n" + b"not truncated\n" * 700
        self.patches = {"agent1.patch": {"sha256": "b" * 64}, "agent2.patch": {"sha256": "c" * 64}}
        self.native = SimpleNamespace(_run_tests=self.run_tests)
        self.original_run_tests = self.native._run_tests
        self.sandbox = SimpleNamespace(exec=mock.Mock(side_effect=self.capture_read))

    def capture_read(self, *args):
        self.assertEqual(args[:2], ("python", "-c"))
        return SimpleNamespace(returncode=0, stdout_read=lambda: base64.b64encode(self.data).decode() + "\n")

    def run_tests(self, sandbox, tests_patch, feature_patch, base):
        return {"passed": True, "exit_code": 0, "tests_passed": 3, "tests_failed": 0,
                "output": f"unchanged native output {tests_patch}"}

    def perform(self, condition="strongshare", status="clean", strategy="naive", filename="merged.patch", fail=False):
        def invoke():
            outcomes = []
            for number in (1, 2):
                outcomes.append(self.native._run_tests(self.sandbox, f"tests{number}.patch", filename, self.task.base))
            result = {"both_passed": not fail, "error": None}
            for number, outcome in enumerate(outcomes, 1):
                result[f"feature{number}"] = {"passed": outcome["passed"], "test_output": outcome["output"]}
            if condition == "strongshare":
                result["merge"] = {"status": status, "strategy": strategy, "diff": "irrelevant truncated or failed diff"}
            return result
        if fail:
            self.native._run_tests = lambda *args: {"passed": False, "exit_code": 1, "output": "native test failure"}
            self.original_run_tests = self.native._run_tests
        raw = evaluator.capture_evaluation(self.native, invoke, condition, self.task, self.patches, self.attempt)
        self.assertIs(self.native._run_tests, self.original_run_tests)
        receipt = json.loads((self.attempt / "evaluated_delivery.json").read_text())
        summary = {"evaluated_delivery": evaluator.delivery_summary(self.attempt)}
        evaluator.validate_delivery(self.attempt, summary, raw, condition, self.task, self.patches)
        return raw, receipt, summary

    def test_clean_merge_exports_complete_actually_tested_bytes_not_diff(self):
        raw, receipt, _ = self.perform()
        self.assertGreater(len(self.data), 5000)
        self.assertEqual((self.attempt / "delivered.patch").read_bytes(), self.data)
        self.assertEqual(receipt["selection"], "native_naive_merge")
        self.assertEqual(receipt["selected_call_indices"], [1, 2])
        self.assertEqual([c["native_patch_name"] for c in receipt["calls"]], ["merged.patch"] * 2)
        self.assertEqual(raw["merge"]["diff"], "irrelevant truncated or failed diff")
        self.assertEqual(self.sandbox.exec.call_count, 2)

    def test_identical_strategy_captures_normalized_native_patch(self):
        _, receipt, _ = self.perform(status="identical", strategy="skip-merge-identical")
        self.assertEqual(receipt["selection"], "identical_normalized")

    def test_successful_fallback_captures_only_tested_lead_patch(self):
        _, receipt, _ = self.perform(status="conflicts", strategy="solo-agent1", filename="patch1.patch")
        self.assertEqual(receipt["selection"], "native_lead_fallback")
        self.assertEqual([c["native_patch_name"] for c in receipt["calls"]], ["patch1.patch"] * 2)

    def test_failed_fallback_does_not_invent_deliverable(self):
        def invoke():
            self.native._run_tests(self.sandbox, "tests1.patch", "patch1.patch", self.task.base)
            return {"both_passed": False, "error": None, "merge": {"status": "conflicts", "strategy": "naive"},
                    "feature1": {"passed": False, "test_output": ""}, "feature2": {"passed": False, "test_output": ""}}
        raw = evaluator.capture_evaluation(self.native, invoke, "strongshare", self.task, self.patches, self.attempt)
        receipt = json.loads((self.attempt / "evaluated_delivery.json").read_text())
        self.assertEqual(receipt["selection"], "no_deliverable_conflicts")
        self.assertIsNone(receipt["delivered_patch"])
        self.assertFalse((self.attempt / "delivered.patch").exists())
        evaluator.validate_delivery(self.attempt, {"evaluated_delivery": evaluator.delivery_summary(self.attempt)}, raw,
                                    "strongshare", self.task, self.patches)

    def test_clean_official_failure_still_exports_its_tested_delivery(self):
        raw, receipt, _ = self.perform(fail=True)
        self.assertFalse(raw["both_passed"])
        self.assertEqual(receipt["selection"], "native_naive_merge")
        self.assertTrue((self.attempt / "delivered.patch").is_file())

    def test_consolidate_captures_native_solo_patch_including_legal_empty(self):
        self.data = b""
        _, receipt, _ = self.perform(condition="consolidate", filename="solo.patch")
        self.assertEqual(receipt["selection"], "single_patch")
        self.assertEqual(receipt["delivered_patch"]["bytes"], 0)

    def test_two_features_receiving_different_bytes_is_rejected(self):
        def invoke():
            outcomes = []
            for number in (1, 2):
                self.data = str(number).encode()
                outcomes.append(self.native._run_tests(self.sandbox, f"tests{number}.patch", "merged.patch", self.task.base))
            return {"both_passed": True, "merge": {"status": "clean", "strategy": "naive"},
                    **{f"feature{i}": {"passed": True, "test_output": out["output"]} for i, out in enumerate(outcomes, 1)}}
        with self.assertRaisesRegex(ValueError, "identical patch bytes"):
            evaluator.capture_evaluation(self.native, invoke, "strongshare", self.task, self.patches, self.attempt)
        self.assertIs(self.native._run_tests, self.original_run_tests)

    def test_native_exception_restores_original_hook(self):
        def invoke():
            raise RuntimeError("native failure")
        with self.assertRaisesRegex(RuntimeError, "native failure"):
            evaluator.capture_evaluation(self.native, invoke, "strongshare", self.task, self.patches, self.attempt)
        self.assertIs(self.native._run_tests, self.original_run_tests)

    def test_wrong_base_rejected_before_native_test_or_read(self):
        def invoke():
            return self.native._run_tests(self.sandbox, "tests1.patch", "merged.patch", "wrong")
        with self.assertRaisesRegex(ValueError, "task base"):
            evaluator.capture_evaluation(self.native, invoke, "strongshare", self.task, self.patches, self.attempt)
        self.sandbox.exec.assert_not_called()

    def test_cached_capture_rejects_selected_or_per_call_patch_tamper(self):
        raw, _, summary = self.perform()
        for name in ("delivered.patch", "evaluated_patches/call_001.patch"):
            path = self.attempt / name
            original = path.read_bytes()
            path.write_bytes(b"modified")
            with self.assertRaises(ValueError):
                evaluator.validate_delivery(self.attempt, summary, raw, "strongshare", self.task, self.patches)
            path.write_bytes(original)

    def test_cached_capture_rejects_manifest_or_native_result_tamper(self):
        raw, receipt, summary = self.perform()
        manifest = self.attempt / "evaluated_delivery.json"
        before = manifest.read_bytes()
        receipt["selection"] = "native_lead_fallback"
        manifest.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            evaluator.validate_delivery(self.attempt, summary, raw, "strongshare", self.task, self.patches)
        manifest.write_bytes(before)
        tampered = copy.deepcopy(raw)
        tampered["both_passed"] = False
        with self.assertRaisesRegex(ValueError, "does not describe"):
            evaluator.validate_delivery(self.attempt, summary, tampered, "strongshare", self.task, self.patches)

    def test_cached_score_validation_calls_original_then_capture_validation(self):
        raw, _, summary = self.perform()
        summary["attempt_directory"] = str(self.attempt)
        (self.attempt / "official_evaluation.json").write_text(json.dumps(raw))
        phase = self.attempt / "strongshare"
        module = SimpleNamespace(patch_info=mock.Mock(return_value=self.patches))
        with mock.patch.object(evaluator.original, "resume_completed", return_value=summary) as verify:
            self.assertEqual(evaluator.resume_completed(module, phase, self.task, {}), summary)
            verify.assert_called_once_with(module, phase, self.task, {})

    def test_new_adapter_source_is_bound_and_worker_child_returns_to_new_cli(self):
        module = evaluator.make_evaluator("strongshare")
        self.assertEqual(Path(module.__file__).name, "luna_collaboration_evaluator.py")
        with mock.patch.object(evaluator.original, "source_manifest", return_value={"old.py": "oldhash"}):
            sources = module.source_manifest()
        self.assertEqual(sources["old.py"], "oldhash")
        self.assertEqual(len(sources["luna_collaboration_evaluator.py"]), 64)
        with self.assertRaises(ValueError):
            evaluator.make_evaluator("solo")


if __name__ == "__main__":
    unittest.main()
