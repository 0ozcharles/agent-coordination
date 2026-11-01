"""Separate Pillow supplemental audit for the fixed-integrator comparison.

Default: print eight expected primary-delivery paths, without reading live jobs.
--verify: read-only validation after all 16 branches and their prescribed SS
lead diagnostics are terminal. --run-probes: additionally run the unchanged
18-case metadata probe, only on actual PRIMARY native delivered.patch records.
Diagnostic SS lead patches are never eligible replacements for primary output.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from audit_luna_timing_pillow import (ROOT, RUNS, TASK, TASKS, TIMINGS, IDENTITY,
    PROBE_SHA, PROBE_TEST_SHA, read, info, digest, identity, phase_path,
    verify_execution_hashes, check_probe_sources)

DEFAULT_SUITE = RUNS / "luna_integrator_v1"
CONDITIONS = ("strongshare", "integrator")


def expected_jobs(task=TASK):
    return [{"task": task, "timing": when, "repeat": repeat, "condition": condition}
            for when in TIMINGS for condition in CONDITIONS for repeat in (1, 2)]


def diagnostic_identity(row):
    return row["task"], row["timing"], row["repeat"]


def checklist(suite):
    return {"schema": "luna_integrator_pillow_probe_checklist_v1", "mode": "plan_only",
        "suite": str(suite), "model_api_calls": 0, "docker_calls": 0,
        "rows": [{**job, "phase_directory": str(phase_path(suite, job)),
                  "evaluation_summary": str(phase_path(suite, job) / "evaluation_summary.json"),
                  "patch_selector": "PRIMARY evaluated_delivery.delivered_patch only; null means no deliverable",
                  "primary_policy": "native merge/lead fallback" if job["condition"] == "strongshare" else "fixed agent1 single patch",
                  "probe_label": f"luna_integrator_{job['timing']}_{job['condition']}_r{job['repeat']}"}
                 for job in expected_jobs()],
        "execution_gate": "Explicit --run-probes only after parent confirms all 16 paid branches and the eight scheduled SS diagnostics ended; completed status/summary and native receipts are checked again.",
        "diagnostic_policy": "Read and protect diagnostic receipts separately; never probe, choose, or substitute diagnostic output as the SS primary delivery.",
        "interpretation": "Fixed 18 synthetic metadata cases (strip 6/default preservation 12), separate from official feature scores; not exhaustive Pillow coverage."}


def completed_suite(suite):
    protocol, summary, status = [read(suite / name) for name in ("protocol.json", "summary.json", "status.json")]
    expected = {identity(j) for task in TASKS for j in expected_jobs(task)}
    planned = [identity(j) for j in protocol["jobs"]]
    rows, skipped = summary.get("rows", []), summary.get("skipped_jobs", [])
    recorded = [identity(j) for j in rows + skipped]
    diagnostics = summary.get("strongshare_lead_only_diagnostics", [])
    expected_diagnostics = {diagnostic_identity(row) for row in rows if row["condition"] == "strongshare"}
    actual_diagnostics = [diagnostic_identity(row) for row in diagnostics]
    if (protocol.get("version") != "luna_integrator_v1" or protocol.get("offline_selftest") is not False
            or protocol.get("model") != "gpt-6-luna" or status.get("status") != "completed"
            or summary.get("pipeline_completed") is not True or summary.get("paid_execution_completed") is not True
            or len(planned) != 16 or set(planned) != expected or len(recorded) != 16 or set(recorded) != expected
            or status.get("completed_jobs") != len(rows) or status.get("skipped_jobs") != len(skipped)
            or status.get("planned_jobs") != 16 or summary.get("planned_jobs") != 16
            or summary.get("protocol_sha256") != digest(protocol)
            or len(actual_diagnostics) != len(expected_diagnostics) or set(actual_diagnostics) != expected_diagnostics
            or status.get("completed_diagnostics") != len(actual_diagnostics)
            or any(row.get("primary_outcome") is not False or not row.get("evaluation", {}).get("evaluation_completed")
                   for row in diagnostics)):
        raise ValueError("All 16 branches and the prescribed completed-SS diagnostics must be terminal and consistent")
    for relative, expected_hash in protocol["source_manifest"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid frozen source path")
        for path in (ROOT / relative, suite / "source_snapshot" / relative):
            if info(path)["sha256"] != expected_hash:
                raise ValueError("Frozen integrator source differs: " + relative)
    return protocol, summary


def load_primary(phase, condition):
    if condition == "strongshare":
        from luna_collaboration_evaluator import make_evaluator, resume_completed
    elif condition == "integrator":
        from luna_integrator_evaluator import make_evaluator, resume_completed
    else:
        raise ValueError("Unsupported primary condition")
    module = make_evaluator(condition)
    task, config = module.load_task(phase)
    score = resume_completed(module, phase, task, config)
    if score is None:
        raise ValueError("Primary evaluation is incomplete")
    return task, config, score


def verify_primary(suite, job, protocol, summary_row):
    phase = phase_path(suite, job)
    task, config, score = load_primary(phase, job["condition"])
    reference = protocol["checkpoint_references"][TASK][job["timing"]]
    submission_policy = "authoritative_agent1_only" if job["condition"] == "integrator" else "native_cooperative_merge"
    if (task.key != TASK or config.get("timing") != job["timing"] or config.get("repeat") != job["repeat"]
            or config.get("integrator_suite") != str(suite) or config.get("offline_selftest") is not False
            or config.get("experiment_version") != "luna_integrator_v1"
            or config.get("submission_policy") != submission_policy
            or config.get("condition_version") != protocol["condition_versions"][job["condition"]]
            or config.get("protocol_sha256") != digest(protocol)
            or config.get("checkpoint_sha256") != reference["record"]["checkpoint_sha256"]
            or config.get("checkpoint_sha256") != summary_row["checkpoint_sha256"]
            or digest(read(phase.parent / "checkpoint.json")) != config["checkpoint_sha256"]
            or summary_row.get("run_directory") != str(phase.parent)
            or score != summary_row["evaluation"] or score["both_passed"] != summary_row["official_success"]):
        raise ValueError("Primary branch differs from the declared checkpoint, policy or saved score")
    execution = verify_execution_hashes(phase)
    delivery = score["evaluated_delivery"]
    if job["condition"] == "integrator" and (score.get("primary_outcome") is not True
            or score.get("submission_policy") != "authoritative_agent1_only"
            or delivery.get("selection") != "predesignated_agent1" or delivery.get("authoritative_agent") != "agent1"):
        raise ValueError("Integrator primary delivery is not the fixed agent1 artifact")
    patch = delivery["delivered_patch"]
    if patch is not None and info(Path(patch["path"])) != patch:
        raise ValueError("Actual primary native delivery bytes changed")
    return {**job, "phase_directory": str(phase), "primary_outcome": True,
            "official_both_passed": score["both_passed"], "official_feature_results": score["feature_results"],
            "official_evaluation_summary": info(phase / "evaluation_summary.json"),
            "execution_receipt": execution, "delivery": delivery,
            "native_merge_status": score.get("merge_status"), "native_merge_strategy": score.get("merge_strategy"),
            "native_lead_fallback_used": score.get("lead_patch_fallback_used", False),
            "status": "ready" if patch is not None else "no_deliverable"}


def verify_diagnostics(suite, summary):
    from luna_integrator_evaluator import make_evaluator, resume_completed
    verified = []
    for saved in summary["strongshare_lead_only_diagnostics"]:
        job = {**{key: saved[key] for key in ("task", "timing", "repeat")}, "condition": "strongshare"}
        phase = phase_path(suite, job)
        if saved.get("run_directory") != str(phase.parent):
            raise ValueError("Lead diagnostic path differs from its SS branch")
        module = make_evaluator("strongshare", diagnostic=True)
        task, config = module.load_task(phase)
        score = resume_completed(module, phase, task, config)
        if (score is None or score != saved["evaluation"] or score.get("primary_outcome") is not False
                or score.get("submission_policy") != "diagnostic_agent1_only"):
            raise ValueError("Saved lead-only diagnostic differs from its independent native receipt")
        verified.append({**job, "primary_outcome": False, "used_for_probe": False,
                         "official_both_passed": score["both_passed"],
                         "summary": info(phase / "lead_only_diagnostic/evaluation_summary.json"),
                         "delivery_manifest": score["evaluated_delivery"]["manifest"]})
    return verified


def verified_inputs(suite):
    protocol, summary = completed_suite(suite)
    diagnostics = verify_diagnostics(suite, summary)
    by_job = {identity(row): row for row in summary["rows"]}
    skipped = {identity(row): row for row in summary["skipped_jobs"]}
    rows = []
    for job in expected_jobs():
        key = identity(job)
        if key in skipped:
            rows.append({**job, "status": "skipped", "reason": skipped[key]["reason"],
                         "phase_directory": str(phase_path(suite, job)), "delivery": None, "primary_outcome": True})
        else:
            rows.append(verify_primary(suite, job, protocol, by_job[key]))
    return {"schema": "luna_integrator_pillow_supplemental_audit_v1", "status": "inputs_verified",
            "suite": str(suite), "task": TASK, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "protocol": info(suite / "protocol.json"), "summary": info(suite / "summary.json"),
            "audit_sources": {name: info(ROOT / name) for name in ("audit_luna_integrator_pillow.py", "audit_luna_timing_pillow.py")},
            "model_api_calls": 0, "official_scores_modified": False, "production_sources_modified": False,
            "rows": rows, "lead_only_diagnostics_verified_but_not_probed": diagnostics}


def run_probes(suite):
    before = verified_inputs(suite)
    check_probe_sources()
    from pillow_metadata_probe_v1 import evaluate_patch, SUITE_ID
    for row in before["rows"]:
        if row["status"] != "ready":
            row["supplemental_status"] = row["status"]
            continue
        patch = row["delivery"]["delivered_patch"]
        result = evaluate_patch(Path(patch["path"]), label=f"luna_integrator_{row['timing']}_{row['condition']}_r{row['repeat']}")
        directory = Path(result["artifact_directory"])
        inputs = read(directory / "input.json")
        if (inputs["patch"] != patch["path"] or inputs["patch_sha256"] != patch["sha256"]
                or inputs["probe_sha256"] != PROBE_SHA or inputs["test_sha256"] != PROBE_TEST_SHA
                or info(Path(patch["path"])) != patch or not result.get("patch_unchanged")
                or not result.get("probe_unchanged") or result.get("model_calls") != 0):
            raise ValueError("Supplemental receipt differs from the primary native delivery")
        applied = not result.get("patch_apply_failed")
        row.update(supplemental_status="patch_apply_failed" if not applied else result["status"],
                   supplemental_passed=result.get("passed"), result=info(directory / "result.json"),
                   input=info(directory / "input.json"), tests_passed=result.get("tests_passed"),
                   tests_total=result.get("tests_total"), strip=result.get("groups", {}).get("strip"),
                   default_preservation=result.get("groups", {}).get("default_preservation"),
                   failed_cases=[case for case in result.get("checks", []) if not case["passed"]])
        if result["status"] == "completed" and applied:
            if (result["suite_id"] != SUITE_ID or result["tests_total"] != 18
                    or row["strip"]["total"] != 6 or row["default_preservation"]["total"] != 12
                    or len(result["checks"]) != 18 or result.get("container_removed") is not True):
                raise ValueError("Supplemental run is not a cleaned-up fixed 18-case suite")
    after = verified_inputs(suite)
    for previous, current in zip(before["rows"], after["rows"]):
        if any(previous[key] != value for key, value in current.items()):
            raise ValueError("Primary evidence changed during supplemental scoring")
        previous["official_receipts_unchanged_after_probe"] = True
    for key in ("protocol", "summary", "audit_sources", "lead_only_diagnostics_verified_but_not_probed"):
        if before[key] != after[key]:
            raise ValueError("Completed suite or excluded diagnostic evidence changed: " + key)
    check_probe_sources()
    before.update(status="completed" if all(row["supplemental_status"] in ("completed", "no_deliverable", "skipped")
                  for row in before["rows"]) else "probe_infrastructure_issue", probe_suite_id=SUITE_ID, probe_sha256=PROBE_SHA,
                  interpretation="Only primary native deliveries were probed. SS diagnostics were never substituted or selected by score. Eighteen cases do not cover all Pillow requirements; apply errors remain separate from functional failures.")
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verify", action="store_true")
    modes.add_argument("--run-probes", action="store_true")
    parser.add_argument("--output", type=Path, help="New audit JSON only, never overwritten")
    args = parser.parse_args()
    suite = args.suite.absolute()
    if suite.parent != RUNS or suite.name != "luna_integrator_v1" or suite.resolve() != suite:
        raise ValueError("Audit is fixed to the luna_integrator_v1 umbrella")
    if args.output is not None:
        target = args.output.absolute()
        if target.exists() or target.is_symlink():
            raise ValueError("Refusing to overwrite an existing artifact")
        if (target.resolve().parent not in (ROOT / "artifacts/pillow_metadata_probe_v1", suite)
                or not target.name.startswith("audit_") or target.suffix != ".json"):
            raise ValueError("Save a new audit_*.json beside probe results or in this umbrella suite")
    if args.run_probes and args.output is None:
        raise ValueError("--run-probes requires a new --output")
    result = run_probes(suite) if args.run_probes else verified_inputs(suite) if args.verify else checklist(suite)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        with args.output.open("x") as handle:
            handle.write(text)
        print(json.dumps({"status": result.get("status", result.get("mode")), "output": str(args.output), "model_api_calls": 0}))
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
