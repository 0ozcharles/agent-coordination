"""Extend one immutable Luna early checkpoint by exactly two ordinary paired steps.

No handoff packet, role change, deadline reminder or new task instruction is
inserted. Native mailbox polling may append ordinary peer messages. The late
checkpoint is eligible only at the declared second boundary; an earlier PR,
exit, deadline, or an ineligible final state is terminal without resampling.
Importing this module does not connect to Docker or any model provider.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import time

from budget_guard import write_json
from luna_accounting import LunaAccounting, MODEL, MAX_OUTPUT
from luna_model import _redact
from run_baselines import close_interrupted_tool_calls, digest, load_state, save_state, usage
from run_solo_smoke import configure
from task_specs import COMMIT, REPO
from wip_checkpoint import checkpoint_eligible, code_progress, validate_checkpoint

RULE = "fixed_two_paired_rounds_after_early_unfinished_source_changes_v1"
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
ADDITIONAL_ROUNDS = 2
PROVIDER_FIELDS = {"role", "content", "tool_calls", "tool_call_id", "name"}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_error(exc):
    # Match the transport's redaction without persisting environment values.
    import os
    return _redact(str(exc), os.environ.get("OPENAI_API_KEY", "").strip())


def check_arguments(run_dir, early_prefix_dir, task, execution_seconds, offline, additional_rounds):
    from cross_task_specs import get_task
    if (task is None or getattr(task, "key", None) not in TASK_KEYS
            or task.as_dict() != get_task(task.key).as_dict()):
        raise ValueError("Task differs from the pinned Luna timing scope")
    if (isinstance(execution_seconds, bool) or not isinstance(execution_seconds, (int, float))
            or not math.isfinite(execution_seconds) or execution_seconds <= 0):
        raise ValueError("Extension time must be finite and positive")
    if type(additional_rounds) is not int or additional_rounds != ADDITIONAL_ROUNDS:
        raise ValueError("This protocol requires exactly two additional paired rounds")
    if not isinstance(offline, bool):
        raise ValueError("offline must be a boolean")
    destination, origin = Path(run_dir).resolve(), Path(early_prefix_dir).resolve()
    if destination == origin or origin in destination.parents or destination in origin.parents:
        raise ValueError("Late output must be separate from the immutable early prefix")
    if (destination / "checkpoint.json").exists() or (destination / "shared_git.tar").exists():
        raise ValueError("Late checkpoint output already exists; verify and reuse it in the supervisor")
    status_path = destination / "prefix/status.json"
    if status_path.exists() and json.loads(status_path.read_text()).get("status") in ("completed", "checkpoint_not_reached"):
        raise ValueError("A terminal timing extension must be reused, never resampled")


def read_early(early_prefix_dir, task, offline):
    """Validate provenance before any environment or model is constructed."""
    origin = Path(early_prefix_dir).resolve()
    paths = [origin / "checkpoint.json", origin / "shared_git.tar", origin / "prefix/status.json"]
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError("Early prefix inputs must be regular existing files")
    checkpoint = json.loads(paths[0].read_text())
    status = json.loads(paths[2].read_text())
    validate_checkpoint(checkpoint, task)
    elapsed = status.get("elapsed_seconds")
    if (status.get("status") != "completed" or status.get("offline_selftest") is not offline
            or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed) or elapsed <= 0):
        raise ValueError("Early prefix status/timing/offline provenance is invalid")
    gate = checkpoint["wip_checkpoint"]
    if (gate.get("rule") != "first_paired_round_both_source_changes_and_unfinished_v1"
            or gate.get("selection_uses_test_results") is not False
            or type(gate.get("paired_round")) is not int or gate["paired_round"] < 1
            or checkpoint.get("prefix_steps_per_agent") != gate["paired_round"]):
        raise ValueError("Expected the original first-eligible early checkpoint")
    if file_hash(paths[1]) != checkpoint["git_archive_sha256"]:
        raise ValueError("Early shared Git archive checksum changed")
    ledger = origin / "usage_ledger.json"
    if ledger.exists() or ledger.is_symlink():
        if not ledger.is_file() or ledger.is_symlink():
            raise ValueError("Early accounting ledger must be a regular file")
        accounting = json.loads(ledger.read_text())
        if accounting.get("model") != MODEL or not isinstance(accounting.get("requests"), list):
            raise ValueError("Early prefix is not a verified Luna accounting run")
        if offline and accounting["requests"]:
            raise ValueError("Offline early prefix must have zero provider requests")
        paths.append(ledger)
    elif not offline:
        raise ValueError("Real early prefix requires a Luna accounting ledger")
    for optional in (origin / "task_config.json", origin / "prefix/source_manifest.json"):
        if optional.exists():
            if optional.is_symlink():
                raise ValueError("Early provenance must not be symlinked")
            paths.append(optional)
    return checkpoint, float(elapsed), {str(p): file_hash(p) for p in paths}


def provider_messages(messages):
    return [{k: copy.deepcopy(v) for k, v in m.items() if k in PROVIDER_FIELDS} for m in messages]


def initialize_accounting(run_dir):
    """Persist an explicit new zero-request ledger, including offline/deadline runs."""
    run_dir = Path(run_dir)
    guard = LunaAccounting(run_dir / "usage_ledger.json", run_dir.name, model=MODEL,
                           enforce_limits=False, max_output_tokens=MAX_OUTPUT)
    if not guard.path.exists():
        guard._persist()
    return guard


def verify_receipt(run_dir, task, *, early_prefix_dir):
    """Read-only verification for cached completed or terminal-unavailable late prefixes."""
    from run_consolidate import validate_message_layout
    run_dir, phase_dir = Path(run_dir).resolve(), Path(run_dir).resolve() / "prefix"
    status = json.loads((phase_dir / "status.json").read_text())
    config = json.loads((run_dir / "task_config.json").read_text())
    receipt = json.loads((phase_dir / "execution_receipt.json").read_text())
    if (receipt.get("schema") != "luna_fixed_timing_prefix_receipt_v1"
            or status.get("status") not in ("completed", "checkpoint_not_reached")
            or receipt.get("status") != status["status"] or status.get("task") != task.as_dict()
            or config.get("task") != task.as_dict() or config.get("task_key") != task.key
            or config.get("condition") != "prefix" or config.get("timing") != "late"
            or status.get("offline_selftest") != config.get("offline_selftest")
            or status.get("timing_rule") != RULE or status.get("additional_rounds") != ADDITIONAL_ROUNDS):
        raise ValueError("Timing prefix status/config/receipt is inconsistent")
    protected = receipt.get("protected_files_sha256", {})
    required = {"task_config.json", "usage_ledger.json", "prefix/source_manifest.json", "prefix/status.json",
                "prefix/progress.json", "prefix/usage_summary.json", "prefix/agent_outcomes.json",
                "prefix/messages.json", "prefix/restore_proof.json", "prefix/early_provenance.json"}
    if status["status"] == "completed":
        required |= {"checkpoint.json", "shared_git.tar"}
        required |= {f"prefix/{a}.traj.json" for a in task.agents}
        required |= {f"prefix/first_request_{a}.json" for a in task.agents}
    if not required.issubset(protected):
        raise ValueError("Timing receipt omits mandatory evidence")
    for rel, expected in protected.items():
        path = run_dir / rel
        if (Path(rel).is_absolute() or ".." in Path(rel).parts or path.is_symlink()
                or not path.is_file() or file_hash(path) != expected):
            raise ValueError("Timing evidence differs from its receipt: " + rel)
    sources = json.loads((phase_dir / "source_manifest.json").read_text())
    if not sources or "luna_timing_prefix.py" not in sources:
        raise ValueError("Timing source manifest is incomplete")
    for rel, expected in sources.items():
        source = Path(__file__).resolve().parent / rel
        if Path(rel).is_absolute() or ".." in Path(rel).parts or file_hash(source) != expected:
            raise ValueError("Frozen timing source changed: " + rel)
    checkpoint, early_elapsed, early_files = read_early(early_prefix_dir, task, status["offline_selftest"])
    provenance = json.loads((phase_dir / "early_provenance.json").read_text())
    if (receipt.get("early_files_sha256") != early_files or provenance.get("files_sha256") != early_files
            or provenance.get("source_directory") != str(Path(early_prefix_dir).resolve())
            or provenance.get("checkpoint_sha256") != digest(checkpoint)
            or status.get("early_checkpoint_sha256") != digest(checkpoint)
            or config.get("early_checkpoint_sha256") != digest(checkpoint)):
        raise ValueError("Early checkpoint provenance changed")
    elapsed, extension, budget = (status.get(k) for k in
                                 ("elapsed_seconds", "extension_elapsed_seconds", "extension_budget_seconds"))
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in (elapsed, extension, budget)) or abs(elapsed - early_elapsed - extension) > .003
            or status.get("early_elapsed_seconds") != early_elapsed
            or config.get("execution_seconds") != budget
            or (status["status"] == "completed" and extension > budget + .003)):
        raise ValueError("Timing prefix elapsed-time accounting changed")
    proof = json.loads((phase_dir / "restore_proof.json").read_text())
    if proof.get("checkpoint_hash") != digest(checkpoint) or proof.get("broker_hash") != digest(checkpoint["broker"]):
        raise ValueError("Early restore proof differs")
    for a in task.agents:
        saved, restored = checkpoint["agents"][a], proof.get("agents", {}).get(a, {})
        if (restored.get("state_hash") != digest(saved["state"])
                or restored.get("filesystem_manifest_hash") != digest(saved["manifest"])
                or restored.get("source_image") != saved["image"]
                or not restored.get("state_verified") or not restored.get("filesystem_verified")):
            raise ValueError("Early agent restore evidence differs")
        first_path = phase_dir / f"first_request_{a}.json"
        if first_path.exists():
            first = json.loads(first_path.read_text())
            expected = provider_messages(saved["state"]["messages"])
            if (first.get("early_state_sha256") != digest(saved["state"])
                    or first.get("history_sha256") != digest(expected)
                    or first.get("history_message_count") != len(expected)
                    or first.get("role_change_injected") is not False
                    or first.get("handoff_packet_injected") is not False):
                raise ValueError("First extension history proof differs")
            if not status["offline_selftest"]:
                actual = phase_dir / first.get("actual_provider_input", "missing")
                if (not first.get("actual_transport_input_verified") or not actual.is_file()
                        or str(actual.relative_to(run_dir)) not in protected
                        or file_hash(actual) != first.get("actual_provider_input_sha256")):
                    raise ValueError("First actual provider input is not protected")
                messages = json.loads(actual.read_text())["request"]["messages"]
                validate_message_layout(messages)
                if messages[:len(expected)] != expected or digest(messages) != first.get("model_input_sha256"):
                    raise ValueError("Actual provider history differs from early state")
    evidence = json.loads((phase_dir / "progress.json").read_text())
    if (evidence.get("rule") != RULE or evidence.get("selection_uses_test_results") is not False
            or evidence.get("early_paired_round") != checkpoint["wip_checkpoint"]["paired_round"]
            or evidence.get("additional_paired_rounds_required") != ADDITIONAL_ROUNDS):
        raise ValueError("Fixed timing rule changed")
    if status["status"] == "completed":
        late = json.loads((run_dir / "checkpoint.json").read_text())
        validate_checkpoint(late, task)
        if (late.get("wip_checkpoint") != evidence or not evidence.get("eligible")
                or evidence.get("additional_paired_rounds_completed") != ADDITIONAL_ROUNDS
                or late["prefix_steps_per_agent"] != checkpoint["prefix_steps_per_agent"] + ADDITIONAL_ROUNDS
                or evidence["paired_round"] != late["prefix_steps_per_agent"]
                or late.get("timing_origin") != {"early_checkpoint_sha256": digest(checkpoint),
                                                "additional_paired_rounds": ADDITIONAL_ROUNDS}
                or file_hash(run_dir / "shared_git.tar") != late["git_archive_sha256"]
                or status.get("checkpoint_sha256") != digest(late)
                or any(o.get("phase_turns") != ADDITIONAL_ROUNDS for o in status["outcomes"].values())):
            raise ValueError("Late checkpoint is not the declared two-round boundary")
    elif (evidence.get("eligible") or (run_dir / "checkpoint.json").exists()
          or (run_dir / "shared_git.tar").exists()):
        raise ValueError("Unavailable timing unexpectedly has a selectable checkpoint")
    ledger = json.loads((run_dir / "usage_ledger.json").read_text())
    if ledger.get("model") != MODEL or any(not r["phase"].startswith("prefix:") for r in ledger["requests"]):
        raise ValueError("Extension ledger contains a different model/phase")
    if status["offline_selftest"] and ledger["requests"]:
        raise ValueError("Offline extension must have zero provider requests")
    if json.loads((phase_dir / "usage_summary.json").read_text()) != usage(run_dir, "prefix"):
        raise ValueError("Extension usage summary differs from ledger")
    return receipt


def install_history_check(model, state, recipient, phase_dir, *, offline):
    """Verify restored history after native formatting and at actual transport."""
    from run_consolidate import validate_message_layout
    expected = provider_messages(state["messages"])
    original, observed = model._query, False

    def checked(messages, **kwargs):
        nonlocal observed
        if observed:
            return original(messages, **kwargs)
        validate_message_layout(messages)
        clean = provider_messages(messages)
        if clean[:len(expected)] != expected:
            raise RuntimeError("Early history changed before the first extension query")
        proof = {"recipient": recipient, "verified": True,
                 "early_state_sha256": digest(state), "history_message_count": len(expected),
                 "history_sha256": digest(expected), "model_input_sha256": digest(clean),
                 "model_input_message_count": len(clean),
                 "ordinary_native_appended_messages": len(clean) - len(expected),
                 "role_change_injected": False, "handoff_packet_injected": False,
                 "offline_selftest": offline, "actual_transport_input_verified": False}
        observed = True
        proof_path = Path(phase_dir) / f"first_request_{recipient}.json"
        write_json(proof_path, proof)
        response_dir = Path(phase_dir) / "responses" / recipient
        before = set(response_dir.glob("*_input.json"))
        try:
            return original(messages, **kwargs)
        finally:
            inputs = sorted(set(response_dir.glob("*_input.json")) - before)
            if not offline and inputs:
                payload = json.loads(inputs[0].read_text())["request"]["messages"]
                if payload != clean:
                    raise RuntimeError("Actual first provider input differs from verified restored history")
                proof.update(actual_transport_input_verified=True,
                             actual_provider_input=str(inputs[0].relative_to(phase_dir)),
                             actual_provider_input_sha256=file_hash(inputs[0]))
                write_json(proof_path, proof)

    model._query = checked


def make_offline_model(task, agent_id):
    """Exercise two real native tool steps, never a model or a task solution."""
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    peer = "agent2" if agent_id == "agent1" else "agent1"
    script = ("from pathlib import Path; Path(" + repr(task.code_roots[0] + "/_rsi_late_" + agent_id + ".py")
              + ").write_text('TIMING_INFRASTRUCTURE_MARKER = 2\\n')")
    commands = [
        f"send_message {peer} <<'MSG'\nOrdinary timing extension mailbox probe.\nMSG\n"
        "test ! -e /shared-evidence && test ! -e /patches && echo timing_ordinary_visibility_ok",
        "python -c " + shlex.quote(script),
    ]

    class OfflineModel(LitellmModel):
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]

        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            if self.index >= ADDITIONAL_ROUNDS:
                raise RuntimeError("Unexpected extra timing-extension query")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-timing-{agent_id}-{self.index}", "object": "chat.completion",
                "created": 0, "model": "offline-scripted-model", "choices": [{"index": 0,
                "finish_reason": "tool_calls", "message": {"role": "assistant",
                "content": "Offline ordinary-prefix infrastructure check.", "tool_calls": [{
                    "id": f"call_timing_{agent_id}_{self.index}", "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def drive_two_rounds(agents, envs, task, phase_dir, *, deadline, early_round,
                     offline=False, clock=time.monotonic, progress_reader=code_progress):
    """Stop at the predetermined boundary; never choose a replacement point."""
    from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow
    turns = {a: 0 for a in agents}
    last = {a: {"exited": False, "submitted": False, "infrastructure_error": False} for a in agents}
    evidence = {"rule": RULE, "paired_round": early_round, "early_paired_round": early_round,
                "additional_paired_rounds_required": ADDITIONAL_ROUNDS, "additional_paired_rounds_completed": 0,
                "progress": {}, "outcomes": last, "eligible": False,
                "selection_uses_test_results": False, "stop_reason": "deadline"}

    def step(a):
        agent = agents[a]
        if clock() >= deadline:
            return {**last[a], "step_started": False, "deadline": True}
        try:
            try:
                agent.step()
            except InterruptAgentFlow as exc:
                if any(m.get("role") == "exit" for m in exc.messages):
                    close_interrupted_tool_calls(agent)
                agent.add_messages(*exc.messages)
            turns[a] += 1
            if offline and turns[a] == 1:
                observations = [m for m in agent.messages if m.get("role") == "tool"]
                if (not observations or observations[-1].get("extra", {}).get("returncode") != 0
                        or "timing_ordinary_visibility_ok" not in str(observations[-1])):
                    raise RuntimeError("Offline ordinary visibility probe failed")
            return {"step_started": True, "exited": any(m.get("role") == "exit" for m in agent.messages),
                    "submitted": bool(agent._opened_pr()), "infrastructure_error": False}
        except Exception as exc:
            return {"step_started": True, "infrastructure_error": True,
                    "error_type": type(exc).__name__, "error": safe_error(exc)}
        finally:
            agent.save(agent.config.output_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for additional in range(1, ADDITIONAL_ROUNDS + 1):
            if clock() >= deadline:
                break
            futures = {a: pool.submit(step, a) for a in agents}
            last = {a: f.result() for a, f in futures.items()}
            evidence.update(outcomes=last)
            if any(o.get("infrastructure_error") for o in last.values()):
                write_json(Path(phase_dir) / "progress.json", evidence)
                raise RuntimeError("Timing prefix infrastructure failure: " + json.dumps(last))
            paired = all(turns[a] == additional for a in agents)
            progress = {a: progress_reader(envs[a], task) for a in agents} if paired else {}
            expired = clock() >= deadline
            departed = any(o.get("exited") or o.get("submitted") for o in last.values())
            final = paired and additional == ADDITIONAL_ROUNDS
            eligible = final and not expired and not departed and checkpoint_eligible(progress, last)
            reason = ("deadline" if expired or not paired else "early_submission_or_exit" if departed
                      else "fixed_boundary_reached" if eligible else "ineligible_at_fixed_boundary" if final
                      else "continue_to_declared_boundary")
            evidence.update(paired_round=early_round + (additional if paired else additional - 1),
                            additional_paired_rounds_completed=additional if paired else additional - 1,
                            progress=progress, eligible=eligible, stop_reason=reason)
            write_json(Path(phase_dir) / f"progress_round_{early_round + additional:03d}.json", evidence)
            write_json(Path(phase_dir) / "progress.json", evidence)
            if expired or not paired or departed or final:
                break
    write_json(Path(phase_dir) / "progress.json", evidence)
    return {a: {"status": "checkpoint" if evidence["eligible"] else "checkpoint_not_reached",
                "phase_turns": turns[a], "model_turns": agents[a].n_calls,
                "infrastructure_error": False} for a in agents}, evidence


def phase_worker(run_dir, task, *, early_prefix_dir, execution_seconds, offline=False,
                 additional_rounds=ADDITIONAL_ROUNDS):
    """Create a late prefix whose elapsed time includes the immutable early run.

    The supplied extension allowance includes restore, both paired steps, and
    snapshot creation. Cleanup time is reported separately. The new ledger
    contains extension requests only; supervisors add the historical early cost.
    """
    check_arguments(run_dir, early_prefix_dir, task, execution_seconds, offline, additional_rounds)
    started = time.monotonic()
    deadline = started + float(execution_seconds)
    configure()
    import docker
    import yaml
    from baseline_runtime import (MemoryBroker, SnapshotDockerEnvironment, restore_volume,
                                  verify_workspace_manifest, backup_volume)
    from luna_model import make_model
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
    from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector

    run_dir, early_prefix_dir = Path(run_dir).resolve(), Path(early_prefix_dir).resolve()
    phase_dir = run_dir / "prefix"
    phase_dir.mkdir(parents=True, exist_ok=True)
    label = run_dir.name + "-prefix"
    volume = "rsi-" + label.lower().replace("_", "-")
    dc = broker = None
    envs, agents, connectors, snapshots = {}, {}, {}, []
    created_volume = False
    early_elapsed, early_files, cleanup_errors = 0.0, {}, []
    exit_code = 2
    result = {"phase": "prefix", "status": "initializing", "task": task.as_dict(),
              "offline_selftest": offline, "timing_rule": RULE, "additional_rounds": ADDITIONAL_ROUNDS,
              "extension_budget_seconds": float(execution_seconds), "initialization_included_in_budget": True,
              "money_limit": None, "request_limit": None, "ledger_scope": "extension_only",
              "new_prompt_messages_inserted": 0, "evidence_channel_enabled": False}
    write_json(phase_dir / "status.json", result)
    try:
        checkpoint, early_elapsed, early_files = read_early(early_prefix_dir, task, offline)
        result.update(early_elapsed_seconds=early_elapsed, early_checkpoint_sha256=digest(checkpoint),
                      early_checkpoint_file_sha256=file_hash(early_prefix_dir / "checkpoint.json"))
        write_json(phase_dir / "early_provenance.json", {"source_directory": str(early_prefix_dir),
                   "checkpoint_sha256": digest(checkpoint), "files_sha256": early_files,
                   "early_elapsed_seconds": early_elapsed, "new_ledger_scope": "extension_only"})
        expected = json.loads(task.image_record_path.read_text())
        dc = docker.from_env(timeout=120)
        if dc.images.get(task.image).id != expected["Id"]:
            raise RuntimeError("Task image differs from the pinned image")
        if subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
            raise RuntimeError("Pinned CooperBench commit changed")
        subprocess.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                        "src/cooperbench/agents/mini_swe_agent_v2", str(task.task_dir.relative_to(REPO))],
                       check=True, capture_output=True)
        restore_volume(early_prefix_dir / "shared_git.tar", volume, expected["Id"], label,
                       expected_sha256=checkpoint["git_archive_sha256"])
        created_volume = True
        proof = {"checkpoint_hash": digest(checkpoint), "agents": {}, "ordinary_collaboration": True}
        for a in task.agents:
            saved = checkpoint["agents"][a]
            env = SnapshotDockerEnvironment(image=saved["image"], run_label=label, agent_id=a,
                    shared_git_volume=volume, evidence_volume=None, container_timeout="30m")
            envs[a] = env
            verify_workspace_manifest(env, saved["manifest"])
            proof["agents"][a] = {"state_hash": digest(saved["state"]),
                "filesystem_manifest_hash": digest(saved["manifest"]), "source_image": saved["image"],
                "filesystem_verified": True}
        broker = MemoryBroker.from_snapshot(checkpoint["broker"])
        guard = initialize_accounting(run_dir)
        cfg = yaml.safe_load(get_config_path("coop").read_text())
        for a in task.agents:
            model = make_offline_model(task, a) if offline else make_model(MODEL, guard, "prefix", a, phase_dir)
            model.config = model.config.model_copy(update={k: cfg["model"][k]
                                                       for k in ("observation_template", "format_error_template")})
            agent = DefaultAgent(model, envs[a], comm=broker.connector(a), agent_id=a,
                        **{**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False,
                           "output_path": phase_dir / f"{a}.traj.json"})
            agents[a] = agent
            state = checkpoint["agents"][a]["state"]
            load_state(agent, state)
            if digest(save_state(agent)) != digest(state):
                raise RuntimeError("Restored agent state/history differs from early checkpoint")
            proof["agents"][a]["state_verified"] = True
            install_history_check(model, state, a, phase_dir, offline=offline)
            git = GitConnector(agent_id=a, agents=list(task.agents), server_url="/shared-git/team.git")
            git._base_sha, git._initialized = checkpoint["agents"][a]["git_base"], True
            connectors[a] = git
        if digest(broker.snapshot()) != digest(checkpoint["broker"]):
            raise RuntimeError("Restored ordinary mailbox changed")
        proof["broker_hash"] = digest(broker.snapshot())
        write_json(phase_dir / "restore_proof.json", proof)
        setup_end = time.monotonic()
        result["restore_setup_seconds"] = round(setup_end - started, 3)
        outcomes, evidence = drive_two_rounds(agents, envs, task, phase_dir, deadline=deadline,
                    early_round=checkpoint["wip_checkpoint"]["paired_round"], offline=offline)
        drive_end = time.monotonic()
        result.update(outcomes=outcomes, paired_execution_seconds=round(drive_end - setup_end, 3))
        write_json(phase_dir / "agent_outcomes.json", outcomes)
        write_json(phase_dir / "messages.json", broker.snapshot())
        if offline and usage(run_dir, "prefix")["requests"]:
            raise RuntimeError("Offline timing prefix unexpectedly used a provider")
        if evidence["eligible"]:
            for a in task.agents:
                first = json.loads((phase_dir / f"first_request_{a}.json").read_text())
                if not offline and first.get("actual_transport_input_verified") is not True:
                    raise RuntimeError("First actual extension provider input was not verified")
            saved = {"task": task.as_dict(), "prefix_steps_per_agent": evidence["paired_round"],
                     "wip_checkpoint": evidence, "broker": broker.snapshot(), "agents": {},
                     "timing_origin": {"early_checkpoint_sha256": digest(checkpoint),
                                       "additional_paired_rounds": ADDITIONAL_ROUNDS}}
            for a, agent in agents.items():
                env = envs[a]
                manifest = env.workspace_manifest()
                image = env.snapshot_image()
                snapshots.append(image)
                saved["agents"][a] = {"state": save_state(agent), "git_base": connectors[a]._base_sha,
                                      "manifest": manifest, "image": image}
            # Publish the archive and checkpoint only if snapshot work fits the horizon.
            pending = run_dir / "shared_git.pending.tar"
            backup_volume(volume, pending, expected["Id"], label)
            if time.monotonic() >= deadline:
                pending.unlink(missing_ok=True)
                evidence.update(eligible=False, stop_reason="deadline_during_snapshot")
                write_json(phase_dir / "progress.json", evidence)
                result["outcomes"] = {a: {**o, "status": "checkpoint_not_reached"} for a, o in outcomes.items()}
                write_json(phase_dir / "agent_outcomes.json", result["outcomes"])
            else:
                saved["git_archive_sha256"] = file_hash(pending)
                validate_checkpoint(saved, task)
                pending.replace(run_dir / "shared_git.tar")
                write_json(run_dir / "checkpoint.json", saved)
                result["checkpoint_sha256"] = digest(saved)
        result.update(status="completed" if evidence["eligible"] else "checkpoint_not_reached",
                      stop_reason=evidence["stop_reason"], snapshot_seconds=round(time.monotonic() - drive_end, 3))
        exit_code = 0
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__, error=safe_error(exc),
                      infrastructure_error=True)
    finally:
        execution_end = time.monotonic()
        if result["status"] == "completed" and execution_end >= deadline:
            # Include final validation/serialization in the same fixed horizon.
            (run_dir / "checkpoint.json").unlink(missing_ok=True)
            (run_dir / "shared_git.tar").unlink(missing_ok=True)
            result.pop("checkpoint_sha256", None)
            evidence.update(eligible=False, stop_reason="deadline_during_snapshot")
            result.update(status="checkpoint_not_reached", stop_reason=evidence["stop_reason"],
                          outcomes={a: {**o, "status": "checkpoint_not_reached"}
                                    for a, o in result["outcomes"].items()})
            write_json(phase_dir / "progress.json", evidence)
            write_json(phase_dir / "agent_outcomes.json", result["outcomes"])
        result.update(extension_elapsed_seconds=round(execution_end - started, 3),
                      elapsed_seconds=round(early_elapsed + execution_end - started, 3))
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
        if dc:
            if result["status"] != "completed":
                for image in snapshots:
                    try:
                        dc.images.remove(image)
                    except Exception as exc:
                        cleanup_errors.append("unused snapshot cleanup: " + safe_error(exc))
            if created_volume:
                try:
                    dc.volumes.get(volume).remove()
                except Exception as exc:
                    cleanup_errors.append("volume cleanup: " + safe_error(exc))
            try:
                dc.close()
            except Exception as exc:
                cleanup_errors.append("Docker client close: " + safe_error(exc))
        try:
            if any(file_hash(p) != expected_hash for p, expected_hash in early_files.items()):
                raise RuntimeError("Immutable early prefix evidence changed during extension")
        except Exception as exc:
            result.update(status="error", infrastructure_error=True, error=safe_error(exc))
            exit_code = 2
        if cleanup_errors:
            result.update(cleanup_errors=cleanup_errors, infrastructure_error=True)
            exit_code = 2
        result["cleanup_seconds"] = round(time.monotonic() - execution_end, 3)
        write_json(phase_dir / "usage_summary.json", usage(run_dir, "prefix"))
        write_json(phase_dir / "status.json", result)
        paths = [p for p in phase_dir.rglob("*") if p.is_file()
                 and p.name not in ("execution_receipt.json", "console.log")]
        paths += [run_dir / n for n in ("task_config.json", "usage_ledger.json", "checkpoint.json", "shared_git.tar")
                  if (run_dir / n).is_file()]
        write_json(phase_dir / "execution_receipt.json", {
            "schema": "luna_fixed_timing_prefix_receipt_v1", "status": result["status"],
            "early_files_sha256": early_files, "new_ledger_scope": "extension_only",
            "protected_files_sha256": {str(p.relative_to(run_dir)): file_hash(p) for p in sorted(paths)}})
    return exit_code
