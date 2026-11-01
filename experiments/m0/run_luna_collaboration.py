"""Two Luna checkpoints and eight budget-aligned SS/C continuations.

Existing Luna Solo runs remain historical controls. Old source files/results are
never modified; this supervisor freezes its own workers and evaluator adapter.
"""
import argparse
import fcntl
import math
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

from budget_guard import write_json
from cross_task_specs import ROOT, COMMIT, get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import (
    archive_incomplete, build_row, copy_checkpoint, incomplete_seconds,
    prefix_record, verify_execution_receipt, verify_references,
    source_manifest as original_sources,
)
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed

RUNS = ROOT / "artifacts/handoff_expansion_runs"
SOLO = RUNS / "luna_solo_v1"
PROOF = ROOT / "artifacts/luna_collaboration_offline_validation.json"
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
CONDITIONS = ("strongshare", "consolidate")
ORDERS = (CONDITIONS, tuple(reversed(CONDITIONS)))
VERSION = "luna_collaboration_v1"
VARIANT = "source_handoff_diff_v1"
MODEL = "gpt-6-luna"
PREFIX_SECONDS = 1200
EXHAUSTED = "time_budget_exhausted_before_first_query"
EXHAUSTED_STATES = (EXHAUSTED, "budget_exhausted")
NEW_SOURCES = (
    "run_luna_collaboration.py", "luna_collaboration_worker.py",
    "luna_handoff_worker.py", "luna_collaboration_evaluator.py",
    "luna_model.py", "luna_accounting.py", "pillow_metadata_probe_v1.py",
)


def solo_reference():
    protocol = read_json(SOLO / "protocol.json")
    if (protocol.get("model") != MODEL or protocol.get("reasoning_effort") != "none"
            or protocol.get("offline_selftest") is not False):
        raise ValueError("Historical Luna Solo protocol differs")
    expected = dict(zip(TASK_KEYS, (1466.261, 1269.899)))
    paths = [SOLO / "protocol.json", SOLO / "summary.json"]
    for key in TASK_KEYS:
        for repeat in (1, 2):
            run = SOLO / key / "solo_jobs" / f"{key}_r{repeat:02d}_solo"
            config, result = read_json(run / "task_config.json"), read_json(run / "result.json")
            if (config.get("task") != get_task(key).as_dict() or config.get("fresh_solo") is not True
                    or config.get("execution_seconds") != expected[key]
                    or result.get("model") != MODEL or result.get("condition") != "solo"
                    or result.get("repeat") != repeat or result.get("task") != key
                    or not result.get("evaluation", {}).get("evaluation_completed")):
                raise ValueError("Historical Luna Solo result differs: " + key)
            paths += [run / "task_config.json", run / "result.json", run / "solo/execution_receipt.json"]
    return {"directory": str(SOLO), "total_execution_seconds": expected,
            "files_sha256": {str(p.relative_to(ROOT)): file_hash(p) for p in paths},
            "interpretation": "Budget-aligned historical fresh-Solo reference; not a concurrent randomized comparison and not resumed from these checkpoints."}


def source_manifest():
    result = original_sources()
    result.update({name: file_hash(ROOT / name) for name in NEW_SOURCES})
    return result


