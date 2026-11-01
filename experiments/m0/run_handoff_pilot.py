"""Four source-handoff continuations of one frozen Dirty Equals checkpoint."""
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
from run_solo_smoke import MAX_OUTPUT, MODEL, ROOT, configure
from run_task_expansion import add_usage, file_hash, verify_references, write_fixed
from task_specs import REPO, get_task
from wip_checkpoint import validate_checkpoint

TASK_KEY = "dirty_equals43_f3_f7"
TASK = get_task(TASK_KEY)
RUNS = ROOT / "artifacts/expansion_runs"
SOURCE_RUN = RUNS / "expansion_wip_v1" / TASK_KEY / "prefix"
SOURCE_CHECKPOINT = "2d3f607d0ae8071442fc094753eec00632d48ba6590187e42060617dabeaa7c0"
CONDITIONS = ("strongshare", "consolidate")
CONTINUATION_SECONDS = 1200
SOURCE_FILES = (
    "run_handoff_pilot.py", "run_handoff_pilot_with_together_key.sh",
    "handoff_worker.py", "handoff_packet.py", "task_specs.py", "wip_checkpoint.py",
    "baseline_runtime.py", "baseline_model.py", "baseline_evidence.py", "budget_guard.py",
    "run_baselines.py", "run_consolidate.py", "run_repeats.py", "run_task_expansion.py",
    "run_solo_smoke.py", "evaluate_task_expansion.py", "evaluate_solo_smoke.py",
)


def source_manifest():
    paths = [ROOT / name for name in SOURCE_FILES]
    paths += [SOURCE_RUN / name for name in (
        "checkpoint.json", "shared_git.tar", "usage_ledger.json", "prefix/status.json")]
    paths += [TASK.image_record_path, ROOT / "artifacts/task_expansion_reference_validation.json",
              REPO / "src/cooperbench/utils.py"]
    paths += sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2").rglob("*.py"))
    paths += sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2").rglob("*.sh"))
    paths += sorted((REPO / "src/cooperbench/agents/mini_swe_agent_v2/config").glob("*.yaml"))
    paths += sorted((REPO / "src/cooperbench/eval").rglob("*.py"))
    paths += [TASK.task_dir / "runner.sh"]
    paths += [TASK.task_dir / f"feature{feature}" / name
              for feature in TASK.features for name in ("feature.md", "tests.patch")]
    return {str(path.relative_to(ROOT)): file_hash(path) for path in paths}


def source_checkpoint():
    checkpoint = read_json(SOURCE_RUN / "checkpoint.json")
    if digest(checkpoint) != SOURCE_CHECKPOINT:
        raise RuntimeError("The predeclared Dirty Equals checkpoint changed")
    validate_checkpoint(checkpoint, TASK)
    if file_hash(SOURCE_RUN / "shared_git.tar") != checkpoint["git_archive_sha256"]:
        raise RuntimeError("The source shared Git archive changed")
    if read_json(SOURCE_RUN / "prefix/status.json").get("status") != "completed":
        raise RuntimeError("The original prefix is not complete")
    return checkpoint


