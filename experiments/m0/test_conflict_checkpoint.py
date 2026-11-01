"""Real local Git fixtures, zero Docker and zero provider calls."""
import copy
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import conflict_checkpoint as checkpoint


class LocalEnvironment:
    def __init__(self, path): self.path = path
    def execute(self, action):
        argv = shlex.split(action["command"])
        if argv[:2] != ["python", "-c"] or len(argv) != 3:
            raise AssertionError("Only the observer's Python script is allowed")
        result = subprocess.run([sys.executable, *argv[1:]], cwd=self.path, capture_output=True, text=True)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


def active():
    return {"exited": False, "submitted": False, "exit_status": None, "infrastructure_error": False}


def local_manifest(root):
    rows = []
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        row = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid}
        if path.is_symlink(): row.update(type="symlink", target=path.readlink().as_posix())
        elif path.is_dir(): row.update(type="directory")
        else: row.update(type="file", size=st.st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        rows.append(row)
    roots = {"/workspace/repo": rows, "/tmp": []}
    return {"schema_version": 1, "roots": roots, "sha256": checkpoint.fingerprint(roots)}


class ConflictCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.env = LocalEnvironment(self.repo)
        self.git("init", "-q")
        self.git("config", "user.name", "Offline fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "implementation.py").write_text("value = 0\n")
        self.git("add", "."); self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        features = self.root / "public_requirements"
        for feature in (1, 2):
            path = features / f"feature{feature}" / "feature.md"
            path.parent.mkdir(parents=True); path.write_text(f"Public requirement {feature}\n")
        identity = {"key": "fixture", "base": self.base, "features": [1, 2]}
        self.task = SimpleNamespace(base=self.base, task_dir=features,
            agents={"agent1": 1, "agent2": 2}, as_dict=lambda: copy.deepcopy(identity))

    def git(self, *args, okay=(0,)):
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, capture_output=True)
        self.assertIn(result.returncode, okay, result.stderr)
        return result.stdout

    def conflict(self, operation="merge", delete=False):
        self.git("checkout", "-qb", "other")
        (self.repo / "implementation.py").write_text("value = 2\n")
        self.git("commit", "-qam", "other implementation")
        other = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-qB", "local", self.base)
        if delete: self.git("rm", "implementation.py")
        else: (self.repo / "implementation.py").write_text("value = 1\n")
        self.git("commit", "-qam", "local implementation")
        self.git(operation, other, okay=(1,))
        return checkpoint.inspect_conflict(self.env)

    def saved(self, observation=None):
        observation = observation or self.conflict()
        state = {"messages": [{"role": "system", "content": "native system"}],
            "n_calls": 2, "cost": 0.0, "sent_messages": [], "extra_template_vars": {},
            "_last_prompt_tokens": 0, "_segments": [], "_current_segment_messages": [], "_compaction_count": 0}
        value = {"schema": checkpoint.CHECKPOINT_SCHEMA, "task": self.task.as_dict(),
            "prefix_steps_per_agent": 2, "git_archive_sha256": "a" * 64,
            "conflict_checkpoint": {"rule": checkpoint.CHECKPOINT_RULE, "paired_round": 2,
                "terminal_policy": "both_active", "eligible": True, "selection_uses_test_results": False,
                "observations": {a: copy.deepcopy(observation) for a in checkpoint.AGENTS},
                "outcomes": {a: active() for a in checkpoint.AGENTS}},
            "broker": {"schema_version": 1, "agents": list(checkpoint.AGENTS),
                "inboxes": {a: [] for a in checkpoint.AGENTS}, "exited": {},
                "alive": list(checkpoint.AGENTS), "blocked": {}, "events": [], "sequence": 0}, "agents": {}}
        manifest = local_manifest(self.repo)
        for a, feature in self.task.agents.items():
            own_state = copy.deepcopy(state)
            task_text = (self.task.task_dir / f"feature{feature}/feature.md").read_text()
            own_state["extra_template_vars"]["task"] = task_text
            own_state["messages"].append({"role": "user", "content": task_text})
            value["agents"][a] = {"state": own_state, "git_base": self.base,
                "manifest": copy.deepcopy(manifest), "image": "sha256:" + "b" * 64}
        return value

    def test_current_merge_stages_and_operation_state_are_required(self):
        (self.repo / "old.log").write_text("CONFLICT (content): historical error\n")
        (self.repo / "implementation.py").write_text("<<<<<<< ours\n=======\n>>>>>>> theirs\n")
        self.assertFalse(checkpoint.inspect_conflict(self.env)["has_unmerged"])
        self.git("checkout", "--", "implementation.py")
        current = self.conflict()
        self.assertTrue(current["has_unmerged"])
        self.assertEqual([r["stage"] for r in current["unmerged_entries"]], [1, 2, 3])
        self.assertEqual(current["operation_metadata"]["MERGE_HEAD"]["type"], "file")
        self.assertIn("UU implementation.py\x00", current["status_porcelain_z"])
        index = (self.repo / ".git/index").read_bytes()
        self.assertEqual(checkpoint.inspect_conflict(self.env), current)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.git("add", "implementation.py")
        resolved = checkpoint.inspect_conflict(self.env)
        self.assertFalse(resolved["has_unmerged"])
        self.assertEqual(resolved["operation_metadata"]["MERGE_HEAD"]["type"], "file")

    def test_cherry_pick_and_delete_conflict_do_not_require_three_stages(self):
        current = self.conflict("cherry-pick", delete=True)
        self.assertTrue(current["has_unmerged"])
        self.assertEqual({r["stage"] for r in current["unmerged_entries"]}, {1, 3})
        self.assertEqual(current["operation_metadata"]["CHERRY_PICK_HEAD"]["type"], "file")
        self.assertEqual(current["operation_metadata"]["MERGE_HEAD"]["type"], "missing")

    def test_rebase_metadata_is_preserved_and_restore_detects_mutation(self):
        current = self.conflict()
        rebase = self.repo / ".git/rebase-merge"; rebase.mkdir()
        (rebase / "git-rebase-todo").write_text("pick " + self.base + " fixture\n")
        current = checkpoint.inspect_conflict(self.env)
        self.assertIn("rebase-merge/git-rebase-todo", current["operation_metadata"])
        copied = self.root / "restored"; shutil.copytree(self.repo, copied)
        env = LocalEnvironment(copied)
        self.assertTrue(checkpoint.verify_restored_conflict(env, current)["verified"])
        (copied / ".git/rebase-merge/git-rebase-todo").write_text("different continuation\n")
        with self.assertRaisesRegex(ValueError, "Restored unmerged"):
            checkpoint.verify_restored_conflict(env, current)

    def test_valid_new_schema_and_archive_binding(self):
        value = self.saved()
        checkpoint.validate_conflict_checkpoint(value, self.task)
        run = self.root / "checkpoint"; run.mkdir()
        archive = run / "shared_git.tar"; archive.write_bytes(b"offline archive fixture")
        value["git_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        (run / "checkpoint.json").write_text(json.dumps(value))
        self.assertEqual(checkpoint.verify_checkpoint_files(run, self.task), value)
        archive.write_bytes(b"changed archive")
        with self.assertRaisesRegex(ValueError, "archive differs"):
            checkpoint.verify_checkpoint_files(run, self.task)

    def test_terminal_agent_or_failed_boundary_is_ineligible(self):
        observation = self.conflict()
        observations = {a: observation for a in checkpoint.AGENTS}
        for agent in checkpoint.AGENTS:
            outcomes = {a: active() for a in checkpoint.AGENTS}
            outcomes[agent].update(exited=True, submitted=True, exit_status="Submitted")
            self.assertFalse(checkpoint.conflict_eligible(observations, outcomes))
            self.assertFalse(checkpoint.conflict_eligible(observations, outcomes, "lead_active_peer_may_be_submitted"))
        outcomes = {a: active() for a in checkpoint.AGENTS}
        outcomes["agent2"]["infrastructure_error"] = True
        self.assertFalse(checkpoint.conflict_eligible(observations, outcomes))

    def test_old_wip_schema_cross_task_and_changed_manifest_are_rejected(self):
        value = self.saved()
        variants = []
        old = copy.deepcopy(value); old["wip_checkpoint"] = {"eligible": True}; variants.append(old)
        wrong = copy.deepcopy(value); wrong["task"]["key"] = "other-task"; variants.append(wrong)
        tampered = copy.deepcopy(value)
        rows = tampered["agents"]["agent1"]["manifest"]["roots"]["/workspace/repo"]
        next(row for row in rows if row["path"] == ".git/index")["sha256"] = "c" * 64
        tampered["agents"]["agent1"]["manifest"]["sha256"] = checkpoint.fingerprint(
            tampered["agents"]["agent1"]["manifest"]["roots"])
        variants.append(tampered)
        for changed in variants:
            with self.subTest(changed=changed.get("task")):
                with self.assertRaises(ValueError): checkpoint.validate_conflict_checkpoint(changed, self.task)

    def test_unfinished_tool_call_or_incomplete_broker_is_rejected(self):
        value = self.saved()
        value["agents"]["agent1"]["state"]["messages"].append({"role": "assistant", "tool_calls": [{"id": "pending"}]})
        with self.assertRaisesRegex(ValueError, "unfinished tool call"):
            checkpoint.validate_conflict_checkpoint(value, self.task)
        value["agents"]["agent1"]["state"]["messages"].append({"role": "tool", "tool_call_id": "pending", "content": "complete"})
        checkpoint.validate_conflict_checkpoint(value, self.task)
        value["broker"]["blocked"] = {"agent1": "agent2"}
        with self.assertRaisesRegex(ValueError, "quiescent"):
            checkpoint.validate_conflict_checkpoint(value, self.task)

    def test_observation_hash_or_stage_tampering_cannot_be_used_as_gate(self):
        current = self.conflict()
        current["unmerged_entries"][0]["oid"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "checksum"):
            checkpoint.validate_observation(current)
        current["fingerprint_sha256"] = checkpoint.fingerprint({k: v for k, v in current.items() if k != "fingerprint_sha256"})
        current["unmerged_entries"][0]["stage"] = 0
        with self.assertRaisesRegex(ValueError, "index entry"):
            checkpoint.validate_observation(current)


if __name__ == "__main__": unittest.main()
