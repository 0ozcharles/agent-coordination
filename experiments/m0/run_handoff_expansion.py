"""Three predeclared repositories: one prefix, three SS/C/Solo repeats each."""
import argparse
import fcntl
import hashlib
import importlib.util
import math
import os
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime, timezone

from budget_guard import write_json
from cross_task_specs import COMMIT, REPO, ROOT, TASKS, get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_repeats import archive_failed_job, read_json, run_command
from run_solo_smoke import MAX_OUTPUT, MODEL, configure
from run_task_expansion import add_usage, file_hash, write_fixed
from wip_checkpoint import validate_checkpoint

RUNS = ROOT / "artifacts/handoff_expansion_runs"
SELECTION = ROOT / "task_handoff_expansion_selection.json"
REFERENCE_PROOF = ROOT / "artifacts/cross_task_reference_validation.json"
OFFLINE_PROOF = ROOT / "artifacts/handoff_expansion_offline_validation.json"
CONDITIONS = ("strongshare", "consolidate", "solo")
ORDERS = (CONDITIONS, ("consolidate", "solo", "strongshare"), ("solo", "strongshare", "consolidate"))
CONTINUATION_SECONDS = 1200
VARIANT = "source_handoff_diff_v1"
SOURCE_FILES = (
    "run_handoff_expansion.py", "run_handoff_expansion_with_together_key.sh", "cross_task_specs.py",
    "cross_task_workers.py", "cross_task_packet.py", "cross_task_evidence.py", "evaluate_cross_task.py",
    "preflight_cross_tasks.py", "preflight_task_candidates.py", "task_handoff_expansion_selection.json",
    "task_specs.py", "wip_checkpoint.py", "handoff_worker.py", "handoff_packet.py",
    "solo_expansion_worker.py", "baseline_runtime.py", "baseline_model.py", "baseline_evidence.py",
    "budget_guard.py", "run_baselines.py", "run_consolidate.py", "run_repeats.py",
    "run_solo_smoke.py", "run_task_expansion.py", "run_handoff_pilot.py", "run_solo_expansion.py",
    "evaluate_task_expansion.py", "evaluate_solo_expansion.py", "evaluate_solo_smoke.py",
)


def verify_selection():
    selection = read_json(SELECTION)
    if selection.get("dataset_commit") != COMMIT:
        raise RuntimeError("Selection dataset commit changed")
    selected = selection.get("selected_tasks", [])
    if len(selected) != len(TASKS) or {t.get("task_key") for t in selected} != set(TASKS):
        raise RuntimeError("Selection and registry task identities differ")
    for item in selected:
        task = get_task(item["task_key"])
        expected = {"repo": task.repo, "task_id": task.task_id, "features": list(task.features),
                    "base_commit": task.base, "image": task.image, "code_roots": list(task.code_roots)}
        if any(item.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Selection differs from registry: " + task.key)
        evidence = item.get("features_evidence", [])
        if len(evidence) != 2 or {f.get("feature") for f in evidence} != set(task.features):
            raise RuntimeError("Selection feature evidence is incomplete")
        for feature in evidence:
            path = task.task_dir / f"feature{feature['feature']}" / "feature.md"
            if (feature.get("path") != str(path.relative_to(ROOT))
                    or feature.get("sha256") != file_hash(path)):
                raise RuntimeError("Selected original requirement changed")
        environment = item.get("environment", {})
        dockerfile = task.task_dir / "Dockerfile"
        if (environment.get("dockerfile") != str(dockerfile.relative_to(ROOT))
                or environment.get("dockerfile_sha256") != file_hash(dockerfile)):
            raise RuntimeError("Selected original Dockerfile changed")
    return selection


def verify_references():
    verify_selection()
    proof = read_json(REFERENCE_PROOF)
    if (not proof.get("passed") or proof.get("model_api_calls") != 0
            or proof.get("cooperbench_commit") != COMMIT
            or proof.get("reference_data_exposed_to_agents") is not False
            or set(proof.get("tasks", {})) != set(TASKS)):
        raise RuntimeError("Complete all three reference/environment checks first")
    for key, task in TASKS.items():
        row = proof["tasks"][key]
        if (row.get("task") != task.as_dict()
                or row.get("image_id") != read_json(task.image_record_path).get("Id")
                or not all(row.get(k) for k in ("base_fails_both", "individual_gold_passes_both", "combined_gold_passes_both"))):
            raise RuntimeError("Reference proof is stale for " + key)
    for section in [proof, *proof["tasks"].values()]:
        for name, expected in section.get("source_hashes", {}).items():
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT) or file_hash(path) != expected:
                raise RuntimeError("Reference proof source changed")
    return proof