def protocol_for(offline):
    source_checkpoint()
    return {
        "version": "dirty_source_handoff_v1", "offline_selftest": offline,
        "task": TASK.as_dict(), "checkpoint_sha256": SOURCE_CHECKPOINT,
        "source_run": str(SOURCE_RUN),
        "jobs": [{"task": TASK_KEY, "repeat": repeat, "condition": condition}
                 for repeat, order in ((1, ("strongshare", "consolidate")),
                                       (2, ("consolidate", "strongshare")))
                 for condition in order],
        "condition_version": "source_handoff_v1",
        "condition_interpretation": {
            "strongshare": "Two continuing agents receive the source handoff evidence and retain separate responsibilities.",
            "consolidate": "One continuing agent receives the same source handoff evidence and owns both features; the peer is retired.",
        },
        "model": "offline-scripted-model" if offline else MODEL,
        "temperature": 0.7, "top_p": 0.8, "enable_thinking": False, "seed": None,
        "max_output_tokens": MAX_OUTPUT, "money_limit": None, "request_limit": None,
        "continuation_seconds": CONTINUATION_SECONDS,
        "time_rule": "Preserve the prior runner: 1200 seconds after initialization and handoff delivery. Report actual worker elapsed including initialization; evaluation is separate.",
        "handoff_scope": "Source evidence from the frozen checkpoint only; no prior continuation, evaluator outcome, reference answer or new prefix.",
        "task_failure": "Retain every completed outcome and continue; never resample for success.",
        "infrastructure_failure": "Pause and preserve all usage. On explicit resume, archive only incomplete execution; a completed model or score is not rerun.",
        "actual_cost": "New continuation ledgers including incomplete attempts; historical prefix is not rerun or billed.",
        "comparison_cost": "Each accepted continuation plus the historical shared prefix once; this is not a billing total.",
        "score": "Frozen official CooperBench evaluator: native two-patch merge for StrongShare, native single-patch scoring for Consolidate.",
        "source_manifest": source_manifest(), "reference_validation": verify_references(),
    }


def verify_preflight():
    verify_references()
    source_checkpoint()
    proof = read_json(ROOT / "artifacts/handoff_offline_validation.json", {})
    if (not proof.get("passed") or proof.get("model_api_calls") != 0
            or proof.get("jobs_completed") != 4 or proof.get("checkpoint_sha256") != SOURCE_CHECKPOINT
            or proof.get("source_manifest") != source_manifest()):
        raise RuntimeError("Complete all four offline handoff executions and scores for these source versions first")


def prepare_checkpoint(run_dir):
    checkpoint = source_checkpoint()
    run_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("checkpoint.json", "shared_git.tar"):
        source, target = SOURCE_RUN / name, run_dir / name
        data = source.read_bytes()
        if target.exists() and (file_hash(target) != file_hash(source) or target.read_bytes() != data):
            raise RuntimeError("Saved handoff branch input changed: " + name)
        if not target.exists():
            target.write_bytes(data)
        files[name] = file_hash(target)
    provenance = {
        "source_directory": str(SOURCE_RUN), "checkpoint_sha256": SOURCE_CHECKPOINT, "files": files,
        "prefix_usage": usage(SOURCE_RUN, "prefix"),
        "prefix_elapsed_seconds": read_json(SOURCE_RUN / "prefix/status.json")["elapsed_seconds"],
        "new_prefix_model_calls": 0,
    }
    write_fixed(run_dir / "checkpoint_provenance.json", provenance)
    return checkpoint, provenance


def configure_run(run_dir, task, condition, protocol):
    run_dir.mkdir(parents=True, exist_ok=True)
    write_fixed(run_dir / "task_config.json", {
        "task_key": task.key, "task": task.as_dict(), "condition": condition,
        "condition_version": protocol["condition_version"], "offline_selftest": protocol["offline_selftest"],
        "protocol_sha256": digest(protocol), "checkpoint_sha256": SOURCE_CHECKPOINT,
        "continuation_seconds": CONTINUATION_SECONDS,
    })


def verify_restore(phase_dir, checkpoint):
    proof = read_json(phase_dir / "restore_proof.json")
    if proof.get("checkpoint_hash") != digest(checkpoint) or proof.get("broker_hash") != digest(checkpoint["broker"]):
        raise RuntimeError("Restored checkpoint or mailbox hash differs")
    if set(proof.get("agents", {})) != set(TASK.agents):
        raise RuntimeError("Restore proof is missing an agent")
    for agent_id, saved in checkpoint["agents"].items():
        actual = proof["agents"][agent_id]
        if (actual.get("state_hash") != digest(saved["state"])
                or actual.get("filesystem_manifest_hash") != digest(saved["manifest"])
                or actual.get("source_image") != saved["image"]
                or not actual.get("verified", actual.get("filesystem_verified", False))):
            raise RuntimeError("An agent was not restored from the common checkpoint")
    return proof


