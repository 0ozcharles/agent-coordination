"""Explicit real Docker capture/restore proof; no provider/model calls.

Only this offline fixture manufactures a merge conflict. Production collection
must observe a naturally occurring current index conflict without injection.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from run_solo_smoke import configure
configure()
import docker
import yaml
from baseline_runtime import (MemoryBroker, SnapshotDockerEnvironment, backup_volume,
    create_shared_git_volume, restore_volume, verify_workspace_manifest)
from budget_guard import write_json
from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
import conflict_checkpoint as cc
from run_baselines import digest, load_state, save_state
from selector_task_specs import get_task

ROOT = Path(__file__).resolve().parent


def file_hash(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def model_query_forbidden(*args, **kwargs):
    raise AssertionError("A provider/model query is forbidden in the checkpoint fixture")


class ConflictDockerCheckpointTest(unittest.TestCase):
    def test_actual_unmerged_index_survives_independent_docker_restore(self):
        task = get_task("click2068_f4_f10")
        expected = json.loads(task.image_record_path.read_text())
        name = "conflict_checkpoint_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run = ROOT / "artifacts/conflict_checkpoint_checks" / name
        run.mkdir(parents=True)
        frozen = json.loads((ROOT / "artifacts/publication_runs/luna_publication_v1/protocol.json").read_text())["source_manifest"]
        for relative, wanted in frozen.items(): self.assertEqual(file_hash(ROOT / relative), wanted, relative)
        dc = docker.from_env(timeout=120)
        envs, restored_envs, images, volumes = {}, {}, [], []
        volume = "rsi-" + name.lower().replace("_", "-")
        other_volume = volume + "-restored"
        proof = None
        try:
            self.assertEqual(dc.images.get(task.image).id, expected["Id"])
            self.assertEqual(expected["expected_base"], task.base)
            cfg = yaml.safe_load(get_config_path("coop").read_text())
            create_shared_git_volume(volume, expected["Id"], name); volumes.append(volume)
            broker, agents, connectors = MemoryBroker(list(task.agents)), {}, {}
            model = SimpleNamespace(format_message=lambda **kwargs: kwargs,
                get_template_vars=lambda: {}, query=model_query_forbidden)
            for agent_id, feature in task.agents.items():
                env = SnapshotDockerEnvironment(image=expected["Id"], run_label=name, agent_id=agent_id,
                    shared_git_volume=volume, evidence_volume=None, container_timeout="10m", **cfg.get("environment", {}))
                envs[agent_id] = env
                git = GitConnector(agent_id=agent_id, agents=list(task.agents), server_url="/shared-git/team.git")
                git.setup(env); connectors[agent_id] = git
                self.assertEqual(git._base_sha, task.base)
                agent = DefaultAgent(model, env, comm=broker.connector(agent_id), agent_id=agent_id,
                    **{**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False})
                task_text = (task.task_dir / f"feature{feature}/feature.md").read_text()
                agent.extra_template_vars = {"task": task_text, "agent_id": agent_id, "agents": list(task.agents),
                    "git_enabled": True, "messaging_enabled": True}
                agent.add_messages(model.format_message(role="system", content=agent._render_template(agent.config.system_template)),
                    model.format_message(role="user", content=agent._render_template(agent.config.instance_template)))
                agents[agent_id] = agent
            self.assertTrue(agents["agent1"].comm.send("agent2", "Unread offline checkpoint message"))
            commands = (
                "printf 'base\\n' > conflict_checkpoint_offline.txt && "
                "git add conflict_checkpoint_offline.txt && git commit -m 'Offline common fixture' && "
                "git branch offline-conflict-peer && "
                "printf 'local implementation\\n' > conflict_checkpoint_offline.txt && "
                "git commit -am 'Offline local implementation' && "
                "git checkout offline-conflict-peer && "
                "printf 'peer implementation\\n' > conflict_checkpoint_offline.txt && "
                "git commit -am 'Offline peer implementation' && git checkout agent1 && "
                "git merge offline-conflict-peer"
            )
            conflict_result = envs["agent1"].execute({"command": commands})
            self.assertEqual(conflict_result["returncode"], 1)
            agents["agent1"].add_messages({"role": "assistant", "content": "Offline fixture only.",
                "tool_calls": [{"id": "offline_conflict_fixture", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": commands})}}]},
                {"role": "tool", "tool_call_id": "offline_conflict_fixture", "content": conflict_result["output"]})
            scratch = envs["agent2"].execute({"command": "printf 'untracked scratch evidence\\n' > /tmp/conflict_checkpoint_scratch.txt"})
            self.assertEqual(scratch["returncode"], 0)
            observations = {a: cc.inspect_conflict(env) for a, env in envs.items()}
            self.assertTrue(observations["agent1"]["has_unmerged"])
            self.assertFalse(observations["agent2"]["has_unmerged"])
            self.assertEqual({r["stage"] for r in observations["agent1"]["unmerged_entries"]}, {1, 2, 3})
            outcomes = {a: {"exited": False, "submitted": False, "exit_status": None,
                "infrastructure_error": False, "published_pr": False} for a in task.agents}
            checkpoint = {"schema": cc.CHECKPOINT_SCHEMA, "task": task.as_dict(), "prefix_steps_per_agent": 1,
                "conflict_checkpoint": {"rule": cc.CHECKPOINT_RULE, "paired_round": 1, "eligible": True,
                    "terminal_policy": "both_active", "selection_uses_test_results": False,
                    "observations": observations, "outcomes": outcomes}, "broker": broker.snapshot(), "agents": {}}
            for a, env in envs.items():
                manifest, image = env.workspace_manifest(), env.snapshot_image()
                images.append(image)
                checkpoint["agents"][a] = {"state": save_state(agents[a]), "git_base": connectors[a]._base_sha,
                    "manifest": manifest, "image": image}
            backup_volume(volume, run / "shared_git.tar", expected["Id"], name)
            checkpoint["git_archive_sha256"] = file_hash(run / "shared_git.tar")
            cc.validate_conflict_checkpoint(checkpoint, task)
            write_json(run / "checkpoint.json", checkpoint)
            self.assertEqual(cc.verify_checkpoint_files(run, task), checkpoint)
            restore_volume(run / "shared_git.tar", other_volume, expected["Id"], name,
                expected_sha256=checkpoint["git_archive_sha256"])
            volumes.append(other_volume)
            restored_broker = MemoryBroker.from_snapshot(checkpoint["broker"])
            restore_proof = {"checkpoint_sha256": file_hash(run / "checkpoint.json"), "agents": {},
                "broker_sha256": digest(restored_broker.snapshot())}
            self.assertEqual(restored_broker.snapshot(), checkpoint["broker"])
            for a, saved in checkpoint["agents"].items():
                env = SnapshotDockerEnvironment(image=saved["image"], run_label=name, agent_id=a,
                    shared_git_volume=other_volume, evidence_volume=None, container_timeout="10m", **cfg.get("environment", {}))
                restored_envs[a] = env
                verify_workspace_manifest(env, saved["manifest"])
                conflict_proof = cc.verify_restored_conflict(env, observations[a])
                restored = DefaultAgent(model, env, comm=restored_broker.connector(a), agent_id=a,
                    **{**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False})
                load_state(restored, saved["state"])
                self.assertEqual(save_state(restored), saved["state"])
                self.assertEqual(restored.n_calls, 0)
                source_refs = envs[a].execute({"command": "git ls-remote origin"})
                restored_refs = env.execute({"command": "git ls-remote origin"})
                self.assertEqual(restored_refs, source_refs)
                restore_proof["agents"][a] = {"conflict": conflict_proof, "workspace_verified": True,
                    "state_sha256": digest(save_state(restored)), "shared_git_refs_verified": True}
            self.assertEqual(restored_broker.snapshot(), checkpoint["broker"])
            self.assertEqual(restored_broker.connector("agent2").receive(), checkpoint["broker"]["inboxes"]["agent2"])
            write_json(run / "restore_proof.json", restore_proof)
            result = restored_envs["agent1"].execute({"command": "rm .git/MERGE_HEAD"})
            self.assertEqual(result["returncode"], 0)
            with self.assertRaisesRegex(ValueError, "Restored unmerged"):
                cc.verify_restored_conflict(restored_envs["agent1"], observations["agent1"])
            # Source container remains unchanged: the two volumes/images are independent.
            self.assertEqual(cc.inspect_conflict(envs["agent1"]), observations["agent1"])
            for relative, wanted in frozen.items(): self.assertEqual(file_hash(ROOT / relative), wanted, relative)
            proof = {"passed": True, "model_api_calls": 0,
                "checkpoint_source_sha256": file_hash(ROOT / "conflict_checkpoint.py"),
                "test_source_sha256": file_hash(__file__), "schema": cc.CHECKPOINT_SCHEMA,
                "schema_verified": True, "old_source_count": len(frozen), "old_sources_unchanged": True,
                "restored_two_agents": True, "restored_shared_git": True, "restored_broker": True,
                "restored_full_state": True, "restored_unread_message": True,
                "current_unmerged_stages": [1, 2, 3], "merge_head_tamper_rejected": True,
                "source_unchanged_after_restore_mutation": True,
                "run_dir": str(run.relative_to(ROOT)), "checkpoint_sha256": file_hash(run / "checkpoint.json"),
                "restore_proof_sha256": file_hash(run / "restore_proof.json")}
        finally:
            for env in [*restored_envs.values(), *envs.values()]: env.cleanup()
            for name in volumes: dc.volumes.get(name).remove()
            for image in images: dc.images.remove(image)
            dc.close()
        proof["cleanup_completed"] = True
        write_json(ROOT / "artifacts/conflict_checkpoint_docker_validation.json", proof)


if __name__ == "__main__": unittest.main()