def source_manifest():
    paths = [ROOT / name for name in SOURCE_FILES] + [REFERENCE_PROOF, REPO / "src/cooperbench/utils.py"]
    for extension in ("*.py", "*.sh", "*.yaml"):
        paths += sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2").rglob(extension))
    paths += sorted((REPO / "src/cooperbench/eval").rglob("*.py"))
    for task in TASKS.values():
        paths += [task.image_record_path, task.task_dir / "runner.sh", task.task_dir / "Dockerfile"]
        paths += [task.task_dir / f"feature{f}" / name for f in task.features for name in ("feature.md", "tests.patch")]
    return {str(path.relative_to(ROOT)): file_hash(path) for path in sorted(set(paths))}


def protocol_for(offline=False):
    reference = verify_references()
    orders = ORDERS[:1] if offline else ORDERS
    return {
        "version": "cross_repo_handoff_v1", "condition_version": VARIANT, "offline_selftest": offline,
        "tasks": {key: task.as_dict() for key, task in TASKS.items()},
        "jobs": [{"task": key, "repeat": repeat, "condition": condition}
                 for key in TASKS for repeat, order in enumerate(orders, 1) for condition in order],
        "planned_formal_jobs": 27, "orders": [list(order) for order in orders],
        "model": "offline-scripted-model" if offline else MODEL,
        "provider": "Together", "model_revision": "Provider serverless model identifier; an immutable weights revision is not supplied by this endpoint.",
        "temperature": 0.7, "top_p": 0.8, "enable_thinking": False, "seed": None,
        "max_output_tokens": MAX_OUTPUT, "money_limit": None, "request_limit": None,
        "transport_retries": "At most one retry for HTTP 5xx; every transport attempt remains in its usage ledger. No retry for timeout, 4xx or parse/usage errors.",
        "prefix_horizon_seconds": 1200, "continuation_seconds": CONTINUATION_SECONDS,
        "checkpoint_rule": "Frozen v1: first paired completed round with Python AST differences by both unfinished agents. Comments/formatting are excluded; docstring AST differences can count. No score-based selection or second checkpoint.",
        "missing_checkpoint": "Record checkpoint_not_reached, skip six SS/C jobs without resampling, and retain three fresh Solo runs as feasibility-only results.",
        "handoff": "Schema-2 complete unified diff, original tasks and eligible scratch text from the common checkpoint; same packet for SS and C; no truncation or automatic code transfer.",
        "readonly_evidence_scope": "Peer evidence worktree contains only the registered task.code_roots, aligned with the diff packet source scope; it is not a full-repository mirror. Original task, complete model/tool history and eligible temporary files remain available.",
        "time_rule": "SS/C retain 1200 seconds after restore/handoff; measured remaining time includes all restore and handoff work. Fresh Solo allowance is realized prefix elapsed + 1200 seconds including initialization. Evaluation measured separately.",
        "primary_comparison": "SS versus C after the same checkpoint: official success and remaining execution cost/time including handoff and incomplete attempts. Unknown charges remain unknown.",
        "full_task_comparison": "SS/C accepted prefix plus remaining execution; Solo starts fresh and has no prefix charge. Solo success/full-task cost is an independent comparison, not a requirement that C beat Solo on every task.",
        "investment_gate": {"minimum_task_groups": 2, "per_task_requirements": [
            "C official success count >= SS official success count", "C has at least one official success",
            "C mean measured remaining cost is strictly lower, including failed requests/attempts; unknown usage cannot be treated as zero",
            "Trace review confirms concrete use of peer work; packet delivery alone is insufficient"],
            "interpretation": "Three task groups, not 27 independent tasks. Numerical checks are reported without precommitting a positive conclusion; trace evidence requires separate review."},
        "task_failure": "Keep completed outcomes and do not retry for success.",
        "infrastructure_failure": "Pause and retain all charges; explicit resume retries only incomplete stages. Completed models and validated scores never repeat.",
        "actual_cost": "Every newly collected prefix is counted once, plus all job ledgers including failed transports and incomplete attempts. Comparison columns are not billing totals.",
        "selection_sha256": file_hash(SELECTION), "reference_validation": reference,
        "source_manifest": source_manifest(),
    }


