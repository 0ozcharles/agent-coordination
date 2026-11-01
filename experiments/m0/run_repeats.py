"""Six predeclared continuations of one common checkpoint, with two score columns.

This repeats sampling on one task/checkpoint; it is not a multi-task study.
Existing agent runners and official scorers are reused without changing their
prompts. Supplemental checks run only after submission, never inside an agent.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from budget_guard import write_json
from run_baselines import AGENTS, CONTINUATION_SECONDS, cleanup_phase, digest, safe_error, usage
from run_consolidate import SOURCE_CHECKPOINT, SOURCE_RUN, prepare_checkpoint
from run_solo_smoke import MAX_OUTPUT, MODEL, ROOT, configure

RUNS = ROOT / "artifacts/repeat_runs"
DEFAULT_RUN = RUNS / "repeat_smoke_jinja1621_v1"
ORDER = ((2, ("strongshare", "consolidate", "keep")), (3, ("consolidate", "keep", "strongshare")))
SOURCE_FILES = (
    "run_repeats.py", "run_repeats_with_together_key.sh", "run_baselines.py", "run_consolidate.py",
    "baseline_runtime.py", "baseline_model.py", "baseline_evidence.py", "budget_guard.py", "run_solo_smoke.py",
    "evaluate_baseline_smoke.py", "evaluate_consolidate_smoke.py", "evaluate_solo_smoke.py",
    "evaluate_supplemental.py", "supplemental_probe_v1.py", "artifacts/task_image.json",
)


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def file_hash(path):
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Ordinary saved file required: " + path.name)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    return {name: file_hash(ROOT / name) for name in SOURCE_FILES}


def job_plan(suite_dir):
    suffix = suite_dir.name.removeprefix("repeat_smoke_")
    jobs = []
    for repeat, order in ORDER:
        for position, condition in enumerate(order, 1):
            family = "consolidate" if condition == "consolidate" else "baseline"
            run_dir = ROOT / "artifacts" / (family + "_runs") / (
                family + f"_smoke_repeat_{suffix}_r{repeat:02d}_{condition}")
            jobs.append({"repeat": repeat, "position": position, "condition": condition,
                         "job_id": f"r{repeat:02d}_{condition}", "run_directory": str(run_dir),
                         "condition_directory": str(run_dir / condition)})
    return jobs


def protocol_for(suite_dir, offline=False):
    return {
        "protocol_version": "jinja1621_repeated_continuations_v1", "offline_selftest": offline,
        "model": "offline-scripted-model" if offline else MODEL,
        "temperature": 0.7, "top_p": 0.8, "enable_thinking": False, "seed": None,
        "task": {"repo": "pallets_jinja_task", "id": 1621, "features": [1, 6]},
        "checkpoint_sha256": SOURCE_CHECKPOINT, "source_run": str(SOURCE_RUN),
        "continuation_seconds": CONTINUATION_SECONDS, "max_output_tokens": MAX_OUTPUT,
        "money_limit": None, "request_limit": None, "compaction_enabled": False,
        "jobs": job_plan(suite_dir), "sequential_conditions": True,
        "scoring": {
            "official": "Unchanged pinned CooperBench evaluator, including its native fallback semantics",
            "supplemental_suite_sha256": file_hash(ROOT / "supplemental_probe_v1.py"),
            "enhanced_success": "official both_passed AND all supplemental checks passed on the accepted artifact",
            "supplemental_feedback_to_agents": False,
        },
        "stopping": {
            "task_failure": "Keep result and continue to next predeclared job; never retry for a better outcome",
            "infrastructure_failure": "Pause batch, preserve cost/trace; resume only incomplete stage after repair",
            "evaluation_failure": "Preserve completed model run; retry evaluator only",
        },
        "accounting": {
            "new_actual_spend": "Sum all newly incurred requests including failed infrastructure attempts",
            "full_task_equivalent": "Each accepted continuation plus original prefix once, for comparison only",
            "prefix_reexecuted_or_rebilled": False,
        },
        "interpretation": "Two new samples per condition on one fixed task/checkpoint; exploratory first run kept separate. Rotating positions is not full order balance within the new six runs. No significance or population win-rate claim.",
        "source_manifest": source_manifest(),
    }


def verify_preflight():
    proof = read_json(ROOT / "artifacts/repeats_offline_validation.json", {})
    if not proof.get("passed") or proof.get("source_manifest") != source_manifest():
        raise RuntimeError("Complete offline repeated-run validation for these source versions first")


def patch_hashes(condition_dir, condition):
    names = ["final.patch"] if condition == "consolidate" else ["agent1.patch", "agent6.patch"]
    return {name: file_hash(condition_dir / name) for name in names}


def archive_failed_job(run_dir, condition):
    phase_dir = run_dir / condition
    if not phase_dir.exists():
        return
    archive = run_dir / "failed_attempts"
    archive.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.move(str(phase_dir), str(archive / (condition + "_" + stamp)))
    ledger_path = run_dir / "usage_ledger.json"
    if ledger_path.exists():
        ledger = read_json(ledger_path)
        for row in ledger["requests"]:
            if row["phase"].startswith(condition + ":") and not row.get("excluded_from_baseline"):
                row.update(excluded_from_baseline=True, excluded_reason="incomplete infrastructure attempt",
                           archived_attempt=stamp)
                if row.get("status") == "in_flight":
                    row.update(status="error", error_type="InterruptedAttemptUsageUnknown")
        write_json(ledger_path, ledger)


def run_command(command, log_path, timeout, lock_fd, env=None):
    """One supervised subprocess; caller owns stage-specific Docker cleanup."""
    with log_path.open("wb") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, pass_fds=(lock_fd,), env=env)
        try:
            return proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=15)
            raise


def verify_execution_receipt(condition_dir, condition):
    receipt_path = condition_dir / "execution_receipt.json"
    actual = patch_hashes(condition_dir, condition)
    if condition == "consolidate":
        status = read_json(condition_dir / "status.json", {})
        if status.get("final_patch_sha256") != actual["final.patch"]:
            raise RuntimeError("Consolidate submission differs from its completed worker status")
    if receipt_path.exists():
        if read_json(receipt_path)["patch_sha256"] != actual:
            raise RuntimeError("Completed submission changed; refusing to resume")
    else:
        # Written before evaluation. This also recovers a process interrupted
        # immediately after the worker saved its completion status.
        write_json(receipt_path, {"patch_sha256": actual})
    return actual


def run_job(job, suite_dir, protocol, lock_fd, offline=False):
    condition = job["condition"]
    run_dir, phase_dir = Path(job["run_directory"]), Path(job["condition_directory"])
    run_dir.mkdir(parents=True, exist_ok=True)
    _, provenance = prepare_checkpoint(run_dir)
    config = {"suite_directory": str(suite_dir), "protocol_sha256": digest(protocol), "job": job,
              "model": protocol["model"], "offline_selftest": offline, "checkpoint_sha256": SOURCE_CHECKPOINT}
    config_path = run_dir / "repeat_job_config.json"
    if config_path.exists() and read_json(config_path) != config:
        raise RuntimeError("Existing repeat job configuration differs")
    write_json(config_path, config)
    status_path = phase_dir / "status.json"
    status = read_json(status_path, {})
    if status.get("status") != "completed":
        if not offline and not os.environ.get("TOGETHER_API_KEY", "").strip():
            raise RuntimeError("TOGETHER_API_KEY absent; use run_repeats_with_together_key.sh")
        archive_failed_job(run_dir, condition)
        phase_dir.mkdir()
        write_json(phase_dir / "source_manifest.json", source_manifest())
        command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", "--condition", condition,
                   "--run-dir", str(run_dir)]
        if offline:
            command.append("--offline-selftest")
        print(f"{job['job_id']}: running from the common checkpoint", flush=True)
        try:
            code = run_command(command, phase_dir / "console.log", CONTINUATION_SECONDS + 240, lock_fd)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            write_json(status_path, {"status": "error", "infrastructure_error": True,
                                     "error": type(exc).__name__})
            raise
        finally:
            cleanup_phase(run_dir, condition)
        status = read_json(status_path, {})
        if code or status.get("status") != "completed":
            raise RuntimeError(f"{job['job_id']}: model execution paused; inspect saved status")
    submission_hashes = verify_execution_receipt(phase_dir, condition)
    official = read_json(phase_dir / "evaluation_summary.json", {})
    if not official.get("evaluation_completed"):
        evaluator = "evaluate_consolidate_smoke.py" if condition == "consolidate" else "evaluate_baseline_smoke.py"
        from evaluate_solo_smoke import evaluator_environment
        code = run_command([sys.executable, str(ROOT / evaluator), "--run-dir", str(phase_dir)],
                           phase_dir / "evaluation_console.log", 720, lock_fd, env=evaluator_environment())
        official = read_json(phase_dir / "evaluation_summary.json", {})
        if code or not official.get("evaluation_completed"):
            raise RuntimeError(f"{job['job_id']}: official evaluator paused; model execution will not repeat")
    if patch_hashes(phase_dir, condition) != submission_hashes:
        raise RuntimeError("Submission changed during official scoring")
    supplemental = read_json(phase_dir / "supplemental_summary.json", {})
    suite_hash = protocol["scoring"]["supplemental_suite_sha256"]
    if supplemental.get("supplemental_evaluation_completed") and supplemental.get("suite_sha256") != suite_hash:
        raise RuntimeError("Saved supplemental score uses a different frozen suite")
    if not supplemental.get("supplemental_evaluation_completed"):
        from evaluate_solo_smoke import evaluator_environment
        code = run_command([sys.executable, str(ROOT / "evaluate_supplemental.py"), "--condition-dir", str(phase_dir)],
                           phase_dir / "supplemental_console.log", 360, lock_fd, env=evaluator_environment())
        supplemental = read_json(phase_dir / "supplemental_summary.json", {})
        if code or not supplemental.get("supplemental_evaluation_completed"):
            raise RuntimeError(f"{job['job_id']}: supplemental evaluator paused; model execution will not repeat")
    if supplemental.get("suite_sha256") != suite_hash:
        raise RuntimeError("Supplemental score does not match the frozen suite")
    if patch_hashes(phase_dir, condition) != submission_hashes:
        raise RuntimeError("Submission changed during supplemental scoring")
    row = build_row(job, provenance, status, official, supplemental)
    write_json(run_dir / "repeat_result.json", row)
    print(f"{job['job_id']}: official={row['official_success']}, enhanced={row['enhanced_success']}, "
          f"continuation=${row['continuation_usage']['known_cost_usd']:.4f}", flush=True)
    return row


def build_row(job, provenance, execution, official, supplemental):
    run_dir = Path(job["run_directory"])
    accepted = usage(run_dir, job["condition"])
    prefix = provenance["prefix_usage"]
    return {
        **job, "official_success": bool(official["both_passed"]),
        "supplemental_success": supplemental.get("supplemental_passed"),
        "enhanced_success": bool(official["both_passed"] and supplemental.get("supplemental_passed")),
        "supplemental_status": supplemental.get("status"),
        "continuation_usage": accepted, "new_actual_usage": usage(run_dir),
        "excluded_infrastructure_usage": usage(run_dir, excluded_only=True),
        "full_task_equivalent_known_cost_usd": round(prefix["known_cost_usd"] + accepted["known_cost_usd"], 9),
        "full_task_equivalent_unknown_usage_requests": prefix["unknown_usage_requests"] + accepted["unknown_usage_requests"],
        "execution_seconds_including_prefix": round(provenance["prefix_elapsed_seconds"] + execution["elapsed_seconds"], 3),
        "official_evaluation_seconds": official.get("elapsed_seconds"),
        "supplemental_evaluation_seconds": supplemental.get("elapsed_seconds"),
        "execution": execution, "official_evaluation": official, "supplemental_evaluation": supplemental,
    }


def batch_summary(protocol, rows):
    totals = {"requests": 0, "known_cost_usd": 0.0, "unknown_usage_requests": 0,
              "prompt_tokens": 0, "completion_tokens": 0}
    # Include incomplete job attempts too; reporting must never hide spent requests.
    for job in protocol["jobs"]:
        actual = usage(Path(job["run_directory"]))
        for key in totals:
            totals[key] += actual[key]
    totals["known_cost_usd"] = round(totals["known_cost_usd"], 9)
    aggregates = {}
    for condition in ("keep", "strongshare", "consolidate"):
        group = [row for row in rows if row["condition"] == condition]
        costs = [row["full_task_equivalent_known_cost_usd"] for row in group]
        aggregates[condition] = {
            "completed_new_repeats": len(group), "planned_new_repeats": 2,
            "official_successes": sum(row["official_success"] for row in group),
            "enhanced_successes": sum(row["enhanced_success"] for row in group),
            "full_task_equivalent_known_costs_usd": costs,
            "median_known_cost_usd": statistics.median(costs) if costs else None,
        }
    return {"pipeline_completed": len(rows) == len(protocol["jobs"]), "offline_selftest": protocol["offline_selftest"],
            "protocol_sha256": digest(protocol), "rows": rows, "conditions": aggregates,
            "new_actual_collection_usage": totals,
            "exploratory_first_run": {"baseline_directory": str(SOURCE_RUN),
                "consolidate_directory": str(ROOT / "artifacts/consolidate_runs/consolidate_smoke_20261030T030112Z"),
                "pooled_into_new_repeat_statistics": False},
            "interpretation": protocol["interpretation"]}


def validate_offline_completion(protocol, rows, summary):
    if len(rows) != 6 or summary["new_actual_collection_usage"]["requests"] != 0:
        raise RuntimeError("Offline batch did not complete six jobs with zero API requests")
    for row in rows:
        phase_dir = Path(row["condition_directory"])
        outcome = row["execution"]
        if row["condition"] == "consolidate":
            if outcome.get("outcome") != "Submitted" or outcome.get("phase_turns") != 4:
                raise RuntimeError("Offline Consolidate did not submit after four scripted turns")
            if "consolidate_offline_probe.txt" not in (phase_dir / "final.patch").read_text():
                raise RuntimeError("Offline Consolidate patch missing")
        else:
            for agent, feature in AGENTS.items():
                record = outcome["outcomes"][agent]
                if record.get("status") != "Submitted" or record.get("phase_turns") != 4:
                    raise RuntimeError("Offline paired continuation did not submit")
                if f"repeat_offline_{agent}.txt" not in (phase_dir / f"agent{feature}.patch").read_text():
                    raise RuntimeError("Offline paired patch missing")
        if not row["official_evaluation"].get("evaluation_completed") or not row["supplemental_evaluation"].get("supplemental_evaluation_completed"):
            raise RuntimeError("Offline scoring incomplete")


def supervise(suite_dir, offline=False):
    configure()
    suite_dir.mkdir(parents=True, exist_ok=True)
    print(suite_dir, flush=True)
    with (ROOT / "artifacts/model_run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another experiment owns the model-run lock")
        if not offline:
            verify_preflight()
        protocol = protocol_for(suite_dir, offline)
        protocol_path = suite_dir / "protocol.json"
        if protocol_path.exists() and read_json(protocol_path) != protocol:
            raise RuntimeError("Frozen protocol/source differs; do not silently change a running batch")
        write_json(protocol_path, protocol)
        rows = []
        try:
            for job in protocol["jobs"]:
                if source_manifest() != protocol["source_manifest"]:
                    raise RuntimeError("Source changed during the batch")
                write_json(suite_dir / "status.json", {"status": "running", "current_job": job,
                                                       "completed_jobs": len(rows), "planned_jobs": 6})
                rows.append(run_job(job, suite_dir, protocol, lock.fileno(), offline))
                write_json(suite_dir / "repeat_summary.json", batch_summary(protocol, rows))
            summary = batch_summary(protocol, rows)
            if offline:
                validate_offline_completion(protocol, rows, summary)
                write_json(ROOT / "artifacts/repeats_offline_validation.json", {
                    "passed": True, "suite_directory": str(suite_dir), "model_api_calls": 0,
                    "source_manifest": source_manifest(), "jobs_completed": 6,
                    "all_conditions_restored_submitted_and_scored": True,
                })
            write_json(suite_dir / "status.json", {"status": "completed", "completed_jobs": 6, "planned_jobs": 6})
            print("All six continuations and both score types completed: " + str(suite_dir / "repeat_summary.json"), flush=True)
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite_dir / "repeat_summary.json", batch_summary(protocol, rows))
            write_json(suite_dir / "status.json", {"status": "paused", "completed_jobs": len(rows),
                                                   "error_type": type(exc).__name__, "error": safe_error(exc)})
            print("Batch paused: " + safe_error(exc), flush=True)
            return 2


def make_paired_offline_model(agent_id, phase):
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    peer = "agent2" if agent_id == "agent1" else "agent1"
    first = (f"test -s /shared-evidence/{peer}/latest/task.md && "
             f"test -s /shared-evidence/{peer}/latest/history.json && "
             "if touch /shared-evidence/write-probe; then exit 88; else echo evidence_read_only; fi") if phase == "strongshare" else (
             "test ! -d /shared-evidence && echo ordinary_visibility")
    commands = [first,
        f"printf 'synthetic repeat plumbing test\\n' > repeat_offline_{agent_id}.txt && "
        f"git add repeat_offline_{agent_id}.txt && git commit -m 'Offline repeat test' && git push",
        "gh pr create --title 'Offline repeat test' --body 'Synthetic infrastructure check'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]

    class OfflineModel(LitellmModel):
        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            from run_consolidate import validate_message_layout
            validate_message_layout(messages)
            if self.index >= len(commands):
                raise RuntimeError("Unexpected additional offline query")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-{phase}-{agent_id}-{self.index}", "object": "chat.completion", "created": 0,
                "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline infrastructure check.", "tool_calls": [{
                    "id": f"call_repeat_{agent_id}_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def worker(run_dir, condition, offline):
    if condition == "consolidate":
        from run_consolidate import phase_worker
    else:
        import run_baselines
        if offline:
            run_baselines.make_fake_model = make_paired_offline_model
        return run_baselines.phase_worker(run_dir, condition, offline)
    return phase_worker(run_dir, offline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--condition", choices=("keep", "strongshare", "consolidate"), help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        if not args.run_dir or not args.condition:
            raise ValueError("Worker requires run directory and condition")
        run_dir = args.run_dir.resolve()
        family = "consolidate" if args.condition == "consolidate" else "baseline"
        if run_dir.parent != (ROOT / "artifacts" / (family + "_runs")).resolve() or not run_dir.name.startswith(family + "_smoke_repeat_"):
            raise ValueError("Invalid repeat worker directory")
        return worker(run_dir, args.condition, args.offline_selftest)
    default = RUNS / ("repeat_smoke_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")) if args.offline_selftest else DEFAULT_RUN
    suite_dir = (args.suite_dir or default).resolve()
    if suite_dir.parent != RUNS.resolve() or not re.fullmatch(r"repeat_smoke_[A-Za-z0-9_-]+", suite_dir.name):
        raise ValueError("Suite directory must be artifacts/repeat_runs/repeat_smoke_*")
    return supervise(suite_dir, args.offline_selftest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + safe_error(exc), file=sys.stderr)
        sys.exit(2)
