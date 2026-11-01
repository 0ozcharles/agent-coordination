"""Continue the original two-agent checkpoint with agent1 responsible for both features.

The peer's checkpoint evidence is frozen, no peer model is constructed, and one
published patch is evaluated against both original official feature suites.
"""
import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from budget_guard import BudgetGuard, write_json
from run_baselines import (
    AGENTS, CONTINUATION_SECONDS, cleanup_phase, close_interrupted_tool_calls,
    digest, load_state, safe_error, save_state, usage,
)
from run_solo_smoke import BASE, IMAGE, MAX_OUTPUT, MODEL, REPO, ROOT, configure

SOURCE_RUN = ROOT / "artifacts/baseline_runs/baseline_smoke_20261029T224045Z"
SOURCE_CHECKPOINT = "b9030a4cb811d353b1252b1c7e481da3cfc0498dcbfcd338e3d07b491a226aab"
ACTIVE, RETIRED, PHASE = "agent1", "agent2", "consolidate"
COMMIT = "63b9d44d9f39a02fccf5bf0052db48a917a011fd"
SOURCE_FILES = (
    "run_consolidate.py", "evaluate_consolidate_smoke.py", "run_baselines.py",
    "run_solo_smoke.py", "baseline_runtime.py", "baseline_model.py",
    "baseline_evidence.py", "budget_guard.py", "evaluate_solo_smoke.py",
)


def source_manifest():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def verify_preflight():
    path = ROOT / "artifacts/consolidate_offline_validation.json"
    proof = json.loads(path.read_text()) if path.exists() else {}
    if not proof.get("passed") or proof.get("source_manifest") != source_manifest():
        raise RuntimeError("Run the complete offline Consolidate validation for these source versions first")


def prepare_checkpoint(run_dir):
    """Copy only checkpoint inputs; no outcomes or final patches enter the branch."""
    original = json.loads((SOURCE_RUN / "checkpoint.json").read_text())
    if digest(original) != SOURCE_CHECKPOINT:
        raise RuntimeError("The original common checkpoint has changed")
    if hashlib.sha256((SOURCE_RUN / "shared_git.tar").read_bytes()).hexdigest() != original["git_archive_sha256"]:
        raise RuntimeError("The original shared Git archive has changed")
    provenance = {
        "source_run": str(SOURCE_RUN), "checkpoint_sha256": digest(original),
        "files": {}, "prefix_usage": usage(SOURCE_RUN, "prefix"),
        "prefix_elapsed_seconds": json.loads((SOURCE_RUN / "prefix/status.json").read_text())["elapsed_seconds"],
        "new_prefix_model_calls": 0,
    }
    for name in ("checkpoint.json", "shared_git.tar"):
        data = (SOURCE_RUN / name).read_bytes()
        destination = run_dir / name
        if destination.exists() and destination.read_bytes() != data:
            raise RuntimeError("Saved branch input changed: " + name)
        if not destination.exists():
            destination.write_bytes(data)
        provenance["files"][name] = hashlib.sha256(data).hexdigest()
    path = run_dir / "checkpoint_provenance.json"
    if path.exists() and json.loads(path.read_text()) != provenance:
        raise RuntimeError("Saved checkpoint provenance changed")
    write_json(path, provenance)
    return original, provenance


