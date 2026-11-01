"""Handoff supervisor integrity/resume tests; temporary files, no Docker/API."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import evaluate_task_expansion as evaluator
import run_handoff_pilot as runner
import task_specs


class HandoffSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "artifacts").mkdir()
        self.source = self.root / "original/prefix"
        self.suite = self.root / "artifacts/expansion_runs/handoff_dirty_unit"
        self.task = runner.TASK
        self.checkpoint = {"broker": {"messages": []}, "agents": {
            agent: {"state": {"messages": [{"role": "system", "content": "original"}]},
                    "manifest": {"source.py": "frozen-code"}, "image": "frozen-image"}
            for agent in self.task.agents}}
        self.checkpoint_hash = runner.digest(self.checkpoint)
        (self.root / "worker.py").write_text("# source fixture\n")
        self.manifest = {"worker.py": runner.file_hash(self.root / "worker.py")}
        self.events = []
        self.packet_message = lambda packet: json.dumps(packet, sort_keys=True)
        patches = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "SOURCE_RUN", self.source),
            mock.patch.object(runner, "SOURCE_CHECKPOINT", self.checkpoint_hash),
            mock.patch.object(runner, "source_checkpoint", return_value=self.checkpoint),
            mock.patch.object(runner, "source_manifest", return_value=self.manifest),
            mock.patch.object(runner, "verify_references", return_value={"passed": True}),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(evaluator, "source_manifest", return_value={"evaluation.py": "fixed"}),
            mock.patch.object(evaluator, "verify_upstream"),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict("sys.modules", {"handoff_packet": SimpleNamespace(packet_message=self.packet_message)}),
            mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": "offline-unit-placeholder"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write(self.source / "checkpoint.json", self.checkpoint)
        (self.source / "shared_git.tar").write_bytes(b"unit archive")
        self.write(self.source / "prefix/status.json", {"status": "completed", "elapsed_seconds": 38.524})
        self.add_usage(self.source, 0.08159694, phase="prefix:agent1")
        self.write(self.task.image_record_path, {"Id": "frozen-image"})
        self.protocol = runner.protocol_for(False)
        patcher = mock.patch.object(runner, "run_command", side_effect=self.command)
        self.command_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_usage(self, run_dir, cost, phase, status="completed"):
        path = run_dir / "usage_ledger.json"
        ledger = runner.read_json(path, {"requests": []})
        ledger["requests"].append({"phase": phase, "actual_cost_usd": cost, "reserved_usd": 0.05,
                                   "status": status, "prompt_tokens": 100, "completion_tokens": 10})
        self.write(path, ledger)

    def command(self, command, *args, **kwargs):
        path = Path(command[command.index("--run-dir") + 1])
        if "--worker" in command:
            condition = command[command.index("--condition") + 1]
            self.events.append(("model", path.name))
            self.complete_model(path, condition)
        else:
            self.events.append(("evaluation", path.parent.name))
            self.complete_score(path)
        return 0

    def complete_model(self, run_dir, condition):
        phase = run_dir / condition
        config = runner.read_json(run_dir / "task_config.json")
        offline = config["offline_selftest"]
        active = ("agent1",) if condition == "consolidate" else tuple(self.task.agents)
        packet = {"checkpoint_sha256": self.checkpoint_hash, "task_key": self.task.key,
                  "base_commit": self.task.base, "checkpoint_only": True,
                  "agents": {agent: {"source": "unchanged shared evidence"} for agent in self.task.agents}}
        packet_hash = runner.digest(packet)
        text = self.packet_message(packet)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        self.write(phase / "handoff_packet.json", packet)
        (phase / "handoff_packet.txt").write_text(text)
        delivery = {"condition": condition, "checkpoint_sha256": self.checkpoint_hash,
                    "packet_sha256": packet_hash, "source_checkpoint_verified": True,
                    "code_automatically_transferred": False, "first_request_verified": True, "recipients": {}}
        for agent in active:
            delivery["recipients"][agent] = {"packet_sha256": packet_hash, "message_sha256": text_hash,
                "message_index": 2, "history_preserved": True, "all_packet_content_delivered": True, "role": "user"}
            self.write(phase / f"first_request_{agent}.json", {"verified": True, "recipient": agent,
                "message_sha256": text_hash, "full_packet_present": True, "actual_transport_input_verified": not offline})
            self.write(phase / f"{agent}.traj.json", {"messages": [{"role": "user", "content": text}]})
            if not offline:
                self.write(phase / "responses" / agent / "request_000001_input.json",
                           {"request": {"messages": [{"role": "user", "content": text}]}})
        self.write(phase / "handoff_delivery.json", delivery)
        proof = {"checkpoint_hash": self.checkpoint_hash, "broker_hash": runner.digest(self.checkpoint["broker"]),
                 "agents": {agent: {"state_hash": runner.digest(saved["state"]),
                    "filesystem_manifest_hash": runner.digest(saved["manifest"]), "source_image": saved["image"],
                    "filesystem_verified": True} for agent, saved in self.checkpoint["agents"].items()}}
        self.write(phase / "restore_proof.json", proof)
        status = {"status": "completed", "offline_selftest": offline, "task": self.task.as_dict(),
                  "handoff_variant": "explicit_checkpoint_packet_v1", "handoff_packet_sha256": packet_hash,
                  "elapsed_seconds": 100, "outcomes": {a: {"status": "Submitted", "phase_turns": 4} for a in active}}
        if condition == "consolidate":
            (phase / "final.patch").write_text("synthetic final patch")
            status.update(outcome="Submitted", retired_agent_model_calls=0,
                          final_patch_sha256=runner.file_hash(phase / "final.patch"))
            self.write(phase / "intervention.json", {"retired": "agent2"})
        else:
            for feature in self.task.features:
                (phase / f"agent{feature}.patch").write_text(f"synthetic feature {feature} patch")
        self.write(phase / "status.json", status)
        if not offline:
            self.add_usage(run_dir, 1, phase=condition + ":agent1")

    def complete_score(self, phase):
        task, config = evaluator.load_task(phase)
        attempt = phase / "evaluation_attempts/accepted"
        current = evaluator.input_manifest(phase, task, evaluator.patch_info(phase, task))
        self.write(attempt / "evaluation_input.json", {"task": task.as_dict(),
            "protocol_sha256": config["protocol_sha256"], "input_manifest": current})
        self.write(attempt / "integrity.json", {"unchanged": True, "before": current, "after": current})
        native = {"both_passed": False, "feature1": {"passed": True}, "feature2": {"passed": False}}
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json", phase / "evaluation.json"):
            self.write(path, native)
        summary = evaluator.summarize(native, "failed", phase, attempt, 2.5, task, config)
        self.write(attempt / "evaluation_summary.json", summary)
        self.write(phase / "evaluation_summary.json", summary)

    def run_suite(self, offline=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.supervise(self.suite, offline)

    def first_phase(self):
        return next((self.suite / self.task.key / "jobs").glob("*_r01_strongshare")) / "strongshare"

    def test_fixed_order_failed_samples_retained_and_resume_runs_neither_model_nor_score(self):
        self.assertEqual([(j["repeat"], j["condition"]) for j in self.protocol["jobs"]],
                         [(1, "strongshare"), (1, "consolidate"), (2, "consolidate"), (2, "strongshare")])
        self.assertEqual(self.run_suite(), 0)
        summary = runner.read_json(self.suite / "summary.json")
        self.assertEqual(len(summary["rows"]), 4)
        self.assertFalse(any(row["official_success"] for row in summary["rows"]))
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 4)
        self.assertEqual({row["full_task_equivalent_known_cost_usd"] for row in summary["rows"]}, {1.08159694})
        self.assertEqual({row["comparison_seconds"] for row in summary["rows"]}, {141.024})
        self.events.clear()
        with mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": ""}):
            self.assertEqual(self.run_suite(), 0)
        self.assertEqual(self.events, [])
        self.assertEqual(runner.read_json(self.suite / "summary.json"), summary)

    def test_failed_attempt_keeps_known_and_unknown_cost_but_is_not_accepted_sample(self):
        original = self.command
        failed = False
        def fail_once(command, *args, **kwargs):
            nonlocal failed
            if "--worker" in command and not failed:
                failed = True
                run_dir = Path(command[command.index("--run-dir") + 1])
                condition = command[command.index("--condition") + 1]
                self.add_usage(run_dir, 0.3, condition + ":agent1")
                self.add_usage(run_dir, None, condition + ":agent1", "in_flight")
                self.write(run_dir / condition / "status.json", {"status": "error"})
                return 2
            return original(command, *args, **kwargs)
        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.run_suite(), 0)
        summary = runner.read_json(self.suite / "summary.json")
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 4.3)
        self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"], 1)
        self.assertEqual(sum(r["continuation_usage"]["known_cost_usd"] for r in summary["rows"]), 4)
        ledger = runner.read_json(self.first_phase().parent / "usage_ledger.json")["requests"]
        self.assertTrue(all(r["excluded_from_baseline"] for r in ledger[:2]))
        self.assertEqual(ledger[1]["error_type"], "InterruptedAttemptUsageUnknown")

    def test_incomplete_evaluation_resume_never_repeats_completed_model(self):
        original = self.command
        failed = False
        def fail_once(command, *args, **kwargs):
            nonlocal failed
            if "--worker" not in command and not failed:
                failed = True
                return 2
            return original(command, *args, **kwargs)
        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.run_suite(), 0)
        self.assertEqual(sum(stage == "model" for stage, _ in self.events), 4)

    def test_cleanup_only_failure_retries_cleanup_without_rerunning_completed_model(self):
        original = self.command
        failed = False
        def fail_once(command, *args, **kwargs):
            nonlocal failed
            code = original(command, *args, **kwargs)
            if "--worker" in command and not failed:
                failed = True
                run_dir = Path(command[command.index("--run-dir") + 1])
                condition = command[command.index("--condition") + 1]
                path = run_dir / condition / "status.json"
                status = runner.read_json(path)
                status.update(infrastructure_error=True, cleanup_errors=["temporary cleanup failure"])
                self.write(path, status)
                return 2
            return code
        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        first_run = self.first_phase().parent
        runner.cleanup_phase.reset_mock()
        self.assertEqual(self.run_suite(), 0)
        runner.cleanup_phase.assert_any_call(first_run, "strongshare")
        self.assertEqual(sum(stage == "model" for stage, _ in self.events), 4)

    def test_cached_summary_or_native_result_tampering_rejects_without_model_calls(self):
        self.assertEqual(self.run_suite(), 0)
        phase = self.first_phase()
        summary_path = phase / "evaluation_summary.json"
        summary = runner.read_json(summary_path)
        corrupt = copy.deepcopy(summary)
        corrupt["both_passed"] = True
        self.write(summary_path, corrupt)
        self.events.clear()
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.events, [])
        self.write(summary_path, summary)
        self.write(phase / "evaluation.json", {"both_passed": True})
        with self.assertRaises(RuntimeError):
            runner.completed_evaluation(phase, self.task)

    def test_receipt_binds_intervention_and_rejects_mutated_packet_or_transport(self):
        self.assertEqual(self.run_suite(), 0)
        phase = self.first_phase()
        receipt = runner.read_json(phase / "execution_receipt.json")
        self.assertIn("strongshare/first_request_agent1.json", receipt["protected_files_sha256"])
        input_path = next((phase / "responses/agent1").glob("*_input.json"))
        self.write(input_path, {"request": {"messages": []}})
        with self.assertRaisesRegex(RuntimeError, "complete packet"):
            runner.verify_execution_receipt(phase)
        consolidate = next((self.suite / self.task.key / "jobs").glob("*_r01_consolidate")) / "consolidate"
        receipt = runner.read_json(consolidate / "execution_receipt.json")
        self.assertIn("consolidate/intervention.json", receipt["protected_files_sha256"])
        self.write(consolidate / "intervention.json", {"retired": "nobody"})
        with self.assertRaisesRegex(RuntimeError, "Frozen configuration"):
            runner.verify_execution_receipt(consolidate)

    def test_changed_checkpoint_or_missing_recipient_rejected(self):
        self.assertEqual(self.run_suite(), 0)
        phase = self.first_phase()
        delivery = runner.read_json(phase / "handoff_delivery.json")
        del delivery["recipients"]["agent2"]
        self.write(phase / "handoff_delivery.json", delivery)
        with self.assertRaisesRegex(RuntimeError, "delivery"):
            runner.verify_execution_receipt(phase)
        self.write(phase.parent / "checkpoint.json", {"changed": True})
        with self.assertRaisesRegex(RuntimeError, "Branch checkpoint"):
            runner.verify_execution_receipt(phase)

    def test_offline_proof_requires_four_full_jobs_zero_api_and_source_unchanged(self):
        self.assertEqual(self.run_suite(offline=True), 0)
        proof = runner.read_json(self.root / "artifacts/handoff_offline_validation.json")
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["jobs_completed"], 4)
        self.assertEqual(proof["model_api_calls"], 0)
        self.assertEqual(proof["source_manifest"], self.manifest)
        self.assertEqual(len(self.events), 8)
        self.assertEqual(runner.read_json(self.suite / "summary.json")["actual_new_usage"]["known_cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