def verify_preflight():
    verify_references()
    proof = read_json(OFFLINE_PROOF, {})
    if (not proof.get("passed") or proof.get("model_api_calls") != 0
            or proof.get("prefixes_completed") != 3 or proof.get("jobs_completed") != 9
            or proof.get("source_manifest") != source_manifest()
            or proof.get("task_keys") != list(TASKS)):
        raise RuntimeError("Complete three prefixes and nine offline condition/evaluator checks for this exact source first")


def configure_run(run_dir, task, condition, protocol, seconds=1200, checkpoint_sha=None):
    config = {"task_key": task.key, "task": task.as_dict(), "condition": condition,
              "condition_version": VARIANT, "offline_selftest": protocol["offline_selftest"],
              "protocol_sha256": digest(protocol), "execution_seconds": seconds}
    if condition == "solo":
        config["fresh_solo"] = True
    elif checkpoint_sha is not None:
        config["checkpoint_sha256"] = checkpoint_sha
    run_dir.mkdir(parents=True, exist_ok=True)
    write_fixed(run_dir / "task_config.json", config)


def prefix_record(prefix_dir, task):
    status = read_json(prefix_dir / "prefix/status.json")
    elapsed = status.get("elapsed_seconds")
    if (status.get("status") not in ("completed", "checkpoint_not_reached")
            or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed) or not 0 < elapsed <= 1500):
        raise RuntimeError("Prefix is incomplete or has invalid timing")
    files = [prefix_dir / "task_config.json", prefix_dir / "prefix/status.json",
             prefix_dir / "prefix/source_manifest.json"]
    checkpoint_sha = None
    if status["status"] == "completed":
        checkpoint = read_json(prefix_dir / "checkpoint.json")
        validate_checkpoint(checkpoint, task)
        checkpoint_sha = digest(checkpoint)
        if file_hash(prefix_dir / "shared_git.tar") != checkpoint["git_archive_sha256"]:
            raise RuntimeError("Prefix shared Git archive changed")
        files += [prefix_dir / "checkpoint.json", prefix_dir / "shared_git.tar"]
    if (prefix_dir / "usage_ledger.json").exists():
        files.append(prefix_dir / "usage_ledger.json")
    record = {"task": task.as_dict(), "status": status["status"], "checkpoint_sha256": checkpoint_sha,
              "elapsed_seconds": elapsed, "accepted_usage": usage(prefix_dir, "prefix"),
              "all_attempt_usage": usage(prefix_dir),
              "files_sha256": {str(p.relative_to(prefix_dir)): file_hash(p) for p in files}}
    write_fixed(prefix_dir.parent / "prefix_record.json", record)
    return record


