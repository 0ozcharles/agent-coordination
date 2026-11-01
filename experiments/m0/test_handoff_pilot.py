"""Offline handoff evidence and prompt-contract tests; no Docker or model calls."""

import copy
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from baseline_runtime import MemoryBroker
from run_baselines import digest, load_state, save_state
from run_consolidate import apply_handoff
import handoff_packet as handoff
from handoff_worker import install_first_query_check
import evaluate_task_expansion as evaluator
import run_handoff_pilot as supervisor
import task_specs
from task_specs import TaskSpec


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def manifest(source_files, scratch_files):
    def rows(files):
        return [{"path": path, "type": "file", "mode": 0o644, "uid": 0, "gid": 0,
                 "size": len(text.encode()), "sha256": sha(text)}
                for path, text in sorted(files.items())]

    roots = {"/workspace/repo": rows(source_files), "/tmp": rows(scratch_files)}
    return {"schema_version": 1, "roots": roots,
            "sha256": hashlib.sha256(json.dumps(roots, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def sample_agent(agent_id, task_text):
    calls = [{"id": "call_" + agent_id + str(number), "type": "function",
              "function": {"name": "bash", "arguments": json.dumps({"command": "printf fixture"})}}
             for number in (1, 2)]
    agent = SimpleNamespace(
        messages=[{"role": "system", "content": "Original split responsibilities", "extra": {"kept": True}},
                  {"role": "user", "content": task_text},
                  {"role": "assistant", "content": "Original work", "tool_calls": calls},
                  {"role": "tool", "tool_call_id": calls[0]["id"], "content": "First completed observation"},
                  {"role": "tool", "tool_call_id": calls[1]["id"], "content": "Second completed observation"}],
        n_calls=4, cost=0.012, sent_messages=["Historical peer communication"],
        extra_template_vars={"task": task_text, "agent_id": agent_id, "git_enabled": True,
                             "messaging_enabled": True, "agents": ["agent1", "agent2"]},
        _last_prompt_tokens=1200, _submit_nudges=0, _departed_announced=set(),
        _segments=[], _current_segment_messages=[], _compaction_count=0,
    )
    agent.model = SimpleNamespace(
        format_message=mock.Mock(side_effect=lambda **message: copy.deepcopy(message)),
        query=mock.Mock(side_effect=AssertionError("Unit tests must not call a model")),
    )
    agent.add_messages = lambda *messages: agent.messages.extend(copy.deepcopy(messages))
    return agent


class LocalGitEnvironment:
    """Execute only the collector's Python command inside a temporary Git repository."""

    def __init__(self, path, scratch_root=None):
        self.path = path
        self.scratch_root = scratch_root
        self.commands = []

    def execute(self, action):
        command = shlex.split(action["command"])
        if len(command) != 3 or command[:2] != ["python", "-c"]:
            raise AssertionError("Collector must use one standalone Python command")
        self.commands.append(action["command"])
        if self.scratch_root is not None:
            # Docker /tmp is an ordinary directory, whereas macOS /tmp is an
            # alias. Redirect that fixed root only; retain all path checks.
            if command[2].count("Path('/tmp')") != 1:
                raise AssertionError("Unexpected scratch collector root expression")
            command[2] = command[2].replace("Path('/tmp')", "Path(" + repr(str(self.scratch_root)) + ")")
        result = subprocess.run([sys.executable, *command[1:]], cwd=self.path,
                                capture_output=True, text=True, timeout=20)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


class SourceCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.original = {
            "pkg/committed.py": "value = 1\n", "pkg/staged.py": "value = 2\n",
            "pkg/unstaged.py": "value = 3\n", "pkg/deleted.py": "value = 4\n",
            "pkg/tests/test_internal.py": "assert True\n",
            "outside/hidden.py": "value = 'OUTSIDE_ORIGINAL'\n",
        }
        for path, text in self.original.items():
            write(self.root / path, text)
        self.git("add", ".")
        self.commit("Fixture base")
        self.task = TaskSpec("fixture", "fixture_repo", 1, (3, 7), "fixture-image",
                             self.git("rev-parse", "HEAD").strip(), ("pkg",))
        self.env = LocalGitEnvironment(self.root)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE)

    def commit(self, message):
        self.git("-c", "user.name=Offline test", "-c", "user.email=offline@example.invalid",
                 "commit", "-qm", message)

    def test_reads_full_current_source_across_commit_index_worktree_and_untracked_states(self):
        current = {"pkg/committed.py": "value = 10\n", "pkg/staged.py": "value = 20\n",
                   "pkg/unstaged.py": "value = 30\n", "pkg/new.py": "value = 50\n"}
        write(self.root / "pkg/committed.py", current["pkg/committed.py"])
        self.git("add", "pkg/committed.py")
        self.commit("Unpublished implementation")
        write(self.root / "pkg/staged.py", "value = 19\n")
        self.git("add", "pkg/staged.py")
        # The final working file, rather than the older staged text, is the checkpoint state.
        for path, text in current.items():
            write(self.root / path, text)
        (self.root / "pkg/deleted.py").unlink()
        write(self.root / "pkg/tests/test_internal.py", "assert 'HIDDEN_TEST_SENTINEL'\n")
        write(self.root / "outside/hidden.py", "value = 'OUTSIDE_SENTINEL'\n")
        write(self.root / "pkg/result.json", '{"score": "FUTURE_RESULT_SENTINEL"}')
        before = self.git("status", "--porcelain=v1")

        changes = handoff.collect_source_changes(self.env, self.task)

        by_path = {row["path"]: row for row in changes}
        self.assertEqual(set(by_path), set(current) | {"pkg/deleted.py"})
        for path, text in current.items():
            self.assertEqual(by_path[path]["after"], text)
            self.assertEqual(by_path[path]["before"], self.original.get(path))
        self.assertIsNone(by_path["pkg/deleted.py"]["after"])
        self.assertEqual(by_path["pkg/deleted.py"]["before"], self.original["pkg/deleted.py"])
        self.assertEqual(self.git("status", "--porcelain=v1"), before)
        self.assertNotIn("SENTINEL", json.dumps(changes))

    def test_clean_tree_is_empty_and_collection_failure_is_not_silent_empty_evidence(self):
        self.assertEqual(handoff.collect_source_changes(self.env, self.task), [])
        for output in ({"returncode": 1, "output": "git failed"},
                       {"returncode": 0, "output": "not json"}):
            with self.subTest(output=output):
                broken = SimpleNamespace(execute=mock.Mock(return_value=output))
                with self.assertRaises((ValueError, RuntimeError)):
                    handoff.collect_source_changes(broken, self.task)


class PacketFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patcher = mock.patch.object(task_specs, "REPO", self.root / "benchmark")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = TaskSpec("fixture", "fixture_repo", 1, (3, 7), "fixture-image", "a" * 40, ("pkg",))
        self.tasks = {"agent1": "Implement feature 3: ORIGINAL_EMAIL_REQUIREMENT",
                      "agent2": "Implement feature 7: ORIGINAL_HASH_REQUIREMENT"}
        self.agents = {agent_id: sample_agent(agent_id, text) for agent_id, text in self.tasks.items()}
        self.changes = {
            "agent1": [{"path": "pkg/core.py", "before": "value = 1\n", "after": "value = 2\n"}],
            "agent2": [{"path": "pkg/core.py", "before": "value = 1\n", "after": "value = 3\n"},
                       {"path": "pkg/new_hash.py", "before": None, "after": "new_hash = True\n"}],
        }
        self.scratch = {"agent1": [{"path": "email_draft.py", "content": "draft_email = True\n"}],
                        "agent2": [{"path": "hash_draft.py", "content": "draft_hash = True\n"},
                                   {"path": "notes.txt", "content": "Checkpoint-only work notes\n"}]}
        self.checkpoint = {"task": self.task.as_dict(), "agents": {}, "broker": MemoryBroker(["agent1", "agent2"]).snapshot(),
                           "git_archive_sha256": "b" * 64, "prefix_steps_per_agent": 4,
                           "wip_checkpoint": {"eligible": True, "paired_round": 4, "progress": {}, "outcomes": {}}}
        for agent_id, feature in self.task.agents.items():
            write(self.task.task_dir / f"feature{feature}/feature.md", self.tasks[agent_id])
            self.refresh_saved_files(agent_id)

    def refresh_saved_files(self, agent_id):
        changes = self.changes[agent_id]
        source_files = {row["path"]: row["after"] for row in changes if row["after"] is not None}
        scratch_files = {row["path"]: row["content"] for row in self.scratch[agent_id]}
        self.checkpoint["agents"][agent_id] = {
            "state": save_state(self.agents[agent_id]), "git_base": self.task.base,
            "image": "sha256:" + ("1" if agent_id == "agent1" else "2") * 64,
            "manifest": manifest(source_files, scratch_files),
        }
        self.checkpoint["wip_checkpoint"]["progress"][agent_id] = {
            "changed_files": [{"path": row["path"], "before_sha256": sha(row["before"] or ""),
                               "after_sha256": sha(row["after"] or ""), "untracked": row["before"] is None,
                               "deleted": row["after"] is None} for row in changes],
            "has_code_change": True, "head": self.task.base, "git_status": " M pkg/core.py\n",
        }
        self.checkpoint["wip_checkpoint"]["outcomes"][agent_id] = {
            "submitted": False, "exited": False, "infrastructure_error": False}

    def packet(self, checkpoint=None, changes=None, scratch=None):
        return handoff.build_packet(checkpoint if checkpoint is not None else self.checkpoint, self.task,
                                    changes if changes is not None else self.changes,
                                    scratch if scratch is not None else self.scratch)


class PacketCompletenessTests(PacketFixture):
    def test_packet_contains_both_original_requirements_full_changes_and_scratch_without_mutation(self):
        before = copy.deepcopy((self.checkpoint, self.changes, self.scratch))
        packet = self.packet()
        content = handoff.packet_message(packet)
        for requirement in self.tasks.values():
            self.assertIn(requirement, content)
        for agent_id in self.task.agents:
            self.assertIn(agent_id, content)
            for row in self.changes[agent_id]:
                self.assertIn(row["path"], content)
                for key in ("before", "after"):
                    if row[key]:
                        self.assertIn(row[key].strip(), content)
            for row in self.scratch[agent_id]:
                self.assertIn(row["path"], content)
                self.assertIn(row["content"].strip(), content)
        self.assertEqual((self.checkpoint, self.changes, self.scratch), before)
        self.assertEqual(self.packet(), packet)
        self.assertEqual(handoff.packet_message(self.packet()), content)
        self.assertIn(digest(self.checkpoint), json.dumps(packet))

    def test_code_and_draft_content_are_not_summarized_or_truncated(self):
        text = "# FULL_TEXT_BEGIN\n" + "\n".join(f"value_{number} = {number}" for number in range(1200)) + "\n# FULL_TEXT_END\n"
        self.changes["agent1"][0]["after"] = text
        self.scratch["agent2"][0]["content"] = text
        self.refresh_saved_files("agent1")
        self.refresh_saved_files("agent2")
        packet = self.packet()
        encoded = json.dumps(packet, ensure_ascii=False)
        # Check raw structured data, independent of whether rendering uses JSON escapes.
        self.assertIn(json.dumps(text, ensure_ascii=False), encoded)
        content = handoff.packet_message(packet)
        for marker in ("FULL_TEXT_BEGIN", "value_0", "value_600", "value_1199", "FULL_TEXT_END"):
            self.assertIn(marker, content)

    def test_missing_agent_or_dropped_source_or_scratch_is_rejected(self):
        cases = []
        missing_agent = copy.deepcopy(self.changes)
        del missing_agent["agent2"]
        cases.append((missing_agent, self.scratch))
        missing_source = copy.deepcopy(self.changes)
        missing_source["agent2"].pop()
        cases.append((missing_source, self.scratch))
        missing_scratch = copy.deepcopy(self.scratch)
        missing_scratch["agent2"].pop()
        cases.append((self.changes, missing_scratch))
        for changes, scratch in cases:
            with self.subTest(changes=changes, scratch=scratch):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.packet(changes=changes, scratch=scratch)

    def test_changed_before_after_draft_or_manifest_is_rejected(self):
        for field in ("before", "after"):
            changes = copy.deepcopy(self.changes)
            changes["agent1"][0][field] += "# altered after snapshot\n"
            with self.subTest(field=field), self.assertRaises((ValueError, RuntimeError)):
                self.packet(changes=changes)
        scratch = copy.deepcopy(self.scratch)
        scratch["agent2"][0]["content"] += "tampered\n"
        with self.assertRaises((ValueError, RuntimeError)):
            self.packet(scratch=scratch)
        checkpoint = copy.deepcopy(self.checkpoint)
        checkpoint["agents"]["agent1"]["manifest"]["roots"]["/workspace/repo"][0]["sha256"] = "f" * 64
        with self.assertRaises((ValueError, RuntimeError)):
            self.packet(checkpoint=checkpoint)

    def test_only_original_feature_text_is_read_not_reference_tests_or_later_outcomes(self):
        prohibited = [self.task.task_dir / "combined.patch", self.task.task_dir / "feature3/feature.patch",
                      self.task.task_dir / "feature7/tests.patch", self.root / "later/evaluation_summary.json"]
        for path in prohibited:
            write(path, "FUTURE_EVALUATOR_OR_REFERENCE_SENTINEL")
        original_read = Path.read_text
        allowed = {self.task.task_dir / f"feature{feature}/feature.md" for feature in self.task.features}
        reads = []

        def guarded_read(path, *args, **kwargs):
            reads.append(path)
            if path not in allowed:
                raise AssertionError("Packet builder tried to read a non-requirement host file: " + str(path))
            return original_read(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded_read):
            content = handoff.packet_message(self.packet())
        self.assertNotIn("FUTURE_EVALUATOR_OR_REFERENCE_SENTINEL", content)
        self.assertTrue(set(reads) <= allowed)


class ScratchCollectorTests(PacketFixture):
    def setUp(self):
        super().setUp()
        self.scratch_root = self.root / "scratch"
        self.scratch_root.mkdir()
        self.env = LocalGitEnvironment(self.root, self.scratch_root)
        for row in self.scratch["agent2"]:
            write(self.scratch_root / row["path"], row["content"])

    def test_reads_every_declared_text_draft_but_not_new_files_outside_checkpoint(self):
        write(self.scratch_root / "later_result.txt", "FUTURE_OUTCOME_SENTINEL")
        files = handoff.collect_scratch_files(self.env, self.checkpoint["agents"]["agent2"])
        self.assertEqual(sorted(files, key=lambda row: row["path"]),
                         sorted(self.scratch["agent2"], key=lambda row: row["path"]))
        self.assertNotIn("FUTURE_OUTCOME_SENTINEL", json.dumps(files))

    def test_deleted_or_symlink_substituted_draft_is_not_silently_omitted(self):
        target = self.scratch_root / "hash_draft.py"
        target.unlink()
        with self.assertRaises((ValueError, RuntimeError)):
            handoff.collect_scratch_files(self.env, self.checkpoint["agents"]["agent2"])
        target.symlink_to("notes.txt")
        with self.assertRaises((ValueError, RuntimeError)):
            handoff.collect_scratch_files(self.env, self.checkpoint["agents"]["agent2"])

    def test_modified_restored_draft_is_collected_then_rejected_against_checkpoint_hash(self):
        write(self.scratch_root / "hash_draft.py", "draft_hash = 'post-checkpoint-change'\n")
        scratch = copy.deepcopy(self.scratch)
        scratch["agent2"] = handoff.collect_scratch_files(self.env, self.checkpoint["agents"]["agent2"])
        with self.assertRaises((ValueError, RuntimeError)):
            self.packet(scratch=scratch)

    def test_declared_official_artifact_names_are_rejected_before_any_scratch_read(self):
        for name in ("tests.patch", "solution.patch", "reference.patch", "evaluation_summary.json"):
            with self.subTest(name=name):
                saved = copy.deepcopy(self.checkpoint["agents"]["agent2"])
                saved["manifest"] = manifest({}, {name: "DO_NOT_READ_REFERENCE_SENTINEL"})
                environment = SimpleNamespace(execute=mock.Mock(side_effect=AssertionError("Reference must not be read")))
                with self.assertRaises((ValueError, RuntimeError)):
                    handoff.collect_scratch_files(environment, saved)
                environment.execute.assert_not_called()


class PacketInjectionTests(PacketFixture):
    def test_identical_packet_is_appended_to_both_shared_agents_and_the_consolidated_agent(self):
        packet = self.packet()
        checkpoint_before = copy.deepcopy(self.checkpoint)
        message = handoff.packet_message(packet)
        deliveries = []
        for condition, agent_id in (("strongshare", "agent1"), ("strongshare", "agent2"),
                                    ("consolidate", "agent1")):
            agent = sample_agent(agent_id, self.tasks[agent_id])
            load_state(agent, self.checkpoint["agents"][agent_id]["state"])
            if condition == "consolidate":
                broker = MemoryBroker.from_snapshot(self.checkpoint["broker"])
                apply_handoff(agent, broker, self.checkpoint)
            before = save_state(agent)
            receipt = handoff.inject_packet(agent, packet, condition, agent_id)
            after = save_state(agent)
            self.assertEqual(agent.messages[:-1], before["messages"])
            self.assertEqual(agent.messages[-1]["role"], "user")
            self.assertEqual(agent.messages[-1]["content"], message)
            self.assertEqual([i for i, entry in enumerate(agent.messages) if entry["role"] == "system"], [0])
            self.assertEqual({k: v for k, v in after.items() if k != "messages"},
                             {k: v for k, v in before.items() if k != "messages"})
            self.assertTrue(receipt)
            agent.model.query.assert_not_called()
            deliveries.append(agent.messages[-1]["content"])
        self.assertEqual(deliveries, [message] * 3)
        self.assertEqual(self.checkpoint, checkpoint_before)

    def test_pending_tool_call_is_rejected_before_message_history_is_modified(self):
        packet = self.packet()
        agent = sample_agent("agent1", self.tasks["agent1"])
        agent.messages.pop()  # The second tool call has not returned yet.
        before = save_state(agent)
        with self.assertRaises((ValueError, RuntimeError)):
            handoff.inject_packet(agent, packet, "strongshare", "agent1")
        self.assertEqual(save_state(agent), before)
        agent.model.format_message.assert_not_called()
        agent.model.query.assert_not_called()

    def test_late_system_message_is_rejected_without_silently_rewriting_old_history(self):
        agent = sample_agent("agent1", self.tasks["agent1"])
        agent.messages.append({"role": "system", "content": "A late system message"})
        before = save_state(agent)
        with self.assertRaises((ValueError, RuntimeError)):
            handoff.inject_packet(agent, self.packet(), "strongshare", "agent1")
        self.assertEqual(save_state(agent), before)


class FirstQueryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.packet = "Complete checkpoint packet: 双方源码\nfirst line\nlast line\n"
        self.history = [{"role": "system", "content": "Original instruction"},
                        {"role": "user", "content": "Original requirement"},
                        {"role": "user", "content": self.packet}]
        self.original = mock.Mock(return_value={"mock_response": True})
        self.model = SimpleNamespace(_query=self.original)
        install_first_query_check(self.model, self.packet, "agent1", self.root)

    def test_actual_first_query_payload_is_checked_and_recorded_before_transport(self):
        history = copy.deepcopy(self.history)
        result = self.model._query(history, tools=[{"type": "function"}], temperature=0.7)
        self.assertEqual(result, {"mock_response": True})
        self.original.assert_called_once_with(history, tools=[{"type": "function"}], temperature=0.7)
        receipt_path = self.root / "first_request_agent1.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertTrue(receipt["verified"])
        self.assertTrue(receipt["full_packet_present"])
        self.assertEqual(receipt["message_index"], 2)
        self.assertEqual(receipt["message_sha256"], sha(self.packet))
        self.assertEqual(receipt["message_bytes"], len(self.packet.encode()))
        self.assertEqual(history, self.history)
        receipt_before = receipt_path.read_bytes()
        # Later calls may be reformatted or extend the conversation. The guard
        # certifies the first delivery, rather than relying on a fixed position.
        later = [{"role": "system", "content": "Original instruction"},
                 {"role": "user", "content": "A later turn"}]
        self.model._query(later)
        self.assertEqual(self.original.call_count, 2)
        self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_missing_truncated_duplicated_or_wrong_role_packet_never_reaches_transport(self):
        cases = {
            "missing": self.history[:-1],
            "truncated": self.history[:-1] + [{"role": "user", "content": self.packet[:-3]}],
            "duplicate": self.history + [{"role": "user", "content": self.packet}],
            "wrong_role": self.history[:-1] + [{"role": "assistant", "content": self.packet}],
        }
        for name, history in cases.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(RuntimeError, "packet missing"):
                    self.model._query(copy.deepcopy(history))
                self.original.assert_not_called()
                self.assertFalse((self.root / "first_request_agent1.json").exists())


class CompletedEvaluationTests(unittest.TestCase):
    """Cached scores must still match inputs and the unchanged official result."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.phase = Path(self.temp.name).resolve() / "job/strongshare"
        self.attempt = self.phase / "evaluation_attempts/fixture"
        self.attempt.mkdir(parents=True)
        self.task = task_specs.get_task("dirty_equals43_f3_f7")
        self.config = {"protocol_sha256": "c" * 64, "offline_selftest": True}
        self.current = {"patches": {"agent3.patch": "original", "agent7.patch": "original"},
                        "protected_files": {"receipt": "original"}, "evaluator_sources": {"source": "original"}}
        self.native = {"setting": "coop", "both_passed": False,
                       "merge": {"status": "success", "strategy": "direct"},
                       "feature1": {"passed": True, "tests_passed": 60, "tests_failed": 0, "exit_code": 0},
                       "feature2": {"passed": False, "tests_passed": 70, "tests_failed": 1, "exit_code": 1}}
        for name in ("official_evaluation.json", "evaluation.json"):
            self.write(self.attempt / name, self.native)
        self.write(self.phase / "evaluation.json", self.native)
        self.summary = evaluator.summarize(self.native, "failed", self.phase, self.attempt, 3.25,
                                           self.task, self.config)
        for directory in (self.phase, self.attempt):
            self.write(directory / "evaluation_summary.json", self.summary)
        self.write(self.attempt / "evaluation_input.json", {
            "input_manifest": self.current, "task": self.task.as_dict(),
            "protocol_sha256": self.config["protocol_sha256"]})
        self.write(self.attempt / "integrity.json", {
            "unchanged": True, "before": self.current, "after": self.current})
        for patcher in (
            mock.patch.object(evaluator, "load_task", return_value=(self.task, self.config)),
            mock.patch.object(evaluator, "patch_info", return_value={}),
            mock.patch.object(evaluator, "input_manifest", side_effect=lambda *_: copy.deepcopy(self.current)),
            mock.patch.object(evaluator, "verify_upstream"),
            mock.patch.object(supervisor, "run_command", side_effect=AssertionError("Cached score must not run a process")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, value):
        write(path, json.dumps(value))

    def test_unchanged_failed_official_outcome_is_reused_without_running_evaluation(self):
        self.assertEqual(supervisor.completed_evaluation(self.phase, self.task), self.summary)
        supervisor.run_command.assert_not_called()

    def test_tampered_feature_summary_is_rejected_even_when_both_summary_copies_match(self):
        altered = copy.deepcopy(self.summary)
        altered["feature_results"]["7"]["tests_failed"] = 0
        for directory in (self.phase, self.attempt):
            self.write(directory / "evaluation_summary.json", altered)
        with self.assertRaisesRegex(RuntimeError, "official native result"):
            supervisor.completed_evaluation(self.phase, self.task)

    def test_changed_native_result_or_current_input_cannot_reuse_a_completed_score(self):
        altered = copy.deepcopy(self.native)
        altered["feature2"]["passed"] = True
        self.write(self.attempt / "official_evaluation.json", altered)
        with self.assertRaisesRegex(RuntimeError, "accepted result"):
            supervisor.completed_evaluation(self.phase, self.task)
        self.write(self.attempt / "official_evaluation.json", self.native)
        self.current["patches"]["agent7.patch"] = "replaced after evaluation"
        with self.assertRaisesRegex(RuntimeError, "inputs or evaluator sources changed"):
            supervisor.completed_evaluation(self.phase, self.task)


if __name__ == "__main__":
    unittest.main()
