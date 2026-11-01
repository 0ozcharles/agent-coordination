"""Focused organization, publication and immutable receipt tests: no API/Docker."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cross_task_packet
from cross_task_specs import get_task
import cross_task_workers
import luna_integrator_worker as worker
from run_baselines import save_state, digest


def agent_fixture(agent_id):
    call_id = "old_" + agent_id
    agent = SimpleNamespace(
        messages=[{"role": "system", "content": "Existing native system", "extra": {"keep": True}},
                  {"role": "user", "content": "Existing independent feature task"},
                  {"role": "assistant", "content": "Working", "tool_calls": [{"id": call_id,
                   "type": "function", "function": {"name": "bash", "arguments": '{"command":"true"}'}}]},
                  {"role": "tool", "tool_call_id": call_id, "content": "Existing observation"}],
        n_calls=3, cost=0.001, extra_template_vars={"task": "Original " + agent_id},
        _departed_announced=set(), _segments=[], _current_segment_messages=[], _compaction_count=0)
    agent.model = SimpleNamespace(format_message=lambda **kwargs: copy.deepcopy(kwargs))
    agent.add_messages = lambda *messages: agent.messages.extend(copy.deepcopy(messages))
    return agent


def packet_fixture(task, checkpoint):
    return {"schema_version": 2, "representation": "unified_diff", "context_lines": 3,
            "limits_bytes": {"packet": 131072, "source_file": 524288, "scratch_file": 65536},
            "checkpoint_sha256": digest(checkpoint), "checkpoint_only": True,
            "task_key": task.key, "base_commit": task.base,
            "agents": {a: {"original_task": "Original " + a, "scratch_files": [], "source_changes": [{
                "path": task.code_roots[0] + "/fixture.py", "change_type": "modified",
                "before_sha256": "a" * 64, "after_sha256": "b" * 64,
                "before_bytes": 4, "after_bytes": 4,
                "unified_diff": "diff --git a/fixture.py b/fixture.py\n--- a/fixture.py\n+++ b/fixture.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"}]}
                for a in worker.ACTIVE}}


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.task = get_task("llama_index17070_f1_f3")
        self.agents = {a: agent_fixture(a) for a in worker.ACTIVE}
        self.checkpoint = {"agents": {a: {"state": save_state(agent)} for a, agent in self.agents.items()}}
        self.packet = packet_fixture(self.task, self.checkpoint)

    def assign(self):
        return worker.apply_integrator_assignment(self.agents, self.checkpoint, self.task, remaining_seconds=731.125)

    def test_both_live_roles_preserve_every_original_state_field_and_same_packet(self):
        assignment = self.assign()
        contents = []
        for a, agent in self.agents.items():
            before = self.checkpoint["agents"][a]["state"]
            self.assertEqual(agent.messages[:-1], before["messages"])
            self.assertEqual(agent._departed_announced, set())
            after = save_state(agent)
            self.assertEqual({k: v for k, v in after.items() if k != "messages"},
                             {k: v for k, v in before.items() if k != "messages"})
            text = agent.messages[-1]["content"]
            self.assertIn("Both agent1 and agent2 remain active", text)
            self.assertIn("only agent1's published patch will be evaluated", text)
            self.assertIn("no monetary or model-turn limit", text)
            self.assertIn("feature 1 and feature 3", text)
            self.assertEqual(assignment["recipients"][a]["after_state_sha256"], digest(after))
            delivery = worker.inject_integrator_packet(agent, self.packet, a)
            self.assertEqual(delivery["condition"], "integrator")
            contents.append(agent.messages[-1]["content"])
            worker.verify_first_messages(agent.messages, contents[-1], assignment["recipients"][a], before["messages"])
        self.assertEqual(contents, [cross_task_packet.packet_message(self.packet)] * 2)

    def test_assignment_rejects_missing_peer_modified_history_and_pending_tools(self):
        with self.assertRaises(ValueError):
            worker.apply_integrator_assignment({"agent1": self.agents["agent1"]}, self.checkpoint,
                                               self.task, remaining_seconds=200)
        self.agents["agent1"].n_calls += 1
        with self.assertRaisesRegex(RuntimeError, "state changed"):
            self.assign()
        self.agents["agent1"].n_calls -= 1
        self.agents["agent1"].messages.pop()
        self.checkpoint["agents"]["agent1"]["state"] = save_state(self.agents["agent1"])
        with self.assertRaisesRegex(ValueError, "tool boundary|tool calls"):
            self.assign()

    def test_first_query_rejects_role_loss_partial_packet_and_history_change(self):
        assignment = self.assign()
        agent = self.agents["agent1"]
        worker.inject_integrator_packet(agent, self.packet, "agent1")
        text = cross_task_packet.packet_message(self.packet)
        history = self.checkpoint["agents"]["agent1"]["state"]["messages"]
        for index in (0, len(history), len(history) + 1):
            messages = copy.deepcopy(agent.messages)
            messages[index]["content"] = "changed"
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                worker.verify_first_messages(messages, text, assignment["recipients"]["agent1"], history)

    def test_first_query_fingerprint_contains_no_duplicate_raw_prompt(self):
        assignment = self.assign()
        agent = self.agents["agent1"]
        worker.inject_integrator_packet(agent, self.packet, "agent1")
        agent.model._query = Mock(return_value="synthetic response")
        with TemporaryDirectory() as directory:
            phase = Path(directory)
            worker.install_first_query_check(agent.model, cross_task_packet.packet_message(self.packet),
                assignment["recipients"]["agent1"], self.checkpoint["agents"]["agent1"]["state"]["messages"],
                "agent1", phase)
            self.assertEqual(agent.model._query(agent.messages), "synthetic response")
            record = json.loads((phase / "first_model_input_agent1.json").read_text())
            self.assertNotIn("messages", record)
            self.assertEqual(record["messages_sha256"], digest(worker._provider_messages(agent.messages)))
            self.assertFalse(record["contains_raw_messages"])


class PublicationTests(unittest.TestCase):
    def test_native_pr_branch_and_empty_publication_are_distinct(self):
        sha = "b" * 40
        for output, patch_text, expected in [
            (f"{sha}\trefs/heads/agent1\n{sha}\trefs/tags/pr/agent1\n", "code", "published_pr"),
            (f"{sha}\trefs/heads/agent1\n", "code", "published_branch_fallback"),
            (f"{sha}\trefs/heads/agent1\n", "", "no_published_changes")]:
            with self.subTest(expected=expected):
                env = SimpleNamespace(execute=Mock(return_value={"returncode": 0, "output": output}))
                record = worker.publication_provenance(env, "agent1", patch_text)
                self.assertEqual(record["strategy"], expected)
                self.assertFalse(record["unsubmitted_worktree_used"])
                self.assertIn("git ls-remote", env.execute.call_args.args[0]["command"])
        env = SimpleNamespace(execute=Mock(return_value={"returncode": 0, "output": ""}))
        with self.assertRaisesRegex(RuntimeError, "published branch"):
            worker.publication_provenance(env, "agent1", "nonempty")

    def test_empty_lead_never_selects_nonempty_peer(self):
        with TemporaryDirectory() as directory:
            phase = Path(directory)
            (phase / "agent1.patch").write_text("")
            (phase / "agent2.patch").write_text("peer-only code")
            publications = {a: {"patch_sha256": worker._sha((phase / f"{a}.patch").read_bytes())}
                            for a in worker.ACTIVE}
            delivery = worker.final_delivery_record(phase, publications)
            self.assertEqual(delivery["selection"], "predesignated_agent1")
            self.assertEqual(delivery["scored_patch"], "agent1.patch")
            self.assertEqual(delivery["patch_sha256"], worker._sha(b""))
            self.assertFalse(delivery["automatic_merge"])

    def test_offline_model_integration_is_agent1_tool_work_not_worker_copy(self):
        task = get_task("pillow68_f1_f2")
        lead, peer = (worker.offline_commands(task, a) for a in worker.ACTIVE)
        self.assertEqual(len(lead), 5)
        self.assertEqual(len(peer), 4)
        self.assertIn("git show origin/agent2:handoff_offline_agent2.txt", lead[2])
        self.assertIn("offline_integrator_peer_incorporated", lead[2])
        self.assertIn("gh pr create", lead[3])
        self.assertNotIn("offline_integrator_peer_incorporated", " ".join(peer))

    def test_wrapper_preserves_budget_binds_shared_publisher_and_rejects_bad_scope(self):
        task = get_task("pillow68_f1_f2")
        entered = []

        @contextmanager
        def context(actual):
            self.assertEqual(actual, task)
            entered.append(True)
            try:
                yield
            finally:
                entered.pop()

        def run(*args):
            self.assertTrue(entered)
            self.assertEqual(args, (Path("/tmp/integrator-fixture"), "integrator", task, True, 531.25))
            return 0

        with patch.object(cross_task_workers, "packet_module_context", context), patch.object(worker, "_phase_worker", side_effect=run):
            self.assertEqual(worker.phase_worker("/tmp/integrator-fixture", "integrator", task,
                                                 offline=True, execution_seconds=531.25), 0)
        self.assertFalse(entered)
        with patch.object(worker, "_phase_worker") as run:
            for condition, seconds in [("strongshare", 100), ("integrator", 0), ("integrator", True),
                                       ("integrator", float("inf"))]:
                with self.assertRaises(ValueError):
                    worker.phase_worker("/tmp/no-run", condition, task, execution_seconds=seconds)
            run.assert_not_called()


class ReceiptTests(unittest.TestCase):
    """Synthetic files exercise all receipt logic; only the old WIP gate is mocked."""
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name).resolve()
        self.phase = self.run / "integrator"
        self.phase.mkdir()
        self.task = get_task("llama_index17070_f1_f3")
        self.agents = {a: agent_fixture(a) for a in worker.ACTIVE}
        cp = {"task": self.task.as_dict(), "broker": {"messages": []},
              "git_archive_sha256": worker._sha(b"fixture archive"), "agents": {a: {
                  "state": save_state(agent), "manifest": {"fixture": a}, "image": "sha256:" + a}
                  for a, agent in self.agents.items()}}
        self.checkpoint = cp
        self.patch_gate = patch.object(worker, "validate_checkpoint")
        self.patch_gate.start()
        self.addCleanup(self.patch_gate.stop)
        (self.run / "shared_git.tar").write_bytes(b"fixture archive")
        self.write(self.run / "checkpoint.json", cp)
        self.write(self.run / "checkpoint_provenance.json", {"checkpoint_sha256": digest(cp)})
        self.config = {"task": self.task.as_dict(), "condition": "integrator", "condition_version": worker.CONDITION_VERSION,
            "execution_seconds": 600.0, "offline_selftest": True, "model": "offline-scripted-model",
            "checkpoint_sha256": digest(cp)}
        self.write(self.run / "task_config.json", self.config)
        assignment = worker.apply_integrator_assignment(self.agents, cp, self.task, remaining_seconds=594.5)
        self.write(self.phase / "integrator_assignment.json", assignment)
        packet = packet_fixture(self.task, cp)
        text = cross_task_packet.packet_message(packet)
        self.write(self.phase / "handoff_packet.json", packet)
        (self.phase / "handoff_packet.txt").write_text(text)
        delivery = {"condition": "integrator", "checkpoint_sha256": digest(cp), "packet_sha256": digest(packet),
                    "source_checkpoint_verified": True, "code_automatically_transferred": False,
                    "first_request_verified": True, "recipients": {}}
        restore = {"checkpoint_hash": digest(cp), "broker_hash": digest(cp["broker"]), "agents": {}}
        publications = {}
        for a, agent in self.agents.items():
            saved = cp["agents"][a]
            restore["agents"][a] = {"state_hash": digest(saved["state"]), "filesystem_manifest_hash": digest(saved["manifest"]),
                "source_image": saved["image"], "filesystem_verified": True, "state_verified": True, "state_use": "resumed_model_loop"}
            delivery["recipients"][a] = worker.inject_integrator_packet(agent, packet, a)
            agent.model._query = Mock(return_value=None)
            worker.install_first_query_check(agent.model, text, assignment["recipients"][a], saved["state"]["messages"], a, self.phase)
            agent.model._query(agent.messages)
            agent.add_messages({"role": "assistant", "content": "Synthetic continuation"})
            self.write(self.phase / f"{a}.traj.json", {"messages": agent.messages})
            self.write(self.phase / "evidence_manifests" / f"{a}_checkpoint.json", {"fixture": a})
            (self.phase / f"{a}.patch").write_text(a + " published code")
            env = SimpleNamespace(execute=lambda action, a=a: {"returncode": 0, "output": "b" * 40 + "\trefs/heads/" + a})
            publications[a] = worker.publication_provenance(env, a, a + " published code")
        self.write(self.phase / "restore_proof.json", restore)
        self.write(self.phase / "handoff_delivery.json", delivery)
        final = worker.final_delivery_record(self.phase, publications)
        self.write(self.phase / "final_delivery.json", final)
        outcomes = {a: {"status": "Submitted", "infrastructure_error": False, "phase_turns": 1} for a in worker.ACTIVE}
        self.status = {**self.config, "phase": "integrator", "status": "completed", "handoff_variant": worker.VARIANT,
            "active_agents": list(worker.ACTIVE), "authoritative_agent": "agent1", "peer_retired": False,
            "initialization_included_in_budget": True, "continuation_budget_seconds": 600.0,
            "handoff_packet_sha256": digest(packet), "outcomes": outcomes,
            "final_patch_sha256": final["patch_sha256"], "peer_patch_sha256": final["peer_patch_sha256"],
            "delivery_selection": "predesignated_agent1"}
        self.write(self.phase / "status.json", self.status)
        self.write(self.phase / "agent_outcomes.json", outcomes)
        self.write(self.phase / "messages.json", {"fixture": "native broker"})
        guard = worker.BudgetGuard(self.run / "usage_ledger.json", self.run.name, model=worker.MODEL,
                                  enforce_limits=False, max_output_tokens=worker.MAX_OUTPUT)
        guard._persist()
        self.write(self.phase / "usage_summary.json", worker.usage(self.run, "integrator"))
        self.write(self.phase / "source_manifest.json", {"luna_integrator_worker.py": worker._sha(Path(worker.__file__).read_bytes())})

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def freeze(self):
        return worker.freeze_execution_receipt(self.phase, self.task)

    def test_freeze_then_readonly_verify_and_no_implicit_creation(self):
        with self.assertRaises(RuntimeError):
            worker.verify_worker_artifacts(self.phase, self.task)
        self.assertFalse((self.phase / "execution_receipt.json").exists())
        receipt = self.freeze()
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.run.rglob("*") if p.is_file()}
        self.assertEqual(worker.verify_worker_artifacts(self.phase, self.task), receipt)
        self.assertEqual(worker.freeze_execution_receipt(self.phase, self.task), receipt)
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.run.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(receipt["checkpoint_sha256"], digest(self.checkpoint))

    def test_peer_tamper_invalidates_even_unchanged_authoritative_code(self):
        self.freeze()
        (self.phase / "agent2.patch").write_text("replacement peer")
        with self.assertRaises(RuntimeError):
            worker.verify_worker_artifacts(self.phase, self.task)

    def test_changed_history_role_and_authority_rejected_before_receipt(self):
        path = self.phase / "integrator_assignment.json"
        original = json.loads(path.read_text())
        for change in ({"authoritative_agent": "agent2"}, {"peer_retired": True}, {"automatic_merge": True}):
            self.write(path, {**original, **change})
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.freeze()
        self.write(path, original)
        trpath = self.phase / "agent1.traj.json"
        trajectory = json.loads(trpath.read_text())
        trajectory["messages"][0]["content"] = "Changed system"
        self.write(trpath, trajectory)
        with self.assertRaisesRegex(RuntimeError, "fingerprint|history"):
            self.freeze()

    def test_offline_nonzero_accounting_and_wrong_model_are_rejected(self):
        ledgerpath = self.run / "usage_ledger.json"
        ledger = json.loads(ledgerpath.read_text())
        ledger["requests"] = [{"phase": "integrator:agent1", "actual_cost_usd": 0.01}]
        self.write(ledgerpath, ledger)
        with self.assertRaisesRegex(RuntimeError, "Offline integrator"):
            self.freeze()
        ledger["requests"] = []
        ledger["model"] = "different-model"
        self.write(ledgerpath, ledger)
        with self.assertRaisesRegex(RuntimeError, "wrong model"):
            self.freeze()

    def test_missing_real_transport_input_fails_even_with_synthetic_model_proof(self):
        self.config.update(offline_selftest=False, model=worker.MODEL)
        self.status.update(offline_selftest=False, model=worker.MODEL)
        self.write(self.run / "task_config.json", self.config)
        self.write(self.phase / "status.json", self.status)
        with self.assertRaisesRegex(RuntimeError, "Actual provider input"):
            self.freeze()

    def test_source_and_symlink_changes_are_rejected(self):
        self.write(self.phase / "source_manifest.json", {"luna_integrator_worker.py": "0" * 64})
        with self.assertRaisesRegex(RuntimeError, "Frozen source"):
            self.freeze()
        self.write(self.phase / "source_manifest.json", {"luna_integrator_worker.py": worker._sha(Path(worker.__file__).read_bytes())})
        original = self.phase / "agent1.patch"
        renamed = self.phase / "agent1-real.patch"
        original.rename(renamed)
        original.symlink_to(renamed)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.freeze()


if __name__ == "__main__":
    unittest.main()
