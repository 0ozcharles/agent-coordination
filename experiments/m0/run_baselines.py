"""Paired Keep / StrongShare smoke run from one quiescent, restored checkpoint.

Uses the pinned native agent/tool/Git semantics; only coordination, state
restoration and StrongShare's read-only evidence channel are local additions.
No API keys, reference patches or evaluator tests are mounted into agents.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from budget_guard import BudgetGuard, write_json
from run_solo_smoke import BASE, IMAGE, MAX_OUTPUT, MODEL, REPO, ROOT, configure

PREFIX_STEPS = 4
CONTINUATION_SECONDS = 1200
AGENTS = {"agent1": 1, "agent2": 6}
STATE_FIELDS = (
    "messages", "n_calls", "cost", "sent_messages", "extra_template_vars",
    "_last_prompt_tokens", "_submit_nudges", "_departed_announced",
    "_segments", "_current_segment_messages", "_compaction_count",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save_state(agent):
    state = {}
    for field in STATE_FIELDS:
        if hasattr(agent, field):
            value = getattr(agent, field)
            state[field] = sorted(value) if isinstance(value, set) else copy.deepcopy(value)
    return state


def load_state(agent, state):
    for field, value in copy.deepcopy(state).items():
        setattr(agent, field, set(value) if field == "_departed_announced" else value)


def close_interrupted_tool_calls(agent):
    """Complete API tool-message pairing when the native submission sentinel interrupts."""
    pending = {}
    for message in agent.messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                pending[call["id"]] = call
        elif message.get("role") == "tool":
            pending.pop(message.get("tool_call_id"), None)
    for call_id in pending:
        agent.add_messages({
            "role": "tool", "tool_call_id": call_id,
            "content": "Execution interrupted by the task completion sentinel. Submission status will be checked.",
            "extra": {"interrupt_type": "Submitted"},
        })


def usage(run_dir, phase=None, excluded_only=False):
    ledger = run_dir / "usage_ledger.json"
    rows = json.loads(ledger.read_text())["requests"] if ledger.exists() else []
    if phase:
        rows = [r for r in rows if r["phase"].startswith(phase + ":") and not r.get("excluded_from_baseline")]
    if excluded_only:
        rows = [r for r in rows if r.get("excluded_from_baseline")]
    return {
        "requests": len(rows),
        "known_cost_usd": round(sum(r.get("actual_cost_usd") or 0 for r in rows), 9),
        "unknown_usage_requests": sum(r.get("actual_cost_usd") is None for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in rows),
    }


def make_fake_model(agent_id, phase, task=None):
    """Exercise real tools/containers without contacting any provider."""
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    peer = "agent2" if agent_id == "agent1" else "agent1"
    prefix = [
        "git status --short && git rev-parse HEAD",
        f"printf '{agent_id} checkpoint test\\n' > checkpoint_{agent_id}.txt",
        f"send_message {peer} <<'MSG'\nCheckpoint mailbox test.\nMSG",
        f"printf 'temporary state\\n' > /tmp/checkpoint_{agent_id}.txt",
    ]
    continuation = [
        (f"cat /shared-evidence/{peer}/latest/task.md && "
         f"cat /shared-evidence/{peer}/latest/worktree/checkpoint_{peer}.txt && "
         "if touch /shared-evidence/write-probe; then exit 88; else echo evidence_read_only; fi")
        if phase == "strongshare" else "test ! -d /shared-evidence && echo ordinary_visibility",
        f"git add checkpoint_{agent_id}.txt && git commit -m 'offline plumbing test' && git push",
        "gh pr create --title 'Offline plumbing test' --body 'Synthetic patch for infrastructure verification'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]

    if task is not None:
        import shlex
        script = (
            "from pathlib import Path; import subprocess; "
            + "p=next(Path(" + repr(task.code_roots[0]) + ").rglob('*.py')); "
            + "p.write_text(p.read_text() + "
            + repr("\n\ndef _rsi_wip_" + agent_id + "():\n    return 1\n") + "); "
            + "subprocess.run(['git','add',str(p)],check=True); "
            + "subprocess.run(['git','commit','-m','offline prefix committed source'],check=True); "
            + "p.write_text(p.read_text().replace('def _rsi_wip_" + agent_id + "():\\n    return 1','def _rsi_wip_" + agent_id + "():\\n    return 2')); "
            + "Path(" + repr(task.code_roots[0] + "/_rsi_" + agent_id + ".py") + ").write_text('VALUE = 1\\n'); "
            + "staged=Path(" + repr(task.code_roots[0] + "/_rsi_staged_" + agent_id + ".py") + "); staged.write_text('STAGED = True\\n'); "
            + "subprocess.run(['git','add',str(staged)],check=True); "
            + "Path(" + repr("checkpoint_" + agent_id + ".txt") + ").write_text('offline WIP'); "
            + "Path(" + repr("/tmp/checkpoint_" + agent_id + ".txt") + ").write_text('temporary WIP')"
        )
        prefix[0] = f"send_message {peer} <<'MSG'\nCheckpoint mailbox test.\nMSG"
        prefix[1] = "python -c " + shlex.quote(script)
        continuation[1] = "git add -A && git commit -m 'offline WIP publication' && git push"

    class FakeModel(LitellmModel):
        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            commands = prefix if phase == "prefix" else continuation
            if self.index >= len(commands):
                raise RuntimeError("Offline test unexpectedly requested another step")
            cmd = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-{agent_id}-{self.index}", "object": "chat.completion",
                "created": 0, "model": "offline-scripted-model",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "Offline infrastructure check.",
                    "tool_calls": [{"id": f"call_{agent_id}_{phase}_{self.index}", "type": "function",
                                    "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}],
                }}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return FakeModel()


def phase_worker(run_dir, phase, offline=False, task=None):
    configure()
    agent_features = task.agents if task else AGENTS
    base = task.base if task else BASE
    image = task.image if task else IMAGE
    task_dir = task.task_dir if task else REPO / "dataset/pallets_jinja_task/task1621"
    image_record = task.image_record_path if task else ROOT / "artifacts/task_image.json"
    import docker
    import yaml
    from baseline_runtime import (
        MemoryBroker, SnapshotDockerEnvironment, create_shared_git_volume,
        backup_volume, restore_volume, verify_workspace_manifest,
    )
    from baseline_evidence import StrongSharePublisher
    from baseline_model import make_model
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
    from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
    from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow

    phase_dir = run_dir / phase
    phase_dir.mkdir(exist_ok=True)
    dc = docker.from_env(timeout=120)
    expected = json.loads(image_record.read_text())
    if dc.images.get(image).id != expected["Id"]:
        raise RuntimeError("Task image differs from the verified image")
    head = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if head != "63b9d44d9f39a02fccf5bf0052db48a917a011fd":
        raise RuntimeError("Pinned CooperBench commit changed")
    subprocess.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                    "src/cooperbench/agents/mini_swe_agent_v2", str(task_dir.relative_to(REPO))],
                   check=True, capture_output=True)
    cfg = yaml.safe_load(get_config_path("coop").read_text())
    agent_cfg = {**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False}
    label = run_dir.name + "-" + phase
    volume_name = "rsi-" + label.lower().replace("_", "-")
    checkpoint = None if phase == "prefix" else json.loads((run_dir / "checkpoint.json").read_text())
    if checkpoint is not None and task is not None:
        from wip_checkpoint import validate_checkpoint
        validate_checkpoint(checkpoint, task)
    broker = MemoryBroker(list(agent_features)) if checkpoint is None else MemoryBroker.from_snapshot(checkpoint["broker"])
    guard = BudgetGuard(run_dir / "usage_ledger.json", run_dir.name, model=MODEL,
                        enforce_limits=False, max_output_tokens=MAX_OUTPUT)
    envs, agents, connectors, outcomes = {}, {}, {}, {}
    publisher = None
    evidence_volume = None
    started = time.monotonic()
    result = {"phase": phase, "status": "initializing", "offline_selftest": offline}
    write_json(phase_dir / "status.json", result)
    try:
        if checkpoint is None:
            create_shared_git_volume(volume_name, expected["Id"], label)
        else:
            if hashlib.sha256((run_dir / "shared_git.tar").read_bytes()).hexdigest() != checkpoint["git_archive_sha256"]:
                raise RuntimeError("Shared Git archive differs from the checkpoint")
            restore_volume(run_dir / "shared_git.tar", volume_name, expected["Id"], label)
        if phase == "strongshare":
            evidence_volume = volume_name + "-evidence"
            dc.volumes.create(name=evidence_volume, labels={"agent-rsi-run": label})
            publisher = StrongSharePublisher(dc, expected["Id"], evidence_volume, label)
        restore_proof = {"checkpoint_hash": digest(checkpoint) if checkpoint else None, "agents": {}}
        for agent_id, feature in agent_features.items():
            env = SnapshotDockerEnvironment(
                image=expected["Id"] if checkpoint is None else checkpoint["agents"][agent_id]["image"],
                run_label=label, agent_id=agent_id, shared_git_volume=volume_name,
                evidence_volume=evidence_volume, container_timeout="30m",
            )
            envs[agent_id] = env
            if checkpoint is not None:
                verify_workspace_manifest(env, checkpoint["agents"][agent_id]["manifest"])
            else:
                check = env.execute({"command": "git rev-parse HEAD; test ! -e /patches && echo no_reference_patches"})
                if base not in check["output"] or "no_reference_patches" not in check["output"]:
                    raise RuntimeError("Base revision / reference isolation check failed")
            git = GitConnector(agent_id=agent_id, agents=list(agent_features), server_url="/shared-git/team.git")
            connectors[agent_id] = git
            if checkpoint is None:
                git.setup(env)
                if git._base_sha != base:
                    raise RuntimeError("Git connector has the wrong base SHA")
            else:
                git._base_sha = checkpoint["agents"][agent_id]["git_base"]
                git._initialized = True
            model = (make_fake_model(agent_id, phase, task=task) if task else make_fake_model(agent_id, phase)) if offline else make_model(MODEL, guard, phase, agent_id, phase_dir)
            model.config = model.config.model_copy(update={
                key: cfg["model"][key] for key in ("observation_template", "format_error_template")
            })
            agent = DefaultAgent(model, env, comm=broker.connector(agent_id), agent_id=agent_id,
                                 **{**agent_cfg, "output_path": phase_dir / f"{agent_id}.traj.json"})
            agents[agent_id] = agent
            if checkpoint is None:
                task_text = (task_dir / f"feature{feature}/feature.md").read_text()
                agent.extra_template_vars = {"task": task_text, "agent_id": agent_id, "agents": list(agent_features),
                                             "git_enabled": True, "messaging_enabled": True}
                agent.add_messages(
                    model.format_message(role="system", content=agent._render_template(agent.config.system_template)),
                    model.format_message(role="user", content=agent._render_template(agent.config.instance_template)),
                    model.format_message(role="user", content=(
                        "Use small targeted edits rather than replacing or printing entire large files. "
                        "There is no monetary or model-turn limit. Do not start background processes; "
                        "all work must finish within each bash tool call so execution can be checkpointed."
                    )),
                )
            else:
                state = checkpoint["agents"][agent_id]["state"]
                load_state(agent, state)
                if digest(save_state(agent)) != digest(state):
                    raise RuntimeError("Restored agent history/state does not match checkpoint")
                restore_proof["agents"][agent_id] = {
                    "state_hash": digest(state), "filesystem_manifest_hash": digest(checkpoint["agents"][agent_id]["manifest"]),
                    "source_image": checkpoint["agents"][agent_id]["image"], "verified": True,
                }
        if checkpoint:
            if digest(broker.snapshot()) != digest(checkpoint["broker"]):
                raise RuntimeError("Restored mailbox differs from checkpoint")
            # Restore proof is recorded before adding a condition-specific observation.
            restore_proof["broker_hash"] = digest(broker.snapshot())
            write_json(phase_dir / "restore_proof.json", restore_proof)
        def publish_evidence(agent_id):
            agent = agents[agent_id]
            metadata = publisher.publish(agent_id, envs[agent_id].container_id, "/workspace/repo", agent.messages,
                                         agent.extra_template_vars["task"], agent.n_calls)
            evidence_dir = phase_dir / "evidence_manifests"
            evidence_dir.mkdir(exist_ok=True)
            write_json(evidence_dir / f"{agent_id}_{agent.n_calls:05d}.json", metadata)

        if publisher:
            for agent_id, agent in agents.items():
                publish_evidence(agent_id)
        if phase != "prefix":
            for agent_id, agent in agents.items():
                peer = next(a for a in agent_features if a != agent_id)
                shared = (
                    f"Additional evidence access is now enabled. You can read your colleague's complete task, "
                    f"model/tool history, and latest completed-step working files at /shared-evidence/{peer}/latest/. "
                    "This read-only snapshot updates after every completed step, including uncommitted and untracked files. "
                    "This supersedes the earlier statement that only pushed work is visible. Read the relevant files "
                    "as needed; keep your own independent responsibility and agent loop. Do not edit the evidence mount."
                ) if publisher else (
                    "Continue with the existing ordinary message channel and published Git branches. "
                    "Keep your own independent responsibility and agent loop."
                )
                agent.add_messages(agent.model.format_message(role="user", content=(
                    shared + f" You have {CONTINUATION_SECONDS} seconds from this continuation. "
                    "Leave time to commit, push, open your local PR and issue the exact completion command separately."
                )))
        deadline = time.monotonic() + (480 if phase == "prefix" else CONTINUATION_SECONDS)
        infrastructure_stop = threading.Event()

        def drive(agent_id):
            agent = agents[agent_id]
            peer = next(a for a in agent_features if a != agent_id)
            last_version = None
            reminded = False
            turns = 0
            status = "checkpoint"
            try:
                while phase != "prefix" or turns < PREFIX_STEPS:
                    if infrastructure_stop.is_set():
                        return {"status": "cancelled_after_peer_infrastructure_error", "model_turns": agent.n_calls,
                                "phase_turns": turns, "infrastructure_error": True}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = "deadline"
                        break
                    if phase != "prefix" and remaining <= 120 and not reminded:
                        reminded = True
                        agent.add_messages(agent.model.format_message(role="user", content=(
                            "About two minutes remain. Publish your current implementation, even if incomplete; "
                            "do not claim a failing test passed. Then issue the exact completion command."
                        )))
                    if publisher:
                        version = publisher.version(peer)
                        version_step = version.get("step") if version else None
                        if version_step != last_version:
                            agent.add_messages(agent.model.format_message(role="user", content=(
                                f"Peer evidence updated: /shared-evidence/{peer}/latest/ (version {version_step}). "
                    "Read task.md, history.json, metadata.json, worktree/, and temporary/ as needed."
                            )))
                            last_version = version_step
                    try:
                        agent.step()
                    except InterruptAgentFlow as exc:
                        if any(m.get("role") == "exit" for m in exc.messages):
                            close_interrupted_tool_calls(agent)
                        agent.add_messages(*exc.messages)
                    turns += 1
                    if offline and phase != "prefix" and turns == 1:
                        observations = [m for m in agent.messages if m.get("role") == "tool"]
                        marker = "evidence_read_only" if publisher else "ordinary_visibility"
                        if not observations or observations[-1].get("extra", {}).get("returncode") != 0 or marker not in str(observations[-1]):
                            raise RuntimeError("Offline visibility / read-only probe did not pass")
                    agent.save(agent.config.output_path)
                    if publisher:
                        publish_evidence(agent_id)
                    if agent.messages[-1].get("role") == "exit":
                        if agent._nudge_unsubmitted():
                            # Exit records are internal control flow, not valid provider message roles.
                            agent.messages = [m for m in agent.messages if m.get("role") != "exit"]
                            continue
                        status = agent.messages[-1].get("extra", {}).get("exit_status", "submitted")
                        break
                return {"status": status, "model_turns": agent.n_calls, "phase_turns": turns,
                        "infrastructure_error": False}
            except Exception as exc:
                infrastructure_stop.set()
                return {"status": "error", "error_type": type(exc).__name__, "error": safe_error(exc),
                        "model_turns": agent.n_calls, "infrastructure_error": True}
            finally:
                if phase != "prefix":
                    try:
                        agent.comm.mark_exited(published=agent._opened_pr())
                    except Exception:
                        agent.comm.mark_exited(published=False)
                agent.save(agent.config.output_path)

        wip_evidence = None
        if phase == "prefix" and task is not None:
            from wip_checkpoint import drive_prefix
            outcomes, wip_evidence = drive_prefix(agents, envs, task, phase_dir)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {a: pool.submit(drive, a) for a in agent_features}
                outcomes = {a: future.result() for a, future in futures.items()}
        write_json(phase_dir / "agent_outcomes.json", outcomes)
        write_json(phase_dir / "messages.json", broker.snapshot())
        if phase == "prefix":
            if task is not None and not wip_evidence.get("eligible"):
                result.update(status="checkpoint_not_reached", outcomes=outcomes,
                              elapsed_seconds=round(time.monotonic() - started, 3))
                return 0
            if task is None and any(o["status"] != "checkpoint" or o.get("phase_turns") != PREFIX_STEPS for o in outcomes.values()):
                raise RuntimeError("The predeclared four-step checkpoint was not reached by both agents")
            saved = {"prefix_steps_per_agent": PREFIX_STEPS, "broker": broker.snapshot(), "agents": {}}
            if task is not None:
                saved.update(task=task.as_dict(), wip_checkpoint=wip_evidence,
                             prefix_steps_per_agent=wip_evidence["paired_round"])
            for agent_id, agent in agents.items():
                env = envs[agent_id]
                saved["agents"][agent_id] = {
                    "state": save_state(agent), "git_base": connectors[agent_id]._base_sha,
                    "manifest": env.workspace_manifest(), "image": env.snapshot_image(),
                }
            backup_volume(volume_name, run_dir / "shared_git.tar", expected["Id"], label)
            saved["git_archive_sha256"] = hashlib.sha256((run_dir / "shared_git.tar").read_bytes()).hexdigest()
            write_json(run_dir / "checkpoint.json", saved)
        else:
            for agent_id, feature in agent_features.items():
                patch = connectors[agent_id].submitted_patch(envs[agent_id])
                (phase_dir / f"agent{feature}.patch").write_text(patch or "")
                diagnostic = envs[agent_id].execute({"command": f"git diff --binary {base}"})
                (phase_dir / f"{agent_id}_unsubmitted_diagnostic.patch").write_text(diagnostic.get("output", ""))
            if any(o["infrastructure_error"] for o in outcomes.values()):
                raise RuntimeError("Agent infrastructure failed; saved patches are diagnostic, not a completed baseline")
        result.update(status="completed", elapsed_seconds=round(time.monotonic() - started, 3), outcomes=outcomes)
        return 0
    except Exception as exc:
        result.update(status="error", error=safe_error(exc), error_type=type(exc).__name__)
        return 2
    finally:
        write_json(phase_dir / "status.json", result)
        write_json(phase_dir / "usage_summary.json", usage(run_dir, phase))
        for agent in agents.values():
            close = getattr(agent.model, "close", None)
            if close:
                close()
        for env in envs.values():
            env.cleanup()
        if publisher:
            publisher.cleanup()
        for name in (evidence_volume, volume_name):
            if name:
                try:
                    dc.volumes.get(name).remove()
                except Exception:
                    pass
        dc.close()


def safe_error(exc):
    import re
    text = str(exc)
    key = os.environ.get("TOGETHER_API_KEY", "")
    if key:
        text = text.replace(key, "[REDACTED]")
    return re.sub(r"(?:tgp_|sk-)[A-Za-z0-9_\-]+", "[REDACTED]", text)


def cleanup_phase(run_dir, phase):
    import docker
    dc = docker.from_env(timeout=20)
    try:
        for container in dc.containers.list(all=True, filters={"label": "agent-rsi-run=" + run_dir.name + "-" + phase}):
            container.remove(force=True)
        for volume in dc.volumes.list(filters={"label": "agent-rsi-run=" + run_dir.name + "-" + phase}):
            volume.remove()
    finally:
        dc.close()


def supervise(run_dir, offline=False):
    configure()
    run_dir.mkdir(parents=True, exist_ok=True)
    print(run_dir, flush=True)
    if not offline and not os.environ.get("TOGETHER_API_KEY", "").strip():
        print("TOGETHER_API_KEY is absent. Use run_baselines_with_together_key.sh.", flush=True)
        return 2
    lock = (ROOT / "artifacts/model_run.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another model run owns the experiment lock.", flush=True)
        return 2
    run_config = {
        "model": MODEL if not offline else "offline-scripted-model", "offline_selftest": offline,
        "features": AGENTS, "conditions": ["keep", "strongshare"], "prefix_steps_per_agent": PREFIX_STEPS,
        "continuation_deadline_seconds": CONTINUATION_SECONDS, "parallel_agents_per_condition": 2,
        "conditions_executed_sequentially": True, "money_limit": None, "request_limit": None,
        "max_output_tokens": MAX_OUTPUT, "compaction_enabled": False,
        "checkpoint_method": "Docker filesystem commit plus independent shared Git volume and in-memory agent/mailbox state",
        "evidence_semantics": "Full trace and latest completed-step worktree via read-only snapshots; no shared decision loop",
        "task_scope": "One task pair and one early checkpoint; engineering smoke, not a performance study",
        "continuation_order": ["keep", "strongshare"], "random_seed": None,
    }
    config_path = run_dir / "run_config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != run_config:
        raise RuntimeError("Existing run configuration differs; use a new run directory")
    write_json(config_path, run_config)
    for phase in ("prefix", "keep", "strongshare"):
        path = run_dir / phase
        status_file = path / "status.json"
        existing = json.loads(status_file.read_text()) if status_file.exists() else {}
        if existing.get("status") != "completed":
            if path.exists():
                archive = run_dir / "failed_attempts"
                archive.mkdir(exist_ok=True)
                shutil.move(str(path), str(archive / (phase + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))))
            path.mkdir()
            write_json(path / "source_manifest.json", {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in ("run_baselines.py", "baseline_runtime.py", "baseline_model.py",
                             "baseline_evidence.py", "evaluate_baseline_smoke.py", "budget_guard.py")
            })
            command = [sys.executable, "-u", str(Path(__file__).resolve()), "--run-dir", str(run_dir), "--phase", phase]
            if offline:
                command.append("--offline-selftest")
            print(f"Starting {phase}", flush=True)
            with (path / "console.log").open("wb") as log:
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                        pass_fds=(lock.fileno(),))
                try:
                    code = proc.wait(timeout=600 if phase == "prefix" else CONTINUATION_SECONDS + 240)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=15)
                    write_json(path / "status.json", {"status": "error", "error": "Phase supervisor timeout/interruption"})
                    code = 2
                finally:
                    cleanup_phase(run_dir, phase)
            if code:
                print(f"{phase} stopped; inspect {path / 'status.json'}. Resume uses the same checkpoint.", flush=True)
                return 2
        if phase != "prefix":
            eval_file = path / "evaluation_summary.json"
            summary = json.loads(eval_file.read_text()) if eval_file.exists() else {}
            if not summary.get("evaluation_completed"):
                with (path / "evaluation_console.log").open("wb") as log:
                    code = subprocess.run([sys.executable, str(ROOT / "evaluate_baseline_smoke.py"),
                                           "--run-dir", str(path)], stdout=log, stderr=subprocess.STDOUT,
                                          timeout=720).returncode
                if code:
                    print(f"{phase} evaluator requires attention; model run is saved and will not repeat.", flush=True)
                    return 2
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
    prefix_cost = usage(run_dir, "prefix")
    comparison = {"pipeline_completed": True, "offline_selftest": offline, "checkpoint_sha256": digest(checkpoint),
                  "prefix_usage": prefix_cost, "actual_collection_usage": usage(run_dir),
                  "excluded_infrastructure_attempt_usage": usage(run_dir, excluded_only=True), "conditions": {}}
    for phase in ("keep", "strongshare"):
        phase_usage = usage(run_dir, phase)
        comparison["conditions"][phase] = {
            "evaluation": json.loads((run_dir / phase / "evaluation_summary.json").read_text()),
            "continuation_usage": phase_usage,
            "full_task_known_cost_usd": round(prefix_cost["known_cost_usd"] + phase_usage["known_cost_usd"], 9),
            "execution": json.loads((run_dir / phase / "status.json").read_text()),
            "restore_proof": json.loads((run_dir / phase / "restore_proof.json").read_text()),
        }
    write_json(run_dir / "baseline_summary.json", comparison)
    if offline:
        write_json(ROOT / "artifacts/baseline_offline_validation.json", {
            "passed": True, "run_directory": str(run_dir), "model_calls": 0,
            "restored_both_conditions": True, "visibility_and_readonly_probes_passed": True,
            "official_evaluator_completed_for_both": True,
        })
    else:
        write_json(ROOT / "artifacts/baseline_summary.json", comparison | {"run_directory": str(run_dir)})
    print("Both baseline pipelines completed. See " + str(run_dir / "baseline_summary.json"), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir")
    parser.add_argument("--phase", choices=["prefix", "keep", "strongshare"], help=argparse.SUPPRESS)
    parser.add_argument("--offline-selftest", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = "baseline_smoke_" + ("selftest_" if args.offline_selftest else "") + stamp
    runs = ROOT / "artifacts/baseline_runs"
    run_dir = Path(args.run_dir).resolve() if args.run_dir else runs / name
    if run_dir.parent != runs or not run_dir.name.startswith("baseline_smoke_"):
        raise ValueError("Run directory must be under artifacts/baseline_runs/baseline_smoke_*")
    if args.phase:
        return phase_worker(run_dir, args.phase, args.offline_selftest)
    return supervise(run_dir, args.offline_selftest)


if __name__ == "__main__":
    raise SystemExit(main())
