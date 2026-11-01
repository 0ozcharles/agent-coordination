"""Read saved selector artifacts and independently reprice all transport usage.

No model, Docker, evaluator, subprocess, or production supervisor is invoked.
Only the frozen, pure selector function is called to reproduce its decision.
--partial is a point-in-time view; final auditing requires pipeline_completed.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from audit_luna_integrator_integrity import Reader, raw_usage, score_audit, usage
from audit_luna_timing_results import digest, number, same, sum_usage

ROOT = Path(__file__).resolve().parent
DEFAULT = ROOT / "artifacts/selector_runs/luna_selector_v1"
CONDITIONS = ("strongshare", "integrator", "solo")


def identity(row): return row["task"], row["repeat"], row["condition"]
def run_path(suite, job):
    return suite / job["task"] / ("solo_jobs" if job["condition"] == "solo" else "jobs") / f"{job['task']}_r{job['repeat']:02d}_{job['condition']}"


def ledger(reader, run, phase, live=False):
    path = run / "usage_ledger.json"
    data = reader.read(path)
    rows = data["requests"]
    same(data["model"], "gpt-6-luna", "ledger model")
    same(data["accounting_schema"], "luna_standard_v1", "ledger schema")
    agents = ("solo",) if phase == "solo" else ("agent1", "agent2")
    if any(r["model"] != "gpt-6-luna" or r["phase"] not in [phase + ":" + a for a in agents] for r in rows):
        raise ValueError("Unexpected ledger model/phase")
    if len(rows) != len({r["request_number"] for r in rows}): raise ValueError("Duplicate transport request")
    if not live and any(r["status"] == "in_flight" for r in rows): raise ValueError("Terminal ledger has inflight usage")
    accepted = [r for r in rows if not r.get("excluded_from_baseline")]
    excluded = [r for r in rows if r.get("excluded_from_baseline")]
    grouped = defaultdict(list)
    for row in excluded:
        if not row.get("archived_attempt"): raise ValueError("Excluded usage lost archive identity")
        grouped[row["archived_attempt"]].append(row)
    archives = {p.name.removeprefix(phase + "_"): p for p in (run / "failed_attempts").glob(phase + "_*") if p.is_dir()}
    if not set(grouped).issubset(archives): raise ValueError("Missing failed-attempt artifacts")
    expected_inputs = set()
    for row in rows:
        owner = archives[row["archived_attempt"]] if row.get("excluded_from_baseline") else run / phase
        agent = row["phase"].split(":")[1]
        expected_inputs.add(owner / "responses" / agent / f"request_{row['request_number']:05d}_input.json")
    actual_inputs = set((run / phase / "responses").glob("*/request_*_input.json"))
    actual_inputs |= {p for folder in archives.values() for p in (folder / "responses").glob("*/request_*_input.json")}
    if not actual_inputs.issubset(expected_inputs) or (not live and actual_inputs != expected_inputs):
        raise ValueError("Physical provider requests and ledger entries differ")
    attempts = []
    for key, folder in archives.items():
        saved = reader.read(folder / "status.json") if (folder / "status.json").exists() else {}
        if saved.get("status") == "completed" and not saved.get("infrastructure_error"):
            raise ValueError("Completed model execution was archived and rerun")
        elapsed = saved.get("elapsed_seconds")
        if elapsed is not None: number(elapsed)
        attempts.append({"directory": str(folder), "status": saved.get("status"),
            "usage": usage(grouped[key]), "elapsed_seconds": elapsed})
    clock_discrepancies = []
    for row in rows:
        if row.get("finished_at_unix") is None:
            continue
        owner = archives[row["archived_attempt"]] if row.get("excluded_from_baseline") else run / phase
        state_path = owner / "status.json"
        if not state_path.exists():
            continue
        # Live state is not frozen. Only completed or archived attempts are
        # compared, using their own recorded monotonic elapsed field.
        state = reader.read(state_path) if not live or row.get("excluded_from_baseline") else {}
        elapsed = state.get("elapsed_seconds")
        wall = row["finished_at_unix"] - row["started_at_unix"]
        if elapsed is not None and wall > elapsed + 5:
            clock_discrepancies.append({"request_number": row["request_number"], "phase": row["phase"],
                "attempt_directory": str(owner), "request_wall_timestamp_seconds": wall,
                "whole_attempt_recorded_elapsed_seconds": elapsed, "cause": "not established"})
    return {"directory": str(run), "phase": phase, "all_attempt_usage": usage(rows),
        "accepted_usage": usage(accepted), "failed_attempt_usage": usage(excluded), "failed_attempts": attempts,
        "failed_seconds": sum(a["elapsed_seconds"] or 0 for a in attempts),
        "unknown_time_attempts": sum(a["elapsed_seconds"] is None for a in attempts),
        "first_request_at_unix": min((r["started_at_unix"] for r in rows), default=None),
        "raw_usage": raw_usage(reader, run, rows, partial=live),
        "request_status_counts": dict(Counter(r["status"] for r in rows)),
        "unknown_completed_requests": sum(r.get("actual_cost_usd") is None and r["status"] != "in_flight" for r in rows),
        "clock_discrepancies": clock_discrepancies,
        "ledger_sha256": reader.sha(path), "live": live}


def receipt(reader, run, phase):
    record = reader.read(phase / "execution_receipt.json")
    for name, expected in record["protected_files_sha256"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts: raise ValueError("Unsafe receipt path")
        same(reader.sha(run / name), expected, "protected execution artifact")
    for name, expected in record["patch_sha256"].items(): same(reader.sha(phase / name), expected, "published patch")
    return record


def solo_score(reader, phase):
    score = reader.read(phase / "evaluation_summary.json")
    attempt = Path(score["attempt_directory"])
    if attempt.parent != phase / "evaluation_attempts": raise ValueError("Wrong Solo evaluation attempt")
    same(reader.read(attempt / "evaluation_summary.json"), score, "Solo accepted score")
    native = reader.read(attempt / "official_evaluation.json")
    same(reader.read(attempt / "evaluation.json"), native, "Solo native result")
    same(reader.read(phase / "evaluation.json"), native, "Solo accepted result")
    same(score["evaluation_completed"], True, "Solo evaluated")
    same(score["both_passed"], bool(native.get("both_passed")), "Solo pass")
    if native.get("error") or native.get("cleanup_error"): raise ValueError("Solo infrastructure result accepted")
    manifest = reader.read(attempt / "evaluation_input.json")["input_manifest"]
    same(reader.read(attempt / "integrity.json"), {"unchanged": True, "before": manifest, "after": manifest}, "Solo evaluation integrity")
    for info in manifest["protected_files"].values(): reader.info(info)
    for info in manifest["patches"].values(): reader.info(info)
    for name, expected in manifest["evaluator_sources"].items(): same(reader.sha(ROOT / name), expected, "Solo evaluator source")
    for i, feature in enumerate(score["features"], 1):
        same(score[f"feature_{feature}_passed"], bool((native.get(f"feature{i}") or {}).get("passed")), "Solo feature pass")
    outputs = [(native.get(f"feature{i}") or {}).get("test_output", "") for i in (1,2)]
    empty_apply = not (phase/"final.patch").read_bytes().strip() and all("No valid patches in input" in text for text in outputs)
    return {"official_success": score["both_passed"], "summary_sha256": reader.sha(phase / "evaluation_summary.json"),
        "selection": "fresh_solo_final_patch", "empty_published_patch": not (phase/"final.patch").read_bytes().strip(),
        "failure_stage": "apply_empty_patch_before_feature_tests" if empty_apply else None,
        "functional_tests_executed": False if empty_apply else None}


def verify_branch(reader, suite, row, protocol, prefixes, decisions):
    run, task, condition = run_path(suite, row), protocol["tasks"][row["task"]], row["condition"]
    phase = run / condition
    config, saved = reader.read(run / "task_config.json"), reader.read(phase / "status.json")
    same(config["protocol_sha256"], digest(protocol), "job protocol")
    same(config["task"], task, "job task")
    same(config["repeat"], row["repeat"], "job repeat")
    same(config["condition"], condition, "job condition")
    same(config["condition_version"], protocol["condition_versions"][condition], "job policy version")
    same(saved, row["execution"], "saved execution")
    same(saved["status"], "completed", "completed execution")
    same(reader.read(phase / "source_manifest.json"), protocol["source_manifest"], "worker sources")
    proof = receipt(reader, run, phase)
    if condition == "solo":
        initial = reader.read(phase / "initial_state.json")
        same(config["execution_seconds"], protocol["total_execution_seconds"], "Solo full allowance")
        for key in ("verified", "fresh_solo"): same(initial[key], True, "fresh proof")
        for key in ("checkpoint_loaded", "history_restored", "peer_evidence_mounted", "reference_patches_mounted", "communication_enabled", "credentials_forwarded"):
            same(initial[key], False, "Solo isolation " + key)
        same(initial["initial_message_count"], 2, "fresh history")
        same(initial["base_commit"], task["base"], "fresh base")
        same(initial["workspace"]["head"], task["base"], "fresh worktree base")
        same(initial["workspace"]["status_porcelain"], "", "clean initial worktree")
        same(initial["workspace"]["forbidden_paths_present"], [], "no reference mounts")
        same(initial["network_mode"], "none", "sandbox networking")
        for feature in task["features"]:
            feature_text = (ROOT / "CooperBench/dataset" / task["repo"] / f"task{task['task_id']}" / f"feature{feature}/feature.md").read_text()
            if feature_text not in initial["initial_messages"][1]["content"]: raise ValueError("Solo original requirement missing")
        return {"receipt_sha256": reader.sha(phase / "execution_receipt.json"), "fresh_state_verified": True,
                "score": solo_score(reader, phase)}
    prefix, decision = prefixes[row["task"]], decisions[row["task"]]
    cp = reader.read(run / "checkpoint.json")
    same(cp, prefix["checkpoint"], "common checkpoint")
    same(reader.sha(run / "shared_git.tar"), reader.sha(suite / row["task"] / "prefix/shared_git.tar"), "shared Git snapshot")
    same(reader.read(run / "checkpoint_provenance.json")["checkpoint_sha256"], digest(cp), "restore provenance")
    remaining = round(Decimal(protocol["total_execution_seconds"]) - number(prefix["record"]["elapsed_seconds"]) - number(decision["preparation"]["elapsed_seconds"]), 6)
    same(config["execution_seconds"], remaining, "common remaining allowance")
    same(saved["continuation_budget_seconds"], remaining, "runtime allowance")
    same(saved["initialization_included_in_budget"], True, "handoff consumes allowance")
    packet = reader.read(phase / "handoff_packet.json")
    same(packet, decision["packet"], "branch equals predecision packet")
    delivery = reader.read(phase / "handoff_delivery.json")
    text = (phase / "handoff_packet.txt").read_text()
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    same(delivery["packet_sha256"], digest(packet), "complete delivery digest")
    same(delivery["first_request_verified"], True, "both first requests")
    same(delivery["code_automatically_transferred"], False, "no automatic source transfer")
    restoration = reader.read(phase / "restore_proof.json")
    same(restoration["checkpoint_hash"], digest(cp), "restored checkpoint digest")
    for agent in ("agent1", "agent2"):
        state = cp["agents"][agent]["state"]
        messages = reader.read(phase / f"{agent}.traj.json")["messages"]
        same(messages[:len(state["messages"])], state["messages"], "inherited full history")
        same(restoration["agents"][agent]["state_hash"], digest(state), "model state restoration")
        same(restoration["agents"][agent]["filesystem_manifest_hash"], digest(cp["agents"][agent]["manifest"]), "workspace restoration")
        index = delivery["recipients"][agent]["message_index"]
        same(messages[index]["content"], text, "packet in resumed history")
        same(delivery["recipients"][agent]["message_sha256"], text_hash, "packet bytes")
        first = reader.read(sorted((phase / "responses" / agent).glob("request_*_input.json"))[0])["request"]["messages"]
        clean = [{k:v for k,v in m.items() if k in {"role","content","name","tool_calls","tool_call_id"}} for m in state["messages"]]
        same(first[:len(clean)], clean, "actual inherited history")
        same(first[index]["content"], text, "actual complete packet")
    if condition == "integrator":
        final = reader.read(phase / "final_delivery.json")
        same(final["authoritative_agent"], "agent1", "predesignated authority")
        same(final["patch_sha256"], proof["patch_sha256"]["agent1.patch"], "authoritative published patch")
        same(final["posthoc_selection"], False, "no outcome-based selection")
    return {"receipt_sha256": reader.sha(phase / "execution_receipt.json"), "checkpoint_sha256": digest(cp),
            "packet_sha256": digest(packet), "full_histories_checked": 2, "actual_first_inputs_checked": 2,
            "score": score_audit(reader, phase)}


def collect(suite, partial=False):
    suite = suite.resolve(strict=True)
    reader = Reader()
    protocol, saved, summary = [reader.read(suite / name) for name in ("protocol.json", "status.json", "summary.json")]
    same(protocol["version"], "luna_selector_v1", "suite")
    same(protocol["model"], "gpt-6-luna", "formal model")
    if not partial and (saved["status"] != "completed" or not summary["pipeline_completed"]):
        raise ValueError("Final audit requires completed pipeline")
    expected = [(t, r, c) for t in protocol["tasks"] for r, order in enumerate((CONDITIONS, tuple(reversed(CONDITIONS))), 1) for c in order]
    same([identity(j) for j in protocol["jobs"]], expected, "planned order")
    rows, skips = summary["rows"], summary["skipped_jobs"]
    completed, skipped = set(map(identity, rows)), set(map(identity, skips))
    if len(completed) != len(rows) or len(skipped) != len(skips) or completed & skipped or not (completed | skipped).issubset(expected):
        raise ValueError("Duplicate, overlapping or unplanned outcomes")
    if not partial: same(completed | skipped, set(expected), "complete denominator")
    for name, checksum in protocol["source_manifest"].items():
        same(reader.sha(ROOT / name), checksum, "current frozen source")
        same(reader.sha(suite / "source_snapshot" / name), checksum, "snapshot frozen source")
    historical = reader.read(ROOT / "artifacts/handoff_expansion_runs/luna_integrator_v1/protocol.json")
    for name, checksum in historical["source_manifest"].items():
        same(protocol["source_manifest"][name], checksum, "historical source unchanged")
    prefixes, decisions, audits, checks = {}, {}, {}, []
    preparation_times = []
    for key, task in protocol["tasks"].items():
        run = suite / key / "prefix"
        record = summary["prefixes"][key]
        for name, checksum in record["files_sha256"].items(): same(reader.sha(run / name), checksum, "prefix evidence")
        audit = ledger(reader, run, "prefix")
        audits[str(run)] = audit
        same(audit["all_attempt_usage"], record["all_attempt_usage"], "prefix cost")
        progress = reader.read(run / "prefix/progress.json")
        same(progress["selection_uses_test_results"], False, "checkpoint test blindness")
        cp = None
        if record["status"] == "completed":
            cp = reader.read(run / "checkpoint.json")
            same(digest(cp), record["checkpoint_sha256"], "checkpoint hash")
            same(cp["task"], task, "checkpoint task")
            for agent, feature in zip(("agent1", "agent2"), task["features"]):
                same(progress["progress"][agent]["has_code_change"], True, "two changed source states")
                same(progress["outcomes"][agent], {"submitted": False, "exited": False, "infrastructure_error": False}, "unfinished agents")
                required = (ROOT / "CooperBench/dataset" / task["repo"] / f"task{task['task_id']}" / f"feature{feature}/feature.md").read_text()
                same(cp["agents"][agent]["state"]["extra_template_vars"]["task"], required, "only original public requirement")
            folder = suite / key / "decision"
            packet, decision, prep = [reader.read(folder / name) for name in ("packet.json", "decision.json", "preparation.json")]
            from selector_rule import decide
            same(decide(packet), decision, "frozen outcome-blind rule")
            same(prep["packet_sha256"], digest(packet), "predecision packet receipt")
            same(prep["decision_sha256"], digest(decision), "frozen action receipt")
            same(prep["model_api_calls"], 0, "feature extraction calls")
            same(prep["before_continuation_outcomes"], True, "predecision ordering flag")
            same(packet["checkpoint_sha256"], digest(cp), "selector checkpoint")
            preparation_times.append(datetime.fromisoformat(prep["created_at"]).timestamp())
            for agent in ("agent1", "agent2"):
                same(packet["agents"][agent]["original_task"], cp["agents"][agent]["state"]["extra_template_vars"]["task"], "packet task source")
                changes = {r["path"]: r for r in cp["wip_checkpoint"]["progress"][agent]["changed_files"]}
                for change in packet["agents"][agent]["source_changes"]:
                    name = change["path"]
                    if not any(name.startswith(root + "/") for root in task["code_roots"]): raise ValueError("Packet source outside scope")
                    same(change["after_sha256"], changes[name]["after_sha256"], "packet source hash")
            decisions[key] = {"packet": packet, "decision": decision, "preparation": prep}
        else:
            same(record["status"], "checkpoint_not_reached", "no resampled checkpoint")
            same(progress["eligible"], False, "ineligible prefix")
            if (suite / key / "decision").exists() or (run / "checkpoint.json").exists(): raise ValueError("Ineligible task acquired a checkpoint")
        prefixes[key] = {"record": record, "checkpoint": cp, "ledger": audit, "progress": progress}
    for job in protocol["jobs"]:
        run = run_path(suite, job)
        if (run / "usage_ledger.json").exists():
            audit = ledger(reader, run, job["condition"], live=partial and identity(job) not in completed)
            audits[str(run)] = audit
            if audit["first_request_at_unix"] is not None and any(t >= audit["first_request_at_unix"] for t in preparation_times):
                raise ValueError("A rollout began before all decisions were frozen")
    actual_paths = set(suite.glob("*/prefix/usage_ledger.json")) | set(suite.glob("*/jobs/*/usage_ledger.json")) | set(suite.glob("*/solo_jobs/*/usage_ledger.json"))
    same(actual_paths, {Path(p)/"usage_ledger.json" for p in audits}, "physical ledger inventory")
    for skip in skips:
        if skip["reason"] != "checkpoint_not_reached": raise ValueError("New budget skip requires audit extension")
        if prefixes[skip["task"]]["record"]["status"] != "checkpoint_not_reached" or skip["condition"] == "solo": raise ValueError("Improper ineligibility skip")
        if run_path(suite, skip).exists(): raise ValueError("Ineligible action was executed")
    for row in rows:
        run = run_path(suite, row); audit = audits[str(run)]
        same(reader.read(run / "result.json"), row, "per-run result")
        same(audit["all_attempt_usage"], row["all_attempt_usage"], "all branch requests")
        same(audit["accepted_usage"], row["accepted_usage"], "valid branch requests")
        extra = prefixes[row["task"]]["ledger"] if row["condition"] != "solo" else None
        full = sum_usage([audit["all_attempt_usage"]] + ([extra["all_attempt_usage"]] if extra else []))
        same(row["full_task_known_cost_usd"], full["known_cost_usd"], "full-task comparison cost")
        same(row["full_task_unknown_usage_requests"], full["unknown_usage_requests"], "comparison unknowns")
        elapsed = number(row["execution"]["elapsed_seconds"]) + number(audit["failed_seconds"])
        if extra:
            elapsed += number(prefixes[row["task"]]["record"]["elapsed_seconds"]) + number(extra["failed_seconds"]) + number(decisions[row["task"]]["preparation"]["elapsed_seconds"])
        same(row["full_task_execution_seconds"], round(elapsed,6), "full-task time")
        branch = verify_branch(reader, suite, row, protocol, prefixes, decisions)
        same(branch["score"]["official_success"], row["official_success"], "official outcome")
        same(reader.read(run/row["condition"]/"evaluation_summary.json"), row["evaluation"], "saved score")
        checks.append({"task": row["task"], "repeat": row["repeat"], "condition": row["condition"], **branch})
    selected = [{**row, "policy": "checkpoint_overlap_selector", "selected_action": row["condition"],
                 "reused_counterfactual": True, "additional_model_requests": 0, "additional_model_cost_usd": 0}
                for row in rows if row["condition"] != "solo" and row["condition"] == decisions[row["task"]]["decision"]["selected_action"]]
    same(summary["selector_reused_rows"], selected, "selector is exact rollout reuse")
    same(summary["selector_has_independent_runs"], False, "no extra policy samples")
    coverage = []
    for key in protocol["tasks"]:
        for policy in (*CONDITIONS, "selector"):
            action = decisions.get(key, {}).get("decision", {}).get("selected_action") if policy == "selector" else policy
            representative = action or "strongshare"
            planned = sum(j["task"] == key and j["condition"] == representative for j in protocol["jobs"])
            scored = [r for r in rows if r["task"] == key and r["condition"] == action]
            ineligible = sum(s["task"] == key and s["condition"] == representative for s in skips)
            pending = planned - len(scored) - ineligible
            successes = sum(r["official_success"] for r in scored)
            coverage.append({"task": key, "policy": policy, "selected_action": action, "planned": planned,
                "scored": len(scored), "successes": successes, "budget_failures": 0,
                "ineligible": ineligible, "pending": pending, "completed_eligible": len(scored),
                "budget_aware_success_rate": successes/len(scored) if pending == 0 and scored else None,
                "official_scores_for_budget_failures": None})
    same(summary["policy_coverage"], coverage, "eligible versus ineligible policy denominator")
    total = sum_usage(a["all_attempt_usage"] for a in audits.values())
    unfinished_usage = sum_usage(a["accepted_usage"] for a in audits.values() if a["live"])
    if not partial: same(summary["actual_new_usage"], total, "new actual bill without prefix duplication")
    counters, token_buckets, errors, statuses = Counter(), Counter(), Counter(), Counter()
    for audit in audits.values():
        counters.update(audit["raw_usage"]["counts"]); token_buckets.update(audit["raw_usage"]["tokens"])
        errors.update(audit["raw_usage"]["error_artifact_types"])
        statuses.update(audit["request_status_counts"])
    groups = []
    for key in protocol["tasks"]:
        for condition in (*CONDITIONS, "selector"):
            values = [r for r in (selected if condition == "selector" else rows) if r["task"] == key and (condition == "selector" or r["condition"] == condition)]
            groups.append({"task": key, "condition": condition, "scored": len(values), "passes": sum(r["official_success"] for r in values),
                "mean_full_task_known_cost_usd": float(sum(number(r["full_task_known_cost_usd"]) for r in values)/len(values)) if values else None,
                "mean_full_task_seconds": float(sum(number(r["full_task_execution_seconds"]) for r in values)/len(values)) if values else None,
                "unknown_requests_in_comparison": sum(r["full_task_unknown_usage_requests"] for r in values)})
    changed = reader.stable()
    permissible = {str(suite/"status.json"), str(suite/"summary.json")}
    permissible |= {str(Path(a["directory"])/"usage_ledger.json") for a in audits.values() if a["live"]}
    unexpected = [p for p in changed if p not in permissible]
    if unexpected or changed and not partial: raise ValueError("Evidence changed during audit: " + str(unexpected or changed))
    return {"schema": "independent_selector_integrity_v1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "partial_verified" if partial else "completed_verified", "suite_directory": str(suite),
        "protocol_sha256": digest(protocol), "frozen_sources": len(protocol["source_manifest"]), "historical_sources": len(historical["source_manifest"]),
        "planned_jobs": len(expected), "completed_scores": len(rows), "skipped_jobs": skips,
        "official_passes": sum(r["official_success"] for r in rows), "eligible_checkpoints": len(decisions),
        "environment_exclusions": protocol["environment_exclusions"],
        "decisions": {k:{"action":v["decision"]["selected_action"],"common_files":v["decision"]["features"]["common_source_files"],"overlaps":v["decision"]["features"]["overlaps"]} for k,v in decisions.items()},
        "new_actual_usage": total, "summary_usage_at_read_time": summary["actual_new_usage"],
        "unique_prefix_usage": sum_usage(v["ledger"]["all_attempt_usage"] for v in prefixes.values()),
        "failed_attempt_usage": sum_usage(a["failed_attempt_usage"] for a in audits.values()),
        "unarchived_unfinished_attempt_usage": unfinished_usage,
        "raw_transport_counts": dict(counters), "raw_token_buckets": dict(token_buckets), "error_artifact_types": dict(errors),
        "request_status_counts": dict(statuses), "unknown_completed_requests": sum(a["unknown_completed_requests"] for a in audits.values()),
        "archived_incomplete_attempt_count": sum(len(a["failed_attempts"]) for a in audits.values()),
        "clock_discrepancies": [x for a in audits.values() for x in a["clock_discrepancies"]],
        "official_failure_before_functional_tests": [c for c in checks if c["score"].get("functional_tests_executed") is False],
        "groups": groups, "policy_coverage": coverage, "verified_executions": checks,
        "ledgers": list(audits.values()), "mutable_live_files_during_partial_read": changed,
        "interpretation": ["Selector outcomes reuse fixed preselected branches; they are not extra independent samples.",
            "Prefix costs are charged once in actual spend and once per rollout only in full-task comparison.",
            "Rule projection contains only checkpoint source diffs; original public requirements and scratch are provenance/delivery, not selector features.",
            "No hidden-test/result fields entered the frozen rule; this audit does not establish freedom from model training contamination.",
            "Jinja ineligibility and Outlines environment exclusion are distinct from official task failures.",
            "Click Solo's two empty published patches failed native application before functional tests; they are publication/harness failures, not evidence its implementation failed those tests.",
            "The frozen native harness skips submission nudges when comm=None (Solo), whereas cooperative agents can receive them; this limits algorithmic interpretation of Solo versus multi-agent scores.",
            "Request wall timestamps can diverge from worker monotonic elapsed; preserve both without inferring a host-sleep cause, and do not use timestamp gaps as algorithm runtime estimates.",
            "With only two eligible checkpoints this batch provides descriptive task-level policy evidence, not population inference."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT)
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = collect(args.suite, args.partial)
    if args.output:
        output = args.output.absolute()
        if output.parent != args.suite.resolve() or not output.name.startswith("audit_") or output.suffix != ".json" or output.is_symlink():
            raise ValueError("Output must be a derived audit_*.json inside this suite")
        # Only this derived report may be refreshed; no execution evidence is edited.
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k:result[k] for k in ("status","completed_scores","official_passes","new_actual_usage","failed_attempt_usage","raw_transport_counts","groups")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
