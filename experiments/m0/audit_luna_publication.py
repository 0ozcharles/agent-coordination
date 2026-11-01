"""Read-only provenance, publication and billing audit of the publication pilot.

Only the requested audit JSON is written. Completed native caches are verified,
never evaluated anew. Pending trajectories are not opened. No worker, provider,
Docker client, credential loader, or experiment supervisor is invoked.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "artifacts/publication_runs"
ORIGIN = ROOT / "artifacts/selector_runs/luna_selector_v1"
TASK_KEY = "click2068_f4_f10"
CONDITIONS = ("strongshare", "integrator", "solo")
NEW_SOURCES = {"run_luna_publication.py", "publication_workers.py",
               "publication_evaluator.py", "publication_guard.py"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
AUDIT_NAME = "audit_publication_integrity.json"

# Disable implicit dotenv loading and remote price metadata during safe imports.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(ROOT / "config/mini-swe-agent"))


def regular(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path != path.resolve(strict=True):
        raise ValueError("Expected a regular, unlinked artifact: " + str(path))
    return path


def load(path, default=None):
    path = Path(path)
    return json.loads(regular(path).read_text()) if path.exists() or path.is_symlink() else default


def sha(path):
    return hashlib.sha256(regular(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def number_equal(a, b):
    return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal("0.000000005")


def file_record(path):
    data = regular(path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "empty": not data.strip()}


def stats(rows):
    costs = [Decimal(str(r["actual_cost_usd"])) for r in rows if r.get("actual_cost_usd") is not None]
    require(all(c.is_finite() and c >= 0 for c in costs), "Invalid physical ledger cost")
    return {"requests": len(rows), "known_cost_usd": float(sum(costs, Decimal(0))),
            "unknown_usage_requests": sum(r.get("actual_cost_usd") is None for r in rows),
            "prompt_tokens": sum(r.get("prompt_tokens", 0) or 0 for r in rows),
            "completion_tokens": sum(r.get("completion_tokens", 0) or 0 for r in rows)}


def same_stats(left, right):
    return (isinstance(right, dict) and "known_cost_usd" in right
            and all(left.get(k) == right.get(k) for k in
                ("requests", "unknown_usage_requests", "prompt_tokens", "completion_tokens"))
            and number_equal(left["known_cost_usd"], right["known_cost_usd"]))


def ledger_audit(run, condition):
    path = run / "usage_ledger.json"
    ledger = load(path, {"requests": []})
    rows = ledger.get("requests")
    require(isinstance(rows, list), "Physical ledger has no request list")
    require(all(str(r.get("phase", "")).startswith(condition + ":") for r in rows),
            "Physical job ledger contains another phase, including a possible copied prefix")
    identities = [(r.get("run_id"), r.get("request_number")) for r in rows]
    require(len(identities) == len(set(identities)), "Duplicate request identities in a physical ledger")
    accepted = [r for r in rows if not r.get("excluded_from_baseline")]
    excluded = [r for r in rows if r.get("excluded_from_baseline")]
    return {"path": str(path), "exists": path.exists(), "sha256": sha(path) if path.exists() else None,
            "all_attempts": stats(rows), "accepted_attempt": stats(accepted),
            "archived_attempts": stats(excluded),
            "request_status_counts": dict(Counter(r.get("status", "missing") for r in rows)),
            "counting_rule": "One physical root ledger once; excluded attempts included. No archived ledger or historical prefix ledger added."}, rows


def source_audit(suite, protocol):
    historical = load(ORIGIN / "protocol.json")["source_manifest"]
    current = protocol["source_manifest"]
    require(len(historical) == 141 and len(current) == 145, "Expected old 141 and new 145 frozen source files")
    require(set(current) - set(historical) == NEW_SOURCES, "Unexpected publication source additions")
    require(all(current.get(k) == v for k, v in historical.items()), "Historical source hash changed in new protocol")
    for name, expected in current.items():
        require(sha(ROOT / name) == expected, "Live frozen source changed: " + name)
        require(sha(suite / "source_snapshot" / name) == expected, "Publication source snapshot changed: " + name)
    for name, expected in historical.items():
        require(sha(ORIGIN / "source_snapshot" / name) == expected, "Historical source snapshot changed: " + name)
    reference = protocol["checkpoint_reference"]
    require(reference.get("historical_usage_is_new_billable_usage") is False,
            "Historical prefix must not become new billable usage")
    for name, expected in reference["historical_files"].items():
        require(sha(ROOT / name) == expected, "Historical checkpoint/packet evidence changed: " + name)
    require(load(suite / TASK_KEY / "historical_reference.json") == reference, "Copied historical reference differs")
    for name in ("packet.json", "decision.json", "preparation.json"):
        require(load(suite / TASK_KEY / "decision" / name) == load(ORIGIN / TASK_KEY / "decision" / name),
                "Copied historical decision/packet differs: " + name)
    return {"verified": True, "new_source_count": len(current), "historical_source_count": len(historical),
            "new_files": sorted(NEW_SOURCES), "historical_files_verified": len(reference["historical_files"]),
            "protocol_sha256": sha(suite / "protocol.json"), "semantic_protocol_sha256": digest(protocol),
            "checkpoint_sha256": reference["record"]["checkpoint_sha256"],
            "no_historical_file_written": True}


def guard_audit(phase):
    from publication_guard import reminder
    from publication_workers import read_guard
    guard = read_guard(phase)
    require(guard.get("automatic_publication") is False and guard.get("deadline_extended") is False,
            "Guard must neither publish automatically nor extend time")
    require(guard["deadline_monotonic"] <= guard["started_monotonic"] + guard["execution_seconds"],
            "Guard deadline exceeds its original allowance")
    active = ("solo",) if phase.name == "solo" else ("agent1", "agent2")
    expected, counts = {a: [] for a in active}, {a: 0 for a in active}
    for index, event in enumerate(guard["events"], 1):
        agent = event.get("agent_id")
        require(agent in active and event.get("index") == index, "Guard event identity/order differs")
        if event.get("nudged") is True:
            state = event.get("publication_state", {})
            require(event.get("reason") == "unpublished_local_work" and not state.get("pr_open")
                    and not state.get("remote_nonempty") and (state.get("dirty") or state.get("local_ahead")),
                    "A reminder lacks unpublished-work evidence")
            counts[agent] += 1
            expected[agent].append(reminder(agent, state["dirty"]))
        require(event.get("nudge_count") == counts[agent] and counts[agent] <= 2,
                "Guard event counts differ from reminder events")
    require(all(guard["counts"].get(a, 0) == counts[a] for a in active), "Guard summary counts differ from events")
    traces = {}
    for agent in active:
        path = phase / f"{agent}.traj.json"
        messages = load(path)["messages"]
        variants = {reminder(agent, False), reminder(agent, True)}
        found = [(i, m["content"]) for i, m in enumerate(messages)
                 if m.get("role") == "user" and m.get("content") in variants]
        require([content for _, content in found] == expected[agent],
                "Guard events and exact user reminder messages disagree for " + agent)
        traces[agent] = {"path": str(path), "sha256": sha(path),
                         "reminder_message_indices_zero_based": [i for i, _ in found]}
    return {"verified": True, "path": str(phase / "publication_guard.json"),
            "sha256": sha(phase / "publication_guard.json"), "counts": counts,
            "reason_counts": dict(Counter(e["reason"] for e in guard["events"])), "traces": traces,
            "automatic_publication": False, "deadline_extended": False}


def feature_observations(native, features):
    observations = {}
    for index, feature in enumerate(features, 1):
        item = native.get(f"feature{index}") or {}
        output = ANSI.sub("", item.get("test_output", item.get("output", "")))
        failures = re.findall(r"(?m)^\s*FAILED\s+([^\n]+)", output)
        pytest = re.findall(r"(?m)^.*\b\d+ (?:failed|passed|error|errors)\b.*$", output)
        if item.get("passed") is True:
            category = "official_pass"
        elif "No valid patches in input" in output or re.search(r"(?:patch failed:|patch does not apply|Feature patch failed)", output):
            category = "patch_apply_failure_observed"
        elif failures or re.search(r"\b[1-9]\d* failed\b", output):
            category = "official_test_failure_observed"
        elif not output.strip():
            category = "no_feature_test_output"
        else:
            category = "official_failure_requires_log_review"
        observations[str(feature)] = {"passed": item.get("passed") is True, "observed_category": category,
                                      "failed_test_lines": failures, "pytest_summary_lines": pytest[-3:],
                                      "test_output_sha256": hashlib.sha256(output.encode()).hexdigest()}
    return observations


def audit_completed(run, job, protocol, ledger):
    from publication_evaluator import make_evaluator
    from publication_workers import verify_execution
    from selector_task_specs import get_task
    condition = job["condition"]
    phase, task = run / condition, get_task(TASK_KEY)
    config, execution = load(run / "task_config.json"), load(phase / "status.json")
    require(config["protocol_sha256"] == digest(protocol), "Job is bound to a different protocol")
    require(config["condition"] == condition and config["repeat"] == job["repeat"], "Job identity changed")
    receipt = verify_execution(phase, task, condition, freeze=False)
    evaluator = make_evaluator(condition)
    loaded_task, loaded_config = evaluator.load_task(phase)
    saved = evaluator.resume_completed(phase, loaded_task, loaded_config)
    require(saved is not None, "Completed evaluation failed native cache validation")
    patches = evaluator.patch_info(phase, task)
    guard = guard_audit(phase)
    native = load(Path(saved["attempt_directory"]) / "official_evaluation.json")
    published = {name: file_record(Path(info["path"])) for name, info in patches.items()}
    if condition == "solo":
        # Native Solo filters test-file edits internally. This is the protected
        # submitted input, not a claim to have captured its filtered test bytes.
        delivery = {"selection": "published_solo_input", "submitted_patch": published["final.patch"],
                    "filtered_native_patch_separately_captured": False}
    else:
        delivery = saved.get("evaluated_delivery")
        require(isinstance(delivery, dict), "Native delivered-patch capture missing")
    observed = feature_observations(native, task.features)
    if saved["both_passed"]:
        outcome = "official_pass"
    elif all(info["empty"] for info in published.values()):
        outcome = "all_published_inputs_empty"
    elif condition == "integrator" and published["agent1.patch"]["empty"]:
        outcome = "predesignated_agent1_input_empty"
    elif any(v["observed_category"] == "official_test_failure_observed" for v in observed.values()):
        outcome = "official_test_failure_observed"
    elif any(v["observed_category"] == "patch_apply_failure_observed" for v in observed.values()):
        outcome = "patch_apply_failure_observed"
    elif str((delivery or {}).get("selection", "")).startswith("no_deliverable_"):
        outcome = delivery["selection"]
    else:
        outcome = "official_failure_requires_log_review"
    historical = protocol["checkpoint_reference"]["record"]["all_attempt_usage"] if condition != "solo" else stats([])
    full_cost = ledger["all_attempts"]["known_cost_usd"] + historical["known_cost_usd"]
    full_unknown = ledger["all_attempts"]["unknown_usage_requests"] + historical["unknown_usage_requests"]
    result = load(run / "result.json")
    row_verified = result is not None
    if result is not None:
        require(result.get("evaluation") == saved and result.get("execution") == execution
                and result.get("official_success") == saved["both_passed"], "Saved result disagrees with native result/execution")
        require(same_stats(result["all_attempt_usage"], ledger["all_attempts"])
                and same_stats(result["accepted_usage"], ledger["accepted_attempt"]), "Saved row cost differs from physical ledger")
        require(number_equal(result["full_task_known_cost_usd"], full_cost)
                and result["full_task_unknown_usage_requests"] == full_unknown, "Full comparison cost differs")
        from publication_workers import guard_summary
        require(result["publication_guard"] == guard_summary(phase), "Result guard summary differs")
    return {**job, "run_directory": str(run), "state": "completed", "integrity_verified": True,
            "native_cache_verified": True, "result_row_verified": row_verified,
            "execution_receipt_sha256": sha(phase / "execution_receipt.json"),
            "publication_receipt_sha256": sha(phase / "publication_execution_receipt.json"),
            "official_success": saved["both_passed"], "feature_results": saved["feature_results"],
            "feature_observations": observed, "observed_outcome": outcome,
            "published_inputs": published, "evaluated_delivery": delivery,
            "native_merge_status": saved.get("merge_status"), "native_merge_strategy": saved.get("merge_strategy"),
            "native_lead_fallback_used": saved.get("lead_patch_fallback_used"),
            "guard": guard, "ledger": ledger, "historical_prefix_comparison_usage": historical,
            "full_task_comparison_known_cost_usd": full_cost,
            "full_task_comparison_unknown_usage_requests": full_unknown,
            "official_raw_result": str(Path(saved["attempt_directory"]) / "official_evaluation.json"),
            "official_raw_result_sha256": sha(Path(saved["attempt_directory"]) / "official_evaluation.json"),
            "interpretation": "Observed artifacts and official outcomes only; no inferred function-level failure cause or new scoring."}


def validate_suite(value):
    original = Path(value).expanduser().absolute()
    suite = original.resolve(strict=True)
    require(original == suite and suite.parent == RUNS.resolve() and suite.is_dir()
            and re.fullmatch(r"[A-Za-z0-9_-]+", suite.name), "Expected a regular publication_runs/<suite> directory")
    return suite


def build_audit(suite):
    suite = validate_suite(suite)
    protocol = load(suite / "protocol.json")
    if protocol is None:
        return {"schema": "publication_integrity_audit_v1", "status": "pending_protocol",
                "suite": str(suite), "integrity_verified": None, "complete": False, "model_calls": 0}
    report = {"schema": "publication_integrity_audit_v1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "suite": str(suite), "offline_selftest": protocol["offline_selftest"],
              "model_calls": 0, "scoring_runs": 0, "docker_operations": 0,
              "errors": [], "warnings": [], "rows": [], "historical_prefix_rebilled": False}
    try:
        report["sources"] = source_audit(suite, protocol)
    except Exception as exc:
        report["errors"].append("source_or_reference: " + str(exc))
    jobs = protocol["jobs"]
    expected_n = 3 if protocol["offline_selftest"] else 6
    require(len(jobs) == expected_n, "Unexpected planned job count")
    physical_paths, all_requests, counted_ledgers = [], [], 0
    for job in jobs:
        require(job["task"] == TASK_KEY and job["condition"] in CONDITIONS and type(job["repeat"]) is int
                and job["repeat"] in (1, 2), "Unplanned audit job identity")
        condition = job["condition"]
        run = suite / TASK_KEY / ("solo_jobs" if condition == "solo" else "jobs") / f"{TASK_KEY}_r{job['repeat']:02d}_{condition}"
        physical_paths.append(run / "usage_ledger.json")
        try:
            ledger, requests = ledger_audit(run, condition)
            all_requests.extend(requests)
            counted_ledgers += 1
            phase = run / condition
            state, score = load(phase / "status.json", {}), load(phase / "evaluation_summary.json", {})
            if state.get("status") == "completed" and score.get("evaluation_completed"):
                row = audit_completed(run, job, protocol, ledger)
            else:
                row = {**job, "run_directory": str(run), "state": "pending",
                       "execution_status": state.get("status", "not_started"),
                       "evaluation_status": score.get("status", "not_started"),
                       "official_success": None, "integrity_verified": None, "ledger": ledger,
                       "reason": "No accepted completed native score; pending trajectories were not read."}
            report["rows"].append(row)
        except Exception as exc:
            report["errors"].append(run.name + ": " + str(exc))
            report["rows"].append({**job, "run_directory": str(run), "state": "integrity_error",
                                   "integrity_verified": False, "error": str(exc)})
    require(len(physical_paths) == len(set(physical_paths)), "Duplicate physical jobs in protocol")
    actual_paths = set(suite.glob("*/jobs/*/usage_ledger.json")) | set(suite.glob("*/solo_jobs/*/usage_ledger.json"))
    extra = sorted(str(p) for p in actual_paths - set(physical_paths))
    if extra:
        report["errors"].append("Unplanned physical ledgers: " + repr(extra))
    report["actual_new_usage"] = stats(all_requests)
    report["all_planned_physical_ledgers_counted"] = counted_ledgers == len(jobs) and not extra
    report["physical_ledgers"] = [str(p) for p in physical_paths]
    report["historical_prefix_comparison_only"] = protocol["checkpoint_reference"]["record"]["all_attempt_usage"]
    report["groups"] = []
    for condition in CONDITIONS:
        rows = [r for r in report["rows"] if r["condition"] == condition and r["state"] == "completed"]
        costs = [r["full_task_comparison_known_cost_usd"] for r in rows]
        report["groups"].append({"condition": condition, "planned_n": sum(j["condition"] == condition for j in jobs),
            "completed_verified_n": len(rows), "official_passes": sum(r["official_success"] for r in rows),
            "accepted_known_cost_mean": sum(r["ledger"]["accepted_attempt"]["known_cost_usd"] for r in rows) / len(rows) if rows else None,
            "all_attempt_new_known_cost_mean": sum(r["ledger"]["all_attempts"]["known_cost_usd"] for r in rows) / len(rows) if rows else None,
            "full_task_comparison_known_cost_mean": sum(costs) / len(costs) if costs else None,
            "full_task_comparison_known_cost_range": [min(costs), max(costs)] if costs else None,
            "full_task_comparison_unknown_usage_requests": sum(r["full_task_comparison_unknown_usage_requests"] for r in rows),
            "reminders_accepted_attempts": sum(sum(r["guard"]["counts"].values()) for r in rows)})
    report["complete"] = all(r["state"] == "completed" for r in report["rows"])
    summary = load(suite / "summary.json", {})
    if summary:
        matches = same_stats(report["actual_new_usage"], summary.get("actual_new_usage", {}))
        report["saved_summary_usage_matches"] = matches
        if not matches:
            (report["errors"] if report["complete"] else report["warnings"]).append(
                "Saved summary usage differs from physical ledgers; partial summaries can lag active requests.")
        saved_rows = {(r["condition"], r["repeat"]): r for r in summary.get("rows", [])}
        for row in report["rows"]:
            if row["state"] == "completed" and (row["condition"], row["repeat"]) in saved_rows:
                expected = load(Path(row["run_directory"]) / "result.json")
                if expected != saved_rows[(row["condition"], row["repeat"])]:
                    report["errors"].append("Saved summary row differs from job result: " + Path(row["run_directory"]).name)
        if report["complete"] and len(saved_rows) != expected_n:
            report["warnings"].append("All native scores complete; supervisor summary rows not all persisted yet.")
    report["integrity_verified"] = not report["errors"]
    report["status"] = "integrity_error" if report["errors"] else "complete" if report["complete"] else "partial_pending"
    report["limits"] = ["One previously observed Click task, two new samples per condition; not six independent tasks.",
        "Official pass is not proof of every visible requirement. No new tests were run.",
        "Known spend is a lower bound while unknown usage remains. Historical prefix is included only in comparison columns.",
        "The native existing-PR exit rule is retained; a reminder-free exit does not prove no later local work existed."]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, default=RUNS / "luna_publication_v1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    suite = validate_suite(args.suite_dir)
    output = (args.output or suite / AUDIT_NAME).absolute()
    require(output.name == AUDIT_NAME and output.parent == suite and not output.is_symlink(),
            "Only this suite's independent audit_publication_integrity.json may be written")
    report = build_audit(suite)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "complete": report["complete"],
                      "actual_new_usage": report.get("actual_new_usage"), "errors": report.get("errors", []),
                      "output": str(output)}, ensure_ascii=False))
    return 2 if report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