def protocol_for(offline=False):
    verify_references()
    solo = solo_reference()
    settings = read_json(SOLO / "protocol.json")
    fields = ("provider", "endpoint", "reasoning_effort", "temperature", "top_p",
              "max_completion_tokens", "service_tier", "prompt_cache_options",
              "context_window_tokens", "pricing_usd_per_million")
    orders = ORDERS[:1] if offline else ORDERS
    return {
        "version": VERSION, "condition_version": VARIANT, "dataset_commit": COMMIT,
        "offline_selftest": offline, "model": "offline-scripted-model" if offline else MODEL,
        **{name: settings[name] for name in fields},
        "tasks": {key: get_task(key).as_dict() for key in TASK_KEYS},
        "jobs": [{"task": key, "repeat": repeat, "condition": condition}
                 for key in TASK_KEYS for repeat, order in enumerate(orders, 1) for condition in order],
        "orders": [list(order) for order in orders], "solo_reference": solo,
        "prefix_horizon_seconds": PREFIX_SECONDS,
        "checkpoint_rule": "First paired completed round with source Python AST changes by both unfinished agents. Frozen wip_checkpoint rule; no score-based choice or checkpoint resampling.",
        "checkpoint_origin": "One freshly generated Luna prefix per task in this suite. No Qwen or Solo code/history imported.",
        "time_rule": "Remaining seconds = historical Solo total allowance minus accepted new prefix elapsed seconds. SS/C use the same remaining allowance, including restore and handoff. No new step begins after the deadline; in-flight model/tool calls may finish later and actual time remains recorded.",
        "exhausted_budget": "No checkpoint or nonpositive remaining time skips dependent jobs without resampling. Setup exhausting the allowance before the first query is a terminal skipped job, not an API retry.",
        "money_limit": None, "request_limit": None,
        "handoff": "Same schema-2 full source-diff packet and original history for SS/C; source-scoped read-only peer files. SS retains two agents; C retires agent2. No automatic copying or repair of code.",
        "primary_comparison": "StrongShare versus Consolidate at the identical Luna checkpoint; official success and remaining cost/time. Two task groups, one checkpoint each, two repeats per condition.",
        "solo_comparison": solo["interpretation"],
        "transport_retries": "No SDK retry; at most one retry for HTTP 5xx. Timeout, 4xx, unknown usage and unrecoverable parser errors pause. Native FormatError correction remains part of the original agent loop and every request is charged.",
        "completed_failures": "Never rerun a completed model execution to improve its score. Evaluation retry reuses the saved published code.",
        "cost_rule": "Actual new spend counts both prefixes once plus all eight job ledgers, including interrupted attempts. Comparison full-task cost adds prefix work to each continuation; comparison sums are not billing totals. Historical Solo is not charged again.",
        "cache_effect": "Implicit cache may be shared across repeats and conditions. Alternating SS/C order reduces one order imbalance; costs reflect actual cache usage rather than isolated cold starts.",
        "supplemental": "Pillow fixed metadata probe on the full delivered patch actually selected by the native evaluator; keep separate from official results and disclose reference implementation gaps.",
        "source_manifest": source_manifest(),
    }


def verify_sources(protocol):
    if protocol["source_manifest"] != source_manifest() or protocol["solo_reference"] != solo_reference():
        raise RuntimeError("Frozen source or historical Solo evidence changed")


def verify_preflight():
    proof = read_json(PROOF, {})
    if (proof.get("passed") is not True or proof.get("model_api_calls") != 0
            or proof.get("prefixes_completed") != 2 or proof.get("jobs_completed") != 4
            or proof.get("source_manifest") != source_manifest()
            or proof.get("solo_reference") != solo_reference()):
        raise RuntimeError("Exact-source two-prefix/four-condition offline proof required")