def protected_execution_files(phase_dir):
    run_dir = phase_dir.parent
    paths = [run_dir / name for name in (
        "task_config.json", "checkpoint.json", "shared_git.tar", "checkpoint_provenance.json")]
    paths += [phase_dir / name for name in (
        "status.json", "restore_proof.json", "source_manifest.json", "handoff_packet.json",
        "handoff_packet.txt", "handoff_delivery.json")]
    active = ("agent1",) if phase_dir.name == "consolidate" else tuple(TASK.agents)
    if phase_dir.name == "consolidate":
        paths.append(phase_dir / "intervention.json")
    for agent in active:
        paths += [phase_dir / f"first_request_{agent}.json", phase_dir / f"{agent}.traj.json"]
        inputs = sorted((phase_dir / "responses" / agent).glob("request_*_input.json"))
        if inputs:
            paths.append(inputs[0])
    paths += list((phase_dir / "evidence_manifests").glob("*_checkpoint.json"))
    for path in phase_dir.glob("handoff_*"):
        paths += [path] if path.is_file() or path.is_symlink() else list(path.rglob("*"))
    paths = {path for path in paths if not path.is_dir()}
    return {str(path.relative_to(run_dir)): file_hash(path) for path in sorted(paths)}


def verify_handoff(phase_dir, task, condition, status):
    from handoff_packet import packet_message
    packet = read_json(phase_dir / "handoff_packet.json")
    packet_hash = digest(packet)
    if (packet.get("checkpoint_sha256") != SOURCE_CHECKPOINT or packet.get("task_key") != task.key
            or packet.get("base_commit") != task.base or packet.get("checkpoint_only") is not True
            or set(packet.get("agents", {})) != set(task.agents)):
        raise RuntimeError("Handoff packet is not tied to the complete common checkpoint")
    text = packet_message(packet)
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    if ((phase_dir / "handoff_packet.txt").read_text() != text
            or status.get("handoff_variant") != "explicit_checkpoint_packet_v1"
            or status.get("handoff_packet_sha256") != packet_hash):
        raise RuntimeError("Handoff packet text or completed worker digest changed")
    active = ("agent1",) if condition == "consolidate" else tuple(task.agents)
    delivery = read_json(phase_dir / "handoff_delivery.json")
    if (delivery.get("condition") != condition or delivery.get("checkpoint_sha256") != SOURCE_CHECKPOINT
            or delivery.get("packet_sha256") != packet_hash
            or delivery.get("source_checkpoint_verified") is not True
            or delivery.get("code_automatically_transferred") is not False
            or delivery.get("first_request_verified") is not True
            or set(delivery.get("recipients", {})) != set(active)):
        raise RuntimeError("Handoff delivery is incomplete or belongs to another condition")
    for agent in active:
        recipient = delivery["recipients"][agent]
        first = read_json(phase_dir / f"first_request_{agent}.json")
        if (recipient.get("packet_sha256") != packet_hash or recipient.get("message_sha256") != text_hash
                or recipient.get("history_preserved") is not True
                or recipient.get("all_packet_content_delivered") is not True or recipient.get("role") != "user"
                or type(recipient.get("message_index")) is not int or recipient["message_index"] < 1
                or first.get("recipient") != agent or first.get("verified") is not True
                or first.get("full_packet_present") is not True or first.get("message_sha256") != text_hash):
            raise RuntimeError("An active agent lacks a verified complete handoff query")
        if not status["offline_selftest"]:
            inputs = sorted((phase_dir / "responses" / agent).glob("request_*_input.json"))
            if not inputs or first.get("actual_transport_input_verified") is not True:
                raise RuntimeError("Actual first provider input has not been verified")
            messages = read_json(inputs[0])["request"]["messages"]
            if sum(m.get("role") == "user" and m.get("content") == text for m in messages) != 1:
                raise RuntimeError("Actual first provider input does not contain the complete packet")
    if condition == "consolidate" and status.get("retired_agent_model_calls") != 0:
        raise RuntimeError("The retired agent received model calls")
    return packet_hash


