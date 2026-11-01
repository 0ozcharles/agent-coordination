"""Offline reminders with actual native exit messages and local Git fixtures."""
from concurrent.futures import ThreadPoolExecutor
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

import publication_guard as guard
from run_solo_smoke import configure
configure()
from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
from cooperbench.agents.mini_swe_agent_v2.environments.docker import DockerEnvironment
from cooperbench.agents.mini_swe_agent_v2.exceptions import Submitted


def submitted(text=""):
    try:
        DockerEnvironment._check_finished(None, {"returncode": 0, "output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n" + text})
    except Submitted as exc:
        return list(exc.messages)
    raise AssertionError("Native completion did not emit Submitted")


class LocalEnvironment:
    def __init__(self, root): self.root, self.commands = root, []
    def execute(self, action):
        command = shlex.split(action["command"])
        if command[:2] != ["python", "-c"]: raise AssertionError("Only read-only inspection expected")
        self.commands.append(action["command"])
        result = subprocess.run([sys.executable, *command[1:]], cwd=self.root, capture_output=True, text=True, timeout=20)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


class PublicationGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "implementation.py").write_text("value = 0\n")
        self.git("add", "implementation.py"); self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(self.remote))
        for name in ("solo", "agent1", "agent2"):
            self.git("push", "-q", "origin", "HEAD:refs/heads/" + name)
        self.run = self.root / "job"; self.run.mkdir()
        self.configure("solo")
        self.model = SimpleNamespace(format_message=lambda **kwargs: kwargs)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def configure(self, condition):
        (self.run / "task_config.json").write_text(json.dumps({"condition": condition, "execution_seconds": 60,
            "task_key": "fixture", "task": {"base": self.base}}))

    def agent(self, name="solo"):
        agent = DefaultAgent(self.model, LocalEnvironment(self.repo), agent_id=name,
            comm=None if name == "solo" else SimpleNamespace(agent_id=name),
            system_template="", instance_template="", compaction_enabled=False)
        agent.messages = submitted()
        return agent

    def record(self, condition="solo"):
        return json.loads((self.run / condition / "publication_guard.json").read_text())

    def test_solo_native_completion_after_local_commit_receives_reminder(self):
        (self.repo / "implementation.py").write_text("value = 1\n")
        self.git("add", "implementation.py"); self.git("commit", "-qm", "unpublished")
        agent = self.agent(); old = DefaultAgent._nudge_unsubmitted
        head, remote = self.git("rev-parse", "HEAD"), self.git("ls-remote", "origin")
        with guard.publication_guard(self.run):
            self.assertTrue(agent._nudge_unsubmitted())
            self.assertEqual(agent.messages[-1]["role"], "user")
            self.assertIn("git push origin HEAD:solo", agent.messages[-1]["content"])
            self.assertIn("does not extend", agent.messages[-1]["content"])
        self.assertIs(DefaultAgent._nudge_unsubmitted, old)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("ls-remote", "origin"), remote)
        record = self.record()
        self.assertTrue(record["context_closed"])
        self.assertEqual(record["counts"], {"solo": 1})
        self.assertEqual(record["events"][0]["publication_state"]["local_ahead"], 1)

    def test_existing_pr_does_not_prompt_even_with_new_dirty_work(self):
        self.git("push", "-q", "origin", "HEAD:refs/tags/pr/solo")
        (self.repo / "implementation.py").write_text("value = 9\n")
        agent = self.agent()
        with guard.publication_guard(self.run): self.assertFalse(agent._nudge_unsubmitted())
        self.assertEqual(self.record()["events"][0]["reason"], "already_has_pr")

    def test_nonempty_pushed_branch_is_already_publishable_without_pr(self):
        (self.repo / "implementation.py").write_text("value = 2\n")
        self.git("add", "implementation.py"); self.git("commit", "-qm", "published")
        self.git("push", "-q", "origin", "HEAD:refs/heads/solo")
        agent = self.agent()
        with guard.publication_guard(self.run): self.assertFalse(agent._nudge_unsubmitted())
        self.assertEqual(self.record()["events"][0]["reason"], "already_published_branch")

    def test_clean_empty_peer_withdrawal_is_not_forced_to_publish(self):
        self.configure("integrator")
        agent = self.agent("agent2"); agent.messages = submitted("I withdraw my contribution.")
        with guard.publication_guard(self.run): self.assertFalse(agent._nudge_unsubmitted())
        self.assertEqual(self.record("integrator")["events"][0]["reason"], "no_unpublished_work")
        self.assertEqual(agent.messages[-1]["role"], "exit")

    def test_dirty_work_two_nudge_limit_keeps_third_voluntary_exit(self):
        (self.repo / "implementation.py").write_text("value = 3\n")
        agent = self.agent()
        with guard.publication_guard(self.run):
            for _ in range(2):
                agent.messages += submitted()
                self.assertTrue(agent._nudge_unsubmitted())
            agent.messages += submitted()
            self.assertFalse(agent._nudge_unsubmitted())
        self.assertEqual(self.record()["counts"], {"solo": 2})
        self.assertEqual(self.record()["events"][-1]["reason"], "reminder_limit_reached")
        self.assertEqual(agent.messages[-1]["role"], "exit")

    def test_exception_limit_and_malformed_exits_are_never_revived(self):
        agent = self.agent()
        exits = [{"role": "exit", "content": "stop", "extra": {"exit_status": x, "submission": "stop"}}
                 for x in ("APITimeoutError", "UserInterruption", "LimitsExceeded")]
        exits += [{"role": "exit", "content": "x", "extra": {"exit_status": "Submitted", "submission": "y"}},
                  {"role": "exit", "content": "", "extra": {"exit_status": "Submitted", "submission": "", "traceback": "error"}}]
        with mock.patch.object(guard, "inspect_publication") as inspect, guard.publication_guard(self.run):
            for message in exits:
                agent.messages = [message]
                self.assertFalse(agent._nudge_unsubmitted())
                self.assertEqual(agent.messages, [message])
            inspect.assert_not_called()

    def test_deadline_crossed_during_inspection_does_not_add_prompt(self):
        agent = self.agent(); clock = [100.0]
        observation = dict(pr_open=False, remote_branch=self.base, remote_nonempty=False, dirty=True, local_ahead=0)
        def inspect(*args): clock[0] = 110.1; return observation
        with mock.patch.object(guard.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(guard, "inspect_publication", side_effect=inspect), guard.publication_guard(self.run, deadline=110):
            self.assertFalse(agent._nudge_unsubmitted())
            self.assertEqual(agent.messages[-1]["role"], "exit")
        self.assertEqual(self.record()["events"][0]["reason"], "deadline_reached_after_inspection")

    def test_concurrent_agents_have_separate_limits_and_complete_event_log(self):
        self.configure("strongshare")
        agents = [self.agent("agent1"), self.agent("agent2")]
        observation = dict(pr_open=False, remote_branch=self.base, remote_nonempty=False, dirty=True, local_ahead=0)
        with mock.patch.object(guard, "inspect_publication", return_value=observation), guard.publication_guard(self.run):
            def finish(agent):
                result = []
                for _ in range(3):
                    agent.messages += submitted()
                    result.append(agent._nudge_unsubmitted())
                return result
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(finish, agents))
        self.assertEqual(results, [[True, True, False], [True, True, False]])
        record = self.record("strongshare")
        self.assertEqual(record["counts"], {"agent1": 2, "agent2": 2})
        self.assertEqual([r["index"] for r in record["events"]], list(range(1,7)))

    def test_exception_restores_native_method_and_closes_receipt(self):
        old = DefaultAgent._nudge_unsubmitted
        with self.assertRaisesRegex(RuntimeError, "worker failure"):
            with guard.publication_guard(self.run): raise RuntimeError("worker failure")
        self.assertIs(DefaultAgent._nudge_unsubmitted, old)
        self.assertTrue(self.record()["context_closed"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            with guard.publication_guard(self.run): pass


if __name__ == "__main__": unittest.main()
