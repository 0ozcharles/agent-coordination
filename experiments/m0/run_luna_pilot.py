"""Four separately recorded Luna fresh-Solo runs; no inherited Qwen code.

Reuse pinned tasks, images, tool loop and official evaluator. Completed model
executions and scores are validated on resume, never sampled again for success.
"""
import argparse
import fcntl
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

from budget_guard import write_json
from cross_task_specs import ROOT, REPO, COMMIT, get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import (
    archive_incomplete, evaluate, incomplete_seconds, verify_execution_receipt,
    source_manifest as original_sources, verify_references,
)
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed

RUNS = ROOT / "artifacts/handoff_expansion_runs"
QWEN_SUITE = RUNS / "cross_handoff_v1"
PREFLIGHT = ROOT / "artifacts/luna_pilot_offline_validation.json"
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
MODEL = "gpt-6-luna"
VARIANT = "luna_fresh_solo_v1"
NEW_SOURCES = (
    "run_luna_pilot.py", "luna_solo_worker.py", "luna_model.py",
    "luna_accounting.py", "pillow_metadata_probe_v1.py",
)


def time_allowances():
    expected = dict(zip(TASK_KEYS, (1466.261, 1269.899)))
    records = {}
    for key in TASK_KEYS:
        source = QWEN_SUITE / key / "solo_jobs" / f"{key}_r01_solo/task_config.json"
        config = read_json(source)
        if (config.get("task") != get_task(key).as_dict()
                or config.get("condition") != "solo" or config.get("fresh_solo") is not True
                or config.get("execution_seconds") != expected[key]):
            raise RuntimeError("Historical Solo allowance changed: " + key)
        records[key] = {"seconds": expected[key], "source": str(source.relative_to(ROOT)),
                        "source_sha256": file_hash(source)}
    return records


def source_manifest():
    sources = original_sources()
    sources.update({name: file_hash(ROOT / name) for name in NEW_SOURCES})
    for row in time_allowances().values():
        sources[row["source"]] = row["source_sha256"]
    return sources


def protocol_for(offline=False):
    verify_references()
    repeats = 1 if offline else 2
    return {
        "version": VARIANT, "offline_selftest": offline, "dataset_commit": COMMIT,
        "tasks": {key: get_task(key).as_dict() for key in TASK_KEYS},
        "jobs": [{"task": key, "repeat": repeat, "condition": "solo"}
                 for repeat in range(1, repeats + 1) for key in TASK_KEYS],
        "model": "offline-scripted-model" if offline else MODEL,
        "provider": "OpenAI", "endpoint": "https://api.openai.com/v1/chat/completions",
        "reasoning_effort": "none", "temperature": 0.7, "top_p": 0.8,
        "max_completion_tokens": 16384, "service_tier": "default",
        "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
        "context_window_tokens": 1050000,
        "pricing_usd_per_million": {"ordinary_input": 0.10, "cached_input": 0.01,
            "cache_write_30m": 0.125, "output": 0.50,
            "long_context_threshold": 272000, "long_context_input_multiplier": 2,
            "long_context_output_multiplier": 1.5},
        "model_revision": "Provider model ID; record the resolved model in every raw response.",
        "reasoning_setting": "No additional reasoning, aligned with the earlier Qwen enable_thinking=False setting; not an evaluation of Luna at every reasoning effort.",
        "time_allowances": time_allowances(), "initialization_included": True,
        "money_limit": None, "request_limit": None,
        "initial_state": "Fresh original repository, exact original two-feature prompt, no prior code or model history.",
        "prompt_and_tools": "Unchanged solo_expansion_worker and native mini-SWE Solo prompt/tool loop; no compaction.",
        "official_score": "Unmodified pinned CooperBench evaluator; two features must both pass.",
        "supplemental_score": "Pillow metadata checks are separate from official results, defined before any Luna execution; applied equally to previous Qwen Solo patches. Reference implementation discrepancies must be reported.",
        "historical_comparison": "Existing Qwen Solo results are historical exploratory controls, not a concurrent randomized model comparison. No claim of general superiority from four runs.",
        "transport_retries": "No SDK retries. One retry only for HTTP 5xx; timeout/4xx/usage/parse errors pause. Keep every attempt's cost.",
        "task_failure": "Keep completed failures; never retry for a better score.",
        "infrastructure_failure": "Pause; review before explicit resume. Reuse completed execution and evaluation.",
        "accounting": "Only new Luna API usage; cached-input/write fees are explicit. No historical prefix cost. Unknown charges remain unknown.",
        "source_manifest": source_manifest(),
    }