def verify_execution_receipt(phase_dir, task=TASK, condition=None):
    condition = condition or phase_dir.name
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") != "completed":
        raise RuntimeError("Only completed handoff execution may be scored")
    checkpoint = read_json(phase_dir.parent / "checkpoint.json")
    if digest(checkpoint) != SOURCE_CHECKPOINT:
        raise RuntimeError("Branch checkpoint changed")
    verify_restore(phase_dir, checkpoint)
    packet_hash = verify_handoff(phase_dir, task, condition, status)
    names = ["final.patch"] if condition == "consolidate" else [f"agent{f}.patch" for f in task.features]
    hashes = {name: file_hash(phase_dir / name) for name in names}
    if condition == "consolidate" and status.get("final_patch_sha256") != hashes["final.patch"]:
        raise RuntimeError("Consolidate patch differs from completed worker status")
    receipt = {"patch_sha256": hashes, "handoff_packet_sha256": packet_hash,
               "protected_files_sha256": protected_execution_files(phase_dir)}
    write_fixed(phase_dir / "execution_receipt.json", receipt)
    return receipt


def run_model(run_dir, task, condition, protocol, lock_fd):
    phase_dir = run_dir / condition
    status = read_json(phase_dir / "status.json", {})
    if status.get("status") == "completed":
        verify_execution_receipt(phase_dir, task, condition)
        # A completed worker may have paused only because its cleanup failed.
        # Retry cleanup, never its delivered model execution.
        cleanup_phase(run_dir, condition)
        return status
    if not protocol["offline_selftest"] and not os.environ.get("TOGETHER_API_KEY", "").strip():
        raise RuntimeError("TOGETHER_API_KEY absent; use run_handoff_pilot_with_together_key.sh")
    archive_failed_job(run_dir, condition)
    phase_dir.mkdir()
    write_json(phase_dir / "source_manifest.json", protocol["source_manifest"])
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker",
               "--run-dir", str(run_dir), "--condition", condition]
    if protocol["offline_selftest"]:
        command.append("--offline-selftest")
    print("Source handoff: " + run_dir.name, flush=True)
    try:
        code = run_command(command, phase_dir / "console.log", CONTINUATION_SECONDS + 300, lock_fd)
    finally:
        cleanup_phase(run_dir, condition)
    status = read_json(phase_dir / "status.json", {})
    if code or status.get("status") != "completed":
        raise RuntimeError("Handoff execution paused: " + str(phase_dir))
    verify_execution_receipt(phase_dir, task, condition)
    return status


def completed_evaluation(phase_dir, task):
    """Validate cached official evidence; the frozen evaluator itself has no cache."""
    import evaluate_task_expansion as evaluator
    summary = read_json(phase_dir / "evaluation_summary.json", {})
    if not summary.get("evaluation_completed"):
        return None
    loaded_task, config = evaluator.load_task(phase_dir)
    if loaded_task != task or summary.get("task") != task.as_dict():
        raise RuntimeError("Completed score belongs to another task")
    original = Path(summary.get("attempt_directory", ""))
    attempt = original.resolve(strict=True)
    if original != attempt or attempt.parent != phase_dir / "evaluation_attempts":
        raise RuntimeError("Saved evaluator attempt is outside this phase")
    if summary != read_json(attempt / "evaluation_summary.json"):
        raise RuntimeError("Completed score summary differs from its accepted attempt")
    current = evaluator.input_manifest(phase_dir, task, evaluator.patch_info(phase_dir, task))
    entry = read_json(attempt / "evaluation_input.json")
    integrity = read_json(attempt / "integrity.json")
    if (entry.get("input_manifest") != current or not integrity.get("unchanged")
            or integrity.get("before") != current or integrity.get("after") != current
            or entry.get("task") != task.as_dict()
            or entry.get("protocol_sha256") != config["protocol_sha256"]):
        raise RuntimeError("Completed score inputs or evaluator sources changed")
    evaluator.verify_upstream(task)
    native = read_json(attempt / "official_evaluation.json")
    if (native != read_json(attempt / "evaluation.json") or native != read_json(phase_dir / "evaluation.json")
            or native.get("error") or native.get("cleanup_error")):
        raise RuntimeError("Saved official result no longer matches its accepted result")
    expected = evaluator.summarize(native, "passed" if native.get("both_passed") else "failed",
                                   phase_dir, attempt, summary["elapsed_seconds"], task, config)
    if summary != expected:
        raise RuntimeError("Completed score differs from its official native result")
    return summary