def remaining_seconds(protocol, task_key, record):
    elapsed = record.get("elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError("Invalid accepted prefix elapsed time")
    return round(protocol["solo_reference"]["total_execution_seconds"][task_key] - elapsed, 3)


def configure_run(run, task, condition, protocol, seconds, checkpoint_sha=None):
    run.mkdir(parents=True, exist_ok=True)
    config = {"task_key": task.key, "task": task.as_dict(), "condition": condition,
        "condition_version": VARIANT, "experiment_version": VERSION,
        "offline_selftest": protocol["offline_selftest"], "model": protocol["model"],
        "protocol_sha256": digest(protocol), "execution_seconds": seconds,
        "initialization_included_in_budget": True}
    if checkpoint_sha is not None:
        config["checkpoint_sha256"] = checkpoint_sha
    write_fixed(run / "task_config.json", config)


def verify_luna_prefix(prefix, task, protocol):
    if prefix.resolve() != (prefix.parent.parent / task.key / "prefix").resolve():
        raise ValueError("Invalid prefix location")
    config = read_json(prefix / "task_config.json")
    if (config.get("model") != protocol["model"] or config.get("protocol_sha256") != digest(protocol)
            or config.get("condition") != "prefix" or config.get("task") != task.as_dict()
            or config.get("experiment_version") != VERSION
            or read_json(prefix / "prefix/source_manifest.json") != protocol["source_manifest"]):
        raise ValueError("Prefix does not belong to this Luna protocol")
    ledger = read_json(prefix / "usage_ledger.json", {"requests": []})
    if any(row.get("model") != MODEL for row in ledger["requests"]):
        raise ValueError("Non-Luna request in new checkpoint")
    if not protocol["offline_selftest"] and not ledger["requests"]:
        raise ValueError("Real prefix has no Luna model usage")
    if protocol["offline_selftest"] and ledger["requests"]:
        raise ValueError("Offline prefix made model requests")
    return prefix_record(prefix, task)


def validate_budget_terminal(run, task, condition, protocol):
    """Freeze a genuine deadline outcome so resume cannot invent a skipped job."""
    phase = run / condition
    config, saved = read_json(run / "task_config.json"), read_json(phase / "status.json")
    seconds, elapsed = config.get("execution_seconds"), saved.get("elapsed_seconds")
    if (phase / "execution_receipt.json").exists():
        raise ValueError("A completed execution cannot become a skipped budget outcome")
    if (saved.get("status") not in EXHAUSTED_STATES or saved.get("task") != task.as_dict()
            or saved.get("phase") != condition or saved.get("condition") != condition
            or saved.get("offline_selftest") != protocol["offline_selftest"]
            or saved.get("handoff_variant") != VARIANT
            or saved.get("continuation_budget_seconds") != seconds
            or saved.get("initialization_included_in_budget") is not True
            or config.get("protocol_sha256") != digest(protocol)
            or config.get("task") != task.as_dict() or config.get("condition") != condition
            or config.get("model") != protocol["model"]
            or read_json(phase / "source_manifest.json") != protocol["source_manifest"]
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0
            or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed) or elapsed < seconds - 0.002):
        raise ValueError("Invalid terminal budget outcome")
    active = ("agent1",) if condition == "consolidate" else tuple(task.agents)
    if saved["status"] == EXHAUSTED:
        if (saved.get("continuation_model_requests") != 0 or usage(run, condition)["requests"] != 0
                or any((phase / f"first_request_{a}.json").exists() for a in active)):
            raise ValueError("Pre-query deadline outcome contains a model query")
    else:
        missing, outcomes = saved.get("unqueried_agents"), saved.get("outcomes", {})
        if (not isinstance(missing, list) or not missing or not set(missing).issubset(active)
                or set(outcomes) != set(active)
                or any(o.get("infrastructure_error") for o in outcomes.values())
                or any(outcomes[a].get("status") != "deadline" or (phase / f"first_request_{a}.json").exists()
                       for a in missing)):
            raise ValueError("Partial-query deadline outcome is inconsistent")
    paths = [run / "task_config.json", run / "checkpoint.json", run / "shared_git.tar",
             run / "checkpoint_provenance.json"]
    paths += [p for p in phase.rglob("*") if p.is_file() and p.name not in ("budget_terminal_receipt.json", "console.log")]
    if (run / "usage_ledger.json").exists():
        paths.append(run / "usage_ledger.json")
    write_fixed(phase / "budget_terminal_receipt.json", {
        "status": saved["status"], "task": task.as_dict(), "condition": condition,
        "protocol_sha256": digest(protocol),
        "protected_files_sha256": {str(p.relative_to(run)): file_hash(p) for p in sorted(paths)}})
    return saved


def run_model(run, task, condition, protocol, lock_fd):
    phase = run / condition
    saved = read_json(phase / "status.json", {})
    terminal = ("completed", "checkpoint_not_reached") if condition == "prefix" else ("completed", *EXHAUSTED_STATES)
    if saved.get("status") in terminal:
        if condition == "prefix":
            verify_luna_prefix(run, task, protocol)
        elif saved["status"] == "completed":
            verify_execution_receipt(phase, task, condition)
        else:
            validate_budget_terminal(run, task, condition, protocol)
        cleanup_phase(run, condition)
        return saved
    if not protocol["offline_selftest"] and not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY absent; no API request made")
    archive_incomplete(run, condition)
    phase.mkdir()
    write_json(phase / "source_manifest.json", protocol["source_manifest"])
    command = [sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--worker",
               "--run-dir", str(run), "--condition", condition, "--task", task.key]
    if protocol["offline_selftest"]:
        command.append("--offline-selftest")
    seconds = read_json(run / "task_config.json")["execution_seconds"]
    print(f"{task.key}: {run.name}/{condition}", flush=True)
    try:
        code = run_command(command, phase / "console.log", seconds + 300, lock_fd)
    finally:
        cleanup_phase(run, condition)
    saved = read_json(phase / "status.json", {})
    if code or saved.get("status") not in terminal:
        raise RuntimeError("Luna collaboration paused: " + str(phase))
    if condition == "prefix":
        verify_luna_prefix(run, task, protocol)
    elif saved["status"] == "completed":
        verify_execution_receipt(phase, task, condition)
    else:
        validate_budget_terminal(run, task, condition, protocol)
    return saved


