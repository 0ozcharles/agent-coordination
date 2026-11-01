"""Explicit checkpoint-code handoff, with one or two continuing agent loops.

Both conditions receive exactly the same checkpoint packet before their first
query. StrongShare retains live peer snapshots; Consolidate retires agent2 and
keeps its snapshot frozen. No code is automatically copied, repaired or merged.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shlex
import subprocess
import threading
import time
from pathlib import Path

from budget_guard import BudgetGuard, write_json
from run_baselines import close_interrupted_tool_calls, digest, load_state, safe_error, save_state, usage
from run_consolidate import apply_handoff, validate_message_layout
from run_solo_smoke import MAX_OUTPUT, MODEL, configure
from task_specs import COMMIT, REPO
from wip_checkpoint import validate_checkpoint

VARIANT = "explicit_checkpoint_packet_v1"
CONTINUATION_SECONDS = 1200


def make_offline_model(agent_id, condition):
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    peer = "agent2" if agent_id == "agent1" else "agent1"
    commands = [
        f"test -s /shared-evidence/{peer}/latest/task.md && "
        f"test -s /shared-evidence/{peer}/latest/history.json && "
        f"test -d /shared-evidence/{peer}/latest/worktree/dirty_equals && "
        "test ! -e /patches && "
        "if touch /shared-evidence/write-probe; then exit 88; else echo handoff_read_only_ok; fi",
        "printf 'synthetic handoff infrastructure check, not a solution\\n' > "
        f"handoff_offline_{agent_id}.txt && git add -A && "
        "git commit -m 'Offline explicit handoff check' && git push",
        "gh pr create --title 'Offline handoff check' --body 'Synthetic infrastructure check'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]

    class OfflineModel(LitellmModel):
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]

        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            validate_message_layout(messages)
            if self.index >= len(commands):
                raise RuntimeError("Unexpected extra offline handoff turn")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-handoff-{agent_id}-{self.index}", "object": "chat.completion", "created": 0,
                "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline infrastructure check.", "tool_calls": [{
                    "id": f"call_handoff_{agent_id}_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def install_first_query_check(model, packet_text, recipient, phase_dir):
    """Check after mini-SWE formatting, before the provider transport runs."""
    original = model._query
    observed = False

    def checked_query(messages, **kwargs):
        nonlocal observed
        validate_message_layout(messages)
        if not observed:
            matches = [i for i, m in enumerate(messages)
                       if m.get("role") == "user" and m.get("content") == packet_text]
            if len(matches) != 1:
                raise RuntimeError("Full, untruncated handoff packet missing from first query")
            write_json(phase_dir / f"first_request_{recipient}.json", {
                "verified": True, "recipient": recipient, "message_index": matches[0],
                "message_sha256": hashlib.sha256(packet_text.encode()).hexdigest(),
                "message_bytes": len(packet_text.encode()), "full_packet_present": True,
                "verification_point": "after model message formatting, before transport",
            })
            observed = True
        return original(messages, **kwargs)

    model._query = checked_query


def phase_worker(run_dir, condition, offline=False, task=None):
    if condition not in ("strongshare", "consolidate") or task is None:
        raise ValueError("An explicit task and handoff condition are required")
    configure()
    import docker
    import yaml
    from baseline_runtime import MemoryBroker, SnapshotDockerEnvironment, restore_volume, verify_workspace_manifest
    from baseline_evidence import StrongSharePublisher
    from baseline_model import make_model
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
    from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
    from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow
    from handoff_packet import build_packet, collect_scratch_files, collect_source_changes, inject_packet, packet_message

    started = time.monotonic()
    run_dir = Path(run_dir)
    phase_dir = run_dir / condition
    phase_dir.mkdir(parents=True, exist_ok=True)
    label = run_dir.name + "-" + condition
    volume = "rsi-" + label.lower().replace("_", "-")
    evidence_volume = volume + "-evidence"
    envs, agents, connectors, outcomes = {}, {}, {}, {}
    dc = publisher = broker = None
    created_volumes, cleanup_errors = [], []
    exit_code = 2
    result = {"phase": condition, "condition": condition, "status": "initializing",
              "task": task.as_dict(), "offline_selftest": offline, "handoff_variant": VARIANT,
              "continuation_budget_seconds": CONTINUATION_SECONDS,
              "initialization_included_in_budget": False, "money_limit": None, "request_limit": None}
    if condition == "consolidate":
        result.update(active_agent="agent1", retired_agent_model_calls=0)
    write_json(phase_dir / "status.json", result)
    try:
        checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
        validate_checkpoint(checkpoint, task)
        expected = json.loads(task.image_record_path.read_text())
        dc = docker.from_env(timeout=120)
        if dc.images.get(task.image).id != expected["Id"]:
            raise RuntimeError("Task image changed")
        if subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
            raise RuntimeError("Pinned CooperBench commit changed")
        subprocess.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                        "src/cooperbench/agents/mini_swe_agent_v2", str(task.task_dir.relative_to(REPO))],
                       check=True, capture_output=True)
        restore_volume(run_dir / "shared_git.tar", volume, expected["Id"], label,
                       expected_sha256=checkpoint["git_archive_sha256"])
        created_volumes.append(volume)
        dc.volumes.create(name=evidence_volume, labels={"agent-rsi-run": label})
        created_volumes.append(evidence_volume)
        publisher = StrongSharePublisher(dc, expected["Id"], evidence_volume, label)
        proof = {"checkpoint_hash": digest(checkpoint), "agents": {}}
        # Restore and inspect BOTH worktrees before constructing or calling any model.
        for agent_id in task.agents:
            saved = checkpoint["agents"][agent_id]
            env = SnapshotDockerEnvironment(image=saved["image"], run_label=label, agent_id=agent_id,
                    shared_git_volume=volume, evidence_volume=evidence_volume, container_timeout="30m")
            envs[agent_id] = env
            verify_workspace_manifest(env, saved["manifest"])
            proof["agents"][agent_id] = {
                "state_hash": digest(saved["state"]), "filesystem_manifest_hash": digest(saved["manifest"]),
                "source_image": saved["image"], "filesystem_verified": True,
                "state_use": "frozen_evidence_only" if condition == "consolidate" and agent_id == "agent2" else "resumed_model_loop",
            }
        packet = build_packet(checkpoint, task,
                {a: collect_source_changes(envs[a], task) for a in task.agents},
                {a: collect_scratch_files(envs[a], checkpoint["agents"][a]) for a in task.agents})
        write_json(phase_dir / "handoff_packet.json", packet)
        packet_text = packet_message(packet)
        (phase_dir / "handoff_packet.txt").write_text(packet_text)
        result["handoff_packet_sha256"] = digest(packet)
        broker = MemoryBroker.from_snapshot(checkpoint["broker"])
        guard = BudgetGuard(run_dir / "usage_ledger.json", run_dir.name, model=MODEL,
                            enforce_limits=False, max_output_tokens=MAX_OUTPUT)
        cfg = yaml.safe_load(get_config_path("coop").read_text())
        active = ("agent1",) if condition == "consolidate" else tuple(task.agents)
        for agent_id in active:
            model = make_offline_model(agent_id, condition) if offline else make_model(MODEL, guard, condition, agent_id, phase_dir)
            model.config = model.config.model_copy(update={key: cfg["model"][key]
                    for key in ("observation_template", "format_error_template")})
            agent = DefaultAgent(model, envs[agent_id], comm=broker.connector(agent_id), agent_id=agent_id,
                    **{**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False,
                       "output_path": phase_dir / f"{agent_id}.traj.json"})
            agents[agent_id] = agent
            load_state(agent, checkpoint["agents"][agent_id]["state"])
            if digest(save_state(agent)) != digest(checkpoint["agents"][agent_id]["state"]):
                raise RuntimeError("Restored model state changed")
            proof["agents"][agent_id]["state_verified"] = True
            git = GitConnector(agent_id=agent_id, agents=list(task.agents), server_url="/shared-git/team.git")
            git._base_sha, git._initialized = task.base, True
            connectors[agent_id] = git
        if digest(broker.snapshot()) != digest(checkpoint["broker"]):
            raise RuntimeError("Restored mailbox changed")
        proof["broker_hash"] = digest(broker.snapshot())
        write_json(phase_dir / "restore_proof.json", proof)

        def publish(agent_id, initial=False):
            agent = agents.get(agent_id)
            saved = checkpoint["agents"][agent_id]["state"]
            messages = saved["messages"] if initial else agent.messages
            task_text = saved["extra_template_vars"]["task"]
            step = "checkpoint" if initial else agent.n_calls
            metadata = publisher.publish(agent_id, envs[agent_id].container_id, "/workspace/repo",
                                         messages, task_text, step)
            (phase_dir / "evidence_manifests").mkdir(exist_ok=True)
            write_json(phase_dir / "evidence_manifests" / f"{agent_id}_{step}.json", metadata)

        for agent_id in task.agents:
            publish(agent_id, initial=True)
        if condition == "consolidate":
            intervention = apply_handoff(agents["agent1"], broker, checkpoint)
            envs["agent2"].cleanup()
            del envs["agent2"]
            publisher.cleanup()
            publisher = None
            intervention.update(handoff_variant=VARIANT, handoff_packet_sha256=digest(packet))
            write_json(phase_dir / "intervention.json", intervention)
        else:
            for agent_id, agent in agents.items():
                peer = "agent2" if agent_id == "agent1" else "agent1"
                agent.add_messages(agent.model.format_message(role="user", content=(
                    "Additional evidence access is now enabled. Your colleague's task, model/tool history, "
                    f"and latest completed-step working files are at /shared-evidence/{peer}/latest/. "
                    "This read-only snapshot updates after every completed step, including uncommitted and "
                    "untracked files. This supersedes the earlier statement that only pushed work is visible. "
                    "Keep your own independent responsibility and agent loop; coordinate the integration "
                    "through your existing messages and Git. Do not edit the evidence mount. "
                    "You have 1200 seconds after this handoff. Leave time to commit, push, open your local "
                    "PR and issue the exact completion command separately.")))
        delivery = {"condition": condition, "checkpoint_sha256": digest(checkpoint),
                    "packet_sha256": digest(packet), "source_checkpoint_verified": True,
                    "code_automatically_transferred": False, "recipients": {}, "first_request_verified": False}
        for agent_id, agent in agents.items():
            delivery["recipients"][agent_id] = inject_packet(agent, packet, condition, agent_id)
            install_first_query_check(agent.model, packet_text, agent_id, phase_dir)
            agent.save(agent.config.output_path)
        write_json(phase_dir / "handoff_delivery.json", delivery)
        if offline and condition == "consolidate":
            if (agents["agent1"].comm.send("agent2", "must not deliver") or
                    agents["agent1"].comm.send_and_wait("agent2", "must not wait") != (False, [])):
                raise RuntimeError("Retired peer is still reachable")
        execution_start = time.monotonic()
        result["handoff_seconds"] = round(execution_start - started, 3)
        deadline = execution_start + CONTINUATION_SECONDS
        infrastructure_stop = threading.Event()

        def drive(agent_id):
            agent = agents[agent_id]
            peer = "agent2" if agent_id == "agent1" else "agent1"
            last_version, reminded, turns, outcome = None, False, 0, "deadline"
            try:
                while time.monotonic() < deadline:
                    if infrastructure_stop.is_set():
                        raise RuntimeError("Peer infrastructure failed")
                    if deadline - time.monotonic() <= 120 and not reminded:
                        reminded = True
                        agent.add_messages(agent.model.format_message(role="user", content=(
                            "About two minutes remain. Publish your implementation even if incomplete; "
                            "do not claim a failing test passed. Then issue the exact completion command.")))
                    if publisher:
                        version = publisher.version(peer)
                        step = version.get("step") if version else None
                        if step != last_version:
                            agent.add_messages(agent.model.format_message(role="user", content=(
                                f"Peer evidence updated: /shared-evidence/{peer}/latest/ (version {step}). "
                                "Read task.md, history.json, metadata.json, worktree/, and temporary/ as needed.")))
                            last_version = step
                    try:
                        validate_message_layout(agent.messages)
                        agent.step()
                    except InterruptAgentFlow as exc:
                        if any(m.get("role") == "exit" for m in exc.messages):
                            close_interrupted_tool_calls(agent)
                        agent.add_messages(*exc.messages)
                    turns += 1
                    agent.save(agent.config.output_path)
                    if offline and turns == 1:
                        obs = [m for m in agent.messages if m.get("role") == "tool"]
                        if (not obs or obs[-1].get("extra", {}).get("returncode") != 0
                                or "handoff_read_only_ok" not in str(obs[-1])):
                            raise RuntimeError("Handoff evidence visibility/read-only probe failed")
                    if publisher:
                        publish(agent_id)
                    if agent.messages[-1].get("role") == "exit":
                        if agent._nudge_unsubmitted():
                            agent.messages = [m for m in agent.messages if m.get("role") != "exit"]
                            continue
                        outcome = agent.messages[-1].get("extra", {}).get("exit_status", "submitted")
                        break
                return {"status": outcome, "phase_turns": turns, "model_turns": agent.n_calls,
                        "infrastructure_error": False}
            except Exception as exc:
                infrastructure_stop.set()
                return {"status": "error", "error_type": type(exc).__name__, "error": safe_error(exc),
                        "phase_turns": turns, "model_turns": agent.n_calls, "infrastructure_error": True}
            finally:
                try:
                    agent.comm.mark_exited(published=agent._opened_pr())
                except Exception:
                    agent.comm.mark_exited(published=False)
                agent.save(agent.config.output_path)

        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            futures = {a: pool.submit(drive, a) for a in active}
            outcomes = {a: f.result() for a, f in futures.items()}
        write_json(phase_dir / "agent_outcomes.json", outcomes)
        write_json(phase_dir / "messages.json", broker.snapshot())
        for agent_id in active:
            name = "final.patch" if condition == "consolidate" else f"agent{task.agents[agent_id]}.patch"
            patch = connectors[agent_id].submitted_patch(envs[agent_id]) or ""
            (phase_dir / name).write_text(patch)
            diagnostic = envs[agent_id].execute({"command": "git diff --binary " + shlex.quote(task.base)})
            (phase_dir / f"{agent_id}_unsubmitted_diagnostic.patch").write_text(diagnostic.get("output", ""))
            first = json.loads((phase_dir / f"first_request_{agent_id}.json").read_text())
            if first.get("message_sha256") != delivery["recipients"][agent_id]["message_sha256"]:
                raise RuntimeError("Delivered packet differs from first model query")
            if not offline:
                inputs = sorted((phase_dir / "responses" / agent_id).glob("request_*_input.json"))
                if not inputs:
                    raise RuntimeError("Missing actual first provider input")
                actual = json.loads(inputs[0].read_text())["request"]["messages"]
                if sum(m.get("role") == "user" and m.get("content") == packet_text for m in actual) != 1:
                    raise RuntimeError("Actual first provider input did not contain complete packet")
                first["actual_transport_input_verified"] = True
                write_json(phase_dir / f"first_request_{agent_id}.json", first)
        delivery["first_request_verified"] = True
        write_json(phase_dir / "handoff_delivery.json", delivery)
        if any(o["infrastructure_error"] for o in outcomes.values()):
            raise RuntimeError("Agent infrastructure failed; incomplete attempt retained")
        result.update(status="completed", outcomes=outcomes, infrastructure_error=False,
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      continuation_seconds=round(time.monotonic() - execution_start, 3))
        if condition == "consolidate":
            result.update(outcome=outcomes["agent1"]["status"], phase_turns=outcomes["agent1"]["phase_turns"],
                          model_turns=outcomes["agent1"]["model_turns"],
                          final_patch_sha256=hashlib.sha256((phase_dir / "final.patch").read_bytes()).hexdigest())
        exit_code = 0
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__, error=safe_error(exc),
                      infrastructure_error=True, elapsed_seconds=round(time.monotonic() - started, 3))
    finally:
        for agent in agents.values():
            try:
                agent.save(agent.config.output_path)
                close = getattr(agent.model, "close", None)
                if close:
                    close()
            except Exception as exc:
                cleanup_errors.append("agent close: " + safe_error(exc))
        for env in envs.values():
            try:
                env.cleanup()
            except Exception as exc:
                cleanup_errors.append("container cleanup: " + safe_error(exc))
        if publisher:
            try:
                publisher.cleanup()
            except Exception as exc:
                cleanup_errors.append("publisher cleanup: " + safe_error(exc))
        if dc:
            for name in reversed(created_volumes):
                try:
                    dc.volumes.get(name).remove()
                except Exception as exc:
                    cleanup_errors.append("volume cleanup: " + safe_error(exc))
            dc.close()
        if cleanup_errors:
            result.update(cleanup_errors=cleanup_errors, infrastructure_error=True)
            if result.get("status") != "completed":
                result["status"] = "error"
            exit_code = 2
        write_json(phase_dir / "usage_summary.json", usage(run_dir, condition))
        write_json(phase_dir / "status.json", result)
    return exit_code
