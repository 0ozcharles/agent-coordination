"""Audit the eight Pillow timing deliveries, then optionally run the frozen probe.

Default mode prints an expected-path checklist without reading live results.
--verify reads completed artifacts only. --run-probes additionally runs the
unchanged evaluator-only 18-case probe sequentially, after the whole paid suite
is complete. Neither mode writes into production inputs or alters official scores.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "artifacts/handoff_expansion_runs"
DEFAULT_SUITE = RUNS / "luna_timing_v1"
TASK = "pillow68_f1_f2"
TASKS = ("llama_index17070_f1_f3", TASK)
TIMINGS = ("early", "late")
CONDITIONS = ("strongshare", "consolidate")
IDENTITY = ("task", "timing", "repeat", "condition")
PROBE_SHA = "1b054e839fd86cbe48b5fe878b2d6eec3a0ffee04f3064e94ed62f9eb0740bcf"
PROBE_TEST_SHA = "5a629614ff8435a1831a410ce6c151b7bbd6a962899f708c74328042bec51e13"


def read(path):
    return json.loads(ordinary(path).read_text())


def ordinary(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise ValueError("Expected an ordinary, unlinked saved file: " + str(path))
    return path


def info(path):
    path = ordinary(path)
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identity(value):
    return tuple(value[key] for key in IDENTITY)


def expected_jobs(task=TASK):
    return [{"task": task, "timing": timing, "repeat": repeat, "condition": condition}
            for timing in TIMINGS for condition in CONDITIONS for repeat in (1, 2)]


def phase_path(suite, job):
    name = f"{job['task']}_{job['timing']}_r{job['repeat']:02d}_{job['condition']}"
    return (suite.with_name(suite.name + "_" + job["timing"]) / job["task"]
            / "jobs" / name / job["condition"])


def checklist(suite):
    return {"schema": "luna_timing_pillow_probe_checklist_v1", "mode": "plan_only",
            "suite": str(suite), "model_api_calls": 0, "docker_calls": 0,
            "rows": [{**job, "phase_directory": str(phase_path(suite, job)),
                      "evaluation_summary": str(phase_path(suite, job) / "evaluation_summary.json"),
                      "patch_selector": "evaluated_delivery.delivered_patch (null means no deliverable)",
                      "probe_label": f"luna_timing_{job['timing']}_{job['condition']}_r{job['repeat']}"}
                     for job in expected_jobs()],
            "execution_gate": "Explicit --run-probes after parent confirms paid runs ended, plus a completed umbrella status/summary covering all 16 planned branches.",
            "interpretation": "The unchanged 18 cases cover strip_metadata (6) and default preservation (12); they do not replace official tests or prove every Pillow requirement."}


def completed_suite(suite):
    protocol, summary, status = [read(suite / name) for name in ("protocol.json", "summary.json", "status.json")]
    expected = {identity(j) for task in TASKS for j in expected_jobs(task)}
    planned = [identity(j) for j in protocol["jobs"]]
    rows, skipped = summary.get("rows", []), summary.get("skipped_jobs", [])
    recorded = [identity(j) for j in rows + skipped]
    if (protocol.get("offline_selftest") is not False or protocol.get("model") != "gpt-6-luna"
            or status.get("status") != "completed" or summary.get("pipeline_completed") is not True
            or len(planned) != 16 or set(planned) != expected
            or len(recorded) != 16 or set(recorded) != expected
            or status.get("completed_jobs") != len(rows) or status.get("skipped_jobs") != len(skipped)
            or status.get("planned_jobs") != 16 or summary.get("planned_jobs") != 16
            or summary.get("protocol_sha256") != digest(protocol)):
        raise ValueError("All 16 paid branches must be terminal and match the frozen timing protocol")
    for relative, expected_hash in protocol["source_manifest"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid frozen source path")
        for path in (ROOT / relative, suite / "source_snapshot" / relative):
            if info(path)["sha256"] != expected_hash:
                raise ValueError("Frozen timing source differs: " + relative)
    return protocol, summary


def verify_execution_hashes(phase):
    receipt = read(phase / "execution_receipt.json")
    protected = receipt.get("protected_files_sha256")
    required = {"task_config.json", "checkpoint.json", "checkpoint_provenance.json", "shared_git.tar",
                str(Path(phase.name) / "handoff_packet.json"), str(Path(phase.name) / "status.json")}
    if not isinstance(protected, dict) or not required.issubset(protected):
        raise ValueError("Execution receipt omits required task/checkpoint/handoff evidence")
    for relative, expected_hash in protected.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid execution receipt path")
        if info(phase.parent / relative)["sha256"] != expected_hash:
            raise ValueError("Execution evidence changed: " + relative)
    for name, expected_hash in receipt["patch_sha256"].items():
        if Path(name).name != name or info(phase / name)["sha256"] != expected_hash:
            raise ValueError("Submitted patch differs from its execution receipt")
    return info(phase / "execution_receipt.json")


def verify_delivery(suite, job, protocol, summary_row):
    # Import only for read-only cache validation: this never invokes worker/supervise.
    from luna_collaboration_evaluator import make_evaluator, resume_completed
    phase = phase_path(suite, job)
    module = make_evaluator(job["condition"])
    task, config = module.load_task(phase)
    if (task.key != TASK or config.get("timing") != job["timing"]
            or config.get("timing_suite") != str(suite) or config.get("offline_selftest") is not False
            or config.get("protocol_sha256") != digest(protocol)
            or config.get("early_checkpoint_sha256") != protocol["early_references"][TASK]["record"]["checkpoint_sha256"]
            or config.get("checkpoint_sha256") != summary_row["checkpoint_sha256"]
            or digest(read(phase.parent / "checkpoint.json")) != config["checkpoint_sha256"]
            or summary_row.get("run_directory") != str(phase.parent)):
        raise ValueError("Branch does not describe the expected timing checkpoint")
    execution = verify_execution_hashes(phase)
    score = resume_completed(module, phase, task, config)
    if (score is None or score != summary_row["evaluation"]
            or score["both_passed"] != summary_row["official_success"]):
        raise ValueError("Official score differs from the completed suite row")
    delivery = score["evaluated_delivery"]
    patch = delivery["delivered_patch"]
    if patch is not None and info(Path(patch["path"])) != patch:
        raise ValueError("Selected native delivery bytes changed")
    return {**job, "phase_directory": str(phase),
            "official_both_passed": score["both_passed"], "official_feature_results": score["feature_results"],
            "official_evaluation_summary": info(phase / "evaluation_summary.json"),
            "execution_receipt": execution, "delivery": delivery,
            "status": "ready" if patch is not None else "no_deliverable"}


def verified_inputs(suite):
    protocol, summary = completed_suite(suite)
    by_job = {identity(row): row for row in summary["rows"]}
    skipped = {identity(row): row for row in summary["skipped_jobs"]}
    rows = []
    for job in expected_jobs():
        key = identity(job)
        if key in skipped:
            rows.append({**job, "status": "skipped", "reason": skipped[key]["reason"],
                         "phase_directory": str(phase_path(suite, job)), "delivery": None})
        else:
            rows.append(verify_delivery(suite, job, protocol, by_job[key]))
    return {"schema": "luna_timing_pillow_supplemental_audit_v1", "status": "inputs_verified",
            "suite": str(suite), "task": TASK, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "protocol": info(suite / "protocol.json"), "summary": info(suite / "summary.json"),
            "model_api_calls": 0, "official_scores_modified": False, "production_sources_modified": False,
            "rows": rows}


def check_probe_sources():
    for name, expected in (("pillow_metadata_probe_v1.py", PROBE_SHA),
                           ("test_pillow_metadata_probe_v1.py", PROBE_TEST_SHA)):
        if info(ROOT / name)["sha256"] != expected:
            raise ValueError("Fixed metadata probe source changed: " + name)


def run_probes(suite):
    before = verified_inputs(suite)  # Validate every saved delivery before any Docker work.
    check_probe_sources()
    from pillow_metadata_probe_v1 import evaluate_patch, SUITE_ID
    for row in before["rows"]:
        if row["status"] != "ready":
            row["supplemental_status"] = row["status"]
            continue
        patch = row["delivery"]["delivered_patch"]
        label = f"luna_timing_{row['timing']}_{row['condition']}_r{row['repeat']}"
        result = evaluate_patch(Path(patch["path"]), label=label)
        directory = Path(result["artifact_directory"])
        inputs = read(directory / "input.json")
        if (inputs["patch"] != patch["path"] or inputs["patch_sha256"] != patch["sha256"]
                or inputs["probe_sha256"] != PROBE_SHA or inputs["test_sha256"] != PROBE_TEST_SHA
                or info(Path(patch["path"])) != patch or not result.get("patch_unchanged")
                or not result.get("probe_unchanged") or result.get("model_calls") != 0):
            raise ValueError("Supplemental probe receipt does not match the native delivery")
        applied = not result.get("patch_apply_failed")
        row.update(supplemental_status=("patch_apply_failed" if not applied else result["status"]),
                   supplemental_passed=result.get("passed"), result=info(directory / "result.json"),
                   input=info(directory / "input.json"), tests_passed=result.get("tests_passed"),
                   tests_total=result.get("tests_total"), strip=result.get("groups", {}).get("strip"),
                   default_preservation=result.get("groups", {}).get("default_preservation"),
                   failed_cases=[case for case in result.get("checks", []) if not case["passed"]])
        if result["status"] == "completed" and applied:
            if (result["suite_id"] != SUITE_ID or result["tests_total"] != 18
                    or row["strip"]["total"] != 6 or row["default_preservation"]["total"] != 12
                    or len(result["checks"]) != 18):
                raise ValueError("Supplemental result is not the fixed 18-case suite")
    after = verified_inputs(suite)
    for previous, current in zip(before["rows"], after["rows"]):
        if any(previous[key] != value for key, value in current.items()):
            raise ValueError("Official evidence changed during supplemental scoring")
        previous["official_receipts_unchanged_after_probe"] = True
    if before["protocol"] != after["protocol"] or before["summary"] != after["summary"]:
        raise ValueError("Completed timing suite changed during supplemental scoring")
    check_probe_sources()
    before.update(status="completed" if all(r["supplemental_status"] in
                  ("completed", "no_deliverable", "skipped") for r in before["rows"]) else "probe_infrastructure_issue",
                  probe_suite_id=SUITE_ID, probe_sha256=PROBE_SHA,
                  interpretation="Supplemental cases remain separate from official scores; patch-application errors are not metadata test failures. Eighteen passing cases are not proof of all formats or feature1 tuple handling.")
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verify", action="store_true")
    modes.add_argument("--run-probes", action="store_true")
    parser.add_argument("--output", type=Path, help="New derived JSON only; existing files are never overwritten")
    args = parser.parse_args()
    suite = args.suite.absolute()
    if suite.parent != RUNS or suite.name != "luna_timing_v1" or suite.resolve() != suite:
        raise ValueError("This audit is fixed to the luna_timing_v1 umbrella")
    if args.output is not None:
        output = args.output.absolute()
        if output.exists() or output.is_symlink():
            raise ValueError("Refusing to overwrite an existing artifact")
        if output.resolve().parent not in (ROOT / "artifacts/pillow_metadata_probe_v1", suite):
            raise ValueError("Save derived audit JSON beside probe results or in the umbrella suite")
        if not output.name.startswith("audit_") or output.suffix != ".json":
            raise ValueError("Derived outputs must use a new audit_*.json name")
    if args.run_probes and args.output is None:
        raise ValueError("--run-probes requires a new --output audit JSON")
    result = run_probes(suite) if args.run_probes else verified_inputs(suite) if args.verify else checklist(suite)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        with args.output.open("x") as stream:
            stream.write(text)
        print(json.dumps({"status": result.get("status", result.get("mode")), "output": str(args.output), "model_api_calls": 0}))
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
