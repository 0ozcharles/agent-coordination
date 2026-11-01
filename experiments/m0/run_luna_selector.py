"""Frozen edit-overlap policy on three runnable, previously unused tasks.

Every checkpoint receives a decision before any continuation. Two complete
counterfactual actions are sampled; the deterministic policy is evaluated by
reusing its preselected action, not by inventing a third independent run.
Four tasks were selected publicly. Outlines was blocked by a native dependency
installation timeout before pytest; it is retained as an environment exclusion.
"""
import argparse
import fcntl
import math
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone

from budget_guard import write_json
from task_specs import ROOT, REPO, COMMIT
from selector_task_specs import TASKS as ALL_TASKS, get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import archive_incomplete, copy_checkpoint, incomplete_seconds, prefix_record
from run_repeats import read_json, run_command
from run_task_expansion import add_usage, file_hash, write_fixed
from run_solo_smoke import configure

RUNS = ROOT / "artifacts/selector_runs"
VERSION = "luna_selector_v1"
MODEL = "gpt-6-luna"
PREFIX_SECONDS = 1200
TOTAL_SECONDS = 1800
CONDITIONS = ("strongshare", "integrator", "solo")
VARIANTS = {"prefix": "source_handoff_diff_v1", "strongshare": "source_handoff_diff_v1",
            "integrator": "designated_integrator_v1", "solo": "luna_fresh_solo_v1"}
PROOF = ROOT / "artifacts/selector_offline_validation.json"
REFERENCE_PROOF = ROOT / "artifacts/selector_reference_validation.json"
NEW_SOURCES = ("run_luna_selector.py", "selector_workers.py", "selector_evaluator.py",
               "selector_rule.py", "selector_task_specs.py", "task_selector_selection.json",
               "preflight_selector_tasks.py")
EXHAUSTED_STATES = ("time_budget_exhausted_before_first_query", "budget_exhausted")
ENVIRONMENT_BLOCKED = ("outlines1371_f1_f2",)
TASKS = {key: task for key, task in ALL_TASKS.items() if key not in ENVIRONMENT_BLOCKED}


def source_manifest():
    historical = ROOT / "artifacts/handoff_expansion_runs/luna_integrator_v1/protocol.json"
    result = dict(read_json(historical)["source_manifest"])
    for name, expected in result.items():
        if file_hash(ROOT / name) != expected:
            raise RuntimeError("A historical frozen dependency changed: " + name)
    paths = [ROOT / name for name in NEW_SOURCES] + [REFERENCE_PROOF, historical]
    for task in ALL_TASKS.values():
        paths += [task.image_record_path, task.task_dir / "Dockerfile", task.task_dir / "runner.sh"]
        paths += [task.task_dir / f"feature{f}" / name for f in task.features
                  for name in ("feature.md", "tests.patch")]
    result.update({str(p.relative_to(ROOT)): file_hash(p) for p in paths})
    return result


def verify_references():
    proof = read_json(REFERENCE_PROOF, {})
    if (proof.get("model_api_calls") != 0
            or proof.get("cooperbench_commit") != COMMIT
            or proof.get("reference_data_exposed_to_agents") is not False
            or proof.get("selection_sha256") != file_hash(ROOT / "task_selector_selection.json")
            or set(proof.get("tasks", {})) != set(ALL_TASKS)):
        raise RuntimeError("Complete the frozen new-task environment/scoring checks first")
    for key, task in ALL_TASKS.items():
        row = proof["tasks"][key]
        if (row.get("task") != task.as_dict()
                or row.get("image_id") != read_json(task.image_record_path).get("Id")):
            raise RuntimeError("New-task reference/image proof differs: " + key)
        if key in TASKS and not all(row.get(k) is True for k in (
                "passed", "base_fails_both", "individual_gold_passes_both", "combined_gold_passes_both")):
            raise RuntimeError("An active task has not passed environment/scoring checks: " + key)
        if key in ENVIRONMENT_BLOCKED and (row.get("status") != "error" or row.get("passed") is not False):
            raise RuntimeError("Recorded environment exclusion changed; review before freezing a new protocol")
    for record in [proof, *proof["tasks"].values()]:
        for name, expected in {**record.get("source_hashes", {}), **record.get("source_sha256", {})}.items():
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT) or file_hash(path) != expected:
                raise RuntimeError("Reference evidence changed: " + name)
    return proof


