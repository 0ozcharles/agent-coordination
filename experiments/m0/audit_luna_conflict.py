"""Independent, read-only conflict-pilot audit; no model or scoring execution.

Only audit_conflict_integrity.json is written. Completed receipts and native
caches are verified in place. Pending trajectories are never inspected.
"""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import re

from audit_luna_publication import (digest, feature_observations, file_record,
    ledger_audit, load, number_equal, require, same_stats, sha, stats)
from conflict_evaluator import CONDITIONS, TASK_KEYS, VERSION, get_task, make_evaluator
from task_specs import ROOT

RUNS = ROOT / "artifacts/conflict_runs"
AUDIT_NAME = "audit_conflict_integrity.json"
CP_FILES = ("checkpoint.json", "shared_git.tar", "checkpoint_packet.json", "checkpoint_packet.txt")
BUDGET_FAILURES = ("budget_exhausted", "time_budget_exhausted_before_first_query")


def validate_suite(value):
    original = Path(value).expanduser().absolute()
    path = original.resolve(strict=True)
    require(original == path and path.is_dir() and path.parent == RUNS.resolve()
            and re.fullmatch(r"luna_conflict_[A-Za-z0-9_]+", path.name), "Invalid conflict audit suite")
    return path


def sources(suite, protocol):
    import run_luna_conflict as supervision
    current = supervision.source_manifest()
    require(current == protocol["source_manifest"], "Live source manifest differs from frozen conflict protocol")
    prior = load(supervision.ORIGIN / "protocol.json")["source_manifest"]
    require(len(prior) == 145 and len(current) == 150 and set(current) - set(prior) == set(supervision.NEW_SOURCES),
            "Expected exactly 145 unchanged sources plus five conflict modules")
    for name, expected in current.items():
        require(sha(suite / "source_snapshot" / name) == expected, "Frozen conflict snapshot changed: " + name)
    references = supervision.early_references()  # pure reads; does not call prefix_record/write_fixed
    require(references == protocol["early_references"], "Original early prefix evidence changed")
    require(protocol.get("version") == VERSION and protocol.get("terminal_policy") == "both_active"
            and protocol.get("submission_policy") == "authoritative_agent1_only", "Conflict protocol identity differs")
    require(protocol["model"] == ("offline-scripted-model" if protocol["offline_selftest"] else "gpt-6-luna"),
            "Protocol model differs from offline/real mode")
    require(protocol["jobs"] == supervision.schedule(protocol["offline_selftest"]), "Planned conflict cohort/order changed")
    return {"verified": True, "frozen_source_count": len(current), "unchanged_prior_source_count": len(prior),
            "protocol_sha256": sha(suite / "protocol.json"), "semantic_protocol_sha256": digest(protocol),
            "early_reference_verified": list(references), "historical_prefix_reexecuted": False}


