"""Fixed-authority scoring and sidecar integrity, without Docker or model calls."""
import base64
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import luna_integrator_evaluator as evaluator
from task_specs import TaskSpec
import task_specs


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class IntegratorEvaluatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        self.task = TaskSpec("llama_index17070_f1_f3", "fixture_repo", 17070, (1, 3),
                             "fixture:task17070", "a" * 40, ("src/pkg",))
        for patcher in (
            mock.patch.object(evaluator, "RUNS", self.runs),
            mock.patch.object(evaluator, "get_task", return_value=self.task),
            mock.patch.object(evaluator, "source_manifest", return_value={"evaluator.py": "b" * 64}),
            mock.patch.object(evaluator, "verify_upstream"),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        write(self.task.image_record_path, {"Id": "sha256:" + "c" * 64, "Architecture": "arm64",
              "expected_base": self.task.base, "image": self.task.image})
        self.original = b"agent1 complete implementation\n" + b"x" * 7000 + b"\nTEST-ONLY\n"
        self.filtered = self.original.replace(b"TEST-ONLY\n", b"")

    def phase(self, diagnostic=False, empty=False):
        condition = "strongshare" if diagnostic else "integrator"
        phase = self.runs / "luna_integrator_v1_early" / self.task.key / "jobs/job" / condition
        phase.mkdir(parents=True)
        config = {"task_key": self.task.key, "task": self.task.as_dict(), "condition": condition,
                  "condition_version": "source_handoff_diff_v1" if diagnostic else evaluator.VARIANT,
                  "submission_policy": "native_cooperative_merge" if diagnostic else "authoritative_agent1_only", "offline_selftest": True,
                  "protocol_sha256": "d" * 64}
        write(phase.parent / "task_config.json", config)
        write(phase / "status.json", {"status": "completed", "task": self.task.as_dict(), "offline_selftest": True})
        for name in ("checkpoint.json", "checkpoint_provenance.json", "shared_git.tar"):
            write(phase.parent / name, {"fixture": name})
        for name in ("source_manifest.json", "restore_proof.json", "handoff_packet.json", "handoff_packet.txt",
                     "handoff_delivery.json", "first_request_agent1.json", "first_request_agent2.json"):
            write(phase / name, {"fixture": name})
        names = ("agent1.patch", "agent3.patch") if diagnostic else ("agent1.patch", "agent2.patch")
        (phase / names[0]).write_bytes(b"" if empty else self.original)
        (phase / names[1]).write_bytes(b"peer implementation must never be selected\n")
        hashes = {name: evaluator.file_info(phase / name)["sha256"] for name in names}
        protected = {str(p.relative_to(phase.parent)): evaluator.file_info(p)["sha256"]
                     for p in phase.parent.rglob("*") if p.is_file()}
        write(phase / "execution_receipt.json", {"patch_sha256": hashes, "protected_files_sha256": protected})
        if not diagnostic:
            write(phase / "final_delivery.json", {"schema": "predesignated_integrator_delivery_v1",
                  "authoritative_agent": "agent1", "scored_patch": "agent1.patch", "peer_patch": "agent2.patch",
                  "selection": "predesignated_agent1", "automatic_merge": False, "posthoc_selection": False,
                  "active_agents": ["agent1", "agent2"], "patch_sha256": hashes[names[0]],
                  "peer_patch_sha256": hashes[names[1]], "publications": {"agent1": "fixture", "agent2": "fixture"}})
        else:
            write(phase / "evaluation_summary.json", {"unchanged_native_primary": True})
            write(phase / "evaluation.json", {"both_passed": True, "merge": {"strategy": "naive"}})
        return phase, config

    def capture(self, phase, diagnostic=False, empty=False, fail=False, substitute=False):
        patches = evaluator.patch_info(phase, self.task, diagnostic)
        attempt = evaluator.output_dir(phase, diagnostic) / "evaluation_attempts/fixture"
        attempt.mkdir(parents=True)
        expected = b"" if empty else self.filtered
        sandbox = SimpleNamespace(exec=mock.Mock(return_value=SimpleNamespace(returncode=0,
            stdout_read=lambda: base64.b64encode(b"peer wrong patch" if substitute else expected).decode())))
        native = SimpleNamespace(_load_patch=lambda path: Path(path).read_text(),
            _filter_test_files=lambda text: text.replace("TEST-ONLY\n", ""),
            _run_tests=mock.Mock(side_effect=lambda *a: {"passed": not fail, "exit_code": 1 if fail else 0,
                "output": "1 failed" if fail else "5 passed"}),
            test_merged=mock.Mock(side_effect=AssertionError("Never merge")))
        original = native._run_tests

        def test_solo(**kwargs):
            self.assertEqual(kwargs["patch"], phase / "agent1.patch")
            self.assertEqual((kwargs["feature1_id"], kwargs["feature2_id"]), self.task.features)
            self.assertEqual(kwargs["timeout"], evaluator.EXEC_TIMEOUT_SECONDS)
            results = [native._run_tests(sandbox, f"tests{i}.patch", "solo.patch", self.task.base) for i in (1, 2)]
            return {"setting": "solo", "both_passed": not fail, "error": None,
                    **{f"feature{i}": {"passed": r["passed"], "test_output": r["output"]}
                       for i, r in enumerate(results, 1)}}

        native.test_solo = mock.Mock(side_effect=test_solo)
        try:
            result = evaluator.capture_evaluation(native, phase, attempt, self.task, patches, diagnostic)
        finally:
            self.assertIs(native._run_tests, original)
            native.test_merged.assert_not_called()
        native.test_solo.assert_called_once()
        self.assertEqual(original.call_count, 2)
        return attempt, result, patches

    def cache(self, phase, config, diagnostic=False):
        attempt, result, patches = self.capture(phase, diagnostic)
        manifest = evaluator.input_manifest(phase, self.task, patches, diagnostic)
        write(attempt / "evaluation_input.json", {"task": self.task.as_dict(), "protocol_sha256": config["protocol_sha256"],
              "submission_policy": evaluator.policy(diagnostic), "primary_outcome": not diagnostic, "input_manifest": manifest})
        write(attempt / "integrity.json", {"unchanged": True, "before": manifest, "after": manifest})
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json",
                     evaluator.output_dir(phase, diagnostic) / "evaluation.json"):
            write(path, result)
        summary = evaluator.summarize(result, "passed", phase, attempt, 1.2, self.task, config, diagnostic)
        write(attempt / "evaluation_summary.json", summary)
        write(evaluator.output_dir(phase, diagnostic) / "evaluation_summary.json", summary)
        return evaluator.make_evaluator(phase.name, diagnostic=diagnostic), summary, attempt

    def test_exact_full_lead_capture_retains_peer_only_as_contribution(self):
        phase, config = self.phase()
        attempt, native, patches = self.capture(phase)
        self.assertGreater(len(self.filtered), 5000)
        self.assertEqual((attempt / "delivered.patch").read_bytes(), self.filtered)
        saved = evaluator.read(attempt / "evaluated_delivery.json")
        self.assertTrue(saved["contributions"]["agent1"]["graded"])
        self.assertFalse(saved["contributions"]["agent2"]["graded"])
        self.assertEqual(saved["input_patches"], patches)
        summary = evaluator.summarize(native, "passed", phase, attempt, 1, self.task, config)
        evaluator.validate_delivery(attempt, summary, native, phase, self.task, patches)
        self.assertEqual(summary["submission_policy"], "authoritative_agent1_only")
        self.assertFalse(summary["fresh_solo"])
        self.assertEqual(summary["active_agent_count"], 2)

    def test_empty_lead_is_evaluated_and_never_replaced_by_nonempty_peer(self):
        phase, _ = self.phase(empty=True)
        attempt, native, patches = self.capture(phase, empty=True, fail=True)
        self.assertFalse(native["both_passed"])
        self.assertEqual((attempt / "delivered.patch").read_bytes(), b"")
        self.assertTrue(patches["agent1.patch"]["empty"])
        self.assertFalse(patches["agent2.patch"]["empty"])

    def test_missing_lead_rejected_before_native_test(self):
        phase, _ = self.phase()
        (phase / "agent1.patch").unlink()
        with self.assertRaises(ValueError):
            evaluator.patch_info(phase, self.task)

    def test_record_order_cannot_select_peer_as_authoritative_patch(self):
        phase, _ = self.phase()
        actual = evaluator.patch_info(phase, self.task)
        with mock.patch.object(evaluator, "patch_info", return_value=dict(reversed(list(actual.items())))):
            attempt, _, _ = self.capture(phase)
        self.assertEqual((attempt / "delivered.patch").read_bytes(), self.filtered)
        receipt = evaluator.read(attempt / "evaluated_delivery.json")
        self.assertEqual(receipt["contributions"]["agent1"]["patch"]["path"], str(phase / "agent1.patch"))

    def test_peer_substitution_rejected_before_tests(self):
        phase, _ = self.phase()
        with self.assertRaisesRegex(ValueError, "not exactly the fixed agent1"):
            self.capture(phase, substitute=True)

    def test_declaration_cannot_change_final_owner_or_enable_merge(self):
        phase, _ = self.phase()
        path = phase / "final_delivery.json"
        original = evaluator.read(path)
        for key, value in (("authoritative_agent", "agent2"), ("automatic_merge", True), ("posthoc_selection", True),
                           ("active_agents", ["agent1"]), ("scored_patch", "agent2.patch")):
            write(path, {**original, key: value})
            with self.assertRaisesRegex(ValueError, "fixed agent1"):
                evaluator.patch_info(phase, self.task)

    def test_changed_peer_evidence_invalidates_cache_even_if_lead_unchanged(self):
        phase, config = self.phase()
        module, summary, _ = self.cache(phase, config)
        self.assertEqual(evaluator.resume_completed(module, phase, self.task, config), summary)
        (phase / "agent2.patch").write_text("tampered peer evidence")
        with self.assertRaisesRegex(ValueError, "Both saved contributions"):
            evaluator.resume_completed(module, phase, self.task, config)

    def test_policy_tamper_rejected_not_reused_as_other_score(self):
        phase, config = self.phase()
        module, _, attempt = self.cache(phase, config)
        path = attempt / "evaluation_input.json"
        entry = evaluator.read(path)
        entry["submission_policy"] = "diagnostic_agent1_only"
        write(path, entry)
        with self.assertRaisesRegex(ValueError, "policy or sources"):
            evaluator.resume_completed(module, phase, self.task, config)

    def test_delivery_capture_tampering_rejected(self):
        phase, config = self.phase()
        module, _, attempt = self.cache(phase, config)
        (attempt / "delivered.patch").write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "actually tested"):
            evaluator.resume_completed(module, phase, self.task, config)

    def test_diagnostic_uses_agent_id_mapping_and_preserves_native_primary(self):
        phase, config = self.phase(diagnostic=True)
        before = {name: (phase / name).read_bytes() for name in ("evaluation.json", "evaluation_summary.json")}
        module, summary, _ = self.cache(phase, config, diagnostic=True)
        self.assertEqual(evaluator.resume_completed(module, phase, self.task, config), summary)
        self.assertEqual(summary["condition"], "strongshare")
        self.assertFalse(summary["primary_outcome"])
        self.assertEqual(summary["submission_policy"], "diagnostic_agent1_only")
        self.assertEqual(summary["contributions"]["agent2"]["patch"]["path"], str(phase / "agent3.patch"))
        for name, data in before.items():
            self.assertEqual((phase / name).read_bytes(), data)

    def test_diagnostic_cache_is_bound_to_original_native_primary(self):
        phase, config = self.phase(diagnostic=True)
        module, _, _ = self.cache(phase, config, diagnostic=True)
        write(phase / "evaluation_summary.json", {"tampered_primary": True})
        with self.assertRaisesRegex(ValueError, "inputs, policy or sources"):
            evaluator.resume_completed(module, phase, self.task, config)

    def test_primary_and_diagnostic_paths_cannot_be_interchanged(self):
        phase, _ = self.phase()
        self.assertEqual(evaluator.validate_run_dir(phase), phase)
        with self.assertRaises(ValueError):
            evaluator.validate_run_dir(phase, diagnostic=True)
        with self.assertRaises(ValueError):
            evaluator.make_evaluator("strongshare")
        with self.assertRaises(ValueError):
            evaluator.make_evaluator("integrator", diagnostic=True)

    def test_load_calls_readonly_worker_verifier(self):
        phase, config = self.phase()
        verifier = mock.Mock(side_effect=lambda p, task: evaluator.read(p / "execution_receipt.json"))
        with mock.patch.dict("sys.modules", {"luna_integrator_worker": SimpleNamespace(verify_worker_artifacts=verifier)}):
            self.assertEqual(evaluator.load_task(phase), (self.task, config))
        verifier.assert_called_once_with(phase, self.task)

    def test_diagnostic_load_preserves_original_strongshare_variant_and_policy(self):
        phase, config = self.phase(diagnostic=True)
        import luna_collaboration_evaluator as original
        module = SimpleNamespace(load_task=mock.Mock(return_value=(self.task, config)))
        with mock.patch.object(original, "make_evaluator", return_value=module), \
                mock.patch.object(original, "resume_completed", return_value={"primary": "unchanged"}):
            self.assertEqual(evaluator.load_task(phase, True), (self.task, config))
            changed = {**config, "condition_version": evaluator.VARIANT}
            write(phase.parent / "task_config.json", changed)
            with self.assertRaisesRegex(ValueError, "identity is inconsistent"):
                evaluator.load_task(phase, True)

    def test_integrator_cannot_use_native_merge_submission_policy(self):
        phase, config = self.phase()
        write(phase.parent / "task_config.json", {**config, "submission_policy": "native_cooperative_merge"})
        with self.assertRaisesRegex(ValueError, "identity is inconsistent"):
            evaluator.load_task(phase)

    def test_diagnostic_supervisor_error_cannot_overwrite_primary_results(self):
        phase, _ = self.phase(diagnostic=True)
        before = {name: (phase / name).read_bytes() for name in ("evaluation.json", "evaluation_summary.json")}
        with mock.patch.object(evaluator, "load_task", side_effect=ValueError("invalid receipt")), \
                mock.patch.object(evaluator.subprocess, "Popen") as popen, \
                mock.patch("builtins.print"):
            self.assertEqual(evaluator.supervise(phase, diagnostic=True), 2)
        popen.assert_not_called()
        for name, data in before.items():
            self.assertEqual((phase / name).read_bytes(), data)
        failed = evaluator.read(phase / evaluator.DIAGNOSTIC_DIR / "evaluation_summary.json")
        self.assertEqual(failed["status"], "error")
        self.assertFalse(failed["primary_outcome"])
        self.assertIn("invalid receipt", failed["error"])


if __name__ == "__main__":
    unittest.main()