def schedule(offline=False):
    orders = (CONDITIONS,) if offline else (CONDITIONS, tuple(reversed(CONDITIONS)))
    return [{"task": key, "repeat": repeat, "condition": condition}
            for key in TASKS for repeat, order in enumerate(orders, 1) for condition in order]


def protocol_for(offline=False):
    from selector_rule import RULE_ID, RULE
    references = verify_references()
    settings = read_json(ROOT / "artifacts/handoff_expansion_runs/luna_integrator_v1/protocol.json")
    fields = ("provider", "endpoint", "reasoning_effort", "temperature", "top_p", "max_completion_tokens",
              "service_tier", "prompt_cache_options", "pricing_usd_per_million")
    return {"version": VERSION, "offline_selftest": offline, "dataset_commit": COMMIT,
        "model": "offline-scripted-model" if offline else MODEL,
        **{k: settings[k] for k in fields}, "tasks": {k: t.as_dict() for k, t in TASKS.items()},
        "originally_selected_tasks": {k: t.as_dict() for k, t in ALL_TASKS.items()},
        "environment_exclusions": {k: {"status": "environment_blocked_before_model_runs",
            "reason": references["tasks"][k].get("error"), "counts_as_model_failure": False,
            "replaced_with_another_task": False} for k in ENVIRONMENT_BLOCKED},
        "jobs": schedule(offline), "condition_versions": VARIANTS, "selector_rule": RULE_ID,
        "selector_rule_definition": RULE,
        "total_execution_seconds": TOTAL_SECONDS, "prefix_horizon_seconds": PREFIX_SECONDS,
        "money_limit": None, "request_limit": None,
        "research_question": "Can an edit-overlap rule choose between StrongShare and Integrator on unseen tasks better than either fixed action?",
        "development_data": "The previous LlamaIndex17070 and Pillow68 early/late checkpoints informed this rule. All four selected task IDs are new; three passed environment checks and are included before any model run. Click/Jinja repositories were seen earlier. This is an exploratory held-out-task pilot, not a large confirmatory study.",
        "rule": "Use only exact base-relative changed lines and pure insertion positions from the complete checkpoint source diffs. Shared changed base lines or a shared insertion position choose Integrator; otherwise StrongShare. No terminal outcomes, hidden tests, public-test feedback, semantic model, or rule retuning on these tasks.",
        "rule_limit": "Textual overlap includes changed comments/docstrings and identical edits. It is a deliberately simple integration-risk heuristic, not a semantic-conflict detector. Both agents adding the same file, or deleting a file the peer edits, also counts as overlap. The old Llama positive example shares an edited comment line, so development separation alone is not semantic evidence.",
        "checkpoint_rule": "First completed paired round with source AST changes by both unfinished agents; unchanged frozen WIP gate. All task checkpoints and decisions are prepared before any continuation or Solo outcome. No checkpoint resampling for eligibility or score.",
        "time_rule": "1800 seconds total per task. Continuations subtract accepted prefix elapsed time plus common checkpoint-packet/decision preparation time. Both fixed actions receive this identical remainder; restore/handoff are included. Solo starts fresh with 1800 seconds. In-flight calls may finish after deadline. Actual failed infrastructure time remains reported separately.",
        "decision_timing": "Write an immutable decision and preparation receipt before creating any continuation. Recompute and verify from the same packet before every branch. Both actions must receive the identical packet used by the rule.",
        "policy_evaluation": "Full-information rollout reuse: for each task/repeat, select the one branch whose action was frozen at the checkpoint. The selector has no third model run and no independent sample. Never choose the higher score or lower realized cost. Pairing is by common checkpoint, not common random seed.",
        "primary_score": "Unchanged pinned CooperBench two-feature success. StrongShare uses native merge/lead fallback; Integrator uses only predesignated agent1 delivery while both agents stay active. This tests the combined organization-and-delivery policy, not an isolated role-prompt effect.",
        "solo_reference": "Two fresh Solo runs per new task, same original two requirements and total allowance; no inherited code/history. A reference for whether multi-agent use is worthwhile, not a checkpoint action.",
        "missing_states": "If a first prefix ends without an eligible checkpoint, mark its action jobs ineligible and retain Solo. No replacement task or prefix resampling. Report coverage separately; policy results describe eligible states only.",
        "stopping": "Finish this fixed batch: three prefixes and up to 18 scored executions (12 action branches, six Solo). Outlines remains an environment exclusion, not a replacement or model failure. Retain completed failures. Infrastructure problems pause for inspection; scoring retry reuses saved code.",
        "order": "Per task r1 StrongShare/Integrator/Solo; r2 Solo/Integrator/StrongShare. Shared implicit caching is allowed and measured; not cold-cache estimates.",
        "cost_rule": "Actual new spend sums each prefix once and all branch/Solo requests, including interrupted attempts. Per-policy task cost adds the common prefix once per selected rollout for comparison; reused selector outcomes add zero billable calls. These comparison costs must never be summed into project spend.",
        "success_criterion": "Report success counts and cost/time for all fixed actions and the frozen selector. Selection value requires an advantage over the stronger fixed action, not only StrongShare. With three states/two repeats no population/significance claim. If all decisions coincide, this batch cannot show adaptive selection value.",
        "source_manifest": source_manifest()}