def audit_collection(run, task, protocol, ledger):
    from conflict_runtime import verify_collection_artifacts, common_delivery_message
    from conflict_checkpoint import conflict_eligible, verify_checkpoint_files
    from conflict_packet import packet_message
    receipt = verify_collection_artifacts(run, task)
    phase = run / "strongshare"
    status, config = load(phase / "status.json"), load(run / "task_config.json")
    require(status.get("status") in ("completed", "no_conflict"), "Collector is not terminal")
    require(config["protocol_sha256"] == digest(protocol) and config["offline_selftest"] is protocol["offline_selftest"]
            and status.get("offline_selftest") is protocol["offline_selftest"], "Collector protocol/mode binding differs")
    require(config["submission_policy"] == "authoritative_agent1_only", "Collector lacks the shared delivery contract")
    expected_seconds = protocol["total_execution_seconds"] - protocol["early_references"][task.key]["record"]["elapsed_seconds"]
    require(number_equal(config["execution_seconds"], expected_seconds), "Collector allowance does not deduct historical prefix once")
    assignments = load(phase / "collection_assignment.json", {})
    if assignments:
        require(set(assignments.get("recipients", {})) == {"agent1", "agent2"}, "Collector contract did not reach both agents")
        for agent, entry in assignments["recipients"].items():
            require(entry.get("content") == common_delivery_message(task), "Collector common contract differs")
    progress = load(phase / "progress.json", [])
    for index, item in enumerate(progress, 1):
        require(item["paired_round"] == index, "Collector paired-boundary sequence has gaps")
        eligible = conflict_eligible(item["observations"], item["outcomes"], terminal_policy="both_active")
        require(item.get("eligible") is eligible, "Recorded eligibility differs from current unmerged index and agent state")
    eligible_indices = [i for i, item in enumerate(progress) if item["eligible"]]
    row = {"task": task.key, "state": status["status"], "eligible": status["status"] == "completed",
           "reason": status.get("reason"), "run_directory": str(run), "integrity_verified": True,
           "synthetic_offline_fixture": protocol["offline_selftest"], "elapsed_seconds": status["elapsed_seconds"],
           "paired_boundaries_checked": len(progress), "ledger": ledger,
           "receipt_sha256": sha(run / "collection_receipt.json"),
           "progress_sha256": sha(phase / "progress.json"), "common_delivery_contract_before_collection": bool(assignments)}
    if row["eligible"]:
        checkpoint = verify_checkpoint_files(run, task)
        require(checkpoint.get("synthetic_conflict") is protocol["offline_selftest"], "Synthetic checkpoint admitted to wrong mode")
        require(eligible_indices == [len(progress) - 1], "Captured state is not the first eligible paired boundary")
        gate = checkpoint["conflict_checkpoint"]
        require(gate["observations"] == progress[-1]["observations"] and gate["outcomes"] == progress[-1]["outcomes"]
                and gate["paired_round"] == progress[-1]["paired_round"], "Checkpoint differs from current eligible observation")
        packet = load(run / "checkpoint_packet.json")
        require(packet["checkpoint_sha256"] == digest(checkpoint)
                and (run / "checkpoint_packet.txt").read_text() == packet_message(packet), "Captured packet identity differs")
        row.update(checkpoint_sha256=digest(checkpoint), packet_sha256=digest(packet),
                   artifacts={name: sha(run / name) for name in CP_FILES},
                   first_eligible_paired_round=gate["paired_round"], both_agents_active=True,
                   unmerged_paths={a: sorted({x["path"] for x in gate["observations"][a]["unmerged_entries"]})
                                   for a in ("agent1", "agent2")})
    else:
        require(not eligible_indices, "No-conflict result nevertheless contains an eligible unresolved boundary")
        require(not any((run / name).exists() for name in CP_FILES), "Unavailable collection retained an admissible-looking checkpoint")
    return row