def copy_checkpoint(prefix_dir, run_dir, task, record):
    if prefix_record(prefix_dir, task) != record or record["status"] != "completed":
        raise RuntimeError("Common checkpoint is unavailable or changed")
    for name in ("checkpoint.json", "shared_git.tar"):
        data = (prefix_dir / name).read_bytes()
        target = run_dir / name
        if target.exists() and (target.is_symlink() or target.read_bytes() != data):
            raise RuntimeError("Branch checkpoint input changed")
        if not target.exists():
            target.write_bytes(data)
    write_fixed(run_dir / "checkpoint_provenance.json", {
        "source_directory": str(prefix_dir), "checkpoint_sha256": record["checkpoint_sha256"],
        "prefix_usage": record["accepted_usage"], "prefix_elapsed_seconds": record["elapsed_seconds"],
    })


def verify_handoff(phase_dir, task, condition, status):
    from cross_task_packet import packet_message
    expected = digest(read_json(phase_dir.parent / "checkpoint.json"))
    packet = read_json(phase_dir / "handoff_packet.json")
    packet_hash = digest(packet)
    if (packet.get("schema_version") != 2 or packet.get("checkpoint_sha256") != expected
            or packet.get("task_key") != task.key or packet.get("base_commit") != task.base
            or packet.get("checkpoint_only") is not True or set(packet.get("agents", {})) != set(task.agents)):
        raise RuntimeError("Handoff diff packet does not describe the common task checkpoint")
    text = packet_message(packet)
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    if ((phase_dir / "handoff_packet.txt").read_text() != text
            or status.get("handoff_variant") != VARIANT or status.get("handoff_packet_sha256") != packet_hash):
        raise RuntimeError("Handoff variant, text or packet digest changed")
    active = ("agent1",) if condition == "consolidate" else tuple(task.agents)
    delivery = read_json(phase_dir / "handoff_delivery.json")
    if (delivery.get("condition") != condition or delivery.get("checkpoint_sha256") != expected
            or delivery.get("packet_sha256") != packet_hash or delivery.get("source_checkpoint_verified") is not True
            or delivery.get("code_automatically_transferred") is not False or delivery.get("first_request_verified") is not True
            or set(delivery.get("recipients", {})) != set(active)):
        raise RuntimeError("Incomplete handoff delivery")
    for agent in active:
        recipient = delivery["recipients"][agent]
        first = read_json(phase_dir / f"first_request_{agent}.json")
        if (recipient.get("packet_sha256") != packet_hash or recipient.get("message_sha256") != text_hash
                or recipient.get("history_preserved") is not True or recipient.get("all_packet_content_delivered") is not True
                or recipient.get("role") != "user" or type(recipient.get("message_index")) is not int
                or recipient["message_index"] < 1 or first.get("recipient") != agent or first.get("verified") is not True
                or first.get("full_packet_present") is not True or first.get("message_sha256") != text_hash):
            raise RuntimeError("An active agent lacks a complete verified first handoff query")
        if not status["offline_selftest"]:
            inputs = sorted((phase_dir / "responses" / agent).glob("request_*_input.json"))
            if not inputs or first.get("actual_transport_input_verified") is not True:
                raise RuntimeError("Actual first provider input is unverified")
            messages = read_json(inputs[0])["request"]["messages"]
            if sum(m.get("role") == "user" and m.get("content") == text for m in messages) != 1:
                raise RuntimeError("Actual first provider input differs from complete handoff text")
    if condition == "consolidate" and status.get("retired_agent_model_calls") != 0:
        raise RuntimeError("Retired agent received a model call")
    return packet_hash


