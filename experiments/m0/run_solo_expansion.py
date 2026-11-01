"""Four fresh-solo controls for the completed Click and dirty-equals pilot."""
import argparse
import fcntl
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from budget_guard import write_json
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_repeats import archive_failed_job, read_json, run_command
from run_solo_smoke import MAX_OUTPUT, MODEL, ROOT, configure
from run_task_expansion import add_usage, file_hash, verify_references, write_fixed
from task_specs import REPO, TASKS, get_task

RUNS = ROOT / "artifacts/solo_expansion_runs"
SOURCE_SUITE = ROOT / "artifacts/expansion_runs/expansion_wip_v1"
SOURCE_FILES = (
    "run_solo_expansion.py", "run_solo_expansion_with_together_key.sh",
    "solo_expansion_worker.py", "evaluate_solo_expansion.py", "task_specs.py",
    "baseline_runtime.py", "baseline_model.py", "budget_guard.py", "run_baselines.py",
    "run_repeats.py", "run_solo_smoke.py", "run_task_expansion.py",
    "evaluate_solo_smoke.py",
)


def comparison_budgets():
    original = read_json(SOURCE_SUITE / "protocol.json")
    if original.get("offline_selftest") or original.get("continuation_seconds") != 1200:
        raise RuntimeError("Expected the completed real two-task comparison protocol")
    status = read_json(SOURCE_SUITE / "status.json")
    if status.get("status") != "completed" or status.get("completed_jobs") != 12:
        raise RuntimeError("Original comparison must be complete")
    budgets = {}
    for key, task in TASKS.items():
        if original["tasks"][key] != task.as_dict():
            raise RuntimeError("Original comparison task changed")
        prefix = read_json(SOURCE_SUITE / key / "prefix/prefix/status.json")
        elapsed = prefix.get("elapsed_seconds")
        if (prefix.get("status") != "completed" or isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed)
                or not 0 < elapsed <= 1500):
            raise RuntimeError("Invalid original prefix timing")
        budgets[key] = round(elapsed + original["continuation_seconds"], 3)
    return budgets


