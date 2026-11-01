"""Read-only aggregation of Luna collaboration and historical Luna Solo results.

Default use requires a completed suite. Nothing imports a model, Docker, or an
evaluator; no old result/source is changed. --output writes only a new derived
JSON report. Known costs remain lower bounds when usage is unknown. Execution
time and execution-plus-official-evaluation time are reported separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SUITE = ROOT / "artifacts/handoff_expansion_runs/luna_collaboration_v1"
MODEL = "gpt-6-luna"
CONDITIONS = ("strongshare", "consolidate", "solo")
USD_PLACES = Decimal("0.000000001")


def read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected an ordinary saved file: " + str(path))
    return json.loads(path.read_text())


def file_hash(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected an ordinary saved file: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid nonnegative numeric field: " + name)
    return Decimal(str(value))


def money(value):
    return float(value.quantize(USD_PLACES))


def equal_number(actual, expected, name):
    if abs(number(actual, name) - expected) > USD_PLACES:
        raise ValueError("Saved aggregate differs from its underlying records: " + name)


def usage(rows):
    return {
        "requests": len(rows),
        "known_cost_usd": money(sum((number(row["actual_cost_usd"], "request cost")
                                     for row in rows if row.get("actual_cost_usd") is not None), Decimal(0))),
        "unknown_usage_requests": sum(row.get("actual_cost_usd") is None for row in rows),
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in rows),
        "completion_tokens": sum(row.get("completion_tokens", 0) for row in rows),
    }


def sum_usage(records):
    keys = ("requests", "known_cost_usd", "unknown_usage_requests", "prompt_tokens", "completion_tokens")
    result = {key: sum(record[key] for record in records) for key in keys if key != "known_cost_usd"}
    result["known_cost_usd"] = money(sum((Decimal(str(record["known_cost_usd"])) for record in records), Decimal(0)))
    return result


def compare_usage(saved, computed, name):
    for key, value in computed.items():
        if key == "known_cost_usd":
            equal_number(saved[key], Decimal(str(value)), name + "." + key)
        elif saved[key] != value:
            raise ValueError("Saved usage differs from ledger: " + name + "." + key)


def ledger_report(run, phase):
    """Partition every request, including archived responses that completed."""
    path = run / "usage_ledger.json"
    rows = read(path)["requests"] if path.exists() else []
    if any(row.get("model") != MODEL or not row.get("phase", "").startswith(phase + ":") for row in rows):
        raise ValueError("Unexpected model or phase in Luna job ledger: " + str(path))
    numbers = [row["request_number"] for row in rows]
    if len(numbers) != len(set(numbers)):
        raise ValueError("Duplicate request numbers: " + str(path))
    excluded = [row for row in rows if row.get("excluded_from_baseline")]
    accepted = [row for row in rows if not row.get("excluded_from_baseline")]
    by_attempt = defaultdict(list)
    for row in excluded:
        stamp = row.get("archived_attempt")
        if not stamp:
            raise ValueError("Excluded request lacks archived attempt identity")
        by_attempt[stamp].append(row)
    directories = {p.name.removeprefix(phase + "_"): p
                   for p in (run / "failed_attempts").glob(phase + "_*") if p.is_dir()}
    if not set(by_attempt).issubset(directories):
        raise ValueError("Excluded ledger requests have no archived evidence")
    attempts = []
    for stamp, directory in sorted(directories.items()):
        saved = read(directory / "status.json") if (directory / "status.json").exists() else {}
        elapsed = saved.get("elapsed_seconds")
        elapsed_known = isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0
        attempts.append({"directory": str(directory), "archived_attempt": stamp,
            "status": saved.get("status"), "error_type": saved.get("error_type"),
            "elapsed_known_seconds": elapsed if elapsed_known else None,
            "elapsed_time_unknown": not elapsed_known,
            "usage": usage(by_attempt.get(stamp, [])),
            "request_statuses": dict(Counter(row["status"] for row in by_attempt.get(stamp, [])))})
    return {"run_directory": str(run), "phase": phase,
        "ledger_sha256": file_hash(path) if path.exists() else None,
        "all_attempt_usage": usage(rows), "accepted_usage": usage(accepted),
        "failed_attempt_usage": usage(excluded), "failed_attempts": attempts,
        "active_request_statuses": dict(Counter(row["status"] for row in accepted)),
        "failed_attempt_known_seconds": sum(a["elapsed_known_seconds"] or 0 for a in attempts),
        "failed_attempt_unknown_times": sum(a["elapsed_time_unknown"] for a in attempts)}


def aggregate_group(task, condition, rows, planned, skipped):
    selected = [row for row in rows if row["task"] == task and row["condition"] == condition]
    skipped_here = [row for row in skipped if row["task"] == task and row["condition"] == condition]
    costs = [Decimal(str(row["full_task_all_attempt_known_cost_usd"])) for row in selected]
    times = [row["full_task_execution_seconds"] for row in selected]
    total_times = [row["execution_plus_official_evaluation_seconds"] for row in selected]
    unknown = sum(row["full_task_all_attempt_unknown_usage_requests"] for row in selected)
    unknown_times = sum(row["full_task_time_unknown_attempts"] for row in selected)
    mean = lambda values: sum(values) / len(values) if values else None
    return {"task": task, "condition": condition,
        "reference_type": "historical_fresh_solo" if condition == "solo" else "new_common_checkpoint_continuation",
        "planned_runs": planned, "completed_runs": len(selected), "terminal_skips": len(skipped_here),
        "official_passes": sum(row["official_success"] for row in selected),
        "official_failures": sum(not row["official_success"] for row in selected),
        "known_full_task_cost_mean_usd": money(mean(costs)) if costs else None,
        "known_full_task_cost_range_usd": [money(min(costs)), money(max(costs))] if costs else None,
        "comparison_unknown_usage_requests": unknown,
        "cost_means_are_complete": unknown == 0 if selected else None,
        "known_full_execution_seconds_mean": round(mean(times), 6) if times else None,
        "known_full_execution_seconds_range": [min(times), max(times)] if times else None,
        "official_evaluation_seconds_mean": round(mean([r["official_evaluation_seconds"] for r in selected]), 6) if selected else None,
        "known_execution_plus_official_evaluation_seconds_mean": round(mean(total_times), 6) if total_times else None,
        "comparison_unknown_time_attempts": unknown_times,
        "time_means_are_complete": unknown_times == 0 if selected else None,
        "skip_reasons": dict(Counter(row["reason"] for row in skipped_here))}


def collect(suite, *, allow_partial=False):
    suite = suite.resolve(strict=True)
    protocol, state, summary = [read(suite / name) for name in ("protocol.json", "status.json", "summary.json")]
    if (protocol.get("model") != MODEL or protocol.get("offline_selftest") is not False
            or protocol.get("version") != "luna_collaboration_v1"):
        raise ValueError("Expected the real Luna collaboration protocol")
    completed = state.get("status") == "completed" and summary.get("pipeline_completed") is True
    if not completed and not allow_partial:
        raise ValueError("Suite is not complete; final aggregation was not run")
    if summary["protocol_sha256"] != digest(protocol) or summary["planned_jobs"] != len(protocol["jobs"]):
        raise ValueError("Summary protocol identity changed")
    for relative, expected in protocol["source_manifest"].items():
        if file_hash(ROOT / relative) != expected or file_hash(suite / "source_snapshot" / relative) != expected:
            raise ValueError("Frozen source or snapshot changed: " + relative)
    for relative, expected in protocol["solo_reference"]["files_sha256"].items():
        if file_hash(ROOT / relative) != expected:
            raise ValueError("Historical Solo evidence changed: " + relative)
    solo = Path(protocol["solo_reference"]["directory"]).resolve(strict=True)
    if solo == suite:
        raise ValueError("New and historical suites must be distinct")
    tasks = tuple(protocol["tasks"])
    planned = {(job["task"], job["repeat"], job["condition"]) for job in protocol["jobs"]}
    if len(planned) != 8 or len(protocol["jobs"]) != 8:
        raise ValueError("Expected eight predeclared continuations")
    result_ids = {(row["task"], row["repeat"], row["condition"]) for row in summary["rows"]}
    skipped = summary.get("skipped_jobs", [])
    skipped_ids = {(row["task"], row["repeat"], row["condition"]) for row in skipped}
    if (len(result_ids) != len(summary["rows"]) or len(skipped_ids) != len(skipped)
            or result_ids & skipped_ids or not (result_ids | skipped_ids).issubset(planned)
            or (completed and result_ids | skipped_ids != planned)):
        raise ValueError("Duplicate, overlapping, missing or unplanned results")
    if completed and (state["completed_jobs"] != len(result_ids) or state.get("skipped_jobs", 0) != len(skipped_ids)):
        raise ValueError("Terminal status counters differ from saved results")

    ledgers, prefixes, normalized, seen = [], {}, [], set()
    for key in tasks:
        prefix = suite / key / "prefix"
        if not (prefix / "task_config.json").exists():
            continue
        audit = ledger_report(prefix, "prefix")
        ledgers.append(audit)
        seen.add(prefix / "usage_ledger.json")
        record = summary.get("prefixes", {}).get(key)
        if record:
            compare_usage(record["accepted_usage"], audit["accepted_usage"], key + " prefix accepted")
            compare_usage(record["all_attempt_usage"], audit["all_attempt_usage"], key + " prefix all attempts")
        prefixes[key] = {"record": record, "accounting": audit}
    by_run = {}
    for task, repeat, condition in sorted(planned):
        run = suite / task / "jobs" / f"{task}_r{repeat:02d}_{condition}"
        if not (run / "task_config.json").exists():
            continue
        audit = ledger_report(run, condition)
        ledgers.append(audit)
        by_run[(task, repeat, condition)] = audit
        seen.add(run / "usage_ledger.json")
    actual_ledger_files = set(suite.glob("*/prefix/usage_ledger.json")) | set(suite.glob("*/jobs/*/usage_ledger.json"))
    if not actual_ledger_files.issubset(seen):
        raise ValueError("Unexpected ledger outside the frozen schedule")
    for row in summary["rows"]:
        identity = row["task"], row["repeat"], row["condition"]
        audit = by_run[identity]
        run = Path(audit["run_directory"])
        if Path(row["run_directory"]) != run or read(run / "result.json") != row:
            raise ValueError("Per-job result differs from summary")
        compare_usage(row["accepted_usage"], audit["accepted_usage"], str(identity) + " accepted")
        compare_usage(row["all_attempt_usage"], audit["all_attempt_usage"], str(identity) + " all attempts")
        prefix = prefixes[row["task"]]
        full_cost = Decimal(str(prefix["accounting"]["all_attempt_usage"]["known_cost_usd"])) + Decimal(str(audit["all_attempt_usage"]["known_cost_usd"]))
        equal_number(row["full_task_all_attempt_known_cost_usd"], full_cost, "full-task cost")
        expected_unknown = prefix["accounting"]["all_attempt_usage"]["unknown_usage_requests"] + audit["all_attempt_usage"]["unknown_usage_requests"]
        if row["full_task_all_attempt_unknown_usage_requests"] != expected_unknown:
            raise ValueError("Full-task unknown usage differs from ledgers")
        expected_time = (number(prefix["record"]["elapsed_seconds"], "prefix elapsed")
                         + number(prefix["accounting"]["failed_attempt_known_seconds"], "failed prefix time")
                         + number(row["execution"]["elapsed_seconds"], "execution elapsed")
                         + number(audit["failed_attempt_known_seconds"], "failed continuation time"))
        equal_number(row["full_task_execution_seconds"], expected_time, "full-task execution time")
        unknown_times = prefix["accounting"]["failed_attempt_unknown_times"] + audit["failed_attempt_unknown_times"]
        if unknown_times != row["prefix_time_unknown_attempts"] + row["execution_time_unknown_attempts"]:
            raise ValueError("Unknown attempt time differs from archived statuses")
        if audit["active_request_statuses"].get("in_flight", 0):
            raise ValueError("Completed execution still contains an unsettled in-flight request")
        normalized.append({"task":row["task"],"repeat":row["repeat"],"condition":row["condition"],
            "official_success":row["official_success"],"run_directory":str(run),
            "full_task_all_attempt_known_cost_usd":money(full_cost),
            "full_task_all_attempt_unknown_usage_requests":expected_unknown,
            "full_task_execution_seconds":float(expected_time),"full_task_time_unknown_attempts":unknown_times,
            "official_evaluation_seconds":row["evaluation"]["elapsed_seconds"],
            "execution_plus_official_evaluation_seconds":round(float(expected_time)+row["evaluation"]["elapsed_seconds"],6),
            "continuation_all_attempt_usage":audit["all_attempt_usage"],
            "historical_reference":False})
    historical_audits = []
    for key in tasks:
        for repeat in (1, 2):
            run = solo / key / "solo_jobs" / f"{key}_r{repeat:02d}_solo"
            row = read(run / "result.json")
            if (row["task"], row["repeat"], row["condition"], row["model"]) != (key, repeat, "solo", MODEL):
                raise ValueError("Historical Solo identity differs")
            audit = ledger_report(run, "solo")
            historical_audits.append(audit)
            compare_usage(row["all_attempt_usage"], audit["all_attempt_usage"], key + " historical Solo")
            elapsed = number(row["execution_seconds_including_incomplete"], "historical Solo time")
            equal_number(float(elapsed), number(row["execution"]["elapsed_seconds"], "Solo execution")
                         + number(audit["failed_attempt_known_seconds"], "Solo failed time"), "Solo total execution")
            if row["unknown_attempt_times"] != audit["failed_attempt_unknown_times"]:
                raise ValueError("Historical Solo unknown elapsed time differs")
            normalized.append({"task":key,"repeat":repeat,"condition":"solo",
                "official_success":row["official_success"],"run_directory":str(run),
                "full_task_all_attempt_known_cost_usd":audit["all_attempt_usage"]["known_cost_usd"],
                "full_task_all_attempt_unknown_usage_requests":audit["all_attempt_usage"]["unknown_usage_requests"],
                "full_task_execution_seconds":float(elapsed),"full_task_time_unknown_attempts":row["unknown_attempt_times"],
                "official_evaluation_seconds":row["evaluation_seconds"],
                "execution_plus_official_evaluation_seconds":round(float(elapsed)+row["evaluation_seconds"],6),
                "historical_reference":True})

    actual = sum_usage([audit["all_attempt_usage"] for audit in ledgers])
    compare_usage(summary["actual_new_usage"], actual, "suite actual new spend")
    failed = sum_usage([audit["failed_attempt_usage"] for audit in ledgers])
    prefix_usage = sum_usage([audit["all_attempt_usage"] for audit in ledgers if audit["phase"] == "prefix"])
    continuation_usage = sum_usage([audit["all_attempt_usage"] for audit in ledgers if audit["phase"] != "prefix"])
    groups = [aggregate_group(key, condition, normalized, 2, skipped) for key in tasks for condition in CONDITIONS]
    return {"schema":"luna_collaboration_results_aggregation_v1",
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),"status":"complete" if completed else "partial",
        "suite_directory":str(suite),"historical_solo_directory":str(solo),"model":MODEL,
        "audit_api_calls":0,"audit_docker_or_evaluator_calls":0,
        "protocol_sha256":digest(protocol),"summary_sha256":file_hash(suite/"summary.json"),
        "verified_current_source_hashes":len(protocol["source_manifest"]),
        "verified_snapshot_hashes":len(protocol["source_manifest"]),
        "verified_historical_reference_hashes":len(protocol["solo_reference"]["files_sha256"]),
        "new_planned_continuations":len(planned),"new_completed_continuations":len(result_ids),
        "new_terminal_skips":len(skipped_ids),"group_results":groups,"individual_results":normalized,
        "actual_new_usage":actual,"actual_new_prefix_usage_counted_once":prefix_usage,
        "actual_new_continuation_usage":continuation_usage,"actual_new_failed_attempt_usage":failed,
        "actual_new_accepted_or_terminal_attempt_usage":sum_usage([a["accepted_usage"] for a in ledgers]),
        "historical_solo_usage_not_included_in_new_spend":sum_usage([a["all_attempt_usage"] for a in historical_audits]),
        "failed_attempts":[{"task":Path(a["run_directory"]).parent.name if a["phase"]=="prefix" else Path(a["run_directory"]).parent.parent.name,
                            "phase":a["phase"],"run_directory":a["run_directory"],**attempt}
                           for a in ledgers for attempt in a["failed_attempts"]],
        "ledger_partitions":ledgers,"skipped_jobs":skipped,
        "interpretation":[
            "SS/C full-task comparison uses full_task_all_attempt_known_cost_usd and full_task_execution_seconds; it includes all archived prefix and continuation attempts. Prefix cost appears once per comparison run, but actual new spending counts each task prefix ledger only once.",
            "Solo is the immutable earlier Luna fresh-Solo result. Its full cost is all_attempt_usage and full execution time is execution_seconds_including_incomplete. Its cost is never added to this suite's new spending.",
            "Full execution time excludes official scoring; execution-plus-official-evaluation time is a separate column for every condition. Unknown archived times are reported and not represented as known zero.",
            "Known cost is a lower bound when unknown_usage_requests is nonzero. Comparison unknown counts may repeat a shared prefix uncertainty across runs and are not the number of unique billed requests.",
            "Completed task failures and terminal skips remain visible. Use planned/completed/skipped counts together; do not silently drop time-budget skips from the denominator.",
            "This is a budget-aligned historical Solo comparison, not a concurrent randomized three-condition experiment. SS/C share one new checkpoint per task; repeats are not independent task samples.",
            "This script aggregates and validates saved results/ledgers/hashes; it does not rerun tests or substitute for the independent raw-response and evaluated-delivery integrity audit."
        ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--allow-partial", action="store_true", help="Explicitly label and aggregate an unfinished suite")
    parser.add_argument("--output", type=Path, help="Optional new derived JSON output; stdout always prints the report")
    args = parser.parse_args()
    report = collect(args.suite_dir, allow_partial=args.allow_partial)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = args.output.absolute()
        if target.exists() or target.is_symlink():
            raise ValueError("Refusing to overwrite an existing file: " + str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as stream:
            stream.write(text)
    print(text, end="")


if __name__ == "__main__":
    main()