def evaluate(run, task, condition, lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase = run / condition
    before = verify_execution_receipt(phase, task, condition)
    code = run_command([sys.executable, "-B", str(ROOT / "luna_collaboration_evaluator.py"),
                        "--run-dir", str(phase)], phase / "evaluation_console.log", 720,
                       lock_fd, env=evaluator_environment())
    score = read_json(phase / "evaluation_summary.json", {})
    if code or not score.get("evaluation_completed"):
        raise RuntimeError("Evaluation paused; completed model execution will be reused")
    if before != verify_execution_receipt(phase, task, condition):
        raise RuntimeError("Published artifacts changed during evaluation")
    return score


def summarize(suite, protocol, rows, skipped, prefixes):
    ledgers = list(suite.glob("*/prefix/usage_ledger.json")) + list(suite.glob("*/jobs/*/usage_ledger.json"))
    return {"pipeline_completed": len(rows) + len(skipped) == len(protocol["jobs"]),
        "model": protocol["model"], "protocol_sha256": digest(protocol),
        "offline_selftest": protocol["offline_selftest"], "planned_jobs": len(protocol["jobs"]),
        "rows": rows, "skipped_jobs": skipped, "prefixes": prefixes,
        "actual_new_usage": add_usage([usage(p.parent) for p in ledgers]),
        "solo_reference": protocol["solo_reference"],
        "interpretation": protocol["primary_comparison"]}


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
                raise RuntimeError("Frozen Luna source snapshot changed")
            if not destination.exists():
                destination.write_bytes((ROOT / name).read_bytes())
        rows, skipped, prefixes = [], [], {}
        try:
            for key in TASK_KEYS:
                verify_sources(protocol)
                task, prefix = get_task(key), suite / key / "prefix"
                configure_run(prefix, task, "prefix", protocol, PREFIX_SECONDS)
                write_json(suite / "status.json", {"status": "running", "task": key, "stage": "prefix",
                    "completed_jobs": len(rows), "skipped_jobs": len(skipped), "planned_jobs": len(protocol["jobs"])})
                run_model(prefix, task, "prefix", protocol, lock.fileno())
                record = verify_luna_prefix(prefix, task, protocol)
                prefixes[key] = record
                seconds = remaining_seconds(protocol, key, record)
                for job in [j for j in protocol["jobs"] if j["task"] == key]:
                    if record["status"] == "checkpoint_not_reached" or seconds <= 0:
                        skipped.append({**job, "reason": "checkpoint_not_reached" if record["status"] == "checkpoint_not_reached" else "prefix_exhausted_total_budget"})
                        continue
                    verify_sources(protocol)
                    condition, repeat = job["condition"], job["repeat"]
                    run = suite / key / "jobs" / f"{key}_r{repeat:02d}_{condition}"
                    configure_run(run, task, condition, protocol, seconds, record["checkpoint_sha256"])
                    copy_checkpoint(prefix, run, task, record)
                    write_json(suite / "status.json", {"status": "running", "task": key, "stage": condition,
                        "repeat": repeat, "completed_jobs": len(rows), "skipped_jobs": len(skipped),
                        "planned_jobs": len(protocol["jobs"])})
                    execution = run_model(run, task, condition, protocol, lock.fileno())
                    if execution["status"] in EXHAUSTED_STATES:
                        skipped.append({**job, "reason": execution["status"], "run_directory": str(run)})
                        write_json(suite / "summary.json", summarize(suite, protocol, rows, skipped, prefixes))
                        continue
                    verify_sources(protocol)
                    score = evaluate(run, task, condition, lock.fileno())
                    row = build_row(run, task, repeat, condition, record, execution, score)
                    failed_prefix = incomplete_seconds(prefix, "prefix")
                    row.update(model=protocol["model"], total_execution_allowance_seconds=protocol["solo_reference"]["total_execution_seconds"][key],
                        remaining_execution_allowance_seconds=seconds,
                        full_task_all_attempt_known_cost_usd=record["all_attempt_usage"]["known_cost_usd"] + row["all_attempt_usage"]["known_cost_usd"],
                        full_task_all_attempt_unknown_usage_requests=record["all_attempt_usage"]["unknown_usage_requests"] + row["all_attempt_usage"]["unknown_usage_requests"],
                        full_task_execution_seconds=record["elapsed_seconds"] + failed_prefix["known_seconds"] + row["remaining_execution_known_seconds"],
                        prefix_time_unknown_attempts=failed_prefix["unknown_attempts"])
                    rows.append(row)
                    write_json(run / "result.json", row)
                    write_json(suite / "summary.json", summarize(suite, protocol, rows, skipped, prefixes))
                    print(f"{key} {condition} r{repeat}: official={row['official_success']}, remaining cost=${row['all_attempt_usage']['known_cost_usd']:.6f}", flush=True)
            verify_sources(protocol)
            summary = summarize(suite, protocol, rows, skipped, prefixes)
            write_json(suite / "summary.json", summary)
            if offline:
                if len(rows) != 4 or skipped or len(prefixes) != 2 or summary["actual_new_usage"]["requests"]:
                    raise RuntimeError("Offline proof requires two checkpoints, four scored conditions, zero model calls")
                for row in rows:
                    outcomes = [row["execution"]["outcome"]] if row["condition"] == "consolidate" else [o["status"] for o in row["execution"]["outcomes"].values()]
                    if any(value != "Submitted" for value in outcomes):
                        raise RuntimeError("Offline publication did not complete")
                write_json(PROOF, {"passed": True, "model_api_calls": 0, "prefixes_completed": 2,
                    "jobs_completed": 4, "source_manifest": protocol["source_manifest"],
                    "solo_reference": protocol["solo_reference"], "suite_directory": str(suite)})
            write_json(suite / "status.json", {"status": "completed", "completed_jobs": len(rows), "skipped_jobs": len(skipped), "planned_jobs": len(protocol["jobs"])})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / "summary.json", summarize(suite, protocol, rows, skipped, prefixes))
            write_json(suite / "status.json", {"status": "paused", "completed_jobs": len(rows),
                "skipped_jobs": len(skipped), "error": safe_error(exc)})
            print("Luna collaboration paused: " + safe_error(exc), flush=True)
            return 2