def verify_execution_receipt(phase_dir, task, condition):
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") != "completed" or status.get("task") != task.as_dict():
        raise RuntimeError("Execution is incomplete or belongs to another task")
    if condition != "solo":
        # Reuse frozen restore/receipt checks in an isolated module namespace;
        # the old pilot's globals and results are never modified.
        spec = importlib.util.spec_from_file_location("_cross_receipt", ROOT / "run_handoff_pilot.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        verifier.TASK = task
        verifier.SOURCE_CHECKPOINT = digest(read_json(phase_dir.parent / "checkpoint.json"))
        verifier.verify_handoff = verify_handoff
        receipt = verifier.verify_execution_receipt(phase_dir, task, condition)
        write_fixed(phase_dir.parent.parent.parent / "packet_record.json", {
            "checkpoint_sha256": verifier.SOURCE_CHECKPOINT, "handoff_packet_sha256": receipt["handoff_packet_sha256"]})
        return receipt
    config = read_json(phase_dir.parent / "task_config.json")
    initial = read_json(phase_dir / "initial_state.json")
    if status.get("fresh_solo") is not True or config.get("fresh_solo") is not True or initial.get("fresh_solo") is not True:
        raise RuntimeError("Solo is missing fresh-state proof")
    forbidden = ("checkpoint", "restore_proof", "intervention", "handoff_packet", "handoff_delivery")
    if any(any(token in p.name for token in forbidden) for p in phase_dir.parent.rglob("*")):
        raise RuntimeError("Fresh Solo contains inherited handoff artifacts")
    if any(token in key for key in config for token in forbidden):
        raise RuntimeError("Fresh Solo config declares inherited state")
    hashes = {"final.patch": file_hash(phase_dir / "final.patch")}
    if status.get("final_patch_sha256") != hashes["final.patch"]:
        raise RuntimeError("Solo published patch differs from status")
    paths = [phase_dir.parent / "task_config.json"] + [phase_dir / name for name in (
        "status.json", "source_manifest.json", "initial_state.json", "solo.traj.json")]
    receipt = {"patch_sha256": hashes, "protected_files_sha256": {
        str(p.relative_to(phase_dir.parent)): file_hash(p) for p in paths}}
    write_fixed(phase_dir / "execution_receipt.json", receipt)
    return receipt


def archive_incomplete(run_dir, condition):
    before = set((run_dir / "failed_attempts").glob("*"))
    archive_failed_job(run_dir, condition)
    if condition == "prefix":
        leftovers = [run_dir / name for name in ("checkpoint.json", "shared_git.tar") if (run_dir / name).exists()]
        if leftovers:
            new = set((run_dir / "failed_attempts").glob("*")) - before
            if len(new) != 1:
                raise RuntimeError("Orphaned prefix artifacts require inspection before resuming")
            saved = next(iter(new)) / "prefix_artifacts"
            saved.mkdir()
            for path in leftovers:
                shutil.move(str(path), str(saved / path.name))


def run_model(run_dir, task, condition, protocol, lock_fd):
    phase = run_dir / condition
    config = read_json(run_dir / "task_config.json")
    status = read_json(phase / "status.json", {})
    complete = status.get("status") == "completed" or (condition == "prefix" and status.get("status") == "checkpoint_not_reached")
    if complete:
        prefix_record(run_dir, task) if condition == "prefix" else verify_execution_receipt(phase, task, condition)
        cleanup_phase(run_dir, condition)
        return status
    if not protocol["offline_selftest"] and not os.environ.get("TOGETHER_API_KEY", "").strip():
        raise RuntimeError("TOGETHER_API_KEY absent; use the handoff expansion helper")
    archive_incomplete(run_dir, condition)
    phase.mkdir()
    write_json(phase / "source_manifest.json", protocol["source_manifest"])
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", "--run-dir", str(run_dir),
               "--condition", condition, "--task", task.key]
    if protocol["offline_selftest"]:
        command.append("--offline-selftest")
    print(task.key + ": " + run_dir.name + "/" + condition, flush=True)
    try:
        code = run_command(command, phase / "console.log", config["execution_seconds"] + 300, lock_fd)
    finally:
        cleanup_phase(run_dir, condition)
    status = read_json(phase / "status.json", {})
    if code or status.get("status") not in (("completed", "checkpoint_not_reached") if condition == "prefix" else ("completed",)):
        raise RuntimeError("Execution paused: " + str(phase))
    prefix_record(run_dir, task) if condition == "prefix" else verify_execution_receipt(phase, task, condition)
    return status


