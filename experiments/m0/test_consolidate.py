"""Offline checks for Consolidate handoff, provenance, costs, and resume behavior."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import run_consolidate as runner
from baseline_runtime import MemoryBroker


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def sample_agent(task="Original feature 1"):
    agent = SimpleNamespace(
        messages=[{"role": "system", "content": "Original split responsibilities"},
                  {"role": "user", "content": task},
                  {"role": "assistant", "content": "Preserve prior reasoning and tool evidence"}],
        n_calls=4, cost=0.012, sent_messages=["Original communication"],
        extra_template_vars={"task": task, "agent_id": "agent1", "git_enabled": True},
        _last_prompt_tokens=1200, _submit_nudges=0, _departed_announced=set(),
    )
    agent.model = SimpleNamespace(
        format_message=mock.Mock(side_effect=lambda **message: copy.deepcopy(message)),
        query=mock.Mock(side_effect=AssertionError("No model request is allowed in unit tests")),
    )
    agent.add_messages = lambda *messages: agent.messages.extend(copy.deepcopy(messages))
    return agent


def sample_checkpoint(agent):
    peer = sample_agent("Original feature 6")
    peer.extra_template_vars["agent_id"] = "agent2"
    return {"agents": {"agent1": {"state": runner.save_state(agent)},
                       "agent2": {"state": runner.save_state(peer)}}}


class HandoffTests(unittest.TestCase):
    def test_handoff_keeps_history_and_cost_while_assigning_both_features(self):
        agent = sample_agent()
        checkpoint = sample_checkpoint(agent)
        checkpoint_before = copy.deepcopy(checkpoint)
        before = runner.save_state(agent)
        broker = MemoryBroker(["agent1", "agent2"])
        active = broker.connector("agent1")
        peer = broker.connector("agent2")
        peer.send("agent1", "Historical peer work is still relevant")
        inbox_before = copy.deepcopy(broker.inboxes["agent1"])

        intervention = runner.apply_handoff(agent, broker, checkpoint)

        original_messages = before["messages"]
        self.assertEqual(agent.messages[1:len(original_messages)], original_messages[1:])
        self.assertEqual(len(agent.messages), len(original_messages) + 1)
        self.assertEqual([index for index, message in enumerate(agent.messages)
                          if message.get("role") == "system"], [0])
        self.assertTrue(agent.messages[0]["content"].startswith(original_messages[0]["content"]))
        self.assertEqual({key: value for key, value in agent.messages[0].items() if key != "content"},
                         {key: value for key, value in original_messages[0].items() if key != "content"})
        self.assertEqual(agent.n_calls, before["n_calls"])
        self.assertEqual(agent.cost, before["cost"])
        self.assertEqual(agent.sent_messages, before["sent_messages"])
        self.assertEqual(checkpoint, checkpoint_before)
        self.assertEqual(broker.inboxes["agent1"], inbox_before)
        self.assertIn("Original feature 1", agent.extra_template_vars["task"])
        self.assertIn("Original feature 6", agent.extra_template_vars["task"])
        self.assertEqual(agent.messages[-1]["role"], "user")
        update = agent.messages[0]["content"]
        self.assertIn("sole active engineer", update)
        self.assertIn("BOTH feature requirements", update)
        self.assertIn("supersedes the earlier split", update)
        self.assertIn("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", update)
        self.assertIn("no second final submission", update)
        self.assertIn("frozen, read-only snapshot", agent.messages[-1]["content"])
        self.assertEqual(intervention["expected_final_submissions"], 1)
        self.assertFalse(intervention["peer_model_constructed"])
        self.assertFalse(intervention["code_automatically_transferred"])
        self.assertEqual(intervention["before_state_sha256"], runner.digest(before))
        self.assertNotEqual(intervention["before_state_sha256"], intervention["after_state_sha256"])
        self.assertEqual(intervention["system_message_before"], original_messages[0]["content"])
        self.assertEqual(intervention["system_message_after"], agent.messages[0]["content"])
        self.assertEqual(intervention["added_messages"], [agent.messages[-1]])
        agent.model.format_message.assert_called_once_with(role="user", content=agent.messages[-1]["content"])
        agent.model.query.assert_not_called()

        # Retiring the peer must not turn its old work into a successful submission
        # or allow a new blocking send to an agent that has no running model loop.
        self.assertTrue(active.has_exited("agent2"))
        self.assertFalse(active.has_published("agent2"))
        self.assertFalse(active.send("agent2", "No new message should be delivered"))
        self.assertEqual(active.send_and_wait("agent2", "Do not wait", timeout=60), (False, []))
        self.assertIn("agent2", agent._departed_announced)

    def test_handoff_rejects_modified_active_state_before_any_mutation(self):
        agent = sample_agent()
        checkpoint = sample_checkpoint(agent)
        agent.messages.append({"role": "user", "content": "Unexpected change"})
        before = copy.deepcopy(agent.messages)
        broker = MemoryBroker(["agent1", "agent2"])
        with self.assertRaisesRegex(RuntimeError, "changed before intervention"):
            runner.apply_handoff(agent, broker, checkpoint)
        self.assertEqual(agent.messages, before)
        self.assertEqual(broker.exited, {})
        agent.model.format_message.assert_not_called()

    def test_handoff_rejects_invalid_system_layout_without_mutating_state_or_broker(self):
        invalid_layouts = {
            "empty_history": [],
            "no_system": [{"role": "user", "content": "Task without a leading system"}],
            "system_after_user": [{"role": "user", "content": "Task"},
                                  {"role": "system", "content": "Late system"}],
            "two_leading_systems": [{"role": "system", "content": "First system"},
                                    {"role": "system", "content": "Second system"},
                                    {"role": "user", "content": "Task"}],
            "leading_and_late_system": [{"role": "system", "content": "Leading system"},
                                        {"role": "user", "content": "Task"},
                                        {"role": "system", "content": "Late system"}],
            "non_string_system_content": [{"role": "system", "content": [{"type": "text", "text": "Invalid"}]},
                                          {"role": "user", "content": "Task"}],
        }
        for name, history in invalid_layouts.items():
            with self.subTest(layout=name):
                agent = sample_agent()
                agent.messages = copy.deepcopy(history)
                checkpoint = sample_checkpoint(agent)
                before = runner.save_state(agent)
                broker = MemoryBroker(["agent1", "agent2"])
                broker.connector("agent1")
                broker.connector("agent2")
                broker_before = broker.snapshot()
                with self.assertRaisesRegex(RuntimeError, "[Ss]ystem"):
                    runner.apply_handoff(agent, broker, checkpoint)
                self.assertEqual(runner.save_state(agent), before)
                self.assertEqual(broker.snapshot(), broker_before)
                self.assertEqual(checkpoint["agents"]["agent1"]["state"], before)
                agent.model.format_message.assert_not_called()
                agent.model.query.assert_not_called()


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.destination = root / "source", root / "branch"
        self.source.mkdir()
        self.destination.mkdir()
        archive = b"synthetic common-checkpoint git archive"
        (self.source / "shared_git.tar").write_bytes(archive)
        self.checkpoint = sample_checkpoint(sample_agent())
        self.checkpoint["git_archive_sha256"] = hashlib.sha256(archive).hexdigest()
        write(self.source / "checkpoint.json", self.checkpoint)
        write(self.source / "prefix/status.json", {"elapsed_seconds": 12.5})
        write(self.source / "usage_ledger.json", {"requests": [
            {"phase": "prefix:agent1", "actual_cost_usd": 0.01, "prompt_tokens": 10, "completion_tokens": 2},
            {"phase": "keep:agent1", "actual_cost_usd": 9.99},
        ]})
        write(self.source / "keep/evaluation_summary.json", {"result": "FINAL_OUTCOME_SENTINEL"})
        (self.source / "keep/final.patch").write_text("FINAL_PATCH_SENTINEL")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runner, "SOURCE_RUN", self.source).start()
        mock.patch.object(runner, "SOURCE_CHECKPOINT", runner.digest(self.checkpoint)).start()

    def test_only_common_checkpoint_inputs_are_copied_and_prefix_is_not_rebilled(self):
        original, provenance = runner.prepare_checkpoint(self.destination)
        self.assertEqual(original, self.checkpoint)
        self.assertEqual({path.name for path in self.destination.iterdir()},
                         {"checkpoint.json", "shared_git.tar", "checkpoint_provenance.json"})
        self.assertEqual(provenance["prefix_usage"]["known_cost_usd"], 0.01)
        self.assertEqual(provenance["prefix_usage"]["requests"], 1)
        self.assertEqual(provenance["new_prefix_model_calls"], 0)
        self.assertEqual(provenance["prefix_elapsed_seconds"], 12.5)
        for path in self.destination.iterdir():
            self.assertNotIn(b"FINAL_OUTCOME_SENTINEL", path.read_bytes())
            self.assertNotIn(b"FINAL_PATCH_SENTINEL", path.read_bytes())
        self.assertEqual(runner.prepare_checkpoint(self.destination), (original, provenance))

    def test_changed_source_checkpoint_is_rejected(self):
        changed = copy.deepcopy(self.checkpoint)
        changed["agents"]["agent1"]["state"]["messages"].append({"role": "user", "content": "leaked outcome"})
        write(self.source / "checkpoint.json", changed)
        with self.assertRaisesRegex(RuntimeError, "common checkpoint has changed"):
            runner.prepare_checkpoint(self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_changed_git_archive_is_rejected(self):
        (self.source / "shared_git.tar").write_bytes(b"different final git state")
        with self.assertRaisesRegex(RuntimeError, "Git archive has changed"):
            runner.prepare_checkpoint(self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_changed_saved_input_and_provenance_are_rejected(self):
        runner.prepare_checkpoint(self.destination)
        checkpoint_path = self.destination / "checkpoint.json"
        original_bytes = checkpoint_path.read_bytes()
        checkpoint_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "Saved branch input changed"):
            runner.prepare_checkpoint(self.destination)
        checkpoint_path.write_bytes(original_bytes)
        write(self.destination / "checkpoint_provenance.json", {"changed": True})
        with self.assertRaisesRegex(RuntimeError, "provenance changed"):
            runner.prepare_checkpoint(self.destination)


class ArchiveCostTests(unittest.TestCase):
    def test_archiving_attempts_preserves_actual_and_unknown_cost_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            phase_dir = run_dir / runner.PHASE
            write(phase_dir / "status.json", {"status": "error"})
            rows = [
                {"request_number": 1, "phase": "prefix:agent1", "actual_cost_usd": 0.1, "status": "completed"},
                {"request_number": 2, "phase": "consolidate:agent1", "actual_cost_usd": 0.2, "status": "completed"},
                {"request_number": 3, "phase": "consolidate:agent1", "actual_cost_usd": None,
                 "status": "in_flight", "reserved_usd": 0.03},
                {"request_number": 4, "phase": "consolidate:agent1", "actual_cost_usd": 0.3,
                 "status": "completed", "excluded_from_baseline": True, "archived_attempt": "older"},
                {"request_number": 5, "phase": "keep:agent1", "actual_cost_usd": 0.4, "status": "completed"},
            ]
            write(run_dir / "usage_ledger.json", {"requests": rows})
            total_before = runner.usage(run_dir)
            runner.archive_failed_phase(run_dir)
            self.assertEqual(runner.usage(run_dir), total_before)
            self.assertEqual(runner.usage(run_dir, runner.PHASE)["requests"], 0)
            self.assertEqual(runner.usage(run_dir, excluded_only=True)["known_cost_usd"], 0.5)
            self.assertEqual(runner.usage(run_dir, excluded_only=True)["unknown_usage_requests"], 1)
            archived = json.loads((run_dir / "usage_ledger.json").read_text())["requests"]
            self.assertEqual([row["request_number"] for row in archived], list(range(1, 6)))
            self.assertEqual(archived[3]["archived_attempt"], "older")
            self.assertIsNone(archived[2]["actual_cost_usd"])
            self.assertEqual(archived[2]["reserved_usd"], 0.03)
            self.assertEqual(archived[2]["status"], "error")
            self.assertEqual(archived[2]["error_type"], "InterruptedAttemptUsageUnknown")
            self.assertEqual(len(list((run_dir / "failed_attempts").iterdir())), 1)
            self.assertFalse(phase_dir.exists())
            # Repeating archive with no live phase must not change accounting.
            runner.archive_failed_phase(run_dir)
            self.assertEqual(runner.usage(run_dir), total_before)
            write(phase_dir / "status.json", {"status": "error"})
            archived.append({"request_number": 6, "phase": "consolidate:agent1",
                             "actual_cost_usd": 0.6, "status": "completed"})
            write(run_dir / "usage_ledger.json", {"requests": archived})
            runner.archive_failed_phase(run_dir)
            self.assertEqual(runner.usage(run_dir)["requests"], 6)
            self.assertEqual(runner.usage(run_dir)["known_cost_usd"], 1.6)
            self.assertEqual(runner.usage(run_dir, excluded_only=True)["known_cost_usd"], 1.1)
            self.assertEqual(len(list((run_dir / "failed_attempts").iterdir())), 2)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "artifacts/consolidate_runs/consolidate_smoke_unit"
        self.phase_dir = self.run_dir / runner.PHASE
        self.phase_dir.mkdir(parents=True)
        patch = b"synthetic completed patch"
        (self.phase_dir / "final.patch").write_bytes(patch)
        write(self.phase_dir / "status.json", {"status": "completed", "elapsed_seconds": 20,
              "final_patch_sha256": hashlib.sha256(patch).hexdigest()})
        self.provenance = {
            "checkpoint_sha256": "unit-checkpoint", "prefix_elapsed_seconds": 5,
            "prefix_usage": {"known_cost_usd": 0.01, "unknown_usage_requests": 0},
        }
        self.patches = [
            mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "prepare_checkpoint", return_value=({}, self.provenance)),
            mock.patch.object(runner, "source_manifest", return_value={"synthetic": "hash"}),
            mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("Completed model must not rerun")),
            mock.patch.object(runner, "archive_failed_phase", side_effect=AssertionError("Completed model must not archive")),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch.dict(runner.os.environ, {}, clear=True),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_resume_retries_only_failed_evaluator_without_key_or_model_process(self):
        def successful_evaluation(*args, **kwargs):
            write(self.phase_dir / "evaluation_summary.json", {"evaluation_completed": True})
            return SimpleNamespace(returncode=0)
        with mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=2)) as evaluator:
            self.assertEqual(runner.supervise(self.run_dir), 2)
            self.assertEqual(evaluator.call_count, 1)
        with mock.patch.object(runner.subprocess, "run", side_effect=successful_evaluation) as evaluator:
            self.assertEqual(runner.supervise(self.run_dir), 0)
            self.assertEqual(evaluator.call_count, 1)
            self.assertIn("evaluate_consolidate_smoke.py", str(evaluator.call_args.args[0]))
        summary = json.loads((self.run_dir / "consolidate_summary.json").read_text())
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(summary["new_actual_collection_usage"]["requests"], 0)
        self.assertEqual(summary["full_task_known_cost_usd"], 0.01)

    def test_resume_with_completed_evaluation_runs_neither_model_nor_evaluator(self):
        write(self.phase_dir / "evaluation_summary.json", {"evaluation_completed": True})
        with mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("Evaluator must not rerun")):
            self.assertEqual(runner.supervise(self.run_dir), 0)

    def test_changed_completed_patch_is_rejected_instead_of_rerunning_or_scoring(self):
        (self.phase_dir / "final.patch").write_text("tampered patch")
        with mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("Tampered patch must not score")):
            with self.assertRaisesRegex(RuntimeError, "Completed final patch has changed"):
                runner.supervise(self.run_dir)


class OfflineCompletionTests(unittest.TestCase):
    def test_only_complete_zero_api_synthetic_submission_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            phase = run_dir / runner.PHASE
            phase.mkdir()
            expected_patch = (
                "diff --git a/consolidate_offline_probe.txt b/consolidate_offline_probe.txt\n"
                "new file mode 100644\n"
            )
            patch_path = phase / "final.patch"
            patch_path.write_text(expected_patch)
            valid = {
                "execution": {"outcome": "Submitted", "phase_turns": 4},
                "new_actual_collection_usage": {"requests": 0},
                "evaluation": {"evaluation_completed": True},
            }
            runner.validate_offline_completion(run_dir, valid)
            cases = [
                ("deadline", "execution", "outcome", "deadline"),
                ("incomplete_commands", "execution", "phase_turns", 3),
                ("extra_commands", "execution", "phase_turns", 5),
                ("actual_api_request", "new_actual_collection_usage", "requests", 1),
                ("incomplete_evaluation", "evaluation", "evaluation_completed", False),
            ]
            for name, section, field, value in cases:
                with self.subTest(name=name):
                    invalid = copy.deepcopy(valid)
                    invalid[section][field] = value
                    with self.assertRaisesRegex(RuntimeError, "expected synthetic publication"):
                        runner.validate_offline_completion(run_dir, invalid)
            for invalid_patch in ("", "consolidate_offline_probe.txt without a real diff header"):
                with self.subTest(patch=invalid_patch):
                    patch_path.write_text(invalid_patch)
                    with self.assertRaisesRegex(RuntimeError, "expected synthetic publication"):
                        runner.validate_offline_completion(run_dir, valid)


if __name__ == "__main__":
    unittest.main()