def audit_branch(run, job, protocol, collection, ledger):
    from conflict_runtime import verify_worker_artifacts
    phase, task = run / job["condition"], get_task(job["task"])
    config, status = load(run / "task_config.json"), load(phase / "status.json")
    require(config["protocol_sha256"] == digest(protocol) and config["condition"] == job["condition"]
            and config["repeat"] == job["repeat"], "Branch protocol/job identity differs")
    require(config["offline_selftest"] is protocol["offline_selftest"], "Branch offline/real mode differs")
    require(config["submission_policy"] == "authoritative_agent1_only", "Branch delivery contract changed")
    require(config["checkpoint_sha256"] == collection["checkpoint_sha256"], "Branch uses a different collected state")
    require({name: sha(run / name) for name in CP_FILES} == collection["artifacts"], "Branch checkpoint or complete packet differs")
    history = protocol["early_references"][task.key]["record"]
    expected_seconds = max(0, protocol["total_execution_seconds"] - history["elapsed_seconds"] - collection["elapsed_seconds"])
    require(number_equal(config["execution_seconds"], expected_seconds), "Branch received a different remaining allowance")
    verify_worker_artifacts(phase, task)
    row = {**job, "state": "completed", "run_directory": str(run), "integrity_verified": True,
           "checkpoint_sha256": collection["checkpoint_sha256"], "packet_sha256": collection["packet_sha256"],
           "remaining_allowance_seconds": config["execution_seconds"], "execution_status": status["status"],
           "execution_receipt_sha256": sha(phase / "execution_receipt.json"), "ledger": ledger}
    if status["status"] in BUDGET_FAILURES:
        row.update(official_success=False, scored=False, observed_outcome="eligible_branch_budget_failure",
                   evaluated_delivery=None, feature_results=None)
        score = None
    else:
        evaluator = make_evaluator(job["condition"])
        saved_task, saved_config = evaluator.load_task(phase)
        score = evaluator.resume_completed(phase, saved_task, saved_config)
        require(score is not None, "Completed branch lacks a valid native cached score")
        require(score.get("common_delivery_contract") is True and score.get("submission_policy") == "authoritative_agent1_only"
                and score.get("automatic_merge") is False and score.get("lead_patch_fallback_used") is False,
                "Native score used a different delivery policy")
        require(score.get("offline_selftest") is protocol["offline_selftest"], "Native score belongs to another mode")
        patches = evaluator.patch_info(phase, task)
        native_path = Path(score["attempt_directory"]) / "official_evaluation.json"
        native = load(native_path)
        observed = feature_observations(native, task.features)
        outcome = "official_pass" if score["both_passed"] else "empty_published_agent1" if patches["agent1.patch"]["empty"] else "official_failure_requires_log_review"
        if not score["both_passed"] and not patches["agent1.patch"]["empty"]:
            if any(v["observed_category"] == "official_test_failure_observed" for v in observed.values()):
                outcome = "official_test_failure_observed"
            elif any(v["observed_category"] == "patch_apply_failure_observed" for v in observed.values()):
                outcome = "patch_apply_failure_observed"
        row.update(official_success=score["both_passed"], scored=True, observed_outcome=outcome,
                   feature_results=score["feature_results"], feature_observations=observed,
                   published_inputs={name: file_record(Path(info["path"])) for name, info in patches.items()},
                   evaluated_delivery=score["evaluated_delivery"], native_cache_verified=True,
                   official_raw_result=str(native_path), official_raw_result_sha256=sha(native_path),
                   publication=load(phase / "final_delivery.json").get("publications"))
    full_cost = (history["all_attempt_usage"]["known_cost_usd"]
                 + collection["ledger"]["all_attempts"]["known_cost_usd"] + ledger["all_attempts"]["known_cost_usd"])
    full_unknown = (history["all_attempt_usage"]["unknown_usage_requests"]
                    + collection["ledger"]["all_attempts"]["unknown_usage_requests"] + ledger["all_attempts"]["unknown_usage_requests"])
    row.update(full_task_comparison_known_cost_usd=full_cost, full_task_comparison_unknown_usage_requests=full_unknown,
               historical_prefix_usage_comparison_only=history["all_attempt_usage"])
    saved = load(run / "result.json")
    if saved is not None:
        require(saved["official_success"] == row["official_success"] and saved["scored"] == row["scored"]
                and saved["execution"] == status and saved["evaluation"] == score, "Saved result differs from execution/native outcome")
        require(same_stats(saved["all_attempt_usage"], ledger["all_attempts"])
                and same_stats(saved["accepted_usage"], ledger["accepted_attempt"])
                and number_equal(saved["full_task_known_cost_usd"], full_cost)
                and saved["full_task_unknown_usage_requests"] == full_unknown, "Saved result cost differs from physical ledgers")
    row["result_row_verified"] = saved is not None
    return row