def evaluate(run_dir, task, condition, lock_fd):
    phase_dir = run_dir / condition
    before = verify_execution_receipt(phase_dir, task, condition)
    cached = completed_evaluation(phase_dir, task)
    if cached is not None:
        return cached
    from evaluate_solo_smoke import evaluator_environment
    code = run_command([sys.executable, str(ROOT / "evaluate_task_expansion.py"),
                        "--run-dir", str(phase_dir)], phase_dir / "evaluation_console.log",
                       720, lock_fd, env=evaluator_environment())
    score = read_json(phase_dir / "evaluation_summary.json", {})
    if code or not score.get("evaluation_completed"):
        raise RuntimeError("Handoff evaluation paused; completed model execution will not repeat")
    if verify_execution_receipt(phase_dir, task, condition) != before:
        raise RuntimeError("Handoff execution evidence changed during scoring")
    return completed_evaluation(phase_dir, task)


def build_row(run_dir, task, repeat, condition, provenance, execution, score):
    accepted = usage(run_dir, condition)
    prefix = provenance["prefix_usage"]
    return {
        "task": task.key, "repeat": repeat, "condition": condition,
        "condition_version": "source_handoff_v1", "run_directory": str(run_dir),
        "checkpoint_sha256": SOURCE_CHECKPOINT, "handoff_packet_sha256": execution["handoff_packet_sha256"],
        "official_success": bool(score["both_passed"]),
        "execution": execution, "evaluation": score, "continuation_usage": accepted,
        "full_task_equivalent_known_cost_usd": round(prefix["known_cost_usd"] + accepted["known_cost_usd"], 9),
        "full_task_equivalent_unknown_usage_requests": prefix["unknown_usage_requests"] + accepted["unknown_usage_requests"],
        "comparison_seconds": round(provenance["prefix_elapsed_seconds"] + execution["elapsed_seconds"] + score["elapsed_seconds"], 3),
    }