def verify_preflight():
    proof = read_json(PREFLIGHT, {})
    if (not proof.get("passed") or proof.get("model_api_calls") != 0
            or proof.get("completed_jobs") != 2
            or proof.get("source_manifest") != source_manifest()):
        raise RuntimeError("Complete exact-source offline checks for both Luna tasks first")


def verify_sources(protocol):
    if protocol["source_manifest"] != source_manifest():
        raise RuntimeError("Frozen Luna sources changed; no further model request started")


def configure_job(run, task, protocol):
    run.mkdir(parents=True, exist_ok=True)
    write_fixed(run / "task_config.json", {
        "task_key": task.key, "task": task.as_dict(), "condition": "solo",
        "condition_version": VARIANT, "fresh_solo": True,
        "offline_selftest": protocol["offline_selftest"],
        "protocol_sha256": digest(protocol),
        "execution_seconds": protocol["time_allowances"][task.key]["seconds"],
    })


def execute(run, task, protocol, lock_fd):
    phase = run / "solo"
    saved = read_json(phase / "status.json", {})
    if saved.get("status") == "completed":
        verify_execution_receipt(phase, task, "solo")
        cleanup_phase(run, "solo")
        return saved
    if not protocol["offline_selftest"] and not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY absent; no API request made")
    archive_incomplete(run, "solo")
    phase.mkdir()
    write_json(phase / "source_manifest.json", protocol["source_manifest"])
    command = [sys.executable, "-u", str(Path(__file__).resolve()),
               "--worker", "--run-dir", str(run)]
    if protocol["offline_selftest"]:
        command.append("--offline-selftest")
    try:
        code = run_command(command, phase / "console.log",
                           protocol["time_allowances"][task.key]["seconds"] + 300, lock_fd)
    finally:
        cleanup_phase(run, "solo")
    saved = read_json(phase / "status.json", {})
    if code or saved.get("status") != "completed":
        raise RuntimeError("Luna execution paused: " + str(phase))
    verify_execution_receipt(phase, task, "solo")
    return saved