def build_audit(suite):
    suite = validate_suite(suite)
    protocol = load(suite / "protocol.json")
    report = {"schema": "conflict_integrity_audit_v1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "suite": str(suite), "errors": [], "warnings": [], "collections": {}, "rows": [],
              "model_calls": 0, "scoring_runs": 0, "docker_operations": 0, "historical_prefix_rebilled": False}
    if protocol is None:
        return {**report, "status": "pending_protocol", "complete": False, "integrity_verified": None}
    report["offline_selftest"] = protocol["offline_selftest"]
    try:
        report["sources"] = sources(suite, protocol)
    except Exception as exc:
        report["errors"].append("sources: " + str(exc))
    physical_paths, physical_requests, counted = [], [], 0

    def account(run, condition):
        nonlocal counted
        physical_paths.append(run / "usage_ledger.json")
        ledger, rows = ledger_audit(run, condition)
        physical_requests.extend(rows)
        counted += 1
        require(all(r.get("model") == "gpt-6-luna" for r in rows), "Physical request model differs")
        require(not protocol["offline_selftest"] or not rows, "Offline engineering proof contains paid requests")
        return ledger

    for key in TASK_KEYS:
        run = suite / key / "collection"
        try:
            ledger = account(run, "strongshare")
            status = load(run / "strongshare/status.json", {})
            if status.get("status") in ("completed", "no_conflict") and (run / "collection_receipt.json").is_file():
                report["collections"][key] = audit_collection(run, get_task(key), protocol, ledger)
            else:
                report["collections"][key] = {"task": key, "state": "pending", "eligible": None,
                    "execution_status": status.get("status", "not_started"), "ledger": ledger, "run_directory": str(run)}
        except Exception as exc:
            report["errors"].append(key + "/collection: " + str(exc))
            report["collections"][key] = {"task": key, "state": "integrity_error", "eligible": None, "error": str(exc)}
    for job in protocol["jobs"]:
        require(job["task"] in TASK_KEYS and job["condition"] in CONDITIONS and type(job["repeat"]) is int
                and job["repeat"] in (1, 2), "Unplanned conflict job")
        run = suite / job["task"] / "jobs" / f"{job['task']}_r{job['repeat']:02d}_{job['condition']}"
        collected = report["collections"][job["task"]]
        try:
            ledger = account(run, job["condition"])
            if collected.get("eligible") is False:
                require(not run.exists(), "An unavailable task nevertheless has a continuation directory")
                row = {**job, "state": "unavailable", "eligible": False, "official_success": None,
                       "reason": collected["reason"], "ledger": ledger}
            else:
                status = load(run / job["condition"] / "status.json", {})
                score = load(run / job["condition"] / "evaluation_summary.json", {})
                terminal = ((status.get("status") in BUDGET_FAILURES or (status.get("status") == "completed" and score.get("evaluation_completed")))
                            and (run / job["condition"] / "execution_receipt.json").is_file())
                if collected.get("eligible") is True and terminal:
                    row = audit_branch(run, job, protocol, collected, ledger)
                else:
                    row = {**job, "state": "pending", "eligible": collected.get("eligible"),
                           "official_success": None, "execution_status": status.get("status", "not_started"),
                           "ledger": ledger, "run_directory": str(run)}
            report["rows"].append(row)
        except Exception as exc:
            report["errors"].append(run.name + ": " + str(exc))
            report["rows"].append({**job, "state": "integrity_error", "official_success": None, "error": str(exc)})
    require(len(physical_paths) == len(set(physical_paths)), "Duplicate planned physical ledger")
    actual_paths = set(suite.glob("*/collection/usage_ledger.json")) | set(suite.glob("*/jobs/*/usage_ledger.json"))
    unexpected = sorted(str(p) for p in actual_paths - set(physical_paths))
    if unexpected:
        report["errors"].append("Unplanned physical ledgers: " + repr(unexpected))
    report["actual_new_usage"] = stats(physical_requests)
    report["all_planned_physical_ledgers_counted"] = counted == len(physical_paths) and not unexpected
    report["physical_ledgers"] = [str(path) for path in physical_paths]
    report["eligible_checkpoints"] = sum(c.get("eligible") is True for c in report["collections"].values())
    report["completed_executions"] = sum(r["state"] == "completed" for r in report["rows"])
    report["unavailable_branches"] = sum(r["state"] == "unavailable" for r in report["rows"])
    report["groups"] = []
    for key in TASK_KEYS:
        for condition in CONDITIONS:
            rows = [r for r in report["rows"] if r["task"] == key and r["condition"] == condition and r["state"] == "completed"]
            costs = [r["full_task_comparison_known_cost_usd"] for r in rows]
            report["groups"].append({"task": key, "condition": condition, "planned_max_n": sum(j["task"] == key and j["condition"] == condition for j in protocol["jobs"]),
                "eligible": report["collections"][key].get("eligible"), "completed_verified_n": len(rows),
                "official_passes": sum(r["official_success"] for r in rows), "scored_n": sum(r["scored"] for r in rows),
                "remaining_known_cost_mean": sum(r["ledger"]["all_attempts"]["known_cost_usd"] for r in rows) / len(rows) if rows else None,
                "full_task_comparison_known_cost_mean": sum(costs) / len(costs) if costs else None,
                "full_task_comparison_known_cost_range": [min(costs), max(costs)] if costs else None,
                "full_task_comparison_unknown_usage_requests": sum(r["full_task_comparison_unknown_usage_requests"] for r in rows)})
    report["complete"] = (all(c["state"] in ("completed", "no_conflict") for c in report["collections"].values())
                          and all(r["state"] in ("completed", "unavailable") for r in report["rows"]))
    summary = load(suite / "summary.json", {})
    if summary:
        report["saved_summary_usage_matches"] = same_stats(report["actual_new_usage"], summary.get("actual_new_usage", {}))
        if not report["saved_summary_usage_matches"]:
            (report["errors"] if report["complete"] else report["warnings"]).append("Saved summary usage differs from physical ledgers; partial summaries can lag active requests.")
        if report["complete"]:
            counts_match = (summary.get("eligible_checkpoints") == report["eligible_checkpoints"]
                            and summary.get("completed_executions") == report["completed_executions"]
                            and len(summary.get("skipped_jobs", [])) == report["unavailable_branches"])
            if not counts_match:
                messages = report["errors"] if load(suite / "status.json", {}).get("status") == "completed" else report["warnings"]
                messages.append("Supervisor summary cohort counts differ or have not yet been persisted.")
            for saved in summary.get("rows", []):
                if saved != load(Path(saved["run_directory"]) / "result.json"):
                    report["errors"].append("Summary row differs from saved job result: " + saved["run_directory"])
    report["integrity_verified"] = not report["errors"]
    report["status"] = "integrity_error" if report["errors"] else "complete" if report["complete"] else "partial_pending"
    report["limits"] = ["One collector per fixed task; unavailable conflicts are not task failures and are never replaced.",
        "Each pair of condition repeats shares one conflict checkpoint. At most two independent task states, not eight independent tasks.",
        "Both conditions have the same agent1-only delivery contract. Old native-merge StrongShare scores are not this comparison.",
        "Known spend includes all new collection and continuation attempts once; historical early prefix is comparison-only. Unknown usage remains unknown.",
        "An official pass covers the fixed official tests, not every possible behavior. This audit runs no tests or model calls."]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, default=RUNS / VERSION)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    suite = validate_suite(args.suite_dir)
    output = (args.output or suite / AUDIT_NAME).absolute()
    require(output.parent == suite and output.name == AUDIT_NAME and not output.is_symlink(), "Only the independent suite audit file may be written")
    report = build_audit(suite)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    print(json.dumps({"status": report["status"], "complete": report["complete"],
                      "actual_new_usage": report.get("actual_new_usage"), "errors": report["errors"], "output": str(output)}, ensure_ascii=False))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
