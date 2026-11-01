"""Offline audit guards; no live artifacts, Docker, or model calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import audit_luna_timing_pillow as audit
import pillow_metadata_probe_v1 as probe


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.suite = Path(temporary.name).resolve() / "luna_timing_v1"

    def complete(self):
        jobs = [j for task in audit.TASKS for j in audit.expected_jobs(task)]
        protocol = {"jobs": jobs, "model": "gpt-6-luna", "offline_selftest": False,
                    "source_manifest": {}}
        summary = {"pipeline_completed": True, "planned_jobs": 16, "rows": copy.deepcopy(jobs),
                   "skipped_jobs": [], "protocol_sha256": audit.digest(protocol)}
        status = {"status": "completed", "completed_jobs": 16, "skipped_jobs": 0, "planned_jobs": 16}
        for name, value in (("protocol.json", protocol), ("summary.json", summary), ("status.json", status)):
            write(self.suite / name, value)
        return protocol, summary, status

    def test_checklist_has_eight_distinct_flat_child_paths(self):
        plan = audit.checklist(self.suite)
        self.assertEqual(len(plan["rows"]), 8)
        self.assertEqual(len({r["phase_directory"] for r in plan["rows"]}), 8)
        for row in plan["rows"]:
            phase = Path(row["phase_directory"])
            self.assertEqual(phase.parts[-5], "luna_timing_v1_" + row["timing"])
            self.assertEqual(phase.parts[-3], "jobs")
        self.assertFalse(self.suite.exists())

    def test_completed_gate_accepts_exact_job_set(self):
        protocol, summary, _ = self.complete()
        self.assertEqual(audit.completed_suite(self.suite), (protocol, summary))

    def test_gate_rejects_running_status_even_if_summaries_complete(self):
        _, _, status = self.complete()
        status["status"] = "running"
        write(self.suite / "status.json", status)
        with self.assertRaisesRegex(ValueError, "All 16"):
            audit.completed_suite(self.suite)

    def test_gate_rejects_duplicate_job_replacing_missing_job(self):
        _, summary, _ = self.complete()
        summary["rows"][-1] = summary["rows"][0]
        write(self.suite / "summary.json", summary)
        with self.assertRaisesRegex(ValueError, "All 16"):
            audit.completed_suite(self.suite)

    def test_gate_accepts_terminal_skips_without_fabricating_deliveries(self):
        protocol, summary, status = self.complete()
        summary["skipped_jobs"] = [dict(summary["rows"].pop(), reason="checkpoint_not_reached")]
        status.update(completed_jobs=15, skipped_jobs=1)
        write(self.suite / "summary.json", summary)
        write(self.suite / "status.json", status)
        self.assertEqual(audit.completed_suite(self.suite), (protocol, summary))

    def test_receipt_detects_protected_file_tampering(self):
        phase = self.suite / "job/consolidate"
        paths = ("task_config.json", "checkpoint.json", "checkpoint_provenance.json", "shared_git.tar",
                 "consolidate/handoff_packet.json", "consolidate/status.json", "consolidate/final.patch")
        hashes = {}
        for name in paths:
            write(phase.parent / name, {"fixture": name})
            hashes[name] = audit.info(phase.parent / name)["sha256"]
        write(phase / "execution_receipt.json", {
            "protected_files_sha256": {k: v for k, v in hashes.items() if k != "consolidate/final.patch"},
            "patch_sha256": {"final.patch": hashes["consolidate/final.patch"]}})
        audit.verify_execution_hashes(phase)
        write(phase.parent / "task_config.json", {"tampered": True})
        with self.assertRaisesRegex(ValueError, "Execution evidence changed"):
            audit.verify_execution_hashes(phase)

    def test_null_delivery_is_never_replaced_with_agent_patch(self):
        rows = [{**job, "status": "no_deliverable", "delivery": {"delivered_patch": None}}
                for job in audit.expected_jobs()]
        fixture = {"rows": rows, "protocol": {"sha256": "a"}, "summary": {"sha256": "b"}}
        with mock.patch.object(audit, "verified_inputs", side_effect=lambda _: copy.deepcopy(fixture)), \
                mock.patch.object(audit, "check_probe_sources"), \
                mock.patch.object(probe, "evaluate_patch", side_effect=AssertionError("No Docker expected")) as called:
            result = audit.run_probes(self.suite)
        called.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(r["supplemental_status"] == "no_deliverable" for r in result["rows"]))
        self.assertTrue(all("tests_passed" not in r for r in result["rows"]))


if __name__ == "__main__":
    unittest.main()
