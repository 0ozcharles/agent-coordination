"""Offline regression checks for publication-study provenance and resumption.

These tests never instantiate an agent, run Docker, contact a provider, or read
credentials. Native subprocess boundaries are mocked, not the bookkeeping being
checked. Worker/evaluator guards have separate owners and tests.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import run_luna_publication as s
from budget_guard import write_json
from run_baselines import usage


def sample_usage(cost=0.0, requests=0, unknown=0):
    return {"requests": requests, "known_cost_usd": cost,
            "unknown_usage_requests": unknown, "prompt_tokens": 0,
            "completion_tokens": 0}


def fixture_protocol(offline=True):
    return {
        "offline_selftest": offline, "model": "offline-scripted-model" if offline else s.MODEL,
        "publication_guard_version": "test-guard-v1", "source_manifest": {},
        "interpretation": "fixture", "jobs": s.schedule(offline),
        "checkpoint_reference": {
            "directory": "/not-read/historical-prefix", "historical_files": {},
            "historical_usage_is_new_billable_usage": False,
            "record": {"checkpoint_sha256": "fixed-checkpoint", "elapsed_seconds": 16.0,
                       "all_attempt_usage": sample_usage(90.0, 8),
                       "accepted_usage": sample_usage(90.0, 8)},
            "preparation": {"elapsed_seconds": 0.25},
        },
    }


def request(phase, cost, excluded=False):
    return {"phase": phase + ":agent", "actual_cost_usd": cost,
            "prompt_tokens": 10, "completion_tokens": 2,
            "excluded_from_baseline": excluded,
            "status": "in_flight" if cost is None else "completed"}


class PublicationSupervisorTests(unittest.TestCase):
    def test_exact_schedule_and_separate_solo_paths(self):
        jobs = s.schedule(False)
        self.assertEqual([j["condition"] for j in jobs],
                         ["strongshare", "integrator", "solo", "solo", "integrator", "strongshare"])
        self.assertEqual(len({(j["task"], j["repeat"], j["condition"]) for j in jobs}), 6)
        self.assertEqual(len(s.schedule(True)), 3)
        for job in jobs:
            path = s.job_path(Path("/tmp/publication-test"), job)
            self.assertEqual(path.parent.name, "solo_jobs" if job["condition"] == "solo" else "jobs")
        for key, value in (("task", "other"), ("repeat", True), ("repeat", 3), ("condition", "oracle")):
            bad = {**jobs[0], key: value}
            with self.assertRaises(ValueError):
                s.job_path(Path("/tmp/publication-test"), bad)

    def test_allowance_deducts_old_prefix_once_but_solo_is_fresh(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "copy_checkpoint") as copy_cp:
            protocol = fixture_protocol()
            for condition in s.CONDITIONS:
                run = Path(d) / condition
                job = {"task": s.TASK_KEY, "repeat": 1, "condition": condition}
                config = s.configure_run(run, job, protocol)
                self.assertEqual(config["execution_seconds"], 1800 if condition == "solo" else 1783.75)
                self.assertEqual(config["publication_guard_version"], "test-guard-v1")
                if condition == "solo":
                    self.assertTrue(config["fresh_solo"])
                    self.assertNotIn("checkpoint_sha256", config)
                    self.assertNotIn("submission_policy", config)
                else:
                    self.assertEqual(config["checkpoint_sha256"], "fixed-checkpoint")
                    self.assertEqual(config["submission_policy"], "authoritative_agent1_only"
                                     if condition == "integrator" else "native_cooperative_merge")
            self.assertEqual(copy_cp.call_count, 2)

    def test_configuration_cannot_change_on_resume(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "copy_checkpoint"):
            run = Path(d)
            job = {"task": s.TASK_KEY, "repeat": 1, "condition": "integrator"}
            protocol = fixture_protocol()
            s.configure_run(run, job, protocol)
            changed = copy.deepcopy(protocol)
            changed["publication_guard_version"] = "different-guard"
            with self.assertRaises(RuntimeError):
                s.configure_run(run, job, changed)

    def test_historical_source_and_snapshot_drift_rejected(self):
        import run_luna_selector as old
        with tempfile.TemporaryDirectory() as d:
            root, origin = Path(d), Path(d) / "history"
            (origin / "source_snapshot").mkdir(parents=True)
            (root / "old.py").write_text("old\n")
            (origin / "source_snapshot/old.py").write_text("old\n")
            historical = {"old.py": s.file_hash(root / "old.py")}
            write_json(origin / "protocol.json", {"source_manifest": historical})
            for name in s.NEW_SOURCES:
                (root / name).write_text("new:" + name)
            with patch.object(s, "ROOT", root), patch.object(s, "ORIGIN", origin), \
                    patch.object(old, "source_manifest", return_value=historical) as current:
                manifest = s.source_manifest()
                self.assertEqual(set(manifest), {"old.py", *s.NEW_SOURCES})
                current.return_value = {"old.py": "drift"}
                with self.assertRaisesRegex(RuntimeError, "sources changed"):
                    s.source_manifest()
                current.return_value = historical
                (origin / "source_snapshot/old.py").write_text("tampered snapshot")
                with self.assertRaisesRegex(RuntimeError, "snapshot changed"):
                    s.source_manifest()

    def test_preflight_binds_both_new_sources_and_old_checkpoint(self):
        proof = {"passed": True, "model_api_calls": 0, "completed_jobs": 3,
                 "source_manifest": {"code": "fixed"}, "checkpoint_reference": {"cp": "fixed"}}
        guard_proof = {"passed": True, "model_api_calls": 0, "guard_source_sha256": "guard-hash"}
        def read(path, default=None):
            return guard_proof if path.name == "publication_guard_docker_validation.json" else proof
        with patch.object(s, "read_json", side_effect=read), \
                patch.object(s, "file_hash", return_value="guard-hash"), \
                patch.object(s, "source_manifest", return_value={"code": "fixed"}), \
                patch.object(s, "checkpoint_reference", return_value={"cp": "fixed"}):
            s.verify_preflight()
            proof["checkpoint_reference"] = {"cp": "changed"}
            with self.assertRaises(RuntimeError):
                s.verify_preflight()
            proof["checkpoint_reference"] = {"cp": "fixed"}
            proof["source_manifest"] = {"code": "changed"}
            with self.assertRaises(RuntimeError):
                s.verify_preflight()
            proof["source_manifest"] = {"code": "fixed"}
            guard_proof["guard_source_sha256"] = "stale-guard"
            with self.assertRaisesRegex(RuntimeError, "Exact-guard"):
                s.verify_preflight()

    def test_reused_packet_is_exact_and_changed_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            origin, suite = Path(d) / "history", Path(d) / "new-suite"
            old_decision = origin / s.TASK_KEY / "decision"
            old_decision.mkdir(parents=True)
            originals = {name: {"name": name, "checkpoint": "original", "nested": [1, 2]}
                         for name in ("packet.json", "decision.json", "preparation.json")}
            for name, value in originals.items():
                write_json(old_decision / name, value)
            original_bytes = {name: (old_decision / name).read_bytes() for name in originals}
            protocol = fixture_protocol()
            with patch.object(s, "ORIGIN", origin), patch.object(s, "verify_sources") as verify:
                s.copy_historical_packet(suite, protocol)
                verify.assert_called_once_with(protocol)
                for name, value in originals.items():
                    self.assertEqual(json.loads((suite / s.TASK_KEY / "decision" / name).read_text()), value)
                self.assertEqual(json.loads((suite / s.TASK_KEY / "historical_reference.json").read_text()),
                                 protocol["checkpoint_reference"])
                write_json(suite / s.TASK_KEY / "decision/packet.json", {"tampered": True})
                with self.assertRaisesRegex(RuntimeError, "Frozen configuration changed"):
                    s.copy_historical_packet(suite, protocol)
            self.assertEqual({name: (old_decision / name).read_bytes() for name in originals}, original_bytes)

    def test_run_rejects_source_or_historical_reference_drift(self):
        protocol = {"source_manifest": {"code": "fixed"}, "checkpoint_reference": {"cp": "fixed"}}
        with patch.object(s, "source_manifest", return_value={"code": "fixed"}) as sources, \
                patch.object(s, "checkpoint_reference", return_value={"cp": "fixed"}) as reference:
            s.verify_sources(protocol)
            reference.return_value = {"cp": "changed"}
            with self.assertRaisesRegex(RuntimeError, "Frozen publication study inputs changed"):
                s.verify_sources(protocol)
            reference.return_value = {"cp": "fixed"}
            sources.return_value = {"code": "changed"}
            with self.assertRaisesRegex(RuntimeError, "Frozen publication study inputs changed"):
                s.verify_sources(protocol)

    def test_completed_failed_execution_is_reused_without_model_call(self):
        # Completed feature failures must be just as immutable as successes.
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "integrator").mkdir()
            write_json(run / "integrator/status.json", {"status": "completed"})
            write_json(run / "integrator/evaluation_summary.json", {"evaluation_completed": True, "both_passed": False})
            with patch.object(s, "verify_execution") as verify, \
                    patch("run_baselines.cleanup_phase"), patch("run_repeats.run_command") as command:
                result = s.model_phase(run, "integrator", fixture_protocol(False), run, 0)
                self.assertEqual(result["status"], "completed")
                command.assert_not_called()
                verify.assert_called_once_with(run / "integrator", s.TASK, "integrator", freeze=True)

    def test_completed_receipt_failure_does_not_trigger_reexecution(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "solo").mkdir()
            write_json(run / "solo/status.json", {"status": "completed"})
            with patch.object(s, "verify_execution", side_effect=RuntimeError("receipt changed")), \
                    patch("run_baselines.cleanup_phase"), patch("run_repeats.run_command") as command:
                with self.assertRaisesRegex(RuntimeError, "receipt changed"):
                    s.model_phase(run, "solo", fixture_protocol(False), run, 0)
                command.assert_not_called()

    def test_incomplete_resume_archives_usage_and_uses_new_worker_entrypoint(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            phase = run / "solo"
            phase.mkdir()
            write_json(phase / "status.json", {"status": "error", "elapsed_seconds": 5})
            write_json(run / "task_config.json", {"execution_seconds": 1800})
            write_json(run / "usage_ledger.json", {"requests": [request("solo", 0.2), request("solo", None)]})

            def finish(command, log, timeout, lock_fd):
                self.assertEqual(Path(command[3]).name, "run_luna_publication.py")
                self.assertIn("--worker", command)
                self.assertEqual(timeout, 2100)
                write_json(phase / "status.json", {"status": "completed", "elapsed_seconds": 2})
                ledger = json.loads((run / "usage_ledger.json").read_text())
                ledger["requests"].append(request("solo", 0.3))
                write_json(run / "usage_ledger.json", ledger)
                return 0

            with patch.object(s, "verify_execution"), patch("run_baselines.cleanup_phase"), \
                    patch("run_repeats.run_command", side_effect=finish) as command:
                result = s.model_phase(run, "solo", fixture_protocol(), run, 0)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(command.call_count, 1)
            self.assertEqual(len(list((run / "failed_attempts").glob("solo_*"))), 1)
            self.assertEqual(usage(run, "solo")["known_cost_usd"], 0.3)
            self.assertEqual(usage(run)["known_cost_usd"], 0.5)
            self.assertEqual(usage(run)["unknown_usage_requests"], 1)
            ledger = json.loads((run / "usage_ledger.json").read_text())["requests"]
            self.assertTrue(all(x["excluded_from_baseline"] for x in ledger[:2]))
            self.assertEqual(ledger[1]["error_type"], "InterruptedAttemptUsageUnknown")

    def test_row_comparison_includes_history_but_new_spend_does_not(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(sys.modules, {"publication_workers": SimpleNamespace(guard_summary=lambda _: {"reminders": 1})}):
            suite, protocol = Path(d), fixture_protocol()
            job = {"task": s.TASK_KEY, "repeat": 1, "condition": "integrator"}
            run = s.job_path(suite, job)
            run.mkdir(parents=True)
            write_json(run / "task_config.json", {"execution_seconds": 1783.75})
            write_json(run / "usage_ledger.json", {"requests": [request("integrator", 0.2, True), request("integrator", None, True), request("integrator", 0.3)]})
            row = s.build_row(run, job, {"elapsed_seconds": 4}, {"both_passed": False}, protocol)
            self.assertFalse(row["official_success"])
            self.assertEqual(row["accepted_usage"]["known_cost_usd"], 0.3)
            self.assertEqual(row["all_attempt_usage"]["known_cost_usd"], 0.5)
            self.assertEqual(row["full_task_known_cost_usd"], 90.5)
            self.assertEqual(row["full_task_unknown_usage_requests"], 1)
            # Also include a pending job's costs, with no scored result yet.
            pending = s.job_path(suite, {"task": s.TASK_KEY, "repeat": 1, "condition": "solo"})
            pending.mkdir(parents=True)
            write_json(pending / "usage_ledger.json", {"requests": [request("solo", 0.4)]})
            summary = s.summary_for(suite, protocol, [row], [])
            self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 0.9)
            self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"], 1)
            self.assertEqual(summary["new_prefix_requests"], 0)
            self.assertFalse(summary["historical_prefix_rebilled"])
            self.assertFalse(summary["pipeline_completed"])

    def test_fresh_solo_comparison_has_no_historical_prefix(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(sys.modules, {"publication_workers": SimpleNamespace(guard_summary=lambda _: {})}):
            run = Path(d)
            write_json(run / "task_config.json", {"execution_seconds": 1800})
            write_json(run / "usage_ledger.json", {"requests": [request("solo", 0.3)]})
            row = s.build_row(run, {"task": s.TASK_KEY, "repeat": 1, "condition": "solo"},
                              {"elapsed_seconds": 4}, {"both_passed": True}, fixture_protocol())
            self.assertEqual(row["full_task_known_cost_usd"], 0.3)
            self.assertEqual(row["full_task_execution_seconds"], 4)
            self.assertEqual(row["historical_prefix_usage"]["requests"], 0)
            self.assertIsNone(row["checkpoint_sha256"])

    def test_scoring_failure_preserves_completed_execution(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            phase = run / "solo"
            phase.mkdir()
            write_json(phase / "status.json", {"status": "completed"})
            before = (phase / "status.json").read_bytes()
            with patch.object(s, "verify_execution", return_value={"receipt": "fixed"}), \
                    patch.object(s, "run_command", return_value=2), \
                    patch("evaluate_solo_smoke.evaluator_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "Scoring paused"):
                    s.evaluate(run, "solo", 0)
            self.assertEqual((phase / "status.json").read_bytes(), before)

    def test_completed_failed_score_is_accepted_without_best_of(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "solo").mkdir()
            score = {"evaluation_completed": True, "both_passed": False}
            write_json(run / "solo/evaluation_summary.json", score)
            with patch.object(s, "verify_execution", return_value={"receipt": "fixed"}), \
                    patch.object(s, "run_command", return_value=0), \
                    patch("evaluate_solo_smoke.evaluator_environment", return_value={}):
                self.assertEqual(s.evaluate(run, "solo", 0), score)

    def test_execution_mutation_during_scoring_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "solo").mkdir()
            write_json(run / "solo/evaluation_summary.json", {"evaluation_completed": True, "both_passed": True})
            with patch.object(s, "verify_execution", side_effect=[{"receipt": "before"}, {"receipt": "after"}]), \
                    patch.object(s, "run_command", return_value=0), \
                    patch("evaluate_solo_smoke.evaluator_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "Execution changed"):
                    s.evaluate(run, "solo", 0)


if __name__ == "__main__":
    unittest.main()