def worker(run, task, condition, offline):
    from luna_collaboration_worker import phase_worker
    parts = run.relative_to(RUNS.resolve()).parts
    prefix = condition == "prefix"
    if (len(parts) != (3 if prefix else 4) or not parts[0].startswith("luna_collaboration_")
            or parts[1] != task.key or parts[2] != ("prefix" if prefix else "jobs")):
        raise ValueError("Invalid Luna collaboration worker path")
    suite = RUNS / parts[0]
    protocol, config = read_json(suite / "protocol.json"), read_json(run / "task_config.json")
    verify_sources(protocol)
    if (config.get("task_key") != task.key or config.get("task") != task.as_dict()
            or config.get("condition") != condition or config.get("condition_version") != VARIANT
            or config.get("experiment_version") != VERSION or config.get("model") != protocol["model"]
            or config.get("offline_selftest") != offline or protocol.get("offline_selftest") != offline
            or config.get("protocol_sha256") != digest(protocol)):
        raise ValueError("Luna worker configuration differs from frozen protocol")
    if prefix:
        seconds = PREFIX_SECONDS
    else:
        planned = {f"{j['task']}_r{j['repeat']:02d}_{j['condition']}" for j in protocol["jobs"]
                   if j["task"] == task.key and j["condition"] == condition}
        if parts[3] not in planned:
            raise ValueError("Unplanned Luna continuation")
        origin = suite / task.key / "prefix"
        record = verify_luna_prefix(origin, task, protocol)
        seconds = remaining_seconds(protocol, task.key, record)
        if record["status"] != "completed" or seconds <= 0 or config.get("checkpoint_sha256") != record["checkpoint_sha256"]:
            raise ValueError("Unavailable checkpoint or exhausted time budget")
        copy_checkpoint(origin, run, task, record)
    if config.get("execution_seconds") != seconds:
        raise ValueError("Worker time allowance differs from frozen rule")
    return phase_worker(run, condition, task, offline=offline, execution_seconds=seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--task", choices=TASK_KEYS)
    parser.add_argument("--condition", choices=("prefix", *CONDITIONS))
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        return worker(args.run_dir.resolve(strict=True), get_task(args.task), args.condition, args.offline_selftest)
    name = "luna_collaboration_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else "luna_collaboration_v1"
    suite = (args.suite_dir or RUNS / name).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r"luna_collaboration_[A-Za-z0-9_]+", suite.name):
        raise ValueError("Invalid Luna collaboration suite path")
    return supervise(suite, args.offline_selftest)


if __name__ == "__main__":
    raise SystemExit(main())