def verify_sources(protocol):
    if protocol.get("source_manifest") != source_manifest():
        raise RuntimeError("Frozen selector sources or task evidence changed")


def verify_preflight():
    proof = read_json(PROOF, {})
    if (proof.get("passed") is not True or proof.get("model_api_calls") != 0
            or proof.get("prefixes_completed") != len(TASKS)
            or proof.get("completed_jobs") != len(TASKS) * 3
            or proof.get("source_manifest") != source_manifest()):
        raise RuntimeError("Exact-source offline prefix/StrongShare/Integrator/Solo proof required for all three active tasks")


def job_path(suite, job):
    if (job["task"] not in TASKS or job["condition"] not in CONDITIONS
            or type(job["repeat"]) is not int or job["repeat"] not in (1, 2)):
        raise ValueError("Unplanned selector job")
    kind = "solo_jobs" if job["condition"] == "solo" else "jobs"
    return suite / job["task"] / kind / f"{job['task']}_r{job['repeat']:02d}_{job['condition']}"


def configure_run(run, task, condition, protocol, seconds, checkpoint_sha=None, repeat=None):
    config = {"task_key": task.key, "task": task.as_dict(), "condition": condition,
        "condition_version": VARIANTS[condition], "experiment_version": VERSION,
        "offline_selftest": protocol["offline_selftest"], "model": protocol["model"],
        "protocol_sha256": digest(protocol), "execution_seconds": seconds,
        "initialization_included_in_budget": True}
    if repeat is not None:
        config["repeat"] = repeat
    if condition == "solo":
        if checkpoint_sha is not None:
            raise ValueError("Fresh Solo cannot inherit a checkpoint")
        config["fresh_solo"] = True
    elif condition != "prefix":
        config.update(checkpoint_sha256=checkpoint_sha,
            submission_policy="authoritative_agent1_only" if condition == "integrator" else "native_cooperative_merge")
    run.mkdir(parents=True, exist_ok=True)
    write_fixed(run / "task_config.json", config)
    return config


def verify_execution(phase, task, condition, freeze=False):
    from selector_workers import verify_execution as verify
    return verify(phase, task, condition, freeze=freeze)


def verify_prefix(run, task, protocol):
    configure_run(run, task, "prefix", protocol, PREFIX_SECONDS)
    if read_json(run / "prefix/source_manifest.json") != protocol["source_manifest"]:
        raise RuntimeError("Prefix source manifest differs")
    ledger = read_json(run / "usage_ledger.json", {"requests": []})["requests"]
    if (protocol["offline_selftest"] and ledger or not protocol["offline_selftest"] and not ledger
            or any(row.get("model") != MODEL for row in ledger)):
        raise RuntimeError("Unexpected prefix model usage")
    return prefix_record(run, task)