def summarize(suite, protocol, rows):
    ledgers = sorted(suite.glob("*/solo_jobs/*/usage_ledger.json"))
    return {
        "pipeline_completed": len(rows) == len(protocol["jobs"]),
        "protocol_sha256": digest(protocol), "model": protocol["model"],
        "offline_selftest": protocol["offline_selftest"], "planned_jobs": len(protocol["jobs"]),
        "rows": rows, "actual_new_usage": add_usage([usage(p.parent) for p in ledgers]),
        "interpretation": protocol["historical_comparison"],
    }


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
                raise RuntimeError("Luna source snapshot changed")
            if not destination.exists():
                destination.write_bytes((ROOT / name).read_bytes())
        rows = []
        try:
            for job in protocol["jobs"]:
                verify_sources(protocol)
                task, repeat = get_task(job["task"]), job["repeat"]
                run = suite / task.key / "solo_jobs" / f"{task.key}_r{repeat:02d}_solo"
                configure_job(run, task, protocol)
                write_json(suite / "status.json", {"status": "running", "task": task.key,
                    "repeat": repeat, "completed_jobs": len(rows), "planned_jobs": len(protocol["jobs"])})
                print(f"{task.key}: Solo {repeat}", flush=True)
                execution = execute(run, task, protocol, lock.fileno())
                verify_sources(protocol)
                score = evaluate(run, task, "solo", lock.fileno())
                failed_time = incomplete_seconds(run, "solo")
                row = {**job, "model": protocol["model"], "run_directory": str(run),
                    "official_success": bool(score["both_passed"]),
                    "execution": execution, "evaluation": score,
                    "accepted_usage": usage(run, "solo"), "all_attempt_usage": usage(run),
                    "execution_seconds_including_incomplete": round(execution["elapsed_seconds"] + failed_time["known_seconds"], 3),
                    "unknown_attempt_times": failed_time["unknown_attempts"],
                    "evaluation_seconds": score["elapsed_seconds"]}
                rows.append(row)
                write_json(run / "result.json", row)
                write_json(suite / "summary.json", summarize(suite, protocol, rows))
                print(f"{task.key} Solo {repeat}: official={row['official_success']}, known cost=${row['all_attempt_usage']['known_cost_usd']:.6f}", flush=True)
            verify_sources(protocol)
            summary = summarize(suite, protocol, rows)
            if offline:
                if len(rows) != 2 or summary["actual_new_usage"]["requests"]:
                    raise RuntimeError("Offline proof requires two scored jobs and zero API calls")
                if any(row["execution"].get("outcome") != "Submitted" or
                       not row["execution"].get("offline_isolation_probe_passed") for row in rows):
                    raise RuntimeError("Offline publication/isolation check failed")
                write_json(PREFLIGHT, {"passed": True, "model_api_calls": 0,
                    "completed_jobs": 2, "source_manifest": protocol["source_manifest"],
                    "suite_directory": str(suite)})
            write_json(suite / "status.json", {"status": "completed", "completed_jobs": len(rows),
                                               "planned_jobs": len(protocol["jobs"])})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / "summary.json", summarize(suite, protocol, rows))
            write_json(suite / "status.json", {"status": "paused", "completed_jobs": len(rows),
                "planned_jobs": len(protocol["jobs"]), "error": safe_error(exc)})
            print("Luna pilot paused: " + safe_error(exc), flush=True)
            return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        from luna_solo_worker import phase_worker
        run = args.run_dir.resolve(strict=True)
        parts = run.relative_to(RUNS.resolve()).parts
        if len(parts) != 4 or not parts[0].startswith("luna_solo_") or parts[2] != "solo_jobs":
            raise ValueError("Invalid Luna worker path")
        protocol = read_json(RUNS / parts[0] / "protocol.json")
        verify_sources(protocol)
        config, task = read_json(run / "task_config.json"), get_task(parts[1])
        planned_jobs = {f"{job['task']}_r{job['repeat']:02d}_solo"
                        for job in protocol["jobs"]
                        if job["task"] == task.key and job["condition"] == "solo"}
        if (parts[3] not in planned_jobs or config.get("task_key") != task.key
                or task.key not in TASK_KEYS or config.get("protocol_sha256") != digest(protocol)
                or config.get("task") != task.as_dict() or config.get("condition") != "solo"
                or config.get("fresh_solo") is not True or config.get("condition_version") != VARIANT
                or config.get("offline_selftest") != args.offline_selftest
                or protocol.get("offline_selftest") != args.offline_selftest
                or config.get("execution_seconds") != protocol["time_allowances"][task.key]["seconds"]):
            raise ValueError("Luna worker config differs from frozen protocol")
        return phase_worker(run, task, args.offline_selftest, config["execution_seconds"])
    name = ("luna_solo_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")) if args.offline_selftest else "luna_solo_v1"
    suite = (args.suite_dir or RUNS / name).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r"luna_solo_[A-Za-z0-9_]+", suite.name):
        raise ValueError("Invalid Luna suite path")
    return supervise(suite, args.offline_selftest)


if __name__ == "__main__":
    raise SystemExit(main())
