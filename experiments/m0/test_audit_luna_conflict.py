"""Offline audit state tests: pending is never successful or silently scored."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audit_luna_conflict as a
import run_luna_conflict as supervisor


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ConflictAuditTests(unittest.TestCase):
    def fixture(self, directory):
        suite = Path(directory).resolve()
        protocol = {"offline_selftest": False, "jobs": supervisor.schedule(False)}
        write(suite / "protocol.json", protocol)
        return suite

    def audit(self, suite, **patches):
        with patch.object(a, "validate_suite", return_value=suite), \
                patch.object(a, "sources", return_value={"verified": True}):
            return a.build_audit(suite)

    def test_pending_collection_does_not_open_trajectory_or_claim_score(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.fixture(d)
            with patch.object(a, "audit_collection") as collect, patch.object(a, "audit_branch") as branch:
                result = self.audit(suite)
            collect.assert_not_called(); branch.assert_not_called()
            self.assertEqual(result["status"], "partial_pending")
            self.assertEqual(result["completed_executions"], 0)
            self.assertEqual(len(result["rows"]), 8)
            self.assertTrue(all(row["official_success"] is None for row in result["rows"]))

    def test_status_before_receipt_is_pending_not_false_integrity_failure(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.fixture(d)
            for key in a.TASK_KEYS:
                write(suite / key / "collection/strongshare/status.json", {"status": "completed"})
            with patch.object(a, "audit_collection") as collect:
                result = self.audit(suite)
            collect.assert_not_called()
            self.assertEqual(result["status"], "partial_pending")
            self.assertEqual(result["errors"], [])

    def test_unavailable_tasks_do_not_become_eight_failed_scores(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.fixture(d)
            for key in a.TASK_KEYS:
                run = suite / key / "collection"
                write(run / "strongshare/status.json", {"status": "no_conflict"})
                write(run / "collection_receipt.json", {})
            def collected(run, task, protocol, ledger):
                return {"task": task.key, "state": "no_conflict", "eligible": False,
                        "reason": "agent_terminal_at_paired_boundary", "ledger": ledger}
            with patch.object(a, "audit_collection", side_effect=collected), patch.object(a, "audit_branch") as branch:
                result = self.audit(suite)
            branch.assert_not_called()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["eligible_checkpoints"], 0)
            self.assertEqual(result["unavailable_branches"], 8)
            self.assertEqual(result["completed_executions"], 0)
            self.assertTrue(all(row["official_success"] is None for row in result["rows"]))
            self.assertTrue(all(group["completed_verified_n"] == 0 for group in result["groups"]))

    def test_actual_collection_spend_includes_unknown_without_archived_double_count(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.fixture(d)
            run = suite / a.TASK_KEYS[0] / "collection"
            rows = [{"run_id": "collection", "request_number": i, "model": "gpt-6-luna",
                     "phase": "strongshare:agent1", "actual_cost_usd": cost,
                     "excluded_from_baseline": i == 1, "status": "completed"}
                    for i, cost in enumerate((.1, None, .2), 1)]
            write(run / "usage_ledger.json", {"requests": rows})
            write(run / "failed_attempts/old/usage_ledger.json", {"requests": rows[:1]})
            result = self.audit(suite)
            self.assertEqual(result["actual_new_usage"]["requests"], 3)
            self.assertAlmostEqual(result["actual_new_usage"]["known_cost_usd"], .3)
            self.assertEqual(result["actual_new_usage"]["unknown_usage_requests"], 1)
            self.assertFalse(result["historical_prefix_rebilled"])

    def test_offline_mode_rejects_any_physical_provider_request(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.fixture(d)
            write(suite / "protocol.json", {"offline_selftest": True, "jobs": supervisor.schedule(True)})
            write(suite / a.TASK_KEYS[0] / "collection/usage_ledger.json", {"requests": [{
                "run_id": "collection", "request_number": 1, "phase": "strongshare:agent1",
                "model": "gpt-6-luna", "actual_cost_usd": .01}]})
            result = self.audit(suite)
            self.assertEqual(result["status"], "integrity_error")
            self.assertTrue(any("Offline engineering proof" in item for item in result["errors"]))
            self.assertEqual(result["actual_new_usage"]["requests"], 1)
            self.assertEqual(result["actual_new_usage"]["known_cost_usd"], .01)


if __name__ == "__main__":
    unittest.main()
