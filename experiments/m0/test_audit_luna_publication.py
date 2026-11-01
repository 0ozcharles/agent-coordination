"""Offline checks of derived audit logic; no Docker or provider calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audit_luna_publication as a
from publication_guard import reminder


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def request(n, cost, excluded=False, phase="solo:solo"):
    return {"run_id": "fixture", "request_number": n, "phase": phase,
            "actual_cost_usd": cost, "excluded_from_baseline": excluded,
            "prompt_tokens": 10, "completion_tokens": 2, "status": "completed"}


class PublicationAuditTests(unittest.TestCase):
    def test_all_attempts_include_archived_unknown_without_duplicating_saved_copies(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d).resolve()
            rows = [request(1, .2, True), request(2, None, True), request(3, .3)]
            write(run / "usage_ledger.json", {"requests": rows})
            write(run / "failed_attempts/old/usage_ledger.json", {"requests": rows[:2]})
            result, raw = a.ledger_audit(run, "solo")
            self.assertEqual(result["all_attempts"]["requests"], 3)
            self.assertEqual(result["all_attempts"]["known_cost_usd"], .5)
            self.assertEqual(result["all_attempts"]["unknown_usage_requests"], 1)
            self.assertEqual(result["accepted_attempt"]["known_cost_usd"], .3)
            self.assertEqual(result["archived_attempts"]["requests"], 2)
            self.assertEqual(raw, rows)

    def test_prefix_requests_cannot_be_rebilled_in_job_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d).resolve() / "usage_ledger.json", {"requests": [request(1, .1, phase="prefix:agent1")]})
            with self.assertRaisesRegex(ValueError, "another phase"):
                a.ledger_audit(Path(d).resolve(), "integrator")

    def test_duplicate_physical_request_is_not_silently_double_counted(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d).resolve() / "usage_ledger.json", {"requests": [request(1, .1), request(1, .1)]})
            with self.assertRaisesRegex(ValueError, "Duplicate request"):
                a.ledger_audit(Path(d).resolve(), "solo")

    def guard_fixture(self, phase):
        guard = {"automatic_publication": False, "deadline_extended": False,
                 "started_monotonic": 10, "deadline_monotonic": 100,
                 "execution_seconds": 90, "counts": {"solo": 1},
                 "events": [{"index": 1, "agent_id": "solo", "nudged": True,
                             "nudge_count": 1, "reason": "unpublished_local_work",
                             "publication_state": {"pr_open": False, "remote_nonempty": False,
                                                   "dirty": True, "local_ahead": 0}}]}
        write(phase / "publication_guard.json", guard)
        write(phase / "solo.traj.json", {"messages": [
            {"role": "user", "content": "original task"},
            {"role": "user", "content": reminder("solo", True)}]})
        return guard

    def test_guard_event_matches_exact_reminder_in_trajectory(self):
        with tempfile.TemporaryDirectory() as d:
            phase = Path(d).resolve() / "solo"
            guard = self.guard_fixture(phase)
            with patch("publication_workers.read_guard", return_value=guard):
                result = a.guard_audit(phase)
            self.assertEqual(result["counts"], {"solo": 1})
            self.assertEqual(result["traces"]["solo"]["reminder_message_indices_zero_based"], [1])

    def test_guard_missing_trace_reminder_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            phase = Path(d).resolve() / "solo"
            guard = self.guard_fixture(phase)
            write(phase / "solo.traj.json", {"messages": []})
            with patch("publication_workers.read_guard", return_value=guard):
                with self.assertRaisesRegex(ValueError, "exact user reminder"):
                    a.guard_audit(phase)

    def test_guard_count_cannot_exceed_observed_events(self):
        with tempfile.TemporaryDirectory() as d:
            phase = Path(d).resolve() / "solo"
            guard = self.guard_fixture(phase)
            guard["counts"]["solo"] = 2
            with patch("publication_workers.read_guard", return_value=guard):
                with self.assertRaisesRegex(ValueError, "summary counts"):
                    a.guard_audit(phase)

    def test_pending_scores_never_invoke_completed_native_verifier(self):
        with tempfile.TemporaryDirectory() as d:
            suite = Path(d).resolve()
            jobs = [{"task": a.TASK_KEY, "condition": condition, "repeat": repeat}
                    for repeat in (1, 2) for condition in a.CONDITIONS]
            protocol = {"offline_selftest": False, "jobs": jobs,
                        "checkpoint_reference": {"record": {"all_attempt_usage": a.stats([])}}}
            write(suite / "protocol.json", protocol)
            with patch.object(a, "validate_suite", return_value=suite), \
                    patch.object(a, "source_audit", return_value={"verified": True}), \
                    patch.object(a, "audit_completed") as completed:
                result = a.build_audit(suite)
            completed.assert_not_called()
            self.assertEqual(result["status"], "partial_pending")
            self.assertFalse(result["complete"])
            self.assertTrue(result["integrity_verified"])
            self.assertEqual(len(result["rows"]), 6)
            self.assertTrue(all(r["official_success"] is None for r in result["rows"]))
            self.assertTrue(all(g["planned_n"] == 2 and g["completed_verified_n"] == 0 for g in result["groups"]))

    def test_empty_patch_error_is_distinct_from_test_assertion_failure(self):
        native = {"feature1": {"passed": False, "test_output": "error: No valid patches in input"},
                  "feature2": {"passed": False, "test_output": "FAILED tests/test_editor.py::test_cwd - AssertionError\n1 failed, 62 passed"}}
        features = a.feature_observations(native, (4, 10))
        self.assertEqual(features["4"]["observed_category"], "patch_apply_failure_observed")
        self.assertEqual(features["10"]["observed_category"], "official_test_failure_observed")
        self.assertEqual(features["10"]["failed_test_lines"], ["tests/test_editor.py::test_cwd - AssertionError"])


if __name__ == "__main__":
    unittest.main()
