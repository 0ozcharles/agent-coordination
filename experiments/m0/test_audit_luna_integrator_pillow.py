"""Offline guards for the independent primary-delivery supplemental audit."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import audit_luna_integrator_pillow as audit
import pillow_metadata_probe_v1 as probe


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class IntegratorAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.suite = Path(temporary.name).resolve() / "luna_integrator_v1"

    def fixture(self):
        jobs = [job for task in audit.TASKS for job in audit.expected_jobs(task)]
        protocol = {"version": "luna_integrator_v1", "jobs": jobs, "model": "gpt-6-luna",
                    "offline_selftest": False, "source_manifest": {}}
        diagnostics = [{**{key: row[key] for key in ("task", "timing", "repeat")},
                        "primary_outcome": False, "evaluation": {"evaluation_completed": True}}
                       for row in jobs if row["condition"] == "strongshare"]
        summary = {"pipeline_completed": True, "paid_execution_completed": True, "planned_jobs": 16,
                   "rows": copy.deepcopy(jobs), "skipped_jobs": [], "protocol_sha256": audit.digest(protocol),
                   "strongshare_lead_only_diagnostics": diagnostics}
        status = {"status": "completed", "completed_jobs": 16, "skipped_jobs": 0,
                  "planned_jobs": 16, "completed_diagnostics": 8}
        for name, value in (("protocol.json", protocol), ("summary.json", summary), ("status.json", status)):
            write(self.suite / name, value)
        return protocol, summary, status

    def test_plan_is_pure_and_has_only_eight_primary_paths(self):
        plan = audit.checklist(self.suite)
        self.assertEqual(len(plan["rows"]), 8)
        self.assertEqual(len({row["phase_directory"] for row in plan["rows"]}), 8)
        self.assertEqual({row["condition"] for row in plan["rows"]}, {"strongshare", "integrator"})
        self.assertTrue(all("lead_only_diagnostic" not in row["evaluation_summary"] for row in plan["rows"]))
        self.assertFalse(self.suite.exists())

    def test_complete_gate_requires_all_branches_and_diagnostics(self):
        protocol, summary, _ = self.fixture()
        self.assertEqual(audit.completed_suite(self.suite), (protocol, summary))
        summary["strongshare_lead_only_diagnostics"].pop()
        write(self.suite / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "prescribed completed-SS"):
            audit.completed_suite(self.suite)

    def test_gate_rejects_duplicate_diagnostic_even_with_count_eight(self):
        _, summary, _ = self.fixture()
        summary["strongshare_lead_only_diagnostics"][-1] = summary["strongshare_lead_only_diagnostics"][0]
        write(self.suite / "summary.json", summary)
        with self.assertRaises(ValueError):
            audit.completed_suite(self.suite)

    def test_gate_rejects_active_paid_or_diagnostic_work(self):
        _, summary, status = self.fixture()
        status["status"] = "running"
        write(self.suite / "status.json", status)
        with self.assertRaises(ValueError):
            audit.completed_suite(self.suite)
        status["status"] = "completed"
        write(self.suite / "status.json", status)
        summary["paid_execution_completed"] = False
        write(self.suite / "summary.json", summary)
        with self.assertRaises(ValueError):
            audit.completed_suite(self.suite)

    def primary(self, delivery):
        job = {"task": audit.TASK, "timing": "early", "repeat": 1, "condition": "strongshare"}
        phase = audit.phase_path(self.suite, job)
        cp = {"fixture": "checkpoint"}
        write(phase.parent / "checkpoint.json", cp)
        protocol = {"checkpoint_references": {audit.TASK: {"early": {"record": {"checkpoint_sha256": audit.digest(cp)}}}},
                    "condition_versions": {"strongshare": "source_handoff_diff_v1"}}
        config = {"timing": "early", "repeat": 1, "integrator_suite": str(self.suite), "offline_selftest": False,
                  "experiment_version": "luna_integrator_v1", "submission_policy": "native_cooperative_merge",
                  "condition_version": "source_handoff_diff_v1", "protocol_sha256": audit.digest(protocol),
                  "checkpoint_sha256": audit.digest(cp)}
        score = {"both_passed": False, "feature_results": {}, "evaluated_delivery": delivery,
                 "merge_status": "conflicts", "merge_strategy": "naive", "lead_patch_fallback_used": False}
        write(phase / "evaluation_summary.json", score)
        # A separately passing lead diagnostic exists. It is not an eligible primary.
        write(phase / "lead_only_diagnostic/evaluation_summary.json", {
            "both_passed": True, "evaluated_delivery": {"delivered_patch": {"path": "/never/select/diagnostic.patch"}}})
        row = {**job, "run_directory": str(phase.parent), "checkpoint_sha256": audit.digest(cp),
               "evaluation": score, "official_success": False}
        with mock.patch.object(audit, "load_primary", return_value=(SimpleNamespace(key=audit.TASK), config, score)), \
                mock.patch.object(audit, "verify_execution_hashes", return_value={"verified": True}):
            return audit.verify_primary(self.suite, job, protocol, row)

    def test_missing_primary_deliverable_does_not_select_passing_diagnostic(self):
        row = self.primary({"selection": "no_deliverable_conflicts", "delivered_patch": None})
        self.assertEqual(row["status"], "no_deliverable")
        self.assertIsNone(row["delivery"]["delivered_patch"])
        self.assertFalse(row["official_both_passed"])

    def test_primary_failed_native_delivery_kept_despite_better_diagnostic(self):
        patch = self.suite.parent / "native_delivered.patch"
        patch.write_text("complete actual primary bytes\n")
        record = audit.info(patch)
        row = self.primary({"selection": "native_naive_merge", "delivered_patch": record})
        self.assertEqual(row["delivery"]["delivered_patch"], record)
        self.assertEqual(row["status"], "ready")
        self.assertFalse(row["official_both_passed"])

    def test_probe_dispatch_never_uses_diagnostic_delivery_for_null_primary(self):
        fixture = {"rows": [{**job, "status": "no_deliverable", "delivery": {"delivered_patch": None}}
                            for job in audit.expected_jobs()], "protocol": {}, "summary": {}, "audit_sources": {},
                   "lead_only_diagnostics_verified_but_not_probed": [{"primary_outcome": False, "official_both_passed": True}]}
        with mock.patch.object(audit, "verified_inputs", side_effect=lambda _: copy.deepcopy(fixture)), \
                mock.patch.object(audit, "check_probe_sources"), \
                mock.patch.object(probe, "evaluate_patch", side_effect=AssertionError("No Docker")) as invoke:
            result = audit.run_probes(self.suite)
        invoke.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(row["supplemental_status"] == "no_deliverable" for row in result["rows"]))


if __name__ == "__main__":
    unittest.main()