def source_manifest():
    paths = [ROOT / name for name in SOURCE_FILES]
    paths += [SOURCE_SUITE / "protocol.json", SOURCE_SUITE / "status.json"]
    paths += [SOURCE_SUITE / key / "prefix/prefix/status.json" for key in TASKS]
    paths += list(sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2").rglob("*.py")))
    paths += list(sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2").rglob("*.sh")))
    paths += list(sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2/config").glob("*.yaml")))
    paths += list(sorted((REPO / "src/cooperbench/eval").rglob("*.py")))
    for task in TASKS.values():
        paths.append(task.image_record_path)
        paths.extend(task.task_dir / f"feature{f}/feature.md" for f in task.features)
    return {str(p.relative_to(ROOT)): file_hash(p) for p in paths}


def protocol_for(offline):
    return {
        "version": "fresh_solo_controls_v1", "offline_selftest": offline,
        "fresh_solo": True, "tasks": {k: t.as_dict() for k, t in TASKS.items()},
        "jobs": [{"task": key, "repeat": repeat, "condition": "solo"}
                 for key in TASKS for repeat in (1, 2)],
        "model": "offline-scripted-model" if offline else MODEL,
        "temperature": 0.7, "top_p": 0.8, "enable_thinking": False, "seed": None,
        "max_output_tokens": MAX_OUTPUT, "money_limit": None, "request_limit": None,
        "execution_seconds": comparison_budgets(),
        "time_rule": "Original task's recorded prefix elapsed + 1200s. Fresh setup counts toward this horizon; loop checks at completed tool boundaries, as in the comparison. Evaluation measured separately.",
        "initial_state": "Verified original base image, clean repository, both original feature.md requirements at the first model call, one agent, private Git only; no checkpoint, history, peer evidence or prior outcome supplied",
        "prompt": "Pinned upstream mini_swe_agent_v2 solo template with common small-edit and deadline reminders",
        "score": "Pinned official test_solo on the final published patch, both original feature test suites; no repair or reference substitution",
        "task_failure": "Retain outcome and continue; do not retry to obtain success",
        "infrastructure_failure": "Pause, preserve all usage and archive incomplete execution before explicit resume; evaluator failure never repeats a completed model run",
        "cost": "Fresh Solo cost is the entire task cost. Do not add or rebill historical prefix costs. Actual new spend includes incomplete attempts.",
        "source_manifest": source_manifest(), "reference_validation": verify_references(),
    }


def verify_preflight():
    verify_references()
    comparison_budgets()
    proof = read_json(ROOT / "artifacts/solo_expansion_offline_validation.json", {})
    if (not proof.get("passed") or proof.get("model_api_calls") != 0
            or proof.get("jobs_completed") != 4
            or proof.get("source_manifest") != source_manifest()):
        raise RuntimeError("Complete all four offline Solo executions and scores for these source versions first")


def configure_run(run_dir, task, protocol):
    run_dir.mkdir(parents=True, exist_ok=True)
    write_fixed(run_dir / "task_config.json", {
        "task_key": task.key, "task": task.as_dict(), "fresh_solo": True,
        "offline_selftest": protocol["offline_selftest"], "protocol_sha256": digest(protocol),
        "execution_seconds": protocol["execution_seconds"][task.key],
    })


def verify_execution_receipt(phase_dir):
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") != "completed" or status.get("fresh_solo") is not True:
        raise RuntimeError("Only a completed fresh Solo execution can be scored")
    hashes = {"final.patch": file_hash(phase_dir / "final.patch")}
    if status.get("final_patch_sha256") != hashes["final.patch"]:
        raise RuntimeError("Published Solo patch differs from its completed worker status")
    write_fixed(phase_dir / "execution_receipt.json", {"patch_sha256": hashes})
    return hashes


def run_model(run_dir, task, protocol, lock_fd):
    phase_dir = run_dir / "solo"
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") == "completed":
        verify_execution_receipt(phase_dir)
        return status
    if not protocol["offline_selftest"] and not os.environ.get("TOGETHER_API_KEY", "").strip():
        raise RuntimeError("TOGETHER_API_KEY absent; use run_solo_expansion_with_together_key.sh")
    archive_failed_job(run_dir, "solo")
    phase_dir.mkdir()
    write_json(phase_dir / "source_manifest.json", protocol["source_manifest"])
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker",
               "--run-dir", str(run_dir), "--task", task.key]
    if protocol["offline_selftest"]:
        command.append("--offline-selftest")
    print("Fresh Solo: " + run_dir.name, flush=True)
    try:
        code = run_command(command, phase_dir / "console.log",
                           protocol["execution_seconds"][task.key] + 300, lock_fd)
    finally:
        cleanup_phase(run_dir, "solo")
    status = read_json(phase_dir / "status.json", {})
    if code or status.get("status") != "completed":
        raise RuntimeError("Solo execution paused: " + str(phase_dir))
    verify_execution_receipt(phase_dir)
    return status


def evaluate(run_dir, task, lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase_dir = run_dir / "solo"
    before = verify_execution_receipt(phase_dir)
    # The evaluator validates existing input receipts before reusing a completed score.
    code = run_command([sys.executable, str(ROOT / "evaluate_solo_expansion.py"),
                        "--run-dir", str(phase_dir)], phase_dir / "evaluation_console.log",
                       720, lock_fd, env=evaluator_environment())
    score = read_json(phase_dir / "evaluation_summary.json", {})
    if code or not score.get("evaluation_completed"):
        raise RuntimeError("Solo evaluation paused; completed model execution will not repeat")
    if verify_execution_receipt(phase_dir) != before:
        raise RuntimeError("Published patch changed during evaluation")
    return score


def build_row(run_dir, task, repeat, execution, score):
    accepted = usage(run_dir, "solo")
    return {
        "task": task.key, "repeat": repeat, "condition": "solo", "fresh_solo": True,
        "run_directory": str(run_dir), "official_success": bool(score["both_passed"]),
        "execution": execution, "evaluation": score, "accepted_usage": accepted,
        "full_task_known_cost_usd": accepted["known_cost_usd"],
        "full_task_unknown_usage_requests": accepted["unknown_usage_requests"],
        "comparison_seconds": round(execution["elapsed_seconds"] + score["elapsed_seconds"], 3),
    }


def summarize(suite_dir, protocol, rows):
    ledgers = sorted(suite_dir.glob("*/jobs/*/usage_ledger.json"))
    return {
        "pipeline_completed": len(rows) == 4, "protocol_sha256": digest(protocol),
        "offline_selftest": protocol["offline_selftest"], "fresh_solo": True,
        "rows": rows, "actual_new_usage": add_usage([usage(p.parent) for p in ledgers]),
        "interpretation": "Two fresh-solo samples per task; compare separately with the completed expansion, including its prefix in comparison cost. No historical cost is billed here.",
    }


def supervise(suite_dir, offline=False):
    configure()
    suite_dir.mkdir(parents=True, exist_ok=True)
    with (ROOT / "artifacts/model_run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not offline:
            verify_preflight()
        protocol = protocol_for(offline)
        write_fixed(suite_dir / "protocol.json", protocol)
        for name, expected in protocol["source_manifest"].items():
            source, destination = ROOT / name, suite_dir / "source_snapshot" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and file_hash(destination) != expected:
                raise RuntimeError("Saved source snapshot differs from frozen protocol")
            if not destination.exists():
                destination.write_bytes(source.read_bytes())
        rows = []
        try:
            for job in protocol["jobs"]:
                task, repeat = get_task(job["task"]), job["repeat"]
                if source_manifest() != protocol["source_manifest"]:
                    raise RuntimeError("Sources changed during the Solo batch")
                run_dir = suite_dir / task.key / "jobs" / f"{task.key}_r{repeat:02d}_solo"
                configure_run(run_dir, task, protocol)
                write_json(suite_dir / "status.json", {
                    "status": "running", "task": task.key, "repeat": repeat,
                    "stage": "solo", "completed_jobs": len(rows), "planned_jobs": 4,
                })
                execution = run_model(run_dir, task, protocol, lock.fileno())
                score = evaluate(run_dir, task, lock.fileno())
                row = build_row(run_dir, task, repeat, execution, score)
                rows.append(row)
                write_json(run_dir / "result.json", row)
                write_json(suite_dir / "summary.json", summarize(suite_dir, protocol, rows))
                print(f"{task.key} Solo {repeat}: official={row['official_success']}, cost=${row['full_task_known_cost_usd']:.4f}", flush=True)
            if source_manifest() != protocol["source_manifest"]:
                raise RuntimeError("Sources changed during the final Solo stage")
            summary = summarize(suite_dir, protocol, rows)
            write_json(suite_dir / "summary.json", summary)
            if offline:
                if (len(rows) != 4 or summary["actual_new_usage"]["requests"]
                        or any(r["execution"]["outcome"] != "Submitted" for r in rows)):
                    raise RuntimeError("Offline validation must submit and score four jobs with zero API calls")
                for row in rows:
                    proof = read_json(Path(row["run_directory"]) / "solo/initial_state.json")
                    if proof.get("fresh_solo") is not True:
                        raise RuntimeError("Missing fresh initial state evidence")
                write_json(ROOT / "artifacts/solo_expansion_offline_validation.json", {
                    "passed": True, "model_api_calls": 0, "jobs_completed": 4,
                    "source_manifest": protocol["source_manifest"], "suite_directory": str(suite_dir),
                })
            write_json(suite_dir / "status.json", {"status": "completed", "completed_jobs": 4})
            print("Solo controls completed: " + str(suite_dir / "summary.json"), flush=True)
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite_dir / "summary.json", summarize(suite_dir, protocol, rows))
            write_json(suite_dir / "status.json", {"status": "paused", "completed_jobs": len(rows), "error": safe_error(exc)})
            print("Solo controls paused: " + safe_error(exc), flush=True)
            return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--task", choices=tuple(TASKS))
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        from solo_expansion_worker import phase_worker
        task, run_dir = get_task(args.task), args.run_dir.resolve()
        parts = run_dir.relative_to(RUNS.resolve()).parts
        if len(parts) != 4 or parts[1] != task.key or parts[2] != "jobs":
            raise ValueError("Invalid fresh Solo job directory")
        cfg = read_json(run_dir / "task_config.json")
        protocol = read_json(run_dir.parents[2] / "protocol.json")
        if (cfg.get("task") != task.as_dict() or cfg.get("fresh_solo") is not True
                or cfg.get("offline_selftest") != args.offline_selftest
                or cfg.get("execution_seconds") != comparison_budgets()[task.key]
                or cfg.get("protocol_sha256") != digest(protocol)
                or protocol.get("source_manifest") != source_manifest()):
            raise ValueError("Fresh Solo task configuration mismatch")
        return phase_worker(run_dir, task, args.offline_selftest, cfg["execution_seconds"])
    default = "solo_controls_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else "solo_controls_v1"
    suite_dir = (args.suite_dir or RUNS / default).resolve()
    if suite_dir.parent != RUNS.resolve() or not re.fullmatch(r"solo_controls_[A-Za-z0-9_]+", suite_dir.name):
        raise ValueError("Invalid Solo suite directory")
    return supervise(suite_dir, args.offline_selftest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + safe_error(exc), file=sys.stderr)
        sys.exit(2)
