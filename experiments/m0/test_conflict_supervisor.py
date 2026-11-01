"""Offline checks of conflict cohort selection, immutable reuse and actual spend."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import run_luna_conflict as s
from budget_guard import write_json
from run_baselines import usage


def cost(value=0, requests=0, unknown=0):
    return {"known_cost_usd": value, "requests": requests, "unknown_usage_requests": unknown,
            "prompt_tokens": 0, "completion_tokens": 0}


def protocol(offline=False):
    return {"offline_selftest": offline, "model": "offline-scripted-model" if offline else s.MODEL,
            "source_manifest": {}, "submission_policy": "authoritative_agent1_only", "terminal_policy": "both_active",
            "publication_guard_version": "publication_parity_v1", "interpretation": "fixture",
            "jobs": s.schedule(offline), "early_references": {
                key: {"directory": "/not-read/old-prefix/" + key,
                      "record": {"elapsed_seconds": 20, "checkpoint_sha256": "original-cp",
                                 "all_attempt_usage": cost(90, 8), "accepted_usage": cost(90, 8)}}
                for key in s.TASKS}}


def request(n, value, condition="strongshare", excluded=False):
    return {"phase": condition + ":agent1", "request_number": n, "actual_cost_usd": value,
            "excluded_from_baseline": excluded, "status": "in_flight" if value is None else "completed",
            "prompt_tokens": 0, "completion_tokens": 0}


def collection(directory, eligible=True):
    return {"eligible": eligible, "status": "completed" if eligible else "no_conflict",
            "reason": None if eligible else "agent_exited_without_eligible_current_conflict",
            "elapsed_seconds": 30, "checkpoint_sha256": "conflict-cp" if eligible else None,
            "run_directory": str(directory), "all_attempt_usage": cost(.3, 3), "accepted_usage": cost(.3, 3)}


class ConflictSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.worker = Mock(return_value={"receipt": "fixed"})
        self.collector = Mock(return_value={"receipt": "fixed"})
        patcher = patch.dict("sys.modules", {"conflict_runtime": SimpleNamespace(
            verify_worker_artifacts=self.worker, verify_collection_artifacts=self.collector)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fixed_task_cohort_order_and_job_identity(self):
        jobs = s.schedule(False)
        self.assertEqual(len(jobs), 8)
        self.assertEqual(len(s.schedule(True)), 4)
        for key in s.TASKS:
            self.assertEqual([j["condition"] for j in jobs if j["task"] == key],
                             ["strongshare", "integrator", "integrator", "strongshare"])
        self.assertEqual(len({(j["task"], j["repeat"], j["condition"]) for j in jobs}), 8)
        for change in ({"task": "replacement_task"}, {"repeat": True}, {"repeat": 3}, {"condition": "solo"}):
            with self.assertRaises(ValueError):
                s.job_path(Path("/tmp/suite"), {**jobs[0], **change})

    def test_exact_common_allowance_and_delivery_contract(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "copy_conflict_checkpoint") as copied:
            suite = Path(d)
            fixed = protocol()
            collect = collection(suite / "collection")
            for condition in s.CONDITIONS:
                job = {"task": next(iter(s.TASKS)), "repeat": 1, "condition": condition}
                config = s.configure_job(suite, job, fixed, collect)
                self.assertEqual(config["execution_seconds"], 1750)
                self.assertEqual(config["submission_policy"], "authoritative_agent1_only")
                self.assertEqual(config["terminal_policy"], "both_active")
                self.assertEqual(config["checkpoint_sha256"], "conflict-cp")
                self.assertTrue(config["initialization_included_in_budget"])
            self.assertEqual(copied.call_count, 2)
            job = {"task": next(iter(s.TASKS)), "repeat": 2, "condition": "integrator"}
            exhausted = s.configure_job(suite, job, fixed, {**collect, "elapsed_seconds": 1790})
            self.assertEqual(exhausted["execution_seconds"], 0)
            self.assertEqual(exhausted["remaining_seconds_before_clamp"], -10)

    def test_checkpoint_and_protocol_cannot_change_on_resume(self):
        with tempfile.TemporaryDirectory() as d, patch.object(s, "copy_conflict_checkpoint"):
            suite, fixed = Path(d), protocol()
            job = s.schedule(False)[0]
            collect = collection(suite / "collection")
            s.configure_job(suite, job, fixed, collect)
            with self.assertRaisesRegex(RuntimeError, "Frozen configuration changed"):
                s.configure_job(suite, job, fixed, {**collect, "checkpoint_sha256": "other-cp"})

    def test_terminal_unavailable_collector_is_reused_without_model_or_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "strongshare").mkdir()
            saved = {"status": "no_conflict", "elapsed_seconds": 9, "reason": "agent_exited"}
            write_json(run / "strongshare/status.json", saved)
            with patch.object(s, "run_command") as command, patch.object(s, "cleanup_phase"):
                self.assertEqual(s.model_phase(run, next(iter(s.TASKS.values())), "strongshare", protocol(), run, 0, collector=True), saved)
            command.assert_not_called()
            self.collector.assert_called_once()
            self.worker.assert_not_called()

    def test_completed_failed_branch_and_budget_failure_never_resampled(self):
        with tempfile.TemporaryDirectory() as d:
            for status in ("completed", "budget_exhausted"):
                run = Path(d) / status
                (run / "integrator").mkdir(parents=True)
                write_json(run / "integrator/status.json", {"status": status, "elapsed_seconds": 1})
                write_json(run / "integrator/evaluation_summary.json", {"both_passed": False, "evaluation_completed": status == "completed"})
                with patch.object(s, "run_command") as command, patch.object(s, "cleanup_phase"):
                    result = s.model_phase(run, next(iter(s.TASKS.values())), "integrator", protocol(), run, 0)
                self.assertEqual(result["status"], status)
                command.assert_not_called()

    def test_bad_terminal_receipt_rejects_instead_of_reexecuting(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "strongshare").mkdir()
            write_json(run / "strongshare/status.json", {"status": "no_conflict"})
            self.collector.side_effect = ValueError("changed terminal evidence")
            with patch.object(s, "run_command") as command, patch.object(s, "cleanup_phase"):
                with self.assertRaisesRegex(ValueError, "terminal evidence"):
                    s.model_phase(run, next(iter(s.TASKS.values())), "strongshare", protocol(), run, 0, collector=True)
            command.assert_not_called()

    def test_failed_collection_archive_preserves_artifacts_and_unknown_cost(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "strongshare").mkdir()
            write_json(run / "strongshare/status.json", {"status": "error", "elapsed_seconds": 5})
            names = ("checkpoint.json", "shared_git.tar", "checkpoint_packet.json", "checkpoint_packet.txt", "collection_receipt.json")
            for name in names:
                (run / name).write_text("failed artifact: " + name)
            write_json(run / "usage_ledger.json", {"requests": [request(1, .1), request(2, None)]})
            s.archive_attempt(run, "strongshare", collector=True)
            attempts = list((run / "failed_attempts").glob("strongshare_*"))
            self.assertEqual(len(attempts), 1)
            for name in names:
                self.assertFalse((run / name).exists())
                self.assertEqual((attempts[0] / "collection_artifacts" / name).read_text(), "failed artifact: " + name)
            self.assertEqual(usage(run)["known_cost_usd"], .1)
            self.assertEqual(usage(run)["unknown_usage_requests"], 1)
            self.assertEqual(usage(run, "strongshare")["requests"], 0)

    def test_copy_preserves_conflict_packet_and_refuses_modified_destination(self):
        with tempfile.TemporaryDirectory() as d:
            source, dest = Path(d) / "collection", Path(d) / "job"
            source.mkdir(); dest.mkdir()
            names = ("checkpoint.json", "shared_git.tar", "checkpoint_packet.json", "checkpoint_packet.txt")
            for name in names:
                (source / name).write_text("exact source " + name)
            with patch("conflict_checkpoint.verify_checkpoint_files") as verify:
                s.copy_conflict_checkpoint(source, dest, next(iter(s.TASKS.values())))
                self.assertEqual(verify.call_count, 2)
                for name in names:
                    self.assertEqual((dest / name).read_bytes(), (source / name).read_bytes())
                (dest / "checkpoint_packet.txt").write_text("tampered")
                with self.assertRaisesRegex(RuntimeError, "Copied conflict artifact changed"):
                    s.copy_conflict_checkpoint(source, dest, next(iter(s.TASKS.values())))

    def test_physical_spend_counts_collection_once_and_never_rebills_history(self):
        with tempfile.TemporaryDirectory() as d:
            suite, fixed = Path(d), protocol()
            key = next(iter(s.TASKS))
            collect_dir = s.collection_path(suite, key)
            collect_dir.mkdir(parents=True)
            write_json(collect_dir / "usage_ledger.json", {"requests": [request(1, .1, excluded=True), request(2, .2)]})
            collect = collection(collect_dir)
            job = {"task": key, "repeat": 1, "condition": "integrator"}
            run = s.job_path(suite, job)
            run.mkdir(parents=True)
            write_json(run / "task_config.json", {"execution_seconds": 1750})
            write_json(run / "usage_ledger.json", {"requests": [request(1, .4, "integrator", True), request(2, None, "integrator", True), request(3, .5, "integrator")]})
            row = s.build_row(run, job, {"status": "completed", "elapsed_seconds": 4}, {"both_passed": False}, fixed, collect)
            self.assertAlmostEqual(row["full_task_known_cost_usd"], 91.2)
            self.assertEqual(row["full_task_unknown_usage_requests"], 1)
            self.assertEqual(row["accepted_usage"]["known_cost_usd"], .5)
            pending = s.job_path(suite, {**job, "condition": "strongshare"})
            pending.mkdir(parents=True)
            write_json(pending / "usage_ledger.json", {"requests": [request(1, .6)]})
            summary = s.summary_for(suite, fixed, {key: collect}, [row], [])
            self.assertAlmostEqual(summary["actual_new_usage"]["known_cost_usd"], 1.8)
            self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"], 1)
            self.assertFalse(summary["historical_prefix_rebilled"])

    def test_exhausted_eligible_branch_counts_as_failure_not_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            write_json(run / "task_config.json", {"execution_seconds": 0})
            job = s.schedule(False)[0]
            row = s.build_row(run, job, {"status": "budget_exhausted", "elapsed_seconds": 0}, None,
                              protocol(), collection(run / "collection"))
            self.assertFalse(row["official_success"])
            self.assertFalse(row["scored"])
            summary = s.summary_for(run, protocol(), {job["task"]: collection(run)}, [row], [])
            self.assertEqual(summary["comparison_groups"][0]["n"], 1)
            self.assertEqual(summary["comparison_groups"][0]["successes"], 0)

    def test_both_collectors_finish_before_branches_and_unavailable_never_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            root, suite = Path(d), Path(d) / "suite"
            (root / "artifacts").mkdir()
            fixed, calls = protocol(), []
            keys = list(s.TASKS)

            def run_phase(run, task, condition, protocol, suite, lock_fd, collector=False):
                calls.append(("collection" if collector else "branch", task.key))
                return {"status": "completed", "elapsed_seconds": 1}

            def collected(run, task):
                return collection(run, eligible=task.key == keys[1])

            with patch.object(s, "ROOT", root), patch.object(s, "configure"), \
                    patch.object(s, "verify_preflight"), patch.object(s, "protocol_for", return_value=fixed), \
                    patch.object(s, "verify_sources"), patch.object(s, "copy_conflict_checkpoint"), \
                    patch.object(s, "model_phase", side_effect=run_phase), \
                    patch.object(s, "collection_record", side_effect=collected), \
                    patch.object(s, "evaluate", return_value={"both_passed": False}):
                self.assertEqual(s.supervise(suite), 0)
            self.assertEqual(calls[:2], [("collection", key) for key in keys])
            self.assertEqual(calls[2:], [("branch", keys[1])] * 4)
            summary = json.loads((suite / "summary.json").read_text())
            self.assertTrue(summary["pipeline_completed"])
            self.assertEqual(summary["eligible_checkpoints"], 1)
            self.assertEqual(summary["completed_executions"], 4)
            self.assertEqual(len(summary["skipped_jobs"]), 4)
            self.assertTrue(all(not r["eligible"] and r["task"] == keys[0] for r in summary["skipped_jobs"]))

    def test_scoring_receipt_change_rejected_without_model_restart(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "integrator").mkdir()
            write_json(run / "integrator/evaluation_summary.json", {"evaluation_completed": True, "both_passed": False})
            self.worker.side_effect = [{"before": True}, {"after": True}]
            with patch.object(s, "run_command", return_value=0), \
                    patch("evaluate_solo_smoke.evaluator_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "changed during scoring"):
                    s.evaluate(run, next(iter(s.TASKS.values())), "integrator", 0)

    def test_sources_and_early_reference_changes_are_rejected(self):
        fixed = protocol()
        with patch.object(s, "source_manifest", return_value={}), \
                patch.object(s, "early_references", return_value=fixed["early_references"]) as refs:
            s.verify_sources(fixed)
            changed = copy.deepcopy(fixed["early_references"])
            changed[next(iter(changed))]["record"]["checkpoint_sha256"] = "replaced"
            refs.return_value = changed
            with self.assertRaisesRegex(RuntimeError, "Frozen conflict study inputs changed"):
                s.verify_sources(fixed)


if __name__ == "__main__":
    unittest.main()
