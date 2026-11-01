"""Cross-repository scheduling/accounting/integrity tests; no API or Docker."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import run_handoff_expansion as runner
import task_specs


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "artifacts").mkdir()
        self.suite = self.root / "artifacts/handoff_expansion_runs/cross_handoff_unit"
        self.real_root = runner.ROOT
        shutil.copyfile(self.real_root / "run_handoff_pilot.py", self.root / "run_handoff_pilot.py")
        (self.root / "worker.py").write_text("# frozen source fixture\n")
        self.manifest = {"worker.py": runner.file_hash(self.root / "worker.py")}
        self.events, self.score_count = [], 0
        self.not_reached = set()
        self.packet_message = lambda packet: json.dumps(packet, sort_keys=True)
        self.patches = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "RUNS", self.root / "artifacts/handoff_expansion_runs"),
            mock.patch.object(runner, "SELECTION", self.root / "selection.json"),
            mock.patch.object(runner, "REFERENCE_PROOF", self.root / "artifacts/reference.json"),
            mock.patch.object(runner, "OFFLINE_PROOF", self.root / "artifacts/offline.json"),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
            mock.patch.object(runner, "source_manifest", return_value=self.manifest),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch.object(runner, "validate_checkpoint", side_effect=lambda cp, task:
                              self.assertEqual(cp["task"], task.as_dict())),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict("sys.modules", {"cross_task_packet": SimpleNamespace(packet_message=self.packet_message)}),
            mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": "unit-placeholder-not-a-real-key"}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.selection = {"dataset_commit": runner.COMMIT, "selected_tasks": []}
        proof = {"passed": True, "model_api_calls": 0, "cooperbench_commit": runner.COMMIT,
                 "reference_data_exposed_to_agents": False, "tasks": {}}
        for task in runner.TASKS.values():
            item = {"task_key": task.key, "repo": task.repo, "task_id": task.task_id,
                "features": list(task.features), "base_commit": task.base, "image": task.image,
                "code_roots": list(task.code_roots), "features_evidence": []}
            for f in task.features:
                path = task.task_dir / f"feature{f}/feature.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"Original requirement for {task.key} feature {f}\n")
                item["features_evidence"].append({"feature": f, "path": str(path.relative_to(self.root)), "sha256": runner.file_hash(path)})
            dockerfile = task.task_dir / "Dockerfile"
            dockerfile.write_text("FROM unit-image\n")
            item["environment"] = {"dockerfile": str(dockerfile.relative_to(self.root)), "dockerfile_sha256": runner.file_hash(dockerfile)}
            self.selection["selected_tasks"].append(item)
            self.write(task.image_record_path, {"Id": "unit-" + task.key})
            proof["tasks"][task.key] = {"task": task.as_dict(), "image_id": "unit-" + task.key,
                "base_fails_both": True, "individual_gold_passes_both": True, "combined_gold_passes_both": True}
        self.write(runner.SELECTION, self.selection)
        self.write(runner.REFERENCE_PROOF, proof)
        self.protocol = runner.protocol_for(False)
        patcher = mock.patch.object(runner, "run_command", side_effect=self.command)
        self.command_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_usage(self, run, phase, cost, status="completed"):
        path = run / "usage_ledger.json"
        ledger = runner.read_json(path, {"requests": []})
        ledger["requests"].append({"phase": phase + ":agent1", "actual_cost_usd": cost,
            "reserved_usd": 0.05, "status": status, "prompt_tokens": 100, "completion_tokens": 10})
        self.write(path, ledger)

    def command(self, command, *args, **kwargs):
        run = Path(command[command.index("--run-dir") + 1])
        if "--worker" in command:
            condition = command[command.index("--condition") + 1]
            self.events.append(("model", run.name, condition))
            self.complete_model(run, condition)
        else:
            condition = run.name
            self.events.append(("evaluation", run.parent.name, condition))
            existing = runner.read_json(run / "evaluation_summary.json", {})
            if not existing.get("evaluation_completed"):
                self.score_count += 1
                config = runner.read_json(run.parent / "task_config.json")
                self.write(run / "evaluation_summary.json", {"evaluation_completed": True,
                    "both_passed": False, "elapsed_seconds": 2.5, "task": config["task"]})
        return 0

    def complete_model(self, run, condition):
        config = runner.read_json(run / "task_config.json")
        task = runner.get_task(config["task_key"])
        phase = run / condition
        offline = config["offline_selftest"]
        if not offline:
            self.add_usage(run, condition, 0.01 if condition == "prefix" else 1)
        status = {"status": "completed", "task": task.as_dict(), "offline_selftest": offline,
                  "elapsed_seconds": 20 if condition == "prefix" else 100}
        if condition == "prefix":
            if task.key in self.not_reached:
                status["status"] = "checkpoint_not_reached"
            else:
                (run / "shared_git.tar").write_bytes(task.key.encode())
                checkpoint = {"task": task.as_dict(), "broker": {"task_key": task.key},
                    "git_archive_sha256": runner.file_hash(run / "shared_git.tar"),
                    "agents": {a: {"state": {"messages": [{"role": "system", "content": task.key}]},
                        "manifest": {"source.py": task.key}, "image": task.image} for a in task.agents}}
                self.write(run / "checkpoint.json", checkpoint)
        elif condition == "solo":
            (phase / "final.patch").write_text("fresh Solo patch")
            status.update(fresh_solo=True, outcome="Submitted", final_patch_sha256=runner.file_hash(phase / "final.patch"))
            self.write(phase / "initial_state.json", {"fresh_solo": True})
            self.write(phase / "solo.traj.json", {"messages": []})
        else:
            checkpoint = runner.read_json(run / "checkpoint.json")
            checkpoint_hash = runner.digest(checkpoint)
            packet = {"schema_version": 2, "checkpoint_sha256": checkpoint_hash, "task_key": task.key,
                "base_commit": task.base, "checkpoint_only": True, "agents": {a: {} for a in task.agents}}
            packet_hash = runner.digest(packet)
            text = self.packet_message(packet)
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            self.write(phase / "handoff_packet.json", packet)
            (phase / "handoff_packet.txt").write_text(text)
            active = ("agent1",) if condition == "consolidate" else tuple(task.agents)
            delivery = {"condition": condition, "checkpoint_sha256": checkpoint_hash,
                "packet_sha256": packet_hash, "source_checkpoint_verified": True,
                "code_automatically_transferred": False, "first_request_verified": True, "recipients": {}}
            for agent in active:
                delivery["recipients"][agent] = {"packet_sha256": packet_hash, "message_sha256": text_hash,
                    "message_index": 2, "history_preserved": True, "all_packet_content_delivered": True, "role": "user"}
                self.write(phase / f"first_request_{agent}.json", {"verified": True, "recipient": agent,
                    "full_packet_present": True, "message_sha256": text_hash, "actual_transport_input_verified": not offline})
                self.write(phase / f"{agent}.traj.json", {"messages": [{"role": "user", "content": text}]})
                if not offline:
                    self.write(phase / "responses" / agent / "request_00001_input.json",
                        {"request": {"messages": [{"role": "user", "content": text}]}})
            self.write(phase / "handoff_delivery.json", delivery)
            self.write(phase / "restore_proof.json", {"checkpoint_hash": checkpoint_hash,
                "broker_hash": runner.digest(checkpoint["broker"]), "agents": {a: {
                    "state_hash": runner.digest(saved["state"]), "filesystem_manifest_hash": runner.digest(saved["manifest"]),
                    "source_image": saved["image"], "filesystem_verified": True} for a, saved in checkpoint["agents"].items()}})
            status.update(handoff_variant=runner.VARIANT, handoff_packet_sha256=packet_hash,
                outcomes={a: {"status": "Submitted", "phase_turns": 4} for a in active})
            if condition == "consolidate":
                (phase / "final.patch").write_text("consolidated patch")
                status.update(outcome="Submitted", retired_agent_model_calls=0,
                              final_patch_sha256=runner.file_hash(phase / "final.patch"))
                self.write(phase / "intervention.json", {"retired": "agent2"})
            else:
                for feature in task.features:
                    (phase / f"agent{feature}.patch").write_text(f"feature {feature} patch")
        self.write(phase / "status.json", status)

    def run_suite(self, offline=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.supervise(self.suite, offline)

    def summary(self):
        return runner.read_json(self.suite / "summary.json")

    def test_all_27_failed_jobs_retained_prefix_once_and_completed_stages_not_repeated(self):
        self.assertEqual(self.run_suite(), 0)
        summary = self.summary()
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(len(summary["rows"]), 27)
        self.assertFalse(any(r["official_success"] for r in summary["rows"]))
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 27.03)
        self.assertEqual(summary["actual_new_usage"]["requests"], 30)
        self.assertAlmostEqual(sum(r["full_task_equivalent_known_cost_usd"] for r in summary["rows"]), 27.18)
        for task in runner.TASKS:
            rows = [r for r in summary["rows"] if r["task"] == task]
            self.assertEqual([(r["repeat"], r["condition"]) for r in rows],
                [(n, c) for n, order in enumerate(runner.ORDERS, 1) for c in order])
            self.assertEqual(len({runner.read_json(Path(r["run_directory"]) / "checkpoint.json")["task"]["key"]
                                  for r in rows if r["condition"] != "solo"}), 1)
            for row in rows:
                config = runner.read_json(Path(row["run_directory"]) / "task_config.json")
                if row["condition"] == "solo":
                    self.assertEqual(config["execution_seconds"], 1220)
                    self.assertNotIn("checkpoint_sha256", config)
                    self.assertFalse(list(Path(row["run_directory"]).rglob("checkpoint*")))
        self.events.clear()
        with mock.patch.dict(runner.os.environ, {"TOGETHER_API_KEY": ""}):
            self.assertEqual(self.run_suite(), 0)
        self.assertFalse(any(event[0] == "model" for event in self.events))
        self.assertEqual(self.score_count, 27)
        self.assertEqual(summary, self.summary())

    def test_missing_checkpoint_skips_six_jobs_but_keeps_three_feasibility_solos(self):
        key = next(iter(runner.TASKS))
        self.not_reached.add(key)
        self.assertEqual(self.run_suite(), 0)
        summary = self.summary()
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(len(summary["skipped_jobs"]), 6)
        self.assertEqual(len(summary["rows"]), 21)
        rows = [r for r in summary["rows"] if r["task"] == key]
        self.assertEqual([r["condition"] for r in rows], ["solo"] * 3)
        self.assertFalse(any(r["matched_checkpoint_comparison"] for r in rows))
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 21.03)
        self.assertEqual(sum(e[2] == "prefix" for e in self.events), 3)

    def test_failed_attempt_cost_unknown_and_time_are_kept_in_remaining_comparison(self):
        original = self.command
        failed = False
        def fail_once(command, *args, **kwargs):
            nonlocal failed
            if "--worker" in command and command[command.index("--condition") + 1] == "strongshare" and not failed:
                failed = True
                run = Path(command[command.index("--run-dir") + 1])
                self.add_usage(run, "strongshare", 0.3)
                self.add_usage(run, "strongshare", None, "in_flight")
                self.write(run / "strongshare/status.json", {"status": "error", "elapsed_seconds": 7})
                return 2
            return original(command, *args, **kwargs)
        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.run_suite(), 0)
        summary = self.summary()
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 27.33)
        self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"], 1)
        first = summary["rows"][0]
        self.assertEqual(first["accepted_usage"]["known_cost_usd"], 1)
        self.assertEqual(first["remaining_known_cost_usd"], 1.3)
        self.assertEqual(first["remaining_unknown_usage_requests"], 1)
        self.assertEqual(first["remaining_execution_known_seconds"], 107)
        self.assertEqual(first["full_task_equivalent_known_cost_usd"], 1.31)
        self.assertIsNone(summary["investment_gate"]["tasks"][first["task"]]["numerical_requirements_met"])

    def test_prefix_failed_artifact_archived_and_not_accidentally_reused(self):
        original = self.command
        failed = False
        def fail_once(command, *args, **kwargs):
            nonlocal failed
            if "--worker" in command and not failed:
                failed = True
                run = Path(command[command.index("--run-dir") + 1])
                self.add_usage(run, "prefix", 0.2)
                self.write(run / "checkpoint.json", {"stale_failed_attempt": True})
                (run / "shared_git.tar").write_bytes(b"stale")
                self.write(run / "prefix/status.json", {"status": "error"})
                return 2
            return original(command, *args, **kwargs)
        self.command_mock.side_effect = fail_once
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.run_suite(), 0)
        self.assertEqual(self.summary()["actual_new_usage"]["known_cost_usd"], 27.23)
        saved = list(self.suite.glob("*/prefix/failed_attempts/*/prefix_artifacts/checkpoint.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(runner.read_json(saved[0]), {"stale_failed_attempt": True})

    def test_missing_or_invalid_attempt_status_preserves_unknown_execution_time(self):
        run = self.root / "temporary_job"
        (run / "failed_attempts/strongshare_missing_status").mkdir(parents=True)
        self.write(run / "failed_attempts/strongshare_known/status.json", {"elapsed_seconds": 3.5})
        self.write(run / "failed_attempts/strongshare_invalid/status.json", {"elapsed_seconds": float("nan")})
        self.write(run / "failed_attempts/consolidate_other/status.json", {"elapsed_seconds": 99})
        self.assertEqual(runner.incomplete_seconds(run, "strongshare"),
                         {"known_seconds": 3.5, "unknown_attempts": 2})

    def test_cross_task_checkpoint_tampering_stops_without_new_model_calls(self):
        self.assertEqual(self.run_suite(), 0)
        tasks = list(runner.TASKS)
        source = self.suite / tasks[1] / "prefix/checkpoint.json"
        target = next((self.suite / tasks[0] / "jobs").glob("*_r01_strongshare")) / "checkpoint.json"
        target.write_bytes(source.read_bytes())
        self.events.clear()
        self.assertEqual(self.run_suite(), 2)
        self.assertEqual(self.events, [])

    def test_selection_and_original_requirement_hash_changes_rejected(self):
        original = copy.deepcopy(self.selection)
        for field, value in (("repo", "wrong"), ("task_id", -1), ("features", [1, 9]),
                             ("base_commit", "wrong"), ("image", "wrong"), ("code_roots", ["wrong"])):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["selected_tasks"][0][field] = value
                self.write(runner.SELECTION, changed)
                with self.assertRaises(RuntimeError):
                    runner.verify_selection()
        self.write(runner.SELECTION, original)
        task = next(iter(runner.TASKS.values()))
        (task.task_dir / f"feature{task.features[0]}/feature.md").write_text("modified requirement")
        with self.assertRaisesRegex(RuntimeError, "requirement changed"):
            runner.verify_selection()

    def test_gate_needs_nonzero_success_lower_known_cost_and_two_groups(self):
        rows = []
        for key in runner.TASKS:
            for condition, cost in (("strongshare", 2), ("consolidate", 1)):
                for repeat in range(3):
                    rows.append({"task": key, "condition": condition, "official_success": False,
                                 "remaining_known_cost_usd": cost, "remaining_unknown_usage_requests": 0})
        gate = runner.investment_gate(rows, 3)
        self.assertEqual(gate["numerically_qualifying_task_groups"], 0)
        for row in rows:
            row["official_success"] = True
        gate = runner.investment_gate(rows, 3)
        self.assertEqual(gate["numerically_qualifying_task_groups"], 3)
        self.assertIn("trace review", gate["decision"])
        rows[0]["remaining_unknown_usage_requests"] = 1
        gate = runner.investment_gate(rows, 3)
        self.assertEqual(gate["numerically_qualifying_task_groups"], 2)
        self.assertIsNone(gate["tasks"][rows[0]["task"]]["numerical_requirements_met"])

    def test_offline_runs_three_prefixes_nine_jobs_and_zero_api(self):
        self.assertEqual(self.run_suite(offline=True), 0)
        proof = runner.read_json(runner.OFFLINE_PROOF)
        self.assertEqual(proof["jobs_completed"], 9)
        self.assertEqual(proof["prefixes_completed"], 3)
        self.assertEqual(proof["model_api_calls"], 0)
        self.assertEqual(proof["source_manifest"], self.manifest)
        self.assertEqual(len(self.summary()["rows"]), 9)
        self.assertEqual(sum(e[0] == "model" for e in self.events), 12)


if __name__ == "__main__":
    unittest.main()
