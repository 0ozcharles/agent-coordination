"""Two live Luna agents with a predesignated agent1 final integrator.

This explicit copy preserves the frozen Luna handoff loop, tools, accounting,
source-scoped evidence publisher and deadline semantics. Its new organization
update assigns agent1 the only scored published artifact. Agent2 continues its
feature and communicates work; the runner never copies or merges its code.
Only new files implement this condition; frozen workers remain unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
import shlex
import subprocess
import threading
import time
from pathlib import Path

from budget_guard import write_json
from run_baselines import close_interrupted_tool_calls, digest, load_state, safe_error, save_state, usage
from run_consolidate import validate_message_layout
from run_solo_smoke import configure
from task_specs import COMMIT, REPO
from wip_checkpoint import validate_checkpoint
from luna_accounting import LunaAccounting as BudgetGuard, MODEL, MAX_OUTPUT
from luna_handoff_worker import ContinuationBudgetExhausted, unqueried_budget_agents

ROOT = Path(__file__).resolve().parent
VARIANT = "source_handoff_diff_v1"
CONDITION_VERSION = "designated_integrator_v1"
ACTIVE = ("agent1", "agent2")
AUTHORITATIVE_AGENT = "agent1"
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _provider_messages(messages):
    allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
    return [{k: v for k, v in message.items() if k in allowed} for message in messages]


def _validate_boundary(messages):
    # The immutable packet implementation already owns the full tool pairing
    # validator; validate before adding either organization or evidence text.
    from cross_task_packet import _SOURCE_VALIDATOR
    _SOURCE_VALIDATOR._validate_messages(messages)


def organization_message(task, agent_id, remaining_seconds):
    """The same delivery contract is stated explicitly to both live agents."""
    if agent_id not in ACTIVE or not math.isfinite(remaining_seconds) or remaining_seconds < 0:
        raise ValueError("Invalid organization message recipient or remaining time")
    peer = "agent2" if agent_id == "agent1" else "agent1"
    first, second = task.features
    common = (
        "ORGANIZATION UPDATE: designated final integrator. Both agent1 and agent2 remain active, "
        "with separate workspaces and native message/Git tools. Neither agent is retired. "
        f"Agent1 remains responsible for feature {first} and is the predesignated final integrator "
        f"for BOTH feature {first} and feature {second}. Agent2 continues implementing feature {second}, "
        "publishes its proposed changes, and communicates the relevant commits, files and test results "
        "to agent1. Agent1 must review and incorporate needed peer work into agent1's own published "
        "branch and test the combined implementation. No code is automatically copied, repaired or merged. "
        "This update supersedes the original instruction that the evaluator automatically merges both "
        "agents' submissions: only agent1's published patch will be evaluated against BOTH feature "
        "requirements. Agent2's patch is retained as a proposal and will not be separately scored, "
        "automatically merged, selected instead of agent1, or used as a fallback. The choice of agent1 "
        "was fixed before this continuation and does not depend on either agent's results. "
    )
    responsibility = (
        "You are agent1. Deliver the combined implementation on your own agent1 branch; another "
        "agent's correct implementation alone cannot substitute for your final delivery. "
        if agent_id == "agent1" else
        "You are agent2. Continue your assigned feature, publish your proposal on agent2, and tell "
        "agent1 how to integrate it. Your contribution is still required; do not stop merely because "
        "agent1 is the final integrator. You may finish normally after publishing and communicating "
        "your completed contribution; do not wait indefinitely after a colleague has exited. "
    )
    evidence = (
        "Additional evidence access is now enabled. Your colleague's task, model/tool history, "
        f"and latest completed-step working files are at /shared-evidence/{peer}/latest/. "
        "This read-only snapshot updates after every completed step, including uncommitted and "
        "untracked files. This supersedes the earlier statement that only pushed work is visible. "
        "Coordinate through your existing messages and Git. Do not edit the evidence mount. "
        f"You have {remaining_seconds:.3f} seconds remaining in this continuation's total budget, "
        "including restore and handoff, with no monetary or model-turn limit. Leave time to commit, "
        "push, open your local PR and issue the exact completion command separately. Publish even if "
        "incomplete and do not claim failing tests passed."
    )
    return common + responsibility + evidence


def apply_integrator_assignment(agents, checkpoint, task, *, remaining_seconds):
    if set(agents) != set(ACTIVE):
        raise ValueError("Integrator requires both original active agents")
    recipients = {}
    for agent_id in ACTIVE:
        agent = agents[agent_id]
        before = save_state(agent)
        if digest(before) != digest(checkpoint["agents"][agent_id]["state"]):
            raise RuntimeError("Agent state changed before organization assignment")
        _validate_boundary(agent.messages)
        content = organization_message(task, agent_id, remaining_seconds)
        message = agent.model.format_message(role="user", content=content)
        if message.get("role") != "user" or message.get("content") != content:
            raise RuntimeError("Model formatter changed organization update")
        agent.add_messages(message)
        if agent.messages[:-1] != before["messages"]:
            raise RuntimeError("Organization update changed inherited history")
        _validate_boundary(agent.messages)
        recipients[agent_id] = {
            "role": "user", "message_index": len(before["messages"]), "content": content,
            "message_sha256": _sha(content.encode()), "history_preserved": True,
            "before_state_sha256": digest(before), "after_state_sha256": digest(save_state(agent)),
        }
    return {"schema": "designated_integrator_assignment_v1", "condition": "integrator",
            "condition_version": CONDITION_VERSION, "checkpoint_sha256": digest(checkpoint),
            "active_agents": list(ACTIVE), "authoritative_agent": AUTHORITATIVE_AGENT,
            "peer_retired": False, "automatic_merge": False, "posthoc_selection": False,
            "remaining_seconds_at_assignment": remaining_seconds, "recipients": recipients}


def inject_integrator_packet(agent, packet, agent_id):
    """Append the unchanged common packet without mislabelling it StrongShare."""
    from cross_task_packet import packet_message
    if agent_id not in ACTIVE or set(packet.get("agents", {})) != set(ACTIVE):
        raise ValueError("Incomplete integrator packet")
    _validate_boundary(agent.messages)
    before = copy.deepcopy(agent.messages)
    content = packet_message(packet)
    message = agent.model.format_message(role="user", content=content)
    if message.get("role") != "user" or message.get("content") != content:
        raise RuntimeError("Model formatter changed checkpoint evidence")
    agent.add_messages(message)
    if agent.messages[:-1] != before or len(agent.messages) != len(before) + 1:
        raise RuntimeError("Packet injection changed existing history")
    _validate_boundary(agent.messages)
    return {"packet_sha256": digest(packet), "message_sha256": _sha(content.encode()),
            "message_index": len(before), "history_preserved": True,
            "all_packet_content_delivered": True, "role": "user", "condition": "integrator",
            "agent_id": agent_id, "schema_version": 2, "representation": "unified_diff"}


def verify_first_messages(messages, packet_text, assignment, original_history):
    _validate_boundary(messages)
    normalized = _provider_messages(messages)
    if normalized[:len(original_history)] != _provider_messages(original_history):
        raise RuntimeError("First query changed inherited provider history")
    index = assignment["message_index"]
    if (index != len(original_history) or index + 1 >= len(messages)
            or messages[index].get("role") != "user"
            or messages[index].get("content") != assignment["content"]
            or _sha(assignment["content"].encode()) != assignment["message_sha256"]
            or messages[index + 1].get("role") != "user"
            or messages[index + 1].get("content") != packet_text):
        raise RuntimeError("First query lacks the exact organization update followed by full packet")
    for content in (assignment["content"], packet_text):
        if sum(m.get("role") == "user" and m.get("content") == content for m in messages) != 1:
            raise RuntimeError("First query contains missing or duplicated handoff instructions")


def install_first_query_check(model, packet_text, assignment, original_history, recipient, phase_dir):
    original = model._query
    observed = False

    def checked(messages, **kwargs):
        nonlocal observed
        if not observed:
            verify_first_messages(messages, packet_text, assignment, original_history)
            # Store only a fingerprint here: the Luna adapter owns sanitized
            # raw transport capture. The final trajectory independently proves
            # this prefix for offline runs without duplicating unredacted text.
            write_json(phase_dir / f"first_model_input_{recipient}.json", {
                "recipient": recipient, "message_count": len(messages),
                "messages_sha256": digest(_provider_messages(messages)), "contains_raw_messages": False})
            write_json(phase_dir / f"first_request_{recipient}.json", {
                "verified": True, "recipient": recipient,
                "message_index": assignment["message_index"] + 1,
                "message_sha256": _sha(packet_text.encode()), "full_packet_present": True,
                "organization_message_sha256": assignment["message_sha256"],
                "organization_update_present": True, "inherited_history_verified": True,
                "verification_point": "after model formatting, before transport"})
            observed = True
        return original(messages, **kwargs)

    model._query = checked


def publication_provenance(env, agent_id, patch):
    """Read native published refs; never fall back to an unsubmitted worktree."""
    if agent_id not in ACTIVE:
        raise ValueError("Unknown publication agent")
    command = ("git ls-remote origin " + shlex.quote("refs/tags/pr/" + agent_id)
               + " " + shlex.quote("refs/heads/" + agent_id))
    observed = env.execute({"command": command})
    if observed.get("returncode", 0) != 0:
        raise RuntimeError("Cannot verify published remote refs")
    refs = {}
    for line in observed.get("output", "").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 40 and all(c in "0123456789abcdef" for c in parts[0]):
            refs[parts[1]] = parts[0]
    tag = refs.get("refs/tags/pr/" + agent_id)
    branch = refs.get("refs/heads/" + agent_id)
    if patch.strip() and not branch:
        raise RuntimeError("Nonempty submitted patch lacks a published branch")
    strategy = "published_pr" if tag else "published_branch_fallback" if patch.strip() else "no_published_changes"
    return {"agent_id": agent_id, "strategy": strategy, "published_pr": bool(tag),
            "branch_sha": branch, "pr_tag_sha": tag, "patch_sha256": _sha(patch.encode()),
            "patch_nonempty": bool(patch.strip()), "unsubmitted_worktree_used": False}


def final_delivery_record(phase_dir, publications):
    if set(publications) != set(ACTIVE):
        raise RuntimeError("Both publication records are required")
    hashes = {a: _sha((phase_dir / f"{a}.patch").read_bytes()) for a in ACTIVE}
    if any(publications[a].get("patch_sha256") != hashes[a] for a in ACTIVE):
        raise RuntimeError("Publication hash differs from saved patch")
    return {"schema": "predesignated_integrator_delivery_v1", "condition": "integrator",
            "condition_version": CONDITION_VERSION, "active_agents": list(ACTIVE),
            "authoritative_agent": AUTHORITATIVE_AGENT, "selection": "predesignated_agent1",
            "scored_patch": "agent1.patch", "peer_patch": "agent2.patch",
            "patch_sha256": hashes["agent1"], "peer_patch_sha256": hashes["agent2"],
            "automatic_merge": False, "posthoc_selection": False, "peer_retired": False,
            "publications": publications}


def offline_commands(task, agent_id):
    from cross_task_workers import offline_commands as common_commands
    commands = common_commands(task, agent_id)
    if agent_id == "agent1":
        # The scripted MODEL executes this integration through ordinary tools;
        # the worker itself never copies either agent's code into the other.
        commands.insert(2,
            "ready=0; for i in $(seq 1 100); do git fetch -q origin; "
            "if git show origin/agent2:handoff_offline_agent2.txt > /tmp/integrator-peer-proposal.txt 2>/dev/null; "
            "then ready=1; break; fi; sleep 0.1; done; test \"$ready\" = 1 && "
            "cp /tmp/integrator-peer-proposal.txt integrator_peer_proposal.txt && "
            "git add integrator_peer_proposal.txt && git commit -m 'Offline model integrates peer proposal' && "
            "git push && echo offline_integrator_peer_incorporated")
    return commands


def make_offline_model(task, agent_id):
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    commands = offline_commands(task, agent_id)

    class OfflineModel(LitellmModel):
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]

        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            validate_message_layout(messages)
            if self.index >= len(commands):
                raise RuntimeError("Unexpected extra offline integrator turn")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-integrator-{agent_id}-{self.index}", "object": "chat.completion", "created": 0,
                "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline infrastructure check.", "tool_calls": [{
                    "id": f"call_integrator_{agent_id}_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=1200):
    """Supervisor entry: existing checkpoint, two active loops, agent1 delivery."""
    from cross_task_specs import get_task
    from cross_task_workers import packet_module_context
    if condition != "integrator" or task is None or getattr(task, "key", None) not in TASK_KEYS:
        raise ValueError("Integrator requires an authorized task and explicit integrator condition")
    if task.as_dict() != get_task(task.key).as_dict():
        raise ValueError("Task differs from pinned registry")
    if (type(execution_seconds) not in (int, float) or not math.isfinite(execution_seconds)
            or execution_seconds <= 0 or not isinstance(offline, bool)):
        raise ValueError("Execution time must be positive finite; offline must be boolean")
    with packet_module_context(task):
        return _phase_worker(Path(run_dir), condition, task, offline, float(execution_seconds))


def _phase_worker(run_dir, condition, task, offline, execution_seconds):
    configure()
    import docker
    import yaml
    from baseline_runtime import MemoryBroker, SnapshotDockerEnvironment, restore_volume, verify_workspace_manifest
    from baseline_evidence import StrongSharePublisher
    from luna_model import make_model
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
    from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
    from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow
    from cross_task_packet import build_packet, collect_scratch_files, collect_source_changes, packet_message

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
              "continuation_budget_seconds": execution_seconds,
              "initialization_included_in_budget": True, "money_limit": None, "request_limit": None}
    result.update(condition_version=CONDITION_VERSION, active_agents=list(ACTIVE),
                  authoritative_agent=AUTHORITATIVE_AGENT, peer_retired=False)
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
                "state_use": "resumed_model_loop",
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
        guard._persist()
        active = ACTIVE
        for agent_id in active:
            model = make_offline_model(task, agent_id) if offline else make_model(MODEL, guard, condition, agent_id, phase_dir)
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
        assignment = apply_integrator_assignment(agents, checkpoint, task,
                remaining_seconds=max(0.0, started + execution_seconds - time.monotonic()))
        write_json(phase_dir / "integrator_assignment.json", assignment)
        delivery = {"condition": condition, "checkpoint_sha256": digest(checkpoint),
                    "packet_sha256": digest(packet), "source_checkpoint_verified": True,
                    "code_automatically_transferred": False, "recipients": {}, "first_request_verified": False}
        for agent_id, agent in agents.items():
            delivery["recipients"][agent_id] = inject_integrator_packet(agent, packet, agent_id)
            install_first_query_check(agent.model, packet_text, assignment["recipients"][agent_id],
                                      checkpoint["agents"][agent_id]["state"]["messages"], agent_id, phase_dir)
            agent.save(agent.config.output_path)
        write_json(phase_dir / "handoff_delivery.json", delivery)
        execution_start = time.monotonic()
        result["handoff_seconds"] = round(execution_start - started, 3)
        deadline = started + execution_seconds
        if time.monotonic() >= deadline:
            result.update(status="time_budget_exhausted_before_first_query", infrastructure_error=False,
                          continuation_model_requests=0, elapsed_seconds=round(time.monotonic() - started, 3),
                          continuation_seconds=0.0)
            raise ContinuationBudgetExhausted()
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
        unqueried = unqueried_budget_agents(phase_dir, active, outcomes)
        publications = {}
        for agent_id in active:
            name = f"{agent_id}.patch"
            patch = connectors[agent_id].submitted_patch(envs[agent_id]) or ""
            (phase_dir / name).write_text(patch)
            publications[agent_id] = publication_provenance(envs[agent_id], agent_id, patch)
            diagnostic = envs[agent_id].execute({"command": "git diff --binary " + shlex.quote(task.base)})
            (phase_dir / f"{agent_id}_unsubmitted_diagnostic.patch").write_text(diagnostic.get("output", ""))
            if agent_id in unqueried:
                continue
            first = json.loads((phase_dir / f"first_request_{agent_id}.json").read_text())
            if first.get("message_sha256") != delivery["recipients"][agent_id]["message_sha256"]:
                raise RuntimeError("Delivered packet differs from first model query")
            if not offline:
                inputs = sorted((phase_dir / "responses" / agent_id).glob("request_*_input.json"))
                if not inputs:
                    raise RuntimeError("Missing actual first provider input")
                actual = json.loads(inputs[0].read_text())["request"]["messages"]
                verify_first_messages(actual, packet_text, assignment["recipients"][agent_id],
                                      checkpoint["agents"][agent_id]["state"]["messages"])
                first["actual_transport_input_verified"] = True
                write_json(phase_dir / f"first_request_{agent_id}.json", first)
        write_json(phase_dir / "final_delivery.json", final_delivery_record(phase_dir, publications))
        delivery["first_request_verified"] = not unqueried
        write_json(phase_dir / "handoff_delivery.json", delivery)
        if any(o["infrastructure_error"] for o in outcomes.values()):
            raise RuntimeError("Agent infrastructure failed; incomplete attempt retained")
        if offline and not unqueried:
            observations = [m for m in agents["agent1"].messages if m.get("role") == "tool"]
            if (any(outcomes[a]["status"] != "Submitted" for a in ACTIVE)
                    or not any("offline_integrator_peer_incorporated" in str(m)
                               and m.get("extra", {}).get("returncode") == 0 for m in observations)):
                raise RuntimeError("Offline agents did not publish and perform model-driven peer integration")
        if unqueried:
            result.update(status="budget_exhausted", outcomes=outcomes, infrastructure_error=False,
                          unqueried_agents=unqueried, elapsed_seconds=round(time.monotonic() - started, 3),
                          continuation_seconds=round(time.monotonic() - execution_start, 3))
            raise ContinuationBudgetExhausted()
        result.update(status="completed", outcomes=outcomes, infrastructure_error=False,
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      continuation_seconds=round(time.monotonic() - execution_start, 3))
        result.update(final_patch_sha256=hashlib.sha256((phase_dir / "agent1.patch").read_bytes()).hexdigest(),
                      peer_patch_sha256=hashlib.sha256((phase_dir / "agent2.patch").read_bytes()).hexdigest(),
                      delivery_selection="predesignated_agent1")
        exit_code = 0
    except ContinuationBudgetExhausted:
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
            if result.get("status") not in ("completed", "time_budget_exhausted_before_first_query", "budget_exhausted"):
                result["status"] = "error"
            exit_code = 2
        write_json(phase_dir / "usage_summary.json", usage(run_dir, condition))
        write_json(phase_dir / "status.json", result)
    return exit_code


def _read_regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Required regular artifact is missing or is a symlink: " + str(path))
    return path.read_bytes()


def _json(path):
    return json.loads(_read_regular(path))


def _validated_receipt(phase_dir, task):
    """Validate facts before hashing; only filesystem reads, never Docker/API."""
    from cross_task_packet import packet_message
    from cross_task_specs import get_task
    phase_dir = Path(phase_dir)
    run = phase_dir.parent
    if (phase_dir.name != "integrator" or phase_dir.is_symlink()
            or task.key not in TASK_KEYS or task.as_dict() != get_task(task.key).as_dict()):
        raise RuntimeError("Not an authorized integrator phase")
    config = _json(run / "task_config.json")
    status = _json(phase_dir / "status.json")
    for key, value in {"task": task.as_dict(), "condition": "integrator",
                       "condition_version": CONDITION_VERSION}.items():
        if config.get(key) != value or status.get(key) != value:
            raise RuntimeError("Integrator config/status mismatch: " + key)
    if (status.get("status") != "completed" or status.get("phase") != "integrator"
            or status.get("handoff_variant") != VARIANT
            or status.get("active_agents") != list(ACTIVE)
            or status.get("authoritative_agent") != AUTHORITATIVE_AGENT
            or status.get("peer_retired") is not False
            or status.get("initialization_included_in_budget") is not True
            or status.get("continuation_budget_seconds") != config.get("execution_seconds")
            or type(config.get("offline_selftest")) is not bool
            or status.get("offline_selftest") != config["offline_selftest"]
            or config.get("model") != ("offline-scripted-model" if config.get("offline_selftest") else MODEL)):
        raise RuntimeError("Incomplete or inconsistent integrator execution")
    outcomes = _json(phase_dir / "agent_outcomes.json")
    if (set(outcomes) != set(ACTIVE) or outcomes != status.get("outcomes")
            or any(o.get("infrastructure_error") for o in outcomes.values())):
        raise RuntimeError("Both live agent outcomes must be recorded without an execution failure")
    checkpoint = _json(run / "checkpoint.json")
    validate_checkpoint(checkpoint, task)
    cp_hash = digest(checkpoint)
    if (config.get("checkpoint_sha256") != cp_hash
            or _sha(_read_regular(run / "shared_git.tar")) != checkpoint["git_archive_sha256"]
            or _json(run / "checkpoint_provenance.json").get("checkpoint_sha256") != cp_hash):
        raise RuntimeError("Checkpoint provenance or shared Git archive changed")
    restore = _json(phase_dir / "restore_proof.json")
    if (restore.get("checkpoint_hash") != cp_hash
            or restore.get("broker_hash") != digest(checkpoint["broker"])
            or set(restore.get("agents", {})) != set(ACTIVE)):
        raise RuntimeError("Restore proof differs from the original broker/checkpoint")
    packet = _json(phase_dir / "handoff_packet.json")
    packet_hash = digest(packet)
    if (packet.get("schema_version") != 2 or packet.get("checkpoint_sha256") != cp_hash
            or packet.get("task_key") != task.key or packet.get("base_commit") != task.base
            or packet.get("checkpoint_only") is not True or set(packet.get("agents", {})) != set(ACTIVE)
            or packet_hash != status.get("handoff_packet_sha256")):
        raise RuntimeError("Packet no longer matches the common checkpoint")
    text = packet_message(packet)
    if _read_regular(phase_dir / "handoff_packet.txt").decode() != text:
        raise RuntimeError("Packet text changed")
    delivery = _json(phase_dir / "handoff_delivery.json")
    if (delivery.get("condition") != "integrator" or delivery.get("checkpoint_sha256") != cp_hash
            or delivery.get("packet_sha256") != packet_hash
            or delivery.get("source_checkpoint_verified") is not True
            or delivery.get("code_automatically_transferred") is not False
            or delivery.get("first_request_verified") is not True
            or set(delivery.get("recipients", {})) != set(ACTIVE)):
        raise RuntimeError("Both agents must receive the same complete packet")
    assignment = _json(phase_dir / "integrator_assignment.json")
    if (assignment.get("schema") != "designated_integrator_assignment_v1"
            or assignment.get("condition_version") != CONDITION_VERSION
            or assignment.get("checkpoint_sha256") != cp_hash
            or assignment.get("active_agents") != list(ACTIVE)
            or assignment.get("authoritative_agent") != AUTHORITATIVE_AGENT
            or assignment.get("peer_retired") is not False
            or assignment.get("automatic_merge") is not False
            or assignment.get("posthoc_selection") is not False
            or set(assignment.get("recipients", {})) != set(ACTIVE)):
        raise RuntimeError("Final integrator was not fixed for both original agents")
    for agent_id in ACTIVE:
        saved = checkpoint["agents"][agent_id]
        actual = restore["agents"][agent_id]
        if (actual.get("state_hash") != digest(saved["state"])
                or actual.get("filesystem_manifest_hash") != digest(saved["manifest"])
                or actual.get("source_image") != saved["image"]
                or actual.get("filesystem_verified") is not True or actual.get("state_verified") is not True
                or actual.get("state_use") != "resumed_model_loop"):
            raise RuntimeError("An agent is not a verified resumed live loop")
        role = assignment["recipients"][agent_id]
        expected_content = organization_message(task, agent_id, assignment["remaining_seconds_at_assignment"])
        expected_state = copy.deepcopy(saved["state"])
        expected_state["messages"].append({"role": "user", "content": expected_content})
        if (role.get("role") != "user" or role.get("content") != expected_content
                or role.get("message_index") != len(saved["state"]["messages"])
                or role.get("history_preserved") is not True
                or role.get("before_state_sha256") != digest(saved["state"])
                or role.get("after_state_sha256") != digest(expected_state)):
            raise RuntimeError("Organization update or inherited state changed")
        recipient = delivery["recipients"][agent_id]
        first = _json(phase_dir / f"first_request_{agent_id}.json")
        if (recipient.get("condition") != "integrator" or recipient.get("agent_id") != agent_id
                or recipient.get("message_index") != role["message_index"] + 1
                or recipient.get("packet_sha256") != packet_hash
                or recipient.get("message_sha256") != _sha(text.encode())
                or recipient.get("history_preserved") is not True
                or recipient.get("all_packet_content_delivered") is not True
                or recipient.get("role") != "user" or first.get("verified") is not True
                or first.get("recipient") != agent_id or first.get("full_packet_present") is not True
                or first.get("message_sha256") != _sha(text.encode())
                or first.get("organization_message_sha256") != role.get("message_sha256")
                or first.get("organization_update_present") is not True
                or first.get("inherited_history_verified") is not True):
            raise RuntimeError("Incomplete first-query role or packet proof")
        model_input = _json(phase_dir / f"first_model_input_{agent_id}.json")
        trajectory = _json(phase_dir / f"{agent_id}.traj.json")["messages"]
        count = model_input.get("message_count")
        if (model_input.get("recipient") != agent_id or model_input.get("contains_raw_messages") is not False
                or type(count) is not int or count < len(saved["state"]["messages"]) + 2
                or count >= len(trajectory) or trajectory[count].get("role") != "assistant"):
            raise RuntimeError("First model-input fingerprint lacks a complete trajectory prefix")
        first_messages = _provider_messages(trajectory[:count])
        if digest(first_messages) != model_input.get("messages_sha256"):
            raise RuntimeError("First model-input fingerprint differs from trajectory")
        verify_first_messages(first_messages, text, role, saved["state"]["messages"])
        if not config["offline_selftest"]:
            inputs = sorted((phase_dir / "responses" / agent_id).glob("request_*_input.json"))
            if not inputs or first.get("actual_transport_input_verified") is not True:
                raise RuntimeError("Actual provider input was not verified")
            actual_messages = _json(inputs[0])["request"]["messages"]
            verify_first_messages(actual_messages, text, role, saved["state"]["messages"])
            if digest(_provider_messages(actual_messages)) != model_input["messages_sha256"]:
                raise RuntimeError("Actual provider input differs from checked model input")
        n = len(saved["state"]["messages"])
        if trajectory[:n] != saved["state"]["messages"]:
            raise RuntimeError("Final trajectory lost or changed original history")
        if trajectory[n].get("content") != expected_content or trajectory[n + 1].get("content") != text:
            raise RuntimeError("Final trajectory lost role or evidence handoff")
    final = _json(phase_dir / "final_delivery.json")
    if final != final_delivery_record(phase_dir, final.get("publications", {})):
        raise RuntimeError("Authoritative delivery or publication provenance changed")
    if (status.get("delivery_selection") != "predesignated_agent1"
            or status.get("final_patch_sha256") != final["patch_sha256"]
            or status.get("peer_patch_sha256") != final["peer_patch_sha256"]):
        raise RuntimeError("Completed status changed its predesignated delivery")
    for a, pub in final["publications"].items():
        patch = _read_regular(phase_dir / f"{a}.patch").decode()
        expected_strategy = ("published_pr" if pub.get("pr_tag_sha") else
                             "published_branch_fallback" if patch.strip() else "no_published_changes")
        if (pub.get("agent_id") != a or pub.get("strategy") != expected_strategy
                or pub.get("published_pr") != bool(pub.get("pr_tag_sha"))
                or pub.get("patch_nonempty") != bool(patch.strip())
                or pub.get("unsubmitted_worktree_used") is not False
                or (patch.strip() and not pub.get("branch_sha"))):
            raise RuntimeError("Invalid native publication provenance")
    sources = _json(phase_dir / "source_manifest.json")
    if not sources:
        raise RuntimeError("Missing frozen source manifest")
    for name, expected in sources.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or _sha(_read_regular(ROOT / path)) != expected:
            raise RuntimeError("Frozen source changed")
    ledger = _json(run / "usage_ledger.json")
    accepted = [r for r in ledger["requests"] if not r.get("excluded_from_baseline")]
    if ledger.get("model") != MODEL or any(r.get("phase") not in {"integrator:" + a for a in ACTIVE} for r in accepted):
        raise RuntimeError("Ledger contains the wrong model or active phase")
    if config["offline_selftest"] and ledger["requests"]:
        raise RuntimeError("Offline integrator made model requests")
    if not config["offline_selftest"] and any(not any(r["phase"] == "integrator:" + a for r in accepted) for a in ACTIVE):
        raise RuntimeError("An active agent has no recorded provider request")
    if _json(phase_dir / "usage_summary.json") != usage(run, "integrator"):
        raise RuntimeError("Usage summary differs from ledger")
    paths = [run / name for name in ("task_config.json", "checkpoint.json", "shared_git.tar",
                                    "checkpoint_provenance.json", "usage_ledger.json")]
    paths += [phase_dir / name for name in ("status.json", "source_manifest.json", "restore_proof.json",
        "handoff_packet.json", "handoff_packet.txt", "handoff_delivery.json", "integrator_assignment.json",
        "final_delivery.json", "agent_outcomes.json", "messages.json", "usage_summary.json")]
    for a in ACTIVE:
        paths += [phase_dir / f"{a}.patch", phase_dir / f"{a}.traj.json",
                  phase_dir / f"first_request_{a}.json", phase_dir / f"first_model_input_{a}.json",
                  phase_dir / "evidence_manifests" / f"{a}_checkpoint.json"]
    for folder in ("responses", "evidence_manifests"):
        paths += [p for p in (phase_dir / folder).rglob("*") if p.is_file() or p.is_symlink()]
    hashes = {str(p.relative_to(run)): _sha(_read_regular(p)) for p in sorted(set(paths))}
    return {"schema": "designated_integrator_execution_v1", "condition": "integrator",
            "condition_version": CONDITION_VERSION, "active_agents": list(ACTIVE),
            "authoritative_agent": AUTHORITATIVE_AGENT, "checkpoint_sha256": cp_hash,
            "handoff_packet_sha256": packet_hash, "selection": "predesignated_agent1",
            "patch_sha256": {"agent1.patch": final["patch_sha256"], "agent2.patch": final["peer_patch_sha256"]},
            "protected_files_sha256": hashes}


def freeze_execution_receipt(phase_dir, task):
    """First completed run: validate then write once; changed facts never overwrite."""
    receipt = _validated_receipt(phase_dir, task)
    path = Path(phase_dir) / "execution_receipt.json"
    if path.exists() or path.is_symlink():
        if _json(path) != receipt:
            raise RuntimeError("Existing integrator execution receipt differs")
    else:
        # Exclusive creation also prevents an accidental overwrite by a second
        # verifier. The supervisor serializes its worker/evaluator processes.
        with path.open("x") as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def verify_worker_artifacts(phase_dir, task):
    """Strictly read-only verification of a previously frozen completed run."""
    stored = _json(Path(phase_dir) / "execution_receipt.json")
    current = _validated_receipt(phase_dir, task)
    if stored != current:
        raise RuntimeError("Protected integrator execution artifacts changed")
    return stored