def prepare_decision(suite, task, record):
    from selector_rule import decide, verify_decision
    from selector_workers import collect_checkpoint_packet
    prefix = suite / task.key / "prefix"
    folder = suite / task.key / "decision"
    folder.mkdir(exist_ok=True)
    receipt_path = folder / "preparation.json"
    packet_path, decision_path = folder / "packet.json", folder / "decision.json"
    if not receipt_path.exists():
        # No continuation is allowed before a complete frozen receipt exists.
        if any(any((suite / task.key / kind).glob("*")) for kind in ("jobs", "solo_jobs")):
            raise RuntimeError("An experiment branch already exists without its pre-run decision")
        start = time.monotonic()
        collect_checkpoint_packet(prefix, task, packet_path)
        packet = read_json(packet_path)
        decision = decide(packet)
        write_fixed(decision_path, decision)
        write_fixed(receipt_path, {"task": task.key, "checkpoint_sha256": record["checkpoint_sha256"],
            "packet_sha256": digest(packet), "decision_sha256": digest(decision),
            "elapsed_seconds": round(time.monotonic() - start, 6),
            "model_api_calls": 0, "before_continuation_outcomes": True,
            "created_at": datetime.now(timezone.utc).isoformat()})
    packet, decision, receipt = read_json(packet_path), read_json(decision_path), read_json(receipt_path)
    verify_decision(packet, decision)
    elapsed = receipt.get("elapsed_seconds")
    if (packet.get("checkpoint_sha256") != record["checkpoint_sha256"]
            or receipt.get("checkpoint_sha256") != record["checkpoint_sha256"]
            or receipt.get("task") != task.key or packet.get("task_key") != task.key
            or receipt.get("packet_sha256") != digest(packet) or receipt.get("decision_sha256") != digest(decision)
            or receipt.get("model_api_calls") != 0 or receipt.get("before_continuation_outcomes") is not True
            or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed) or elapsed < 0):
        raise RuntimeError("Pre-run selector decision or provenance changed")
    return {"decision": decision, "preparation": receipt}


def remaining_seconds(record, decision):
    return round(TOTAL_SECONDS - record["elapsed_seconds"] - decision["preparation"]["elapsed_seconds"], 6)


def model_phase(run, task, condition, protocol, suite, lock_fd):
    phase = run / condition
    terminal = (("completed", "checkpoint_not_reached") if condition == "prefix" else
                ("completed",) if condition == "solo" else ("completed", *EXHAUSTED_STATES))
    saved = read_json(phase / "status.json", {})
    if saved.get("status") not in terminal:
        if not protocol["offline_selftest"] and not os.environ.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY absent; no API request started")
        archive_incomplete(run, condition)
        phase.mkdir()
        write_json(phase / "source_manifest.json", protocol["source_manifest"])
        config = read_json(run / "task_config.json")
        print(f"{task.key}: {run.name}/{condition}", flush=True)
        try:
            code = run_command([sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--worker",
                "--run-dir", str(run), "--suite-dir", str(suite)], phase / "console.log",
                config["execution_seconds"] + 300, lock_fd)
        finally:
            cleanup_phase(run, condition)
        saved = read_json(phase / "status.json", {})
        if code or saved.get("status") not in terminal:
            raise RuntimeError("Selector experiment execution paused: " + str(phase))
    if condition == "prefix":
        verify_prefix(run, task, protocol)
    elif saved["status"] == "completed":
        verify_execution(phase, task, condition, freeze=True)
    else:
        from run_luna_collaboration import validate_budget_terminal
        if condition not in ("strongshare", "integrator"):
            raise RuntimeError("Only a checkpoint continuation can have a handoff-budget terminal outcome")
        if condition == "integrator" and (saved.get("condition_version") != VARIANTS[condition]
                or saved.get("active_agents") != ["agent1", "agent2"]
                or saved.get("authoritative_agent") != "agent1" or saved.get("peer_retired") is not False):
            raise RuntimeError("Budget terminal lost the designated Integrator policy")
        validate_budget_terminal(run, task, condition, protocol)
    cleanup_phase(run, condition)
    return saved