def evaluate(run_dir, task, condition, lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase = run_dir / condition
    before = verify_execution_receipt(phase, task, condition)
    code = run_command([sys.executable, str(ROOT / "evaluate_cross_task.py"), "--run-dir", str(phase)],
                       phase / "evaluation_console.log", 720, lock_fd, env=evaluator_environment())
    score = read_json(phase / "evaluation_summary.json", {})
    if code or not score.get("evaluation_completed"):
        raise RuntimeError("Evaluation paused; completed model execution will not repeat")
    if before != verify_execution_receipt(phase, task, condition):
        raise RuntimeError("Execution artifacts changed during evaluation")
    return score


def incomplete_seconds(run_dir, condition):
    attempts = [p for p in (run_dir / "failed_attempts").glob(condition + "_*") if p.is_dir()]
    values = [read_json(p / "status.json", {}).get("elapsed_seconds") for p in attempts]
    valid = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    return {"known_seconds": sum(v for v in values if valid(v)),
            "unknown_attempts": sum(not valid(v) for v in values)}


def build_row(run_dir, task, repeat, condition, record, execution, score):
    accepted, all_attempts = usage(run_dir, condition), usage(run_dir)
    failed_time = incomplete_seconds(run_dir, condition)
    fresh = condition == "solo"
    prefix_cost = 0 if fresh else record["accepted_usage"]["known_cost_usd"]
    prefix_unknown = 0 if fresh else record["accepted_usage"]["unknown_usage_requests"]
    prefix_time = 0 if fresh else record["elapsed_seconds"]
    return {"task": task.key, "repeat": repeat, "condition": condition, "condition_version": VARIANT,
        "run_directory": str(run_dir), "fresh_solo": fresh,
        "matched_checkpoint_comparison": record["status"] == "completed", "official_success": bool(score["both_passed"]),
        "execution": execution, "evaluation": score, "accepted_usage": accepted, "all_attempt_usage": all_attempts,
        "remaining_known_cost_usd": None if fresh else all_attempts["known_cost_usd"],
        "remaining_unknown_usage_requests": None if fresh else all_attempts["unknown_usage_requests"],
        "remaining_execution_known_seconds": None if fresh else round(execution["elapsed_seconds"] + failed_time["known_seconds"], 3),
        "execution_time_unknown_attempts": failed_time["unknown_attempts"],
        "full_task_equivalent_known_cost_usd": round(prefix_cost + all_attempts["known_cost_usd"], 9),
        "full_task_equivalent_unknown_usage_requests": prefix_unknown + all_attempts["unknown_usage_requests"],
        "comparison_known_seconds": round(prefix_time + execution["elapsed_seconds"] + failed_time["known_seconds"] + score["elapsed_seconds"], 3),
        "trace_peer_work_use": "requires independent trace review" if not fresh else "not applicable"}


def investment_gate(rows, repeats):
    tasks = {}
    for key in TASKS:
        groups = {c: [r for r in rows if r["task"] == key and r["condition"] == c] for c in ("strongshare", "consolidate")}
        complete = all(len(group) == repeats for group in groups.values())
        successes = {c: sum(r["official_success"] for r in group) for c, group in groups.items()}
        unknown = {c: sum(r["remaining_unknown_usage_requests"] for r in group) for c, group in groups.items()}
        means = {c: (round(sum(r["remaining_known_cost_usd"] for r in group) / len(group), 9) if group else None)
                 for c, group in groups.items()}
        numerical = (successes["consolidate"] >= successes["strongshare"] and successes["consolidate"] >= 1
                     and means["consolidate"] < means["strongshare"]) if complete and not any(unknown.values()) else None
        tasks[key] = {"complete": complete, "successes": successes, "mean_remaining_known_cost_usd": means,
                      "unknown_usage_requests": unknown, "numerical_requirements_met": numerical,
                      "peer_work_reuse": "requires trace review; packet delivery is not sufficient"}
    return {"tasks": tasks, "numerically_qualifying_task_groups": sum(v["numerical_requirements_met"] is True for v in tasks.values()),
            "minimum_task_groups": 2, "decision": "pending trace review and resolution of any unknown-cost comparisons"}


def summarize(suite, protocol, rows, skipped):
    ledgers = list(suite.glob("*/prefix/usage_ledger.json")) + list(suite.glob("*/jobs/*/usage_ledger.json")) + list(suite.glob("*/solo_jobs/*/usage_ledger.json"))
    return {"pipeline_completed": len(rows) + len(skipped) == len(protocol["jobs"]), "protocol_sha256": digest(protocol),
        "offline_selftest": protocol["offline_selftest"], "condition_version": VARIANT,
        "planned_jobs": len(protocol["jobs"]), "rows": rows, "skipped_jobs": skipped,
        "actual_new_usage": add_usage([usage(p.parent) for p in ledgers]),
        "investment_gate": investment_gate(rows, len(protocol["orders"])),
        "interpretation": "Three predeclared task groups. Earlier full-source-packet pilots remain separate. New prefix costs count once in actual spend; full-task comparison columns are not billing totals."}


def supervise(suite, offline=False):
    configure()
    suite.mkdir(parents=True, exist_ok=True)
    with (ROOT / "artifacts/model_run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not offline:
            verify_preflight()
        protocol = protocol_for(offline)
        write_fixed(suite / "protocol.json", protocol)
        for name, expected in protocol["source_manifest"].items():
            destination = suite / "source_snapshot" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and file_hash(destination) != expected:
                raise RuntimeError("Saved source snapshot differs from protocol")
            if not destination.exists():
                destination.write_bytes((ROOT / name).read_bytes())
        rows, skipped, prefixes = [], [], {}
        try:
            for task in TASKS.values():
                if source_manifest() != protocol["source_manifest"]:
                    raise RuntimeError("Source changed before prefix")
                prefix = suite / task.key / "prefix"
                configure_run(prefix, task, "prefix", protocol)
                write_json(suite / "status.json", {"status": "running", "task": task.key, "stage": "prefix",
                           "completed_jobs": len(rows), "skipped_jobs": len(skipped), "planned_jobs": len(protocol["jobs"])})
                run_model(prefix, task, "prefix", protocol, lock.fileno())
                record = prefix_record(prefix, task)
                prefixes[task.key] = record
                for job in [j for j in protocol["jobs"] if j["task"] == task.key]:
                    condition, repeat = job["condition"], job["repeat"]
                    if condition != "solo" and record["status"] == "checkpoint_not_reached":
                        skipped.append({**job, "reason": "checkpoint_not_reached; no resampling"})
                        continue
                    if source_manifest() != protocol["source_manifest"]:
                        raise RuntimeError("Source changed during execution")
                    fresh = condition == "solo"
                    run = suite / task.key / ("solo_jobs" if fresh else "jobs") / f"{task.key}_r{repeat:02d}_{condition}"
                    seconds = round(record["elapsed_seconds"] + 1200, 3) if fresh else 1200
                    configure_run(run, task, condition, protocol, seconds, None if fresh else record["checkpoint_sha256"])
                    if not fresh:
                        copy_checkpoint(prefix, run, task, record)
                    write_json(suite / "status.json", {"status": "running", "task": task.key, "stage": condition, "repeat": repeat,
                               "completed_jobs": len(rows), "skipped_jobs": len(skipped), "planned_jobs": len(protocol["jobs"])})
                    execution = run_model(run, task, condition, protocol, lock.fileno())
                    score = evaluate(run, task, condition, lock.fileno())
                    row = build_row(run, task, repeat, condition, record, execution, score)
                    rows.append(row)
                    write_json(run / "result.json", row)
                    write_json(suite / "summary.json", summarize(suite, protocol, rows, skipped))
                    print(f"{task.key} {condition} r{repeat}: official={row['official_success']}, known cost=${row['all_attempt_usage']['known_cost_usd']:.4f}", flush=True)
            if source_manifest() != protocol["source_manifest"]:
                raise RuntimeError("Source changed during final stage")
            summary = summarize(suite, protocol, rows, skipped)
            write_json(suite / "summary.json", summary)
            if offline:
                if len(rows) != 9 or skipped or summary["actual_new_usage"]["requests"] or len(prefixes) != 3:
                    raise RuntimeError("Offline proof requires all three prefixes and nine scored jobs, zero API")
                for row in rows:
                    execution = row["execution"]
                    outcomes = [execution["outcome"]] if row["condition"] in ("consolidate", "solo") else [o["status"] for o in execution["outcomes"].values()]
                    if any(value != "Submitted" for value in outcomes):
                        raise RuntimeError("Offline synthetic submission did not complete")
                    verify_execution_receipt(Path(row["run_directory"]) / row["condition"], get_task(row["task"]), row["condition"])
                write_json(OFFLINE_PROOF, {"passed": True, "model_api_calls": 0, "prefixes_completed": 3,
                    "jobs_completed": 9, "task_keys": list(TASKS), "source_manifest": protocol["source_manifest"], "suite_directory": str(suite)})
            write_json(suite / "status.json", {"status": "completed", "completed_jobs": len(rows), "skipped_jobs": len(skipped)})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / "summary.json", summarize(suite, protocol, rows, skipped))
            write_json(suite / "status.json", {"status": "paused", "completed_jobs": len(rows), "skipped_jobs": len(skipped), "error": safe_error(exc)})
            print("Cross-task handoff paused: " + safe_error(exc), flush=True)
            return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--condition", choices=("prefix", *CONDITIONS))
    parser.add_argument("--task", choices=tuple(TASKS))
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        from cross_task_workers import phase_worker
        task, run = get_task(args.task), args.run_dir.resolve()
        parts = run.relative_to(RUNS.resolve()).parts
        prefix = args.condition == "prefix"
        expected_folder = "solo_jobs" if args.condition == "solo" else "jobs"
        if (len(parts) != (3 if prefix else 4) or parts[1] != task.key
                or parts[2] != ("prefix" if prefix else expected_folder)):
            raise ValueError("Invalid cross-task worker path")
        suite = RUNS / parts[0]
        protocol = read_json(suite / "protocol.json")
        config = read_json(run / "task_config.json")
        if (config.get("task") != task.as_dict() or config.get("condition") != args.condition
                or config.get("condition_version") != VARIANT or config.get("offline_selftest") != args.offline_selftest
                or config.get("protocol_sha256") != digest(protocol) or protocol.get("source_manifest") != source_manifest()):
            raise ValueError("Cross-task worker config/source mismatch")
        if args.condition == "solo":
            record = prefix_record(suite / task.key / "prefix", task)
            if config.get("fresh_solo") is not True or config["execution_seconds"] != round(record["elapsed_seconds"] + 1200, 3):
                raise ValueError("Fresh Solo allowance differs from realized prefix")
        elif config["execution_seconds"] != 1200:
            raise ValueError("Prefix/handoff allowance must remain 1200 seconds")
        if args.condition in ("strongshare", "consolidate"):
            record = prefix_record(suite / task.key / "prefix", task)
            copy_checkpoint(suite / task.key / "prefix", run, task, record)
            if config.get("checkpoint_sha256") != record["checkpoint_sha256"]:
                raise ValueError("Continuation checkpoint differs from frozen common prefix")
        return phase_worker(run, args.condition, task, offline=args.offline_selftest, execution_seconds=config["execution_seconds"])
    default = "cross_handoff_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else "cross_handoff_v1"
    suite = (args.suite_dir or RUNS / default).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r"cross_handoff_[A-Za-z0-9_]+", suite.name):
        raise ValueError("Invalid cross-task suite directory")
    return supervise(suite, args.offline_selftest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + safe_error(exc), file=sys.stderr)
        sys.exit(2)
