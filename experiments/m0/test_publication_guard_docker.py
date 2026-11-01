"""Explicit zero-provider Docker proof for the publication-parity intervention.

This test uses a real native DefaultAgent and GitConnector. All changes and
publication commands are emitted as scripted model tool calls, never executed
by the guard or an external repair step. Run this file explicitly.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import unittest

from run_solo_smoke import configure
configure()

import docker
import yaml
from openai.types.chat import ChatCompletion
from baseline_runtime import SnapshotDockerEnvironment, create_shared_git_volume
from budget_guard import write_json
from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow
from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
from publication_guard import GUARD_VERSION, publication_guard
from run_baselines import close_interrupted_tool_calls
from selector_task_specs import get_task
from solo_expansion_worker import combined_task_text, initialize_fresh_agent, inspect_initial_state

ROOT = Path(__file__).resolve().parent
MARKER = "publication_guard_offline.txt"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ScriptedPublicationModel(LitellmModel):
    """No provider/client is constructed; commands traverse the native agent."""
    commands = [
        "printf 'Synthetic publication-parity check, not a feature solution\\n' > " + MARKER
        + " && git add " + MARKER + " && git commit -m 'Offline unpublished work'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        "git push origin HEAD:solo && gh pr create --title 'Offline publication-parity test'"
        + " --body 'Synthetic infrastructure verification'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]

    def __init__(self):
        super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
        self.index = 0
        self.saw_reminder = False

    def _query(self, messages, **kwargs):
        if self.index >= len(self.commands):
            raise AssertionError("Unexpected extra model turn")
        if self.index == 2:
            self.saw_reminder = any(m.get("role") == "user" and
                "Your work is still local" in str(m.get("content")) and
                "git push origin HEAD:solo" in str(m.get("content")) for m in messages)
            if not self.saw_reminder:
                raise AssertionError("Publication command must follow the guard reminder")
        command = self.commands[self.index]
        self.index += 1
        return ChatCompletion.model_validate({
            "id": f"publication-offline-{self.index}", "object": "chat.completion", "created": 0,
            "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline publication check.", "tool_calls": [{
                    "id": f"call_publication_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    def _calculate_cost(self, response):
        return {"cost": 0.0}


class NativePublicationDockerTest(unittest.TestCase):
    def test_premature_solo_completion_is_reminded_then_model_publishes(self):
        task = get_task("click2068_f4_f10")
        name = "publication_guard_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run = ROOT / "artifacts/publication_guard_checks" / name
        phase = run / "solo"
        phase.mkdir(parents=True)
        seconds = 180
        write_json(run / "task_config.json", {"task_key": task.key, "task": task.as_dict(),
            "condition": "solo", "execution_seconds": seconds, "offline_selftest": True})
        historical = json.loads((ROOT / "artifacts/selector_runs/luna_selector_v1/protocol.json").read_text())
        frozen = historical["source_manifest"]
        for rel, expected_hash in frozen.items():
            self.assertEqual(sha(ROOT / rel), expected_hash, rel)
        original_nudge = DefaultAgent._nudge_unsubmitted
        expected = json.loads(task.image_record_path.read_text())
        dc = env = None
        volume_created = False
        volume = "rsi-" + name.lower().replace("_", "-")
        result = None
        try:
            dc = docker.from_env(timeout=120)
            self.assertEqual(dc.images.get(task.image).id, expected["Id"])
            self.assertEqual(expected["expected_base"], task.base)
            cfg = yaml.safe_load(get_config_path("solo").read_text())
            started = time.monotonic()
            deadline = started + seconds
            with publication_guard(run, deadline=deadline):
                create_shared_git_volume(volume, expected["Id"], name)
                volume_created = True
                env = SnapshotDockerEnvironment(image=expected["Id"], run_label=name, agent_id="solo",
                    shared_git_volume=volume, evidence_volume=None, container_timeout="5m",
                    **cfg.get("environment", {}))
                git = GitConnector(agent_id="solo", agents=["solo"], server_url="/shared-git/team.git")
                git.setup(env)
                self.assertEqual(git._base_sha, task.base)
                model = ScriptedPublicationModel()
                model.config = model.config.model_copy(update={key: cfg["model"][key]
                    for key in ("observation_template", "format_error_template")})
                agent = DefaultAgent(model, env, comm=None, agent_id="solo", **{**cfg["agent"],
                    "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False,
                    "output_path": phase / "solo.traj.json"})
                initialize_fresh_agent(agent, combined_task_text(task), seconds)
                initial = inspect_initial_state(env, dc, task, volume, expected["Id"], agent)
                write_json(phase / "initial_state.json", initial)
                turns, outcome = 0, None
                while time.monotonic() < deadline:
                    try:
                        agent.step()
                    except InterruptAgentFlow as exc:
                        if any(m.get("role") == "exit" for m in exc.messages):
                            close_interrupted_tool_calls(agent)
                        agent.add_messages(*exc.messages)
                    turns += 1
                    agent.save(agent.config.output_path)
                    if agent.messages[-1].get("role") == "exit":
                        if agent._nudge_unsubmitted():
                            agent.messages = [m for m in agent.messages if m.get("role") != "exit"]
                            continue
                        outcome = agent.messages[-1].get("extra", {}).get("exit_status")
                        break
                self.assertEqual(turns, 4)
                self.assertEqual(outcome, "Submitted")
                self.assertTrue(model.saw_reminder)
                self.assertEqual(model.index, 4)
                self.assertEqual(agent.cost, 0)
                patch = git.submitted_patch(env) or ""
                self.assertIn(f"diff --git a/{MARKER} b/{MARKER}", patch)
                (phase / "final.patch").write_text(patch)
                refs = env.execute({"command": "git ls-remote origin refs/tags/pr/solo refs/heads/solo"})
                self.assertEqual(refs.get("returncode"), 0)
                self.assertIn("refs/tags/pr/solo", refs["output"])
                self.assertLess(time.monotonic(), deadline)
                agent.save(agent.config.output_path)
                result = {"status": "completed", "outcome": outcome, "turns": turns,
                    "model_api_calls": 0, "scripted_model_turns": model.index,
                    "model_saw_reminder_before_publication": model.saw_reminder,
                    "published_pr": True, "final_patch_sha256": sha(phase / "final.patch"),
                    "elapsed_seconds": round(time.monotonic() - started, 6)}
                write_json(phase / "status.json", result)
            guard = json.loads((phase / "publication_guard.json").read_text())
            self.assertEqual(guard["counts"], {"solo": 1})
            self.assertEqual([e["reason"] for e in guard["events"]],
                ["unpublished_local_work", "already_has_pr"])
            self.assertEqual(guard["events"][0]["publication_state"]["local_ahead"], 1)
            self.assertFalse(guard["events"][0]["publication_state"]["dirty"])
            self.assertTrue(guard["context_closed"])
            self.assertFalse(guard["automatic_publication"])
            self.assertFalse(guard["deadline_extended"])
            self.assertIs(DefaultAgent._nudge_unsubmitted, original_nudge)
            for rel, expected_hash in frozen.items():
                self.assertEqual(sha(ROOT / rel), expected_hash, rel)
        finally:
            if env is not None:
                env.cleanup()
            if dc is not None and volume_created:
                dc.volumes.get(volume).remove()
            if dc is not None:
                dc.close()
        proof = {"passed": True, "model_api_calls": 0, "guard_version": GUARD_VERSION,
            "guard_source_sha256": sha(ROOT / "publication_guard.py"),
            "test_source_sha256": sha(__file__), "run_dir": str(run.relative_to(ROOT)),
            "old_source_count": len(frozen), "old_sources_unchanged": True,
            "native_default_agent": True, "native_git_connector": True, "communication_enabled": False,
            "reminders": 1, "automatic_publication": False, "deadline_extended": False,
            "cleanup_completed": True, "result": result,
            "evidence_sha256": {p.name: sha(p) for p in sorted(phase.iterdir()) if p.is_file()}}
        write_json(ROOT / "artifacts/publication_guard_docker_validation.json", proof)


if __name__ == "__main__":
    unittest.main()