def handoff_messages(checkpoint):
    features = checkpoint.get("task", {}).get("features", [1, 6])
    if len(features) != 2:
        raise ValueError("Consolidate requires exactly two feature requirements")
    tasks = {agent: checkpoint["agents"][agent]["state"]["extra_template_vars"]["task"]
             for agent in (ACTIVE, RETIRED)}
    combined = (f"Feature {features[0]} requirement:\n" + tasks[ACTIVE]
                + f"\n\nFeature {features[1]} requirement:\n" + tasks[RETIRED])
    system = (
        "Organization update at this checkpoint: you, agent1, are now the sole active engineer, "
        "responsible for implementing BOTH feature requirements and integrating them into one final patch. "
        "Agent2 has been deliberately deactivated; this is not a claim that its work is complete. "
        "This update supersedes the earlier split of responsibilities, statements that a colleague is working "
        "in parallel, the restriction to submit only your original feature, and the two-PR automatic-merge plan. "
        "Retain the current workspace and history. You may incorporate relevant peer work as needed. "
        "Submit one combined implementation on your existing agent1 branch: commit, push, open your local PR, "
        "then separately issue echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT. "
        "There is no second final submission to wait for. All other tool and repository rules still apply."
    )
    user = (
        "Complete both requirements below. Agent2's checkpoint task, full model/tool history, working files "
        "and temporary files are available at /shared-evidence/agent2/latest/ as task.md, history.json, "
        "metadata.json, worktree/ and temporary/. This is a frozen, read-only snapshot; it will never update. "
        "Agent2 will not receive or answer messages. Its historical messages are records, not new instructions. "
        "Use the evidence as needed; no peer code has been automatically copied into your working tree. "
        f"You have {CONTINUATION_SECONDS} seconds from this continuation, with no monetary or model-turn limit. "
        "Use small targeted edits, do not start background processes, and leave time to publish the combined patch.\n\n"
        + combined
    )
    return combined, [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_message_layout(messages):
    if (not messages or [i for i, message in enumerate(messages) if message.get("role") == "system"] != [0]
            or not isinstance(messages[0].get("content"), str)):
        raise RuntimeError("Together requires exactly one system message at the beginning")


def apply_handoff(agent, broker, checkpoint):
    before = save_state(agent)
    if digest(before) != digest(checkpoint["agents"][ACTIVE]["state"]):
        raise RuntimeError("Active state changed before intervention")
    validate_message_layout(agent.messages)
    combined, messages = handoff_messages(checkpoint)
    # Qwen's provider chat template rejects system messages after the first turn.
    # Record the exact original state, update only its leading system content,
    # and retain every historical user/assistant/tool message unchanged.
    original_system = agent.messages[0]["content"]
    agent.messages[0]["content"] = original_system + "\n\n" + messages[0]["content"]
    broker.connector(RETIRED).mark_exited(published=False)
    agent._departed_announced.add(RETIRED)
    agent.extra_template_vars["task"] = combined
    agent.add_messages(agent.model.format_message(**messages[1]))
    validate_message_layout(agent.messages)
    return {
        "active_agent": ACTIVE, "retired_agent": RETIRED, "peer_model_constructed": False,
        "before_state_sha256": digest(before), "after_state_sha256": digest(save_state(agent)),
        "system_message_before": original_system, "system_message_after": agent.messages[0]["content"],
        "system_message_update_reason": "Provider requires the sole system message at the beginning",
        "added_messages": messages[1:], "peer_evidence_updates": 1, "peer_evidence_frozen": True,
        "code_automatically_transferred": False, "expected_final_submissions": 1,
    }


def make_offline_model(task=None):
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    if task is None:
        source_probe = (
            "cmp src/jinja2/loaders.py /shared-evidence/agent2/latest/worktree/src/jinja2/loaders.py"
        )
    else:
        # WIP branches need not contain identical code. Verify the retired
        # peer's source is readable without comparing it to the active tree.
        source_script = (
            "from pathlib import Path; "
            "base = Path('/shared-evidence/agent2/latest/worktree'); "
            f"roots = [base / name for name in {list(task.code_roots)!r}]; "
            "files = (p for root in roots for p in ([root] if root.is_file() else root.rglob('*')) "
            "if p.is_file()); "
            "source = next(files, None); "
            "assert source is not None, 'No peer source file in declared code roots'; "
            "source.open('rb').read(1)"
        )
        source_probe = "python -c " + shlex.quote(source_script)
    commands = [
        "test -s /shared-evidence/agent2/latest/task.md && "
        "test -s /shared-evidence/agent2/latest/history.json && "
        + source_probe + " && "
        "test ! -e /patches && "
        "if touch /shared-evidence/write-probe; then exit 88; else echo frozen_evidence_read_only; fi",
        "printf 'synthetic infrastructure check, not a feature solution\\n' > consolidate_offline_probe.txt && "
        "git add -A && git commit -m 'Offline handoff plumbing test' && git push",
        "gh pr create --title 'Offline handoff test' --body 'Synthetic patch for infrastructure verification'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]

    class OfflineModel(LitellmModel):
        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            validate_message_layout(messages)
            if self.index >= len(commands):
                raise RuntimeError("Offline test requested an unexpected additional model turn")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-consolidate-{self.index}", "object": "chat.completion", "created": 0,
                "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline infrastructure check.", "tool_calls": [{
                    "id": f"call_consolidate_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def phase_worker(run_dir, offline=False, task=None):
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

    phase_dir = run_dir / PHASE
    phase_dir.mkdir(parents=True, exist_ok=True)
    if task is None:
        checkpoint, _ = prepare_checkpoint(run_dir)
        agents, image, base = AGENTS, IMAGE, BASE
        dataset_path = "dataset/pallets_jinja_task/task1621"
        image_record_path = ROOT / "artifacts/task_image.json"
    else:
        checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
        from wip_checkpoint import validate_checkpoint
        validate_checkpoint(checkpoint, task)
        agents, image, base = task.agents, task.image, task.base
        dataset_path = str(task.task_dir.relative_to(REPO))
        image_record_path = task.image_record_path
        if set(agents) != {ACTIVE, RETIRED} or set(checkpoint["agents"]) != set(agents):
            raise RuntimeError("Checkpoint agent identities do not match the requested task")
        if any(saved["git_base"] != base for saved in checkpoint["agents"].values()):
            raise RuntimeError("Checkpoint Git base does not match the requested task")
    if hashlib.sha256((run_dir / "shared_git.tar").read_bytes()).hexdigest() != checkpoint["git_archive_sha256"]:
        raise RuntimeError("Shared Git archive differs from the checkpoint")
    expected = json.loads(image_record_path.read_text())
    dc = docker.from_env(timeout=120)
    label = run_dir.name + "-" + PHASE
    volume = "rsi-" + label.lower().replace("_", "-")
    evidence_volume = volume + "-evidence"
    envs, agent, publisher, model = {}, None, None, None
    started = time.monotonic()
    result = {"phase": PHASE, "status": "initializing", "offline_selftest": offline,
              "active_agent": ACTIVE, "retired_agent_model_calls": 0}
    if task is not None:
        result["task"] = task.as_dict()
    write_json(phase_dir / "status.json", result)
    try:
        if dc.images.get(image).id != expected["Id"]:
            raise RuntimeError("Task image differs from the verified image")
        if subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
            raise RuntimeError("Pinned CooperBench commit changed")
        subprocess.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                        "src/cooperbench/agents/mini_swe_agent_v2", dataset_path],
                       check=True, capture_output=True)
        cfg = yaml.safe_load(get_config_path("coop").read_text())
        restore_volume(run_dir / "shared_git.tar", volume, expected["Id"], label,
                       expected_sha256=checkpoint["git_archive_sha256"])
        dc.volumes.create(name=evidence_volume, labels={"agent-rsi-run": label})
        publisher = StrongSharePublisher(dc, expected["Id"], evidence_volume, label)
        proof = {"checkpoint_hash": digest(checkpoint), "agents": {}}
        for agent_id in agents:
            saved = checkpoint["agents"][agent_id]
            env = SnapshotDockerEnvironment(image=saved["image"], run_label=label, agent_id=agent_id,
                    shared_git_volume=volume, evidence_volume=evidence_volume, container_timeout="30m")
            envs[agent_id] = env
            verify_workspace_manifest(env, saved["manifest"])
            proof["agents"][agent_id] = {
                "state_hash": digest(saved["state"]), "filesystem_manifest_hash": digest(saved["manifest"]),
                "source_image": saved["image"], "filesystem_verified": True,
                "state_use": "resumed_model_loop" if agent_id == ACTIVE else "frozen_evidence_only",
            }
        broker = MemoryBroker.from_snapshot(checkpoint["broker"])
        if digest(broker.snapshot()) != digest(checkpoint["broker"]):
            raise RuntimeError("Restored mailbox differs from checkpoint")
        guard = BudgetGuard(run_dir / "usage_ledger.json", run_dir.name, model=MODEL,
                            enforce_limits=False, max_output_tokens=MAX_OUTPUT)
        model = make_offline_model(task) if offline else make_model(MODEL, guard, PHASE, ACTIVE, phase_dir)
        model.config = model.config.model_copy(update={
            key: cfg["model"][key] for key in ("observation_template", "format_error_template")})
        agent = DefaultAgent(model, envs[ACTIVE], comm=broker.connector(ACTIVE), agent_id=ACTIVE,
                **{**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False,
                   "output_path": phase_dir / "agent1.traj.json"})
        load_state(agent, checkpoint["agents"][ACTIVE]["state"])
        if digest(save_state(agent)) != digest(checkpoint["agents"][ACTIVE]["state"]):
            raise RuntimeError("Restored active history/state differs from checkpoint")
        if digest(broker.snapshot()) != digest(checkpoint["broker"]):
            raise RuntimeError("Agent initialization changed the restored mailbox")
        proof["agents"][ACTIVE]["state_verified"] = True
        proof["broker_hash"] = digest(broker.snapshot())
        write_json(phase_dir / "restore_proof.json", proof)
        peer = checkpoint["agents"][RETIRED]["state"]
        metadata = publisher.publish(RETIRED, envs[RETIRED].container_id, "/workspace/repo",
                                     peer["messages"], peer["extra_template_vars"]["task"], "checkpoint")
        write_json(phase_dir / "frozen_evidence_manifest.json", metadata)
        intervention = apply_handoff(agent, broker, checkpoint)
        envs.pop(RETIRED).cleanup()
        # Remove the only evidence writer before execution; the active mount is read-only.
        publisher.cleanup()
        publisher = None
        intervention["handoff_elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(phase_dir / "intervention.json", intervention)
        git = GitConnector(agent_id=ACTIVE, agents=list(agents), server_url="/shared-git/team.git")
        git._base_sha, git._initialized = checkpoint["agents"][ACTIVE]["git_base"], True
        if offline and (agent.comm.send(RETIRED, "must not deliver") or
                        agent.comm.send_and_wait(RETIRED, "must not wait", timeout=60) != (False, [])):
            raise RuntimeError("Deactivated peer is still accepting messages")
        execution_start = time.monotonic()
        deadline = execution_start + CONTINUATION_SECONDS
        reminded, turns, status = False, 0, "deadline"
        while time.monotonic() < deadline:
            if deadline - time.monotonic() <= 120 and not reminded:
                reminded = True
                agent.add_messages(model.format_message(role="user", content=(
                    "About two minutes remain. Publish your combined implementation, even if incomplete; "
                    "do not claim a failing test passed. Then issue the exact completion command.")))
            try:
                validate_message_layout(agent.messages)
                agent.step()
            except InterruptAgentFlow as exc:
                if any(message.get("role") == "exit" for message in exc.messages):
                    close_interrupted_tool_calls(agent)
                agent.add_messages(*exc.messages)
            turns += 1
            agent.save(agent.config.output_path)
            if offline and turns == 1:
                observations = [message for message in agent.messages if message.get("role") == "tool"]
                if (not observations or observations[-1].get("extra", {}).get("returncode") != 0 or
                        "frozen_evidence_read_only" not in str(observations[-1])):
                    raise RuntimeError("Frozen evidence visibility/read-only probe failed")
            if agent.messages[-1].get("role") == "exit":
                if agent._nudge_unsubmitted():
                    agent.messages = [message for message in agent.messages if message.get("role") != "exit"]
                    continue
                status = agent.messages[-1].get("extra", {}).get("exit_status", "submitted")
                break
        patch = git.submitted_patch(envs[ACTIVE]) or ""
        (phase_dir / "final.patch").write_text(patch)
        diagnostic = envs[ACTIVE].execute({"command": "git diff --binary " + shlex.quote(base)})
        (phase_dir / "unsubmitted_diagnostic.patch").write_text(diagnostic.get("output", ""))
        result.update(status="completed", outcome=status, phase_turns=turns, model_turns=agent.n_calls,
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      continuation_seconds=round(time.monotonic() - execution_start, 3),
                      handoff_seconds=intervention["handoff_elapsed_seconds"],
                      final_patch_sha256=hashlib.sha256((phase_dir / "final.patch").read_bytes()).hexdigest(),
                      infrastructure_error=False)
        return 0
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__, error=safe_error(exc),
                      elapsed_seconds=round(time.monotonic() - started, 3), infrastructure_error=True)
        return 2
    finally:
        write_json(phase_dir / "status.json", result)
        write_json(phase_dir / "usage_summary.json", usage(run_dir, PHASE))
        if agent:
            agent.save(agent.config.output_path)
            try:
                published = agent._opened_pr()
            except Exception:
                published = False
            agent.comm.mark_exited(published=published)
            write_json(phase_dir / "messages.json", broker.snapshot())
        if model and getattr(model, "close", None):
            model.close()
        for env in envs.values():
            env.cleanup()
        if publisher:
            publisher.cleanup()
        for name in (evidence_volume, volume):
            try:
                dc.volumes.get(name).remove()
            except Exception:
                pass
        dc.close()


def archive_failed_phase(run_dir):
    phase_dir = run_dir / PHASE
    if not phase_dir.exists():
        return
    archive = run_dir / "failed_attempts"
    archive.mkdir(exist_ok=True)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.move(str(phase_dir), str(archive / (PHASE + "_" + suffix)))
    path = run_dir / "usage_ledger.json"
    if path.exists():
        ledger = json.loads(path.read_text())
        for row in ledger["requests"]:
            if row["phase"].startswith(PHASE + ":") and not row.get("excluded_from_baseline"):
                row.update(excluded_from_baseline=True, excluded_reason="incomplete infrastructure attempt",
                           archived_attempt=suffix)
                if row.get("status") == "in_flight":
                    row.update(status="error", error_type="InterruptedAttemptUsageUnknown")
        write_json(path, ledger)


def make_summary(run_dir, provenance, offline):
    continuation = usage(run_dir, PHASE)
    prefix = provenance["prefix_usage"]
    execution = json.loads((run_dir / PHASE / "status.json").read_text())
    return {
        "pipeline_completed": True, "offline_selftest": offline, "condition": PHASE,
        "checkpoint_sha256": provenance["checkpoint_sha256"], "source_run": str(SOURCE_RUN),
        "prefix_usage_reused_not_rebilled": prefix, "continuation_usage": continuation,
        "full_task_known_cost_usd": round(prefix["known_cost_usd"] + continuation["known_cost_usd"], 9),
        "full_task_unknown_usage_requests": prefix["unknown_usage_requests"] + continuation["unknown_usage_requests"],
        "full_task_execution_seconds": round(provenance["prefix_elapsed_seconds"] + execution["elapsed_seconds"], 3),
        "new_actual_collection_usage": usage(run_dir),
        "excluded_infrastructure_attempt_usage": usage(run_dir, excluded_only=True),
        "execution": execution,
        "evaluation": json.loads((run_dir / PHASE / "evaluation_summary.json").read_text()),
        "scope": "One early checkpoint with no implementation edits; task handoff smoke, not failure recovery or a performance study",
    }


def validate_offline_completion(run_dir, summary):
    execution = summary["execution"]
    patch = (run_dir / PHASE / "final.patch").read_text()
    if (execution.get("outcome") != "Submitted" or execution.get("phase_turns") != 4 or
            "diff --git a/consolidate_offline_probe.txt b/consolidate_offline_probe.txt" not in patch or
            summary["new_actual_collection_usage"]["requests"] != 0 or
            not summary["evaluation"].get("evaluation_completed")):
        raise RuntimeError("Offline validation did not complete the expected synthetic publication and scoring")


def supervise(run_dir, offline=False):
    configure()
    run_dir.mkdir(parents=True, exist_ok=True)
    print(run_dir, flush=True)
    with (ROOT / "artifacts/model_run.lock").open("a") as lock:
        return supervise_locked(run_dir, offline, lock)


def supervise_locked(run_dir, offline, lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another model run owns the experiment lock.", flush=True)
        return 2
    if not offline:
        verify_preflight()
    checkpoint, provenance = prepare_checkpoint(run_dir)
    config = {
        "condition": PHASE, "model": "offline-scripted-model" if offline else MODEL,
        "offline_selftest": offline, "checkpoint_sha256": digest(checkpoint), "active_agent": ACTIVE,
        "features": [1, 6], "active_model_loops": 1, "retired_agent_model_calls": 0,
        "continuation_deadline_seconds": CONTINUATION_SECONDS, "money_limit": None, "request_limit": None,
        "max_output_tokens": MAX_OUTPUT, "compaction_enabled": False, "temperature": 0.7, "top_p": 0.8,
        "enable_thinking": False, "random_seed": None, "single_patch_scoring": "official test_solo",
        "peer_evidence": "checkpoint only, frozen, read-only; peer loop deactivated",
    }
    config_path = run_dir / "run_config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise RuntimeError("Existing run configuration differs; use a new run directory")
    write_json(config_path, config)
    phase_dir = run_dir / PHASE
    status_path = phase_dir / "status.json"
    existing = json.loads(status_path.read_text()) if status_path.exists() else {}
    if existing.get("status") != "completed":
        if not offline and not os.environ.get("TOGETHER_API_KEY", "").strip():
            print("TOGETHER_API_KEY is absent. Use run_consolidate_with_together_key.sh.", flush=True)
            return 2
        archive_failed_phase(run_dir)
        phase_dir.mkdir()
        write_json(phase_dir / "source_manifest.json", source_manifest())
        command = [sys.executable, "-u", str(Path(__file__).resolve()), "--run-dir", str(run_dir), "--worker"]
        if offline:
            command.append("--offline-selftest")
        print("Starting Consolidate from the original common checkpoint", flush=True)
        with (phase_dir / "console.log").open("wb") as log:
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    pass_fds=(lock.fileno(),))
            try:
                code = proc.wait(timeout=CONTINUATION_SECONDS + 240)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=15)
                write_json(status_path, {"status": "error", "error": "Supervisor timeout/interruption",
                                         "infrastructure_error": True})
                code = 2
            finally:
                cleanup_phase(run_dir, PHASE)
        if code:
            print("Consolidate paused; saved diagnostics: " + str(status_path), flush=True)
            return 2
    else:
        patch = phase_dir / "final.patch"
        if not patch.is_file() or hashlib.sha256(patch.read_bytes()).hexdigest() != existing["final_patch_sha256"]:
            raise RuntimeError("Completed final patch has changed; refusing to rerun or score it")
    evaluation_path = phase_dir / "evaluation_summary.json"
    evaluation = json.loads(evaluation_path.read_text()) if evaluation_path.exists() else {}
    if not evaluation.get("evaluation_completed"):
        print("Scoring the single patch on both original feature suites", flush=True)
        with (phase_dir / "evaluation_console.log").open("wb") as log:
            code = subprocess.run([sys.executable, str(ROOT / "evaluate_consolidate_smoke.py"),
                                   "--run-dir", str(phase_dir)], stdout=log, stderr=subprocess.STDOUT,
                                  timeout=720).returncode
        if code:
            print("Evaluator needs attention; the model run is saved and will not repeat.", flush=True)
            return 2
    summary = make_summary(run_dir, provenance, offline)
    write_json(run_dir / "consolidate_summary.json", summary)
    if offline:
        validate_offline_completion(run_dir, summary)
        write_json(ROOT / "artifacts/consolidate_offline_validation.json", {
            "passed": True, "run_directory": str(run_dir), "model_calls": 0,
            "source_manifest": source_manifest(), "restoration_and_frozen_readonly_probe_passed": True,
            "retired_peer_send_and_wait_disabled": True, "official_evaluator_completed": True,
        })
    else:
        write_json(ROOT / "artifacts/consolidate_summary.json", summary | {"run_directory": str(run_dir)})
    print("Consolidate execution and evaluation completed: " + str(run_dir / "consolidate_summary.json"), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    run_dir = args.run_dir or ROOT / "artifacts/consolidate_runs" / (
        "consolidate_smoke_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run_dir = run_dir.resolve()
    if run_dir.parent != (ROOT / "artifacts/consolidate_runs").resolve() or not run_dir.name.startswith("consolidate_smoke_"):
        raise ValueError("Use an artifacts/consolidate_runs/consolidate_smoke_* run directory")
    return phase_worker(run_dir, args.offline_selftest) if args.worker else supervise(run_dir, args.offline_selftest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + safe_error(exc), file=sys.stderr)
        sys.exit(2)
