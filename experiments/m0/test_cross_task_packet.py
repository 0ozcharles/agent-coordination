"""Diff packet reconstruction and delivery tests, without Docker or paid calls."""
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

import cross_task_packet as packet
import handoff_packet as legacy
from run_baselines import digest
import task_specs
from task_specs import TaskSpec


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode())


def manifest(files, scratch):
    def rows(values):
        return [{"path": path, "type": "file", "mode": 0o644, "uid": 0, "gid": 0,
                 "sha256": sha(text), "size": len(text.encode())} for path, text in sorted(values.items())]
    roots = {"/workspace/repo": rows(files), "/tmp": rows(scratch)}
    return {"schema_version": 1, "roots": roots,
            "sha256": hashlib.sha256(json.dumps(roots, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


class LocalEnvironment:
    def __init__(self, root, scratch_root=None):
        self.root, self.scratch_root = root, scratch_root

    def execute(self, action):
        command = shlex.split(action["command"])
        if len(command) != 3 or command[:2] != ["python", "-c"]:
            raise AssertionError("Collector must use one Python script")
        if self.scratch_root is not None:
            if command[2].count("Path('/tmp')") != 1:
                raise AssertionError("Unexpected scratch root")
            command[2] = command[2].replace("Path('/tmp')", "Path(" + repr(str(self.scratch_root)) + ")")
        result = subprocess.run([sys.executable, *command[1:]], cwd=self.root,
                                capture_output=True, text=True, timeout=20)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


class PacketFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patcher = mock.patch.object(task_specs, "REPO", self.root / "upstream")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = TaskSpec("fixture", "fixture_repo", 1, (1, 2), "fixture-image", "a" * 40, ("pkg",))
        self.requirements = {"agent1": "Original first feature requirement", "agent2": "Original second feature requirement"}
        self.changes = {a: [{"path": "pkg/core.py", "before": "value = 1\n", "after": f"value = {n}\n"}]
                        for a, n in (("agent1", 2), ("agent2", 3))}
        self.scratch = {a: [{"path": a + "_draft.py", "content": f"# full {a} scratch\ndraft = 1\n"}]
                        for a in packet.AGENTS}
        self.checkpoint = {"task": self.task.as_dict(), "agents": {},
                           "wip_checkpoint": {"progress": {}}}
        for agent, feature in self.task.agents.items():
            write(self.task.task_dir / f"feature{feature}/feature.md", self.requirements[agent])
        self.refresh()

    def refresh(self):
        for agent in packet.AGENTS:
            changes = self.changes[agent]
            self.checkpoint["agents"][agent] = {
                "git_base": self.task.base,
                "state": {"extra_template_vars": {"task": self.requirements[agent]}},
                "manifest": manifest({r["path"]: r["after"] for r in changes if r["after"] is not None},
                                     {r["path"]: r["content"] for r in self.scratch[agent]}),
            }
            self.checkpoint["wip_checkpoint"]["progress"][agent] = {
                "changed_files": [{"path": r["path"], "before_sha256": sha(r["before"] or ""),
                                   "after_sha256": sha(r["after"] or ""), "untracked": r["before"] is None,
                                   "deleted": r["after"] is None} for r in changes]}

    def build(self):
        return packet.build_packet(self.checkpoint, self.task, self.changes, self.scratch)


class ReconstructionTests(PacketFixture):
    def roundtrip(self, path, before, after):
        self.changes["agent1"] = [{"path": path, "before": before, "after": after}]
        self.refresh()
        record = self.build()["agents"]["agent1"]["source_changes"][0]
        self.assertEqual(record["before_sha256"], None if before is None else sha(before))
        self.assertEqual(record["after_sha256"], None if after is None else sha(after))
        self.assertEqual(record["before_bytes"], None if before is None else len(before.encode()))
        self.assertEqual(record["after_bytes"], None if after is None else len(after.encode()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
            target = root / path
            if before is not None:
                write(target, before)
            for arguments in (("--check",), ()):
                result = subprocess.run(["git", "apply", *arguments, "-"], cwd=root,
                                        input=record["unified_diff"].encode(), capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(target.read_bytes() if target.exists() else None,
                             None if after is None else after.encode())
            # A reverse application checks that deletion and missing-newline
            # information was preserved, rather than merely producing a file.
            result = subprocess.run(["git", "apply", "-R", "-"], cwd=root,
                                    input=record["unified_diff"].encode(), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(target.read_bytes() if target.exists() else None,
                             None if before is None else before.encode())
        return record["unified_diff"]

    def test_added_deleted_empty_and_missing_final_newline_files_roundtrip(self):
        cases = [(None, "new = 1\n"), (None, "new = 1"), ("old = 1\n", None),
                 ("old = 1", None), (None, ""), ("", None),
                 ("value = 1", "value = 1\n"), ("value = 1\n", "value = 1"),
                 ("old = 1", "new = 2"), ("", "new = 2"), ("old = 1", "")]
        for before, after in cases:
            with self.subTest(before=before, after=after):
                diff = self.roundtrip("pkg/core.py", before, after)
                if any(text and not text.endswith("\n") for text in (before, after)):
                    self.assertIn("\\ No newline at end of file\n", diff)

    def test_multiple_hunks_three_context_lines_and_unicode_crlf_preserve_exact_bytes(self):
        old = "".join(f"line_{n} = {n}\n" for n in range(40))
        new = old.replace("line_8 = 8", "line_8 = 88").replace("line_30 = 30", "line_30 = 300")
        diff = self.roundtrip("pkg/core.py", old, new)
        self.assertEqual(sum(line.startswith("@@ ") for line in diff.splitlines()), 2)
        self.assertIn(" line_5 = 5\n", diff)
        self.assertIn(" line_11 = 11\n", diff)
        self.assertNotIn(" line_4 = 4\n", diff)
        self.assertNotIn(" line_12 = 12\n", diff)
        self.roundtrip("pkg/有 空格.py", "text = 'a\u2028b'\r\nvalue = 1\r\n",
                       "text = 'a\u2028b'\r\nvalue = 2\r\n")


class PacketValidationTests(PacketFixture):
    def test_large_source_is_fully_validated_but_unchanged_body_is_not_delivered(self):
        old = "# UNCHANGED_LONG_BODY_SENTINEL\n" + "".join(f"value_{n} = {n}\n" for n in range(9000))
        new = old.replace("value_4000 = 4000\n", "value_4000 = 4001\n")
        self.assertGreater(len(old.encode()), legacy.MAX_TEXT_BYTES)
        self.assertLess(len(old.encode()), packet.MAX_SOURCE_FILE_BYTES)
        self.changes["agent1"] = [{"path": "pkg/large.py", "before": old, "after": new}]
        self.refresh()
        original = copy.deepcopy((self.checkpoint, self.changes, self.scratch))
        result = self.build()
        content = packet.packet_message(result)
        row = result["agents"]["agent1"]["source_changes"][0]
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(row["before_sha256"], sha(old))
        self.assertEqual(row["after_sha256"], sha(new))
        self.assertNotIn("before", row)
        self.assertNotIn("after", row)
        self.assertNotIn("UNCHANGED_LONG_BODY_SENTINEL", content)
        self.assertIn("-value_4000 = 4000", content)
        self.assertIn("+value_4000 = 4001", content)
        for a in packet.AGENTS:
            self.assertEqual(result["agents"][a]["scratch_files"], self.scratch[a])
            self.assertIn(self.requirements[a], content)
        self.assertEqual((self.checkpoint, self.changes, self.scratch), original)
        self.assertEqual(self.build(), result)
        self.assertLess(len(content.encode()), packet.MAX_PACKET_BYTES)
        self.changes["agent1"][0]["after"] = new.replace("UNCHANGED_LONG_BODY_SENTINEL", "TAMPERED")
        with self.assertRaisesRegex(ValueError, "checkpoint filesystem"):
            self.build()

    def test_source_scratch_raw_packet_and_delivery_limits_fail_without_truncation(self):
        self.changes["agent1"][0]["after"] = "x" * (packet.MAX_SOURCE_FILE_BYTES + 1)
        self.refresh()
        with self.assertRaisesRegex(ValueError, "refusing truncation"):
            self.build()
        self.changes["agent1"][0]["after"] = "value = 2\n"
        self.scratch["agent1"][0]["content"] = "x" * (packet.MAX_SCRATCH_FILE_BYTES + 1)
        self.refresh()
        with self.assertRaisesRegex(ValueError, "scratch file exceeds"):
            self.build()
        for a in packet.AGENTS:
            self.scratch[a] = []
            self.changes[a] = [{"path": f"pkg/large_{n}.py", "before": "a" * 360000,
                                "after": "b" * 360000} for n in range(3)]
        self.refresh()
        with self.assertRaisesRegex(ValueError, "checkpoint packet exceeds limit; refusing truncation"):
            self.build()
        # Full source validation fits, but the changed lines themselves do not
        # fit in a delivery packet. Reject rather than choosing a smaller diff.
        for a in packet.AGENTS:
            self.changes[a] = [{"path": "pkg/core.py", "before": "a\n", "after": "b" * 70000 + "\n"}]
        self.refresh()
        with self.assertRaisesRegex(ValueError, "diff packet exceeds limit; refusing truncation"):
            self.build()

    def test_missing_or_modified_complete_source_and_scratch_are_rejected(self):
        before = copy.deepcopy((self.changes, self.scratch))
        self.changes["agent1"][0]["before"] = "not the task base\n"
        with self.assertRaisesRegex(ValueError, "WIP hashes"):
            self.build()
        self.changes, self.scratch = copy.deepcopy(before)
        self.changes["agent1"] = []
        with self.assertRaisesRegex(ValueError, "Missing checkpoint source"):
            self.build()
        self.changes, self.scratch = copy.deepcopy(before)
        self.scratch["agent2"] = []
        with self.assertRaisesRegex(ValueError, "Scratch evidence omits"):
            self.build()
        self.changes, self.scratch = copy.deepcopy(before)
        self.scratch["agent2"][0]["content"] += "changed\n"
        with self.assertRaisesRegex(ValueError, "Scratch content differs"):
            self.build()

    def test_raw_source_fields_cannot_accidentally_enter_the_delivered_schema(self):
        result = self.build()
        result["agents"]["agent1"]["source_changes"][0]["after"] = "UNINTENDED_FULL_FILE"
        with self.assertRaisesRegex(ValueError, "Unexpected fields"):
            packet.packet_message(result)

    def test_private_validators_do_not_modify_original_module_or_its_limits(self):
        self.assertEqual(legacy.MAX_TEXT_BYTES, 65536)
        self.assertEqual(legacy.MAX_PACKET_BYTES, 131072)
        self.assertIs(sys.modules["handoff_packet"], legacy)
        self.assertIsNot(packet._SOURCE_VALIDATOR, legacy)
        self.assertIsNot(packet._SCRATCH_VALIDATOR, legacy)
        self.build()
        self.assertEqual(legacy.MAX_TEXT_BYTES, 65536)
        self.assertEqual(legacy.MAX_PACKET_BYTES, 131072)


class CollectorTests(unittest.TestCase):
    def test_real_git_source_collection_accepts_large_files_and_retains_the_complete_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            old = "".join(f"value_{n} = {n}\n" for n in range(9000))
            write(root / "pkg/core.py", old)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Unit test", "-c", "user.email=unit@example.invalid",
                            "commit", "-qm", "Base fixture"], cwd=root, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            task = TaskSpec("fixture", "fixture", 1, (1, 2), "fixture", base, ("pkg",))
            new = old.replace("value_4000 = 4000\n", "value_4000 = 4001\n")
            write(root / "pkg/core.py", new)
            rows = packet.collect_source_changes(LocalEnvironment(root), task)
            self.assertEqual(rows, [{"path": "pkg/core.py", "before": old, "after": new}])
            write(root / "pkg/core.py", "x" * (packet.MAX_SOURCE_FILE_BYTES + 1))
            with self.assertRaises(RuntimeError):
                packet.collect_source_changes(LocalEnvironment(root), task)

    def test_scratch_collector_keeps_64k_limit_despite_larger_source_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            content = "# draft\n" + "x" * 65000
            write(root / "draft.py", content)
            saved = {"manifest": manifest({}, {"draft.py": content})}
            self.assertEqual(packet.collect_scratch_files(LocalEnvironment(root, root), saved),
                             [{"path": "draft.py", "content": content}])
            saved["manifest"] = manifest({}, {"draft.py": "x" * (packet.MAX_SCRATCH_FILE_BYTES + 1)})
            env = SimpleNamespace(execute=mock.Mock(side_effect=AssertionError("Oversize scratch must fail before read")))
            with self.assertRaises(ValueError):
                packet.collect_scratch_files(env, saved)
            env.execute.assert_not_called()


class DeliveryTests(PacketFixture):
    def agent(self):
        model = SimpleNamespace(format_message=mock.Mock(side_effect=lambda **m: copy.deepcopy(m)),
                                query=mock.Mock(side_effect=AssertionError("No model call allowed")))
        agent = SimpleNamespace(messages=[
            {"role": "system", "content": "Original system"},
            {"role": "user", "content": "Original feature"},
            {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function",
                "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call1", "content": "Original tool evidence"}],
            n_calls=4, cost=0.1, model=model)
        agent.add_messages = lambda *m: agent.messages.extend(copy.deepcopy(m))
        return agent

    def test_all_recipients_receive_identical_diff_and_preserve_every_historical_message(self):
        built = self.build()
        expected = packet.packet_message(built)
        self.assertIn("contains only the declared task source roots, not the entire repository", expected)
        self.assertIn("metadata.json and scope.txt in each evidence bundle", expected)
        self.assertIn("StrongShare and Consolidate receive the same evidence worktree scope", expected)
        messages = []
        for condition, recipient in (("strongshare", "agent1"), ("strongshare", "agent2"), ("consolidate", "agent1")):
            agent = self.agent()
            before = copy.deepcopy(agent.messages)
            receipt = packet.inject_packet(agent, built, condition, recipient)
            self.assertEqual(agent.messages[:-1], before)
            self.assertEqual(agent.messages[-1], {"role": "user", "content": expected})
            self.assertEqual(receipt["message_sha256"], sha(expected))
            self.assertEqual(receipt["packet_sha256"], digest(built))
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(receipt["message_index"], len(before))
            self.assertEqual((agent.n_calls, agent.cost), (4, 0.1))
            self.assertEqual([m["role"] for m in agent.messages].count("system"), 1)
            agent.model.query.assert_not_called()
            messages.append(agent.messages[-1]["content"])
        self.assertEqual(messages, [expected] * 3)

    def test_unfinished_tool_call_and_formatter_truncation_are_rejected_before_mutation(self):
        built = self.build()
        agent = self.agent()
        agent.messages.pop()
        before = copy.deepcopy(agent.messages)
        with self.assertRaises(ValueError):
            packet.inject_packet(agent, built, "strongshare", "agent1")
        self.assertEqual(agent.messages, before)
        agent.model.format_message.assert_not_called()
        agent = self.agent()
        before = copy.deepcopy(agent.messages)
        agent.model.format_message.side_effect = lambda **m: {**m, "content": m["content"][:100]}
        with self.assertRaisesRegex(ValueError, "formatter changed"):
            packet.inject_packet(agent, built, "strongshare", "agent1")
        self.assertEqual(agent.messages, before)
        with self.assertRaisesRegex(ValueError, "retired peer"):
            packet.inject_packet(agent, built, "consolidate", "agent2")


if __name__ == "__main__":
    unittest.main()
