"""Offline task isolation and genuine-WIP checks; no Docker or provider calls."""

import copy
from dataclasses import FrozenInstanceError, replace
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

from baseline_runtime import MemoryBroker, verify_workspace_manifest
from run_baselines import load_state, save_state
from run_consolidate import apply_handoff, handoff_messages
import task_specs
from task_specs import TaskSpec, get_task
import wip_checkpoint as wip
import run_task_expansion as expansion


class LocalGitEnvironment:
    """Execute only the observer's Python script in an ephemeral local repo."""

    def __init__(self, path):
        self.path = path
        self.commands = []

    def execute(self, action):
        command = shlex.split(action["command"])
        if len(command) != 3 or command[:2] != ["python", "-c"]:
            raise AssertionError("Source observation must be one local Python script")
        self.commands.append(action["command"])
        result = subprocess.run([sys.executable, *command[1:]], cwd=self.path,
                                capture_output=True, text=True, timeout=15)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


def progress(changed=True):
    return {"changed_files": [{"path": "pkg/core.py", "before_sha256": "old",
                                "after_sha256": "new", "untracked": False,
                                "deleted": False}] if changed else [],
            "has_code_change": changed, "head": "checkpoint-head", "git_status": " M pkg/core.py"}


def unfinished():
    return {"submitted": False, "exited": False, "infrastructure_error": False}


class SourceProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.write("pkg/core.py", "def value():\n    return 1\n")
        self.write("other/core.py", "def value():\n    return 10\n")
        self.write("pkg/tests/test_core.py", "assert True\n")
        self.write("README.md", "Original documentation\n")
        self.git("add", ".")
        self.git("-c", "user.name=Offline test", "-c", "user.email=offline@example.invalid",
                 "commit", "-qm", "Fixture base")
        base = self.git("rev-parse", "HEAD").strip()
        self.task = TaskSpec("fixture", "fixture_repo", 1, (3, 7), "fixture-image", base, ("pkg",))
        self.env = LocalGitEnvironment(self.root)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True,
                                       stderr=subprocess.PIPE)

    def write(self, relative, text):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_clean_comment_formatting_tests_and_logs_do_not_trigger_checkpoint(self):
        clean = wip.code_progress(self.env, self.task)
        self.assertFalse(clean["has_code_change"])
        self.write("pkg/core.py", "# An explanatory comment\ndef value( ):\n\n    return 1 # unchanged\n")
        self.write("pkg/comment_only.py", "# no executable source\n\n")
        self.write("pkg/tests/test_core.py", "assert False\n")
        self.write("pkg/trace.log", "We changed the implementation, allegedly\n")
        self.write("README.md", "Documentation changed\n")
        result = wip.code_progress(self.env, self.task)
        self.assertFalse(result["has_code_change"])
        self.assertEqual(result["changed_files"], [])
        self.assertIn("pkg/core.py", result["git_status"])

    def test_tracked_staged_and_committed_source_are_compared_with_original_base(self):
        source = "def value():\n    return 2\n"
        self.write("pkg/core.py", source)
        first = wip.code_progress(self.env, self.task)
        self.git("add", "pkg/core.py")
        staged = wip.code_progress(self.env, self.task)
        self.git("-c", "user.name=Offline test", "-c", "user.email=offline@example.invalid",
                 "commit", "-qm", "Unpublished WIP")
        committed = wip.code_progress(self.env, self.task)
        self.assertEqual(first["changed_files"], staged["changed_files"])
        self.assertEqual(first["changed_files"], committed["changed_files"])
        self.assertTrue(committed["has_code_change"])
        self.assertNotEqual(committed["head"], self.task.base)
        self.assertEqual(committed["git_status"], "")
        self.assertEqual(first["changed_files"][0]["after_sha256"],
                         hashlib.sha256(source.encode()).hexdigest())

    def test_untracked_source_is_included_but_source_symlink_is_not_followed(self):
        self.write("pkg/new_impl.py", "def additional():\n    return 42\n")
        (self.root / "pkg/alias.py").symlink_to("../other/core.py")
        result = wip.code_progress(self.env, self.task)
        self.assertEqual([row["path"] for row in result["changed_files"]], ["pkg/new_impl.py"])
        self.assertTrue(result["changed_files"][0]["untracked"])
        self.assertFalse(result["changed_files"][0]["deleted"])

    def test_incomplete_syntax_and_deletions_remain_eligible_source_progress(self):
        self.write("pkg/core.py", "def value(:\n")
        incomplete = wip.code_progress(self.env, self.task)
        self.assertTrue(incomplete["has_code_change"])
        (self.root / "pkg/core.py").unlink()
        deleted = wip.code_progress(self.env, self.task)
        self.assertTrue(deleted["has_code_change"])
        self.assertTrue(deleted["changed_files"][0]["deleted"])

    def test_task_source_roots_are_isolated_across_repeated_observations(self):
        other = replace(self.task, key="other", features=(1, 2), code_roots=("other",), image="other-image")
        self.write("other/core.py", "def value():\n    return 11\n")
        self.assertFalse(wip.code_progress(self.env, self.task)["has_code_change"])
        self.assertTrue(wip.code_progress(self.env, other)["has_code_change"])
        self.assertFalse(wip.code_progress(self.env, self.task)["has_code_change"])
        self.write("pkg/core.py", "def value():\n    return 3\n")
        self.assertEqual([r["path"] for r in wip.code_progress(self.env, self.task)["changed_files"]],
                         ["pkg/core.py"])
        self.assertEqual([r["path"] for r in wip.code_progress(self.env, other)["changed_files"]],
                         ["other/core.py"])

    def test_failed_or_malformed_observation_is_not_an_empty_valid_checkpoint(self):
        cases = [({"returncode": 1, "output": "git failed"}, RuntimeError),
                 ({"returncode": 0, "output": "not JSON"}, ValueError),
                 ({"returncode": 0, "output": '{"has_code_change": true}'}, ValueError)]
        for output, exception in cases:
            with self.subTest(output=output):
                env = SimpleNamespace(execute=mock.Mock(return_value=output))
                with self.assertRaises(exception):
                    wip.code_progress(env, self.task)


class TaskCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for attribute, path in (("ROOT", self.root), ("REPO", self.root / "dataset-repo")):
            patcher = mock.patch.object(task_specs, attribute, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tasks = [get_task("click2800_f1_f2"), get_task("dirty_equals43_f3_f7")]
        for task in self.tasks:
            for feature in task.features:
                file = task.task_dir / f"feature{feature}/feature.md"
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(f"{task.key}: exact feature {feature} instruction")

    def checkpoint(self, task):
        return {
            "task": task.as_dict(),
            "wip_checkpoint": {"progress": {a: progress() for a in task.agents},
                               "outcomes": {a: unfinished() for a in task.agents}},
            "agents": {a: {"git_base": task.base, "state": {"extra_template_vars": {
                "task": (task.task_dir / f"feature{feature}/feature.md").read_text()}}}
                for a, feature in task.agents.items()},
            "broker": {"blocked": {}, "exited": {}},
        }

    def test_fixed_task_identity_paths_and_exports_do_not_share_mutable_state(self):
        click, dirty = self.tasks
        self.assertEqual(click.agents, {"agent1": 1, "agent2": 2})
        self.assertEqual(dirty.agents, {"agent1": 3, "agent2": 7})
        for attribute in ("key", "repo", "task_id", "image", "base", "task_dir", "image_record_path"):
            self.assertNotEqual(getattr(click, attribute), getattr(dirty, attribute), attribute)
        exported = dirty.as_dict()
        exported["features"][0] = 100
        exported["code_roots"].append("wrong-root")
        agents = dirty.agents
        agents["agent1"] = 100
        self.assertEqual(dirty.features, (3, 7))
        self.assertEqual(dirty.code_roots, ("dirty_equals",))
        self.assertEqual(dirty.agents["agent1"], 3)
        self.assertEqual(json.loads(json.dumps(dirty.as_dict())), dirty.as_dict())
        with self.assertRaises(FrozenInstanceError):
            dirty.base = "changed"
        with self.assertRaisesRegex(ValueError, "Unknown expansion task"):
            get_task("jinja1621")

    def test_checkpoint_requires_both_source_changes_and_unfinished_agents(self):
        valid = {a: progress() for a in ("agent1", "agent2")}
        outcomes = {a: unfinished() for a in valid}
        self.assertTrue(wip.checkpoint_eligible(valid, outcomes))
        for agent in valid:
            for field in ("submitted", "exited", "infrastructure_error"):
                with self.subTest(agent=agent, field=field):
                    altered = copy.deepcopy(outcomes)
                    altered[agent][field] = True
                    self.assertFalse(wip.checkpoint_eligible(valid, altered))
            for missing in ({}, progress(False), {**progress(), "changed_files": []}):
                with self.subTest(agent=agent, progress=missing):
                    altered = {**valid, agent: missing}
                    self.assertFalse(wip.checkpoint_eligible(altered, outcomes))
        self.assertFalse(wip.checkpoint_eligible({"agent1": progress()}, outcomes))
        self.assertFalse(wip.checkpoint_eligible(valid, {"agent1": unfinished()}))

    def test_validation_rejects_wrong_task_base_feature_and_empty_or_submitted_checkpoint(self):
        task = self.tasks[1]
        checkpoint = self.checkpoint(task)
        before = copy.deepcopy(checkpoint)
        wip.validate_checkpoint(checkpoint, task)
        self.assertEqual(checkpoint, before)
        mutations = {
            "wrong_task": lambda c: c.update(task=self.tasks[0].as_dict()),
            "wrong_base": lambda c: c["agents"]["agent1"].update(git_base=self.tasks[0].base),
            "wrong_feature": lambda c: c["agents"]["agent2"]["state"]["extra_template_vars"].update(task="Feature 6"),
            "empty_source": lambda c: c["wip_checkpoint"]["progress"].update(agent2=progress(False)),
            "submitted": lambda c: c["wip_checkpoint"]["outcomes"]["agent1"].update(submitted=True),
            "missing_agent": lambda c: c["agents"].pop("agent2"),
            "blocked_wait": lambda c: c["broker"].update(blocked={"agent1": "agent2"}),
            "departed_peer": lambda c: c["broker"].update(exited={"agent2": False}),
        }
        for name, mutate in mutations.items():
            with self.subTest(reason=name):
                changed = copy.deepcopy(checkpoint)
                mutate(changed)
                with self.assertRaises(ValueError):
                    wip.validate_checkpoint(changed, task)
        with self.assertRaisesRegex(ValueError, "different task"):
            wip.validate_checkpoint(checkpoint, self.tasks[0])

    def test_consolidate_handoff_uses_current_features_and_preserves_history(self):
        task = self.tasks[1]
        checkpoint = self.checkpoint(task)
        original_task = checkpoint["agents"]["agent1"]["state"]["extra_template_vars"]["task"]
        agent = SimpleNamespace(messages=[{"role": "system", "content": "Original organization"},
                                          {"role": "user", "content": original_task},
                                          {"role": "assistant", "content": "WIP implementation evidence"}],
                                n_calls=8, cost=0.03, sent_messages=["Unfinished peer work"],
                                extra_template_vars={"task": original_task}, _departed_announced=set())
        agent.model = SimpleNamespace(format_message=lambda **m: copy.deepcopy(m))
        agent.add_messages = lambda *m: agent.messages.extend(copy.deepcopy(m))
        checkpoint["agents"]["agent1"]["state"] = save_state(agent)
        before = copy.deepcopy(agent.messages)
        broker = MemoryBroker(["agent1", "agent2"])
        broker.connector("agent2").send("agent1", "WIP details")
        before_mail = copy.deepcopy(broker.inboxes)
        audit = apply_handoff(agent, broker, checkpoint)
        self.assertIn("Feature 3 requirement:", agent.extra_template_vars["task"])
        self.assertIn("Feature 7 requirement:", agent.extra_template_vars["task"])
        self.assertNotIn("Feature 1 requirement:", agent.extra_template_vars["task"])
        self.assertNotIn("Feature 6 requirement:", agent.extra_template_vars["task"])
        self.assertEqual(agent.messages[1:len(before)], before[1:])
        self.assertEqual([i for i, m in enumerate(agent.messages) if m["role"] == "system"], [0])
        self.assertEqual(agent.n_calls, 8)
        self.assertEqual(agent.cost, 0.03)
        self.assertEqual(broker.inboxes, before_mail)
        self.assertFalse(broker.exited["agent2"])
        self.assertFalse(audit["code_automatically_transferred"])
        click_combined, _ = handoff_messages(self.checkpoint(self.tasks[0]))
        self.assertIn("Feature 2 requirement:", click_combined)
        self.assertNotIn(task.key, click_combined)

    def test_wip_restore_requires_all_file_details_and_preserves_history_and_mail(self):
        rows = [
            {"path": "dirty_equals/core.py", "type": "file", "mode": 420, "sha256": "tracked-wip"},
            {"path": "dirty_equals/new.py", "type": "file", "mode": 493, "sha256": "untracked-wip"},
            {"path": "dirty_equals/link.py", "type": "symlink", "mode": 511, "target": "core.py"},
        ]
        expected = {"schema_version": 1, "sha256": "full-checkpoint-digest", "roots": {
            "/workspace/repo": rows, "/tmp": [{"path": "repro.py", "type": "file", "sha256": "repro-wip"}]}}
        env = SimpleNamespace(workspace_manifest=mock.Mock(return_value=copy.deepcopy(expected)))
        self.assertEqual(verify_workspace_manifest(env, expected), expected)
        mutations = [
            lambda m: m["roots"]["/workspace/repo"][0].update(sha256="empty-base"),
            lambda m: m["roots"]["/workspace/repo"].pop(1),
            lambda m: m["roots"]["/workspace/repo"][1].update(mode=420),
            lambda m: m["roots"]["/workspace/repo"][2].update(target="new.py"),
            lambda m: m["roots"]["/tmp"][0].update(sha256="changed-repro"),
            lambda m: m.update(sha256="different-checkpoint"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(change=index):
                changed = copy.deepcopy(expected)
                mutate(changed)
                env.workspace_manifest.return_value = changed
                with self.assertRaisesRegex(RuntimeError, "differ"):
                    verify_workspace_manifest(env, expected)
        state = {"messages": [{"role": "system", "content": "Original"},
                               {"role": "tool", "content": "Uncommitted source diff"}],
                 "extra_template_vars": {"task": "Feature 3"}, "n_calls": 5,
                 "cost": 0.01, "_departed_announced": []}
        agent = SimpleNamespace()
        load_state(agent, state)
        self.assertEqual(save_state(agent), state)
        agent.messages[-1]["content"] = "Independent continuation"
        self.assertEqual(state["messages"][-1]["content"], "Uncommitted source diff")
        broker = MemoryBroker(["agent1", "agent2"])
        broker.connector("agent2").send("agent1", "Unfinished change, please inspect")
        saved_mail = broker.snapshot()
        restored = MemoryBroker.from_snapshot(saved_mail)
        self.assertEqual(restored.snapshot(), saved_mail)


class PrefixSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.phase = Path(self.temp.name)
        self.agents = {}
        for agent_id in ("agent1", "agent2"):
            agent = SimpleNamespace(n_calls=0, messages=[], config=SimpleNamespace(output_path=self.phase / agent_id))
            agent.step = mock.Mock(side_effect=lambda a=agent: setattr(a, "n_calls", a.n_calls + 1))
            agent.save = mock.Mock()
            agent._opened_pr = mock.Mock(return_value=False)
            self.agents[agent_id] = agent
        self.envs = {a: SimpleNamespace(agent_id=a) for a in self.agents}

    def test_prefix_stops_first_paired_round_both_have_code_without_test_based_selection(self):
        def observe(env, task):
            threshold = 1 if env.agent_id == "agent1" else 2
            return progress(self.agents[env.agent_id].n_calls >= threshold)

        with mock.patch.object(wip, "code_progress", side_effect=observe) as observer:
            outcomes, evidence = wip.drive_prefix(self.agents, self.envs, get_task("dirty_equals43_f3_f7"), self.phase)
        self.assertEqual(evidence["paired_round"], 2)
        self.assertTrue(evidence["eligible"])
        self.assertFalse(evidence["selection_uses_test_results"])
        self.assertEqual(observer.call_count, 4)
        self.assertEqual([a.n_calls for a in self.agents.values()], [2, 2])
        self.assertTrue(all(o["status"] == "checkpoint" for o in outcomes.values()))
        self.assertFalse(json.loads((self.phase / "progress_round_001.json").read_text())["eligible"])

    def test_prefix_does_not_resample_after_any_agent_submits_or_exits(self):
        for stopping in ("submitted", "exited"):
            with self.subTest(stopping=stopping):
                for agent in self.agents.values():
                    agent.n_calls = 0
                    agent.messages = []
                    agent._opened_pr.return_value = False
                if stopping == "submitted":
                    self.agents["agent1"]._opened_pr.return_value = True
                else:
                    self.agents["agent1"].messages = [{"role": "exit"}]
                with mock.patch.object(wip, "code_progress", return_value=progress()):
                    outcomes, evidence = wip.drive_prefix(self.agents, self.envs, get_task("click2800_f1_f2"), self.phase)
                self.assertFalse(evidence["eligible"])
                self.assertEqual(evidence["paired_round"], 1)
                self.assertEqual([a.n_calls for a in self.agents.values()], [1, 1])
                self.assertTrue(all(o["status"] == "checkpoint_not_reached" for o in outcomes.values()))

    def test_prefix_infrastructure_failure_stops_before_progress_or_retry(self):
        self.agents["agent1"].step.side_effect = RuntimeError("Synthetic local infrastructure failure")
        with mock.patch.object(wip, "code_progress") as observer:
            with self.assertRaisesRegex(RuntimeError, "Prefix infrastructure failure"):
                wip.drive_prefix(self.agents, self.envs, get_task("click2800_f1_f2"), self.phase)
        observer.assert_not_called()
        self.assertEqual(self.agents["agent1"].step.call_count, 1)
        self.assertEqual(self.agents["agent2"].step.call_count, 1)


class ExpansionOrchestrationTests(unittest.TestCase):
    """Exercise resume and billing through real on-disk phase artifacts."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "artifacts").mkdir()
        source = self.root / "runner.py"
        source.write_text("# Synthetic frozen source fixture\n")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        self.suite = self.root / "artifacts/expansion_runs/expansion_wip_unit"
        self.events = []
        patches = [
            mock.patch.object(expansion, "ROOT", self.root),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
            mock.patch.object(expansion, "source_manifest", return_value={"runner.py": source_hash}),
            mock.patch.object(expansion, "verify_references", return_value={"passed": True, "model_api_calls": 0}),
            mock.patch.object(expansion, "verify_preflight"),
            mock.patch.object(expansion, "configure"),
            mock.patch.object(expansion, "cleanup_phase"),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict(expansion.os.environ, {"TOGETHER_API_KEY": "offline-placeholder-not-a-key"}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        for task in expansion.TASKS.values():
            for feature in task.features:
                path = task.task_dir / f"feature{feature}/feature.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{task.key}, feature {feature}: exact isolated task")
        patcher = mock.patch.object(expansion, "run_command", side_effect=self.command)
        self.command_mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.protocol = expansion.protocol_for(False)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_usage(self, run_dir, condition, cost, status="completed"):
        path = run_dir / "usage_ledger.json"
        ledger = expansion.read_json(path, {"requests": []})
        ledger["requests"].append({"phase": condition + ":agent1", "status": status,
                                   "actual_cost_usd": cost, "reserved_usd": 0.05,
                                   "prompt_tokens": 100 if cost is not None else 0,
                                   "completion_tokens": 10 if cost is not None else 0})
        self.write(path, ledger)

    def describe(self, command):
        run_dir = Path(command[command.index("--run-dir") + 1])
        if "--worker" in command:
            condition = command[command.index("--condition") + 1]
            task = get_task(command[command.index("--task") + 1])
            return "worker", run_dir, condition, task
        condition = run_dir.name
        run_dir = run_dir.parent
        config = expansion.read_json(run_dir / "task_config.json")
        return "evaluation", run_dir, condition, get_task(config["task_key"])

    def prefix_artifacts(self, run_dir, task):
        archive = b"Synthetic shared Git snapshot; never restored by these unit tests"
        (run_dir / "shared_git.tar").write_bytes(archive)
        checkpoint = {
            "task": task.as_dict(), "git_archive_sha256": hashlib.sha256(archive).hexdigest(),
            "wip_checkpoint": {"progress": {a: progress() for a in task.agents},
                               "outcomes": {a: unfinished() for a in task.agents}},
            "agents": {a: {"git_base": task.base, "state": {"extra_template_vars": {
                "task": (task.task_dir / f"feature{feature}/feature.md").read_text()}}}
                for a, feature in task.agents.items()},
            "broker": {"blocked": {}, "exited": {}},
        }
        self.write(run_dir / "checkpoint.json", checkpoint)

    def command(self, command, *args, **kwargs):
        stage, run_dir, condition, task = self.describe(command)
        self.events.append((stage, task.key, run_dir.name, condition))
        phase = run_dir / condition
        if stage == "evaluation":
            self.write(phase / "evaluation_summary.json", {
                "evaluation_completed": True, "both_passed": False,
                "elapsed_seconds": 2, "task": task.as_dict()})
        else:
            status = {"status": "completed", "elapsed_seconds": 5 if condition == "prefix" else 10}
            if condition == "prefix":
                self.prefix_artifacts(run_dir, task)
            else:
                names = ["final.patch"] if condition == "consolidate" else [f"agent{f}.patch" for f in task.features]
                for name in names:
                    (phase / name).write_text(f"{task.key}: {run_dir.name}: {name}")
                status.update(outcome="Submitted", outcomes={a: {"status": "Submitted"} for a in task.agents})
                if condition == "consolidate":
                    status["final_patch_sha256"] = expansion.file_hash(phase / "final.patch")
            self.write(phase / "status.json", status)
            self.add_usage(run_dir, condition, 0.2 if condition == "prefix" else 0.1)
        return 0

    def run_suite(self):
        return expansion.supervise(self.suite, False)

    def test_all_twelve_failed_scores_are_retained_and_completed_stages_never_rerun(self):
        old_run = self.root / "artifacts/repeat_runs/old_jinja"
        old_run.mkdir(parents=True)
        self.add_usage(old_run, "keep", 99)
        self.assertEqual(self.run_suite(), 0)
        summary = expansion.read_json(self.suite / "summary.json")
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(len(summary["rows"]), 12)
        self.assertFalse(any(row["official_success"] for row in summary["rows"]))
        self.assertEqual(summary["actual_new_usage"]["requests"], 14)
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 1.6)
        self.assertEqual({row["full_task_equivalent_known_cost_usd"] for row in summary["rows"]}, {0.3})
        self.assertEqual({row["comparison_seconds"] for row in summary["rows"]}, {17})
        for task in expansion.TASKS.values():
            rows = [r for r in summary["rows"] if r["task"] == task.key]
            self.assertEqual(len(rows), 6)
            self.assertEqual(len({r["checkpoint_sha256"] for r in rows}), 1)
            for row in rows:
                run_dir = Path(row["run_directory"])
                self.assertEqual(expansion.read_json(run_dir / "task_config.json")["task"], task.as_dict())
                names = set(expansion.read_json(run_dir / row["condition"] / "execution_receipt.json")["patch_sha256"])
                self.assertEqual(names, {"final.patch"} if row["condition"] == "consolidate"
                                 else {f"agent{f}.patch" for f in task.features})
        self.command_mock.reset_mock()
        self.command_mock.side_effect = AssertionError("No completed execution or scoring stage may repeat")
        with mock.patch.dict(expansion.os.environ, {"TOGETHER_API_KEY": ""}):
            self.assertEqual(self.run_suite(), 0)
        self.command_mock.assert_not_called()
        self.assertEqual(expansion.read_json(self.suite / "summary.json"), summary)

    def test_evaluator_failure_resumes_only_evaluation_and_preserves_model_usage(self):
        failed = False

        def first_evaluation_fails(command, *args, **kwargs):
            nonlocal failed
            if self.describe(command)[0] == "evaluation" and not failed:
                failed = True
                return 2
            return self.command(command, *args, **kwargs)

        self.command_mock.side_effect = first_evaluation_fails
        self.assertEqual(self.run_suite(), 2)
        before_workers = [event for event in self.events if event[0] == "worker"]
        self.assertEqual(len(before_workers), 2)
        self.command_mock.side_effect = self.command
        self.assertEqual(self.run_suite(), 0)
        for event in before_workers:
            self.assertEqual(self.events.count(event), 1)
        self.assertEqual(expansion.read_json(self.suite / "summary.json")["actual_new_usage"]["known_cost_usd"], 1.6)

    def test_failed_attempt_ledger_survives_resume_but_not_accepted_sample_cost(self):
        failed = False

        def fail_first_continuation(command, *args, **kwargs):
            nonlocal failed
            stage, run_dir, condition, task = self.describe(command)
            if stage == "worker" and condition != "prefix" and not failed:
                failed = True
                self.write(run_dir / condition / "status.json", {"status": "error", "infrastructure_error": True})
                self.add_usage(run_dir, condition, 0.25)
                self.add_usage(run_dir, condition, None, "in_flight")
                return 2
            return self.command(command, *args, **kwargs)

        self.command_mock.side_effect = fail_first_continuation
        self.assertEqual(self.run_suite(), 2)
        paused = expansion.read_json(self.suite / "summary.json")
        self.assertEqual(paused["actual_new_usage"]["known_cost_usd"], 0.45)
        self.assertEqual(paused["actual_new_usage"]["unknown_usage_requests"], 1)
        self.command_mock.side_effect = self.command
        self.assertEqual(self.run_suite(), 0)
        completed = expansion.read_json(self.suite / "summary.json")
        self.assertEqual(completed["actual_new_usage"]["known_cost_usd"], 1.85)
        self.assertEqual(completed["actual_new_usage"]["requests"], 16)
        self.assertEqual(completed["actual_new_usage"]["unknown_usage_requests"], 1)
        self.assertEqual({r["continuation_usage"]["known_cost_usd"] for r in completed["rows"]}, {0.1})
        self.assertEqual({r["full_task_equivalent_unknown_usage_requests"] for r in completed["rows"]}, {0})
        first = Path(completed["rows"][0]["run_directory"])
        ledger = expansion.read_json(first / "usage_ledger.json")["requests"]
        excluded = [r for r in ledger if r.get("excluded_from_baseline")]
        self.assertEqual(len(excluded), 2)
        unknown = next(r for r in excluded if r["actual_cost_usd"] is None)
        self.assertEqual(unknown["status"], "error")
        self.assertEqual(unknown["error_type"], "InterruptedAttemptUsageUnknown")
        self.assertEqual(unknown["reserved_usd"], 0.05)
        self.assertEqual(len(list((first / "failed_attempts").iterdir())), 1)

    def test_unreached_checkpoint_skips_six_jobs_without_resampling_and_keeps_prefix_cost(self):
        skipped_task = next(iter(expansion.TASKS))

        def no_checkpoint(command, *args, **kwargs):
            stage, run_dir, condition, task = self.describe(command)
            if stage == "worker" and condition == "prefix" and task.key == skipped_task:
                self.events.append((stage, task.key, run_dir.name, condition))
                self.write(run_dir / condition / "status.json", {"status": "checkpoint_not_reached", "elapsed_seconds": 1200})
                self.add_usage(run_dir, condition, 0.2)
                return 0
            return self.command(command, *args, **kwargs)

        self.command_mock.side_effect = no_checkpoint
        self.assertEqual(self.run_suite(), 0)
        summary = expansion.read_json(self.suite / "summary.json")
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual(len(summary["rows"]), 6)
        self.assertEqual([r["task"] for r in summary["skipped_tasks"]], [skipped_task])
        self.assertTrue(all(r["task"] != skipped_task for r in summary["rows"]))
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"], 1.0)
        self.assertEqual(summary["actual_new_usage"]["requests"], 8)
        self.command_mock.side_effect = AssertionError("An ineligible prefix cannot be resampled")
        self.assertEqual(self.run_suite(), 0)

    def test_existing_receipt_or_completed_consolidate_status_rejects_patch_tampering(self):
        task = get_task("dirty_equals43_f3_f7")
        for condition in ("keep", "consolidate"):
            with self.subTest(condition=condition):
                run_dir = self.suite / condition
                expansion.configure_run(run_dir, task, self.protocol)
                phase = run_dir / condition
                phase.mkdir()
                command = ["python", "runner.py", "--worker", "--run-dir", str(run_dir),
                           "--condition", condition, "--task", task.key]
                self.command(command)
                if condition == "keep":
                    self.write(phase / "execution_receipt.json", {"patch_sha256": expansion.patch_hashes(phase, task, condition)})
                patch = phase / ("final.patch" if condition == "consolidate" else "agent3.patch")
                patch.write_text("Replaced after worker completion")
                self.command_mock.reset_mock()
                with self.assertRaises(RuntimeError):
                    expansion.evaluate(run_dir, task, condition, -1)
                self.command_mock.assert_not_called()
                self.assertFalse((phase / "evaluation_summary.json").exists())
                if condition == "consolidate":
                    self.assertFalse((phase / "execution_receipt.json").exists())

    def test_task_configuration_and_branch_checkpoint_cannot_be_replaced_on_resume(self):
        task, other = tuple(expansion.TASKS.values())
        prefix = self.suite / task.key / "prefix"
        branch = self.suite / task.key / "jobs/keep"
        expansion.configure_run(prefix, task, self.protocol)
        expansion.configure_run(branch, task, self.protocol)
        with self.assertRaisesRegex(RuntimeError, "Frozen configuration changed"):
            expansion.configure_run(branch, other, self.protocol)
        self.prefix_artifacts(prefix, task)
        self.write(prefix / "prefix/status.json", {"status": "completed", "elapsed_seconds": 5})
        expansion.copy_checkpoint(prefix, branch, task)
        checkpoint_path = branch / "checkpoint.json"
        saved = expansion.read_json(checkpoint_path)
        saved["agents"]["agent1"]["state"]["extra_template_vars"]["task"] = "Different source requirement"
        self.write(checkpoint_path, saved)
        with self.assertRaisesRegex(RuntimeError, "Restored branch input changed"):
            expansion.copy_checkpoint(prefix, branch, task)
        self.command_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