def evaluate(run, task, condition, lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase = run / condition
    before = verify_execution(phase, task, condition)
    code = run_command([sys.executable, "-B", str(ROOT / "selector_evaluator.py"), "--run-dir", str(phase)],
        phase / "evaluation_console.log", 720, lock_fd, env=evaluator_environment())
    score = read_json(phase / "evaluation_summary.json", {})
    if code or not score.get("evaluation_completed"):
        raise RuntimeError("Scoring paused; completed execution will be reused")
    if before != verify_execution(phase, task, condition):
        raise RuntimeError("Published execution changed during scoring")
    return score


def build_row(run, job, execution, score, record=None, decision=None):
    current = usage(run)
    failed = incomplete_seconds(run, job["condition"])
    common = record["all_attempt_usage"] if record is not None else add_usage([])
    prefix_failed = incomplete_seconds(run.parent.parent / "prefix", "prefix") if record is not None else {"known_seconds": 0, "unknown_attempts": 0}
    common_seconds = (record["elapsed_seconds"] + prefix_failed["known_seconds"] + decision["preparation"]["elapsed_seconds"]) if record else 0
    return {**job, "run_directory": str(run), "model": MODEL,
        "official_success": bool(score["both_passed"]), "execution": execution, "evaluation": score,
        "accepted_usage": usage(run, job["condition"]), "all_attempt_usage": current,
        "common_prefix_usage": common, "full_task_known_cost_usd": common["known_cost_usd"] + current["known_cost_usd"],
        "full_task_unknown_usage_requests": common["unknown_usage_requests"] + current["unknown_usage_requests"],
        "full_task_execution_seconds": round(common_seconds + execution["elapsed_seconds"] + failed["known_seconds"], 6),
        "full_task_time_unknown_attempts": prefix_failed["unknown_attempts"] + failed["unknown_attempts"],
        "checkpoint_sha256": record["checkpoint_sha256"] if record else None,
        "decision_sha256": digest(decision["decision"]) if decision else None,
        "remaining_execution_allowance_seconds": read_json(run / "task_config.json")["execution_seconds"],
        "total_execution_allowance_seconds": TOTAL_SECONDS}


def policy_rows(rows, decisions):
    selected = []
    for row in rows:
        if row["condition"] == "solo":
            continue
        decision = decisions[row["task"]]["decision"]
        if row["condition"] == decision["selected_action"]:
            selected.append({**row, "policy": "checkpoint_overlap_selector",
                "selected_action": row["condition"], "reused_counterfactual": True,
                "additional_model_requests": 0, "additional_model_cost_usd": 0})
    return selected


def groups_for(rows, selected):
    groups = []
    for key in TASKS:
        for policy in (*CONDITIONS, "selector"):
            data = [r for r in (selected if policy == "selector" else rows)
                    if r["task"] == key and (policy == "selector" or r["condition"] == policy)]
            if data:
                groups.append({"task": key, "policy": policy, "n": len(data),
                    "means_cover_scored_executions_only": True,
                    "official_passes": sum(r["official_success"] for r in data),
                    "full_task_known_cost_mean": sum(r["full_task_known_cost_usd"] for r in data) / len(data),
                    "unknown_usage_requests": sum(r["full_task_unknown_usage_requests"] for r in data),
                    "full_task_execution_seconds_mean": sum(r["full_task_execution_seconds"] for r in data) / len(data),
                    "unknown_time_attempts": sum(r["full_task_time_unknown_attempts"] for r in data)})
    return groups


def policy_coverage(protocol, rows, skipped, decisions):
    """Keep budget failures in policy denominators; ineligible states separate."""
    result = []
    for key in TASKS:
        for policy in (*CONDITIONS, "selector"):
            action = decisions.get(key, {}).get("decision", {}).get("selected_action") if policy == "selector" else policy
            representative = action or "strongshare"
            planned = [j for j in protocol["jobs"] if j["task"] == key and j["condition"] == representative]
            scored = [r for r in rows if r["task"] == key and r["condition"] == action]
            absent = [r for r in skipped if r["task"] == key and r["condition"] == representative]
            ineligible = sum(r["reason"] == "checkpoint_not_reached" for r in absent)
            budget = sum(r["reason"] in (*EXHAUSTED_STATES, "total_budget_exhausted") for r in absent)
            pending = len(planned) - len(scored) - ineligible - budget
            if pending < 0:
                raise RuntimeError("Duplicate terminal policy records")
            completed = len(scored) + budget
            successes = sum(r["official_success"] for r in scored)
            result.append({"task": key, "policy": policy, "selected_action": action,
                "planned": len(planned), "scored": len(scored), "successes": successes,
                "budget_failures": budget, "ineligible": ineligible, "pending": pending,
                "completed_eligible": completed,
                "budget_aware_success_rate": successes / completed if pending == 0 and completed else None,
                "official_scores_for_budget_failures": None})
    return result


def summary_for(suite, protocol, rows, skipped, prefixes, decisions):
    ledgers = list(suite.glob("*/prefix/usage_ledger.json")) + list(suite.glob("*/jobs/*/usage_ledger.json")) + list(suite.glob("*/solo_jobs/*/usage_ledger.json"))
    selected = policy_rows(rows, decisions)
    return {"version": VERSION, "pipeline_completed": len(rows) + len(skipped) == len(protocol["jobs"]),
        "planned_executions": len(protocol["jobs"]), "completed_executions": len(rows), "rows": rows,
        "skipped_jobs": skipped, "prefixes": prefixes, "decisions": decisions,
        "eligible_checkpoints": len(decisions), "planned_checkpoints": len(TASKS),
        "environment_exclusions": protocol.get("environment_exclusions", {}),
        "selector_reused_rows": selected, "comparison_groups": groups_for(rows, selected),
        "policy_coverage": policy_coverage(protocol, rows, skipped, decisions),
        "actual_new_usage": add_usage([usage(p.parent) for p in ledgers]),
        "selector_has_independent_runs": False, "selector_additional_model_requests": 0,
        "interpretation": protocol["policy_evaluation"]}


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
            path = suite / "source_snapshot" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and file_hash(path) != expected:
                raise RuntimeError("Frozen source snapshot differs")
            if not path.exists():
                path.write_bytes((ROOT / name).read_bytes())
        rows, skipped, prefixes, decisions = [], [], {}, {}
        try:
            for key, task in TASKS.items():
                verify_sources(protocol)
                prefix = suite / key / "prefix"
                configure_run(prefix, task, "prefix", protocol, PREFIX_SECONDS)
                write_json(suite / "status.json", {"status": "running", "stage": "prefix", "task": key, "completed_jobs": 0})
                model_phase(prefix, task, "prefix", protocol, suite, lock.fileno())
                record = verify_prefix(prefix, task, protocol)
                prefixes[key] = record
                if record["status"] == "completed":
                    decisions[key] = prepare_decision(suite, task, record)
                    print(f"{key}: checkpoint decision={decisions[key]['decision']['selected_action']}", flush=True)
                write_json(suite / "summary.json", summary_for(suite, protocol, rows, skipped, prefixes, decisions))
            for job in protocol["jobs"]:
                verify_sources(protocol)
                task, condition = get_task(job["task"]), job["condition"]
                record = prefixes[task.key]
                decision = prepare_decision(suite, task, record) if record["status"] == "completed" else None
                if condition != "solo" and (decision is None or remaining_seconds(record, decision) <= 0):
                    skipped.append({**job, "reason": "checkpoint_not_reached" if decision is None else "total_budget_exhausted"})
                    continue
                seconds = TOTAL_SECONDS if condition == "solo" else remaining_seconds(record, decision)
                run = job_path(suite, job)
                configure_run(run, task, condition, protocol, seconds,
                    None if condition == "solo" else record["checkpoint_sha256"], job["repeat"])
                if condition != "solo":
                    copy_checkpoint(suite / task.key / "prefix", run, task, record)
                write_json(suite / "status.json", {"status": "running", **job, "completed_jobs": len(rows), "planned_jobs": len(protocol["jobs"])})
                execution = model_phase(run, task, condition, protocol, suite, lock.fileno())
                if execution["status"] in EXHAUSTED_STATES:
                    skipped.append({**job, "reason": execution["status"], "run_directory": str(run)})
                else:
                    if condition != "solo" and digest(read_json(run / condition / "handoff_packet.json")) != decision["preparation"]["packet_sha256"]:
                        raise RuntimeError("Action did not receive the exact pre-decision packet")
                    score = evaluate(run, task, condition, lock.fileno())
                    row = build_row(run, job, execution, score, record if condition != "solo" else None, decision if condition != "solo" else None)
                    rows.append(row)
                    write_json(run / "result.json", row)
                    print(f"{task.key} {condition} r{job['repeat']}: official={row['official_success']}, known=${row['all_attempt_usage']['known_cost_usd']:.5f}", flush=True)
                write_json(suite / "summary.json", summary_for(suite, protocol, rows, skipped, prefixes, decisions))
            verify_sources(protocol)
            summary = summary_for(suite, protocol, rows, skipped, prefixes, decisions)
            write_json(suite / "summary.json", summary)
            if offline:
                if len(rows) != len(TASKS) * 3 or len(decisions) != len(TASKS) or skipped or summary["actual_new_usage"]["requests"]:
                    raise RuntimeError("Offline proof needs every task/checkpoint/condition and zero API calls")
                for row in rows:
                    if row["condition"] == "solo" and row["execution"].get("outcome") != "Submitted":
                        raise RuntimeError("Offline Solo must submit")
                    if row["condition"] != "solo" and (set(row["execution"]["outcomes"]) != {"agent1", "agent2"}
                            or any(o["status"] != "Submitted" for o in row["execution"]["outcomes"].values())):
                        raise RuntimeError("Both offline agents must submit")
                write_json(PROOF, {"passed": True, "model_api_calls": 0, "prefixes_completed": len(prefixes),
                    "completed_jobs": len(rows), "source_manifest": protocol["source_manifest"], "suite_directory": str(suite)})
            write_json(suite / "status.json", {"status": "completed", "completed_jobs": len(rows), "skipped_jobs": len(skipped),
                "planned_jobs": len(protocol["jobs"]), "eligible_checkpoints": len(decisions)})
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            write_json(suite / "summary.json", summary_for(suite, protocol, rows, skipped, prefixes, decisions))
            write_json(suite / "status.json", {"status": "paused", "completed_jobs": len(rows), "skipped_jobs": len(skipped), "error": safe_error(exc)})
            print("Selector experiment paused: " + safe_error(exc), flush=True)
            return 2


def worker(run, suite):
    from selector_workers import phase_worker
    protocol = read_json(suite / "protocol.json")
    verify_sources(protocol)
    prefix_keys = [k for k in TASKS if run == suite / k / "prefix"]
    if prefix_keys:
        task, condition, seconds = get_task(prefix_keys[0]), "prefix", PREFIX_SECONDS
        configure_run(run, task, condition, protocol, seconds)
    else:
        jobs = [j for j in protocol["jobs"] if job_path(suite, j) == run]
        if len(jobs) != 1:
            raise ValueError("Unplanned worker directory")
        job = jobs[0]
        task, condition = get_task(job["task"]), job["condition"]
        record = verify_prefix(suite / task.key / "prefix", task, protocol)
        if condition == "solo":
            seconds = TOTAL_SECONDS
            configure_run(run, task, condition, protocol, seconds, repeat=job["repeat"])
        else:
            decision = prepare_decision(suite, task, record)
            seconds = remaining_seconds(record, decision)
            configure_run(run, task, condition, protocol, seconds, record["checkpoint_sha256"], job["repeat"])
            copy_checkpoint(suite / task.key / "prefix", run, task, record)
    if seconds <= 0:
        raise ValueError("No execution allowance")
    return phase_worker(run, condition, task, offline=protocol["offline_selftest"], execution_seconds=seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument("--verify-preflight", action="store_true")
    parser.add_argument("--suite-dir", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.verify_preflight:
        verify_preflight()
        return 0
    name = "luna_selector_offline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else VERSION
    suite = (args.suite_dir or RUNS / name).resolve()
    if suite.parent != RUNS.resolve() or not re.fullmatch(r"luna_selector_[A-Za-z0-9_]+", suite.name):
        raise ValueError("Invalid selector suite path")
    if args.worker:
        return worker(args.run_dir.resolve(strict=True), suite)
    return supervise(suite, args.offline_selftest)


if __name__ == "__main__":
    raise SystemExit(main())
