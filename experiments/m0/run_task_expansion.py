"""Two new tasks, one observable WIP checkpoint each, six continuations each."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from budget_guard import write_json
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_repeats import archive_failed_job, read_json, run_command
from run_solo_smoke import MODEL, MAX_OUTPUT, ROOT, configure
from task_specs import TASKS, get_task
from wip_checkpoint import validate_checkpoint

RUNS = ROOT / "artifacts/expansion_runs"
SOURCE_FILES = (
    "run_task_expansion.py", "run_expansion_with_together_key.sh", "task_specs.py", "wip_checkpoint.py",
    "run_baselines.py", "run_consolidate.py", "baseline_runtime.py", "baseline_evidence.py",
    "baseline_model.py", "budget_guard.py", "run_solo_smoke.py", "run_repeats.py",
    "evaluate_task_expansion.py", "evaluate_solo_smoke.py",
)


def file_hash(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError("Expected ordinary file: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    paths = [ROOT / p for p in SOURCE_FILES]
    for task in TASKS.values():
        paths.append(task.image_record_path)
        paths.extend(task.task_dir / f"feature{f}/feature.md" for f in task.features)
    return {str(p.relative_to(ROOT)): file_hash(p) for p in paths}


def verify_references():
    proof = read_json(ROOT / "artifacts/task_expansion_reference_validation.json", {})
    if not proof.get("passed") or proof.get("model_api_calls") != 0:
        raise RuntimeError("Complete base/gold reference checks for both tasks first")
    for key, task in TASKS.items():
        row = proof.get("tasks", {}).get(key, {})
        image = read_json(task.image_record_path, {})
        if (not all(row.get(k) for k in ("base_fails_both", "individual_gold_passes_both", "combined_gold_passes_both"))
                or row.get("image_id") != image.get("Id")):
            raise RuntimeError("Reference validation missing or stale for " + key)
    return proof


def verify_preflight():
    verify_references()
    proof = read_json(ROOT / "artifacts/expansion_offline_validation.json", {})
    if not proof.get("passed") or proof.get("source_manifest") != source_manifest():
        raise RuntimeError("Complete offline expansion validation for these source versions first")


def protocol_for(offline):
    return {
        "version": "two_tasks_wip_v1", "offline_selftest": offline,
        "tasks": {k: t.as_dict() for k, t in TASKS.items()},
        "model": "offline-scripted-model" if offline else MODEL,
        "temperature": 0.7, "top_p": 0.8, "enable_thinking": False, "seed": None,
        "max_output_tokens": MAX_OUTPUT, "money_limit": None, "request_limit": None,
        "prefix_rule": "First paired completed round with semantic source changes by both agents, neither submitted nor exited",
        "prefix_time_horizon_seconds": 1200, "continuation_seconds": 1200,
        "ineligible_prefix": "Record and skip this task; no resampling for an eligible or favorable state",
        "orders": [["keep", "strongshare", "consolidate"], ["consolidate", "keep", "strongshare"]],
        "score": "Pinned CooperBench official tests on final submissions; no Jinja-specific supplemental tests",
        "task_failure": "Retain outcome and continue, without retrying for success",
        "infrastructure_failure": "Pause and retain cost; retry only failed stage after review",
        "cost": "Actual spend counts each new prefix once, plus all continuations and incomplete attempts",
        "comparison_cost": "Each accepted continuation plus its shared prefix once; not a billing total",
        "source_manifest": source_manifest(), "reference_validation": verify_references(),
    }


def write_fixed(path, value):
    if path.exists() and read_json(path) != value:
        raise RuntimeError("Frozen configuration changed: " + str(path))
    write_json(path, value)


def configure_run(run_dir, task, protocol):
    run_dir.mkdir(parents=True, exist_ok=True)
    write_fixed(run_dir / "task_config.json", {"task_key": task.key, "task": task.as_dict(),
                "offline_selftest": protocol["offline_selftest"], "protocol_sha256": digest(protocol)})


def run_phase(run_dir, condition, task, protocol, lock_fd):
    phase_dir = run_dir / condition
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") in ("completed", "checkpoint_not_reached"):
        return status
    if not protocol["offline_selftest"] and not os.environ.get("TOGETHER_API_KEY", "").strip():
        raise RuntimeError("TOGETHER_API_KEY absent; use run_expansion_with_together_key.sh")
    archive_failed_job(run_dir, condition)
    phase_dir.mkdir()
    write_json(phase_dir / "source_manifest.json", protocol["source_manifest"])
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", "--run-dir", str(run_dir),
               "--condition", condition, "--task", task.key]
    if protocol["offline_selftest"]:
        command.append("--offline-selftest")
    print(task.key + ": " + run_dir.name + "/" + condition, flush=True)
    try:
        code = run_command(command, phase_dir / "console.log", 1500, lock_fd)
    finally:
        cleanup_phase(run_dir, condition)
    status = read_json(phase_dir / "status.json", {})
    if code or status.get("status") not in ("completed", "checkpoint_not_reached"):
        raise RuntimeError("Execution paused: " + str(phase_dir))
    return status


def copy_checkpoint(prefix_dir, run_dir, task):
    checkpoint = read_json(prefix_dir / "checkpoint.json")
    validate_checkpoint(checkpoint, task)
    if file_hash(prefix_dir / "shared_git.tar") != checkpoint["git_archive_sha256"]:
        raise RuntimeError("Shared Git checkpoint archive changed")
    for name in ("checkpoint.json", "shared_git.tar"):
        data = (prefix_dir / name).read_bytes()
        dst = run_dir / name
        if dst.exists() and dst.read_bytes() != data:
            raise RuntimeError("Restored branch input changed")
        if not dst.exists():
            dst.write_bytes(data)
    write_fixed(run_dir / "checkpoint_provenance.json", {
        "source_directory": str(prefix_dir), "checkpoint_sha256": digest(checkpoint),
        "prefix_usage": usage(prefix_dir, "prefix"),
        "prefix_elapsed_seconds": read_json(prefix_dir / "prefix/status.json")["elapsed_seconds"],
    })
    return checkpoint


def patch_hashes(phase_dir, task, condition):
    names = ["final.patch"] if condition == "consolidate" else [f"agent{f}.patch" for f in task.features]
    return {name: file_hash(phase_dir / name) for name in names}


def evaluate(run_dir, task, condition, lock_fd):
    phase_dir = run_dir / condition
    hashes = patch_hashes(phase_dir, task, condition)
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") != "completed":
        raise RuntimeError("Cannot score an incomplete model execution")
    if condition == "consolidate" and status.get("final_patch_sha256") != hashes["final.patch"]:
        raise RuntimeError("Consolidate submission differs from its completed worker status")
    write_fixed(phase_dir / "execution_receipt.json", {"patch_sha256": hashes})
    summary = read_json(phase_dir / "evaluation_summary.json", {})
    if not summary.get("evaluation_completed"):
        from evaluate_solo_smoke import evaluator_environment
        code = run_command([sys.executable, str(ROOT / "evaluate_task_expansion.py"), "--run-dir", str(phase_dir)],
                           phase_dir / "evaluation_console.log", 720, lock_fd, env=evaluator_environment())
        summary = read_json(phase_dir / "evaluation_summary.json", {})
        if code or not summary.get("evaluation_completed"):
            raise RuntimeError("Evaluation paused; completed model run will not repeat: " + str(phase_dir))
    if hashes != patch_hashes(phase_dir, task, condition):
        raise RuntimeError("Submitted patch changed during scoring")
    return summary


def add_usage(rows):
    keys = ("requests", "known_cost_usd", "unknown_usage_requests", "prompt_tokens", "completion_tokens")
    return {key: round(sum(row[key] for row in rows), 9) for key in keys}


def summarize(suite_dir, protocol, rows, skipped):
    ledgers = list(suite_dir.glob("*/prefix/usage_ledger.json")) + list(suite_dir.glob("*/jobs/*/usage_ledger.json"))
    return {"pipeline_completed": len(rows) + 6 * len(skipped) == 12,
            "protocol_sha256": digest(protocol), "offline_selftest": protocol["offline_selftest"],
            "rows": rows, "skipped_tasks": skipped,
            "actual_new_usage": add_usage([usage(p.parent) for p in ledgers]),
            "interpretation": "Two samples per condition on one visible-progress checkpoint per task; report tasks separately. Earlier Jinja results are not pooled."}


def supervise(suite_dir, offline):
    configure()
    suite_dir.mkdir(parents=True, exist_ok=True)
    with (ROOT / "artifacts/model_run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not offline:
            verify_preflight()
        protocol = protocol_for(offline)
        write_fixed(suite_dir / "protocol.json", protocol)
        for name, expected_hash in protocol["source_manifest"].items():
            source = ROOT / name
            destination = suite_dir / "source_snapshot" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and file_hash(destination) != expected_hash:
                raise RuntimeError("Saved source snapshot differs from protocol")
            if not destination.exists():
                destination.write_bytes(source.read_bytes())
        rows, skipped = [], []
        try:
            for task in TASKS.values():
                prefix_dir = suite_dir / task.key / "prefix"
                configure_run(prefix_dir, task, protocol)
                write_json(suite_dir / "status.json", {"status": "running", "task": task.key,
                           "stage": "prefix", "completed_jobs": len(rows), "planned_jobs": 12})
                if source_manifest() != protocol["source_manifest"]:
                    raise RuntimeError("Source changed during the batch")
                prefix_status = run_phase(prefix_dir, "prefix", task, protocol, lock.fileno())
                if prefix_status["status"] == "checkpoint_not_reached":
                    skipped.append({"task": task.key, "reason": "Predeclared WIP checkpoint was not reached"})
                    continue
                prefix_cost = usage(prefix_dir, "prefix")
                for repeat, order in enumerate(protocol["orders"], 1):
                    for condition in order:
                        if source_manifest() != protocol["source_manifest"]:
                            raise RuntimeError("Source changed during the batch")
                        run_dir = suite_dir / task.key / "jobs" / f"{task.key}_r{repeat:02d}_{condition}"
                        configure_run(run_dir, task, protocol)
                        checkpoint = copy_checkpoint(prefix_dir, run_dir, task)
                        write_json(suite_dir / "status.json", {"status": "running", "task": task.key,
                                   "stage": condition, "repeat": repeat, "completed_jobs": len(rows), "planned_jobs": 12})
                        execution = run_phase(run_dir, condition, task, protocol, lock.fileno())
                        score = evaluate(run_dir, task, condition, lock.fileno())
                        continuation = usage(run_dir, condition)
                        row = {"task": task.key, "repeat": repeat, "condition": condition,
                               "run_directory": str(run_dir), "checkpoint_sha256": digest(checkpoint),
                               "official_success": bool(score["both_passed"]), "evaluation": score,
                               "execution": execution, "continuation_usage": continuation,
                               "full_task_equivalent_known_cost_usd": round(prefix_cost["known_cost_usd"] + continuation["known_cost_usd"], 9),
                               "full_task_equivalent_unknown_usage_requests": prefix_cost["unknown_usage_requests"] + continuation["unknown_usage_requests"],
                               "comparison_seconds": round(prefix_status["elapsed_seconds"] + execution["elapsed_seconds"] + score["elapsed_seconds"], 3)}
                        rows.append(row)
                        write_json(run_dir / "result.json", row)
                        write_json(suite_dir / "summary.json", summarize(suite_dir, protocol, rows, skipped))
                        print(f"{task.key} {condition} repeat {repeat}: official={row['official_success']}, continuation=${continuation['known_cost_usd']:.4f}", flush=True)
            if source_manifest() != protocol["source_manifest"]:
                raise RuntimeError("Source changed during the final stage")
            summary = summarize(suite_dir, protocol, rows, skipped)
            write_json(suite_dir / "summary.json", summary)
            if offline:
                if len(rows) != 12 or skipped or summary["actual_new_usage"]["requests"]:
                    raise RuntimeError("Offline test must restore and score all 12 continuations with no API calls")
                for row in rows:
                    execution = row["execution"]
                    outcomes = [execution["outcome"]] if row["condition"] == "consolidate" else [v["status"] for v in execution["outcomes"].values()]
                    if any(x != "Submitted" for x in outcomes):
                        raise RuntimeError("Offline continuation did not publish its synthetic WIP")
                write_json(ROOT / "artifacts/expansion_offline_validation.json", {
                    "passed": True, "source_manifest": protocol["source_manifest"], "suite_directory": str(suite_dir),
                    "model_api_calls": 0, "jobs_completed": 12, "both_tasks_wip_restored": True})
            write_json(suite_dir / "status.json", {"status": "completed", "completed_jobs": len(rows), "skipped_tasks": skipped})
            print("Expansion completed: " + str(suite_dir / "summary.json"), flush=True)
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite_dir / "summary.json", summarize(suite_dir, protocol, rows, skipped))
            write_json(suite_dir / "status.json", {"status": "paused", "completed_jobs": len(rows), "error": safe_error(exc)})
            print("Expansion paused: " + safe_error(exc), flush=True)
            return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--condition", choices=("prefix", "keep", "strongshare", "consolidate"))
    parser.add_argument("--task", choices=tuple(TASKS))
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        task = get_task(args.task)
        run_dir = args.run_dir.resolve()
        if not run_dir.is_relative_to(RUNS.resolve()):
            raise ValueError("Worker directory must be inside expansion_runs")
        cfg = read_json(run_dir / "task_config.json", {})
        if cfg.get("task") != task.as_dict() or cfg.get("offline_selftest") != args.offline_selftest:
            raise ValueError("Task configuration mismatch")
        if args.condition == "consolidate":
            from run_consolidate import phase_worker
            return phase_worker(run_dir, args.offline_selftest, task=task)
        from run_baselines import phase_worker
        return phase_worker(run_dir, args.condition, args.offline_selftest, task=task)
    default = "expansion_wip_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else "expansion_wip_v1"
    suite_dir = (args.suite_dir or RUNS / default).resolve()
    if suite_dir.parent != RUNS.resolve() or not re.fullmatch(r"expansion_wip_[A-Za-z0-9_]+", suite_dir.name):
        raise ValueError("Invalid expansion suite directory")
    return supervise(suite_dir, args.offline_selftest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + safe_error(exc), file=sys.stderr)
        sys.exit(2)