def summarize(suite_dir, protocol, rows):
    ledgers = sorted(suite_dir.glob("*/jobs/*/usage_ledger.json"))
    return {
        "pipeline_completed": len(rows) == 4, "protocol_sha256": digest(protocol),
        "offline_selftest": protocol["offline_selftest"], "condition_version": protocol["condition_version"],
        "rows": rows, "actual_new_usage": add_usage([usage(path.parent) for path in ledgers]),
        "interpretation": "Two source-handoff samples per condition at the one fixed Dirty Equals checkpoint. Earlier versions remain separate. Historical prefix appears only in comparison cost and time.",
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
                if source_manifest() != protocol["source_manifest"]:
                    raise RuntimeError("Source changed during handoff pilot")
                condition, repeat = job["condition"], job["repeat"]
                run_dir = suite_dir / TASK_KEY / "jobs" / f"{TASK_KEY}_r{repeat:02d}_{condition}"
                configure_run(run_dir, TASK, condition, protocol)
                _, provenance = prepare_checkpoint(run_dir)
                write_json(suite_dir / "status.json", {"status": "running", "task": TASK_KEY,
                           "condition": condition, "repeat": repeat, "completed_jobs": len(rows), "planned_jobs": 4})
                execution = run_model(run_dir, TASK, condition, protocol, lock.fileno())
                if rows and execution["handoff_packet_sha256"] != rows[0]["handoff_packet_sha256"]:
                    raise RuntimeError("Conditions did not receive the same checkpoint handoff packet")
                score = evaluate(run_dir, TASK, condition, lock.fileno())
                row = build_row(run_dir, TASK, repeat, condition, provenance, execution, score)
                rows.append(row)
                write_json(run_dir / "result.json", row)
                write_json(suite_dir / "summary.json", summarize(suite_dir, protocol, rows))
                print(f"{condition} {repeat}: official={row['official_success']}, continuation=${row['continuation_usage']['known_cost_usd']:.4f}", flush=True)
            if source_manifest() != protocol["source_manifest"]:
                raise RuntimeError("Source changed during final handoff stage")
            summary = summarize(suite_dir, protocol, rows)
            write_json(suite_dir / "summary.json", summary)
            if offline:
                if len(rows) != 4 or summary["actual_new_usage"]["requests"]:
                    raise RuntimeError("Offline handoff validation requires four scores and zero API requests")
                for row in rows:
                    status = row["execution"]
                    outcomes = [status["outcome"]] if row["condition"] == "consolidate" else [o["status"] for o in status["outcomes"].values()]
                    if any(outcome != "Submitted" for outcome in outcomes):
                        raise RuntimeError("Offline handoff continuation did not publish its submission")
                    if any(o.get("phase_turns") != 4 for o in status["outcomes"].values()):
                        raise RuntimeError("Offline handoff did not execute all four validation steps")
                    verify_execution_receipt(Path(row["run_directory"]) / row["condition"], TASK, row["condition"])
                write_json(ROOT / "artifacts/handoff_offline_validation.json", {
                    "passed": True, "model_api_calls": 0, "jobs_completed": 4,
                    "checkpoint_sha256": SOURCE_CHECKPOINT, "source_manifest": protocol["source_manifest"],
                    "handoff_packet_sha256": rows[0]["handoff_packet_sha256"],
                    "suite_directory": str(suite_dir),
                })
            write_json(suite_dir / "status.json", {"status": "completed", "completed_jobs": 4})
            print("Handoff pilot completed: " + str(suite_dir / "summary.json"), flush=True)
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite_dir / "summary.json", summarize(suite_dir, protocol, rows))
            write_json(suite_dir / "status.json", {"status": "paused", "completed_jobs": len(rows), "error": safe_error(exc)})
            print("Handoff pilot paused: " + safe_error(exc), flush=True)
            return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--condition", choices=CONDITIONS)
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    if args.worker:
        from handoff_worker import phase_worker
        run_dir = args.run_dir.resolve()
        parts = run_dir.relative_to(RUNS.resolve()).parts
        if len(parts) != 4 or parts[1] != TASK_KEY or parts[2] != "jobs" or args.condition not in CONDITIONS:
            raise ValueError("Invalid handoff worker directory or condition")
        config = read_json(run_dir / "task_config.json")
        protocol = read_json(run_dir.parents[2] / "protocol.json")
        if (config.get("task") != TASK.as_dict() or config.get("condition") != args.condition
                or config.get("condition_version") != "source_handoff_v1"
                or config.get("offline_selftest") != args.offline_selftest
                or config.get("protocol_sha256") != digest(protocol)
                or protocol.get("source_manifest") != source_manifest()):
            raise ValueError("Handoff worker configuration or frozen source mismatch")
        prepare_checkpoint(run_dir)
        return phase_worker(run_dir, args.condition, offline=args.offline_selftest, task=TASK)
    default = "handoff_dirty_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else "handoff_dirty_v1"
    suite_dir = (args.suite_dir or RUNS / default).resolve()
    if suite_dir.parent != RUNS.resolve() or not re.fullmatch(r"handoff_dirty_[A-Za-z0-9_]+", suite_dir.name):
        raise ValueError("Invalid handoff suite directory")
    return supervise(suite_dir, args.offline_selftest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + safe_error(exc), file=sys.stderr)
        sys.exit(2)
