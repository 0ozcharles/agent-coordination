"""Validate the fixed selector tasks without any model calls.

Selection is checked before evaluator-only reference reads. Reuses the pinned
reference evaluator unchanged; never creates agent inputs or repairs solutions.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from evaluate_solo_smoke import evaluator_environment, write_json
from preflight_task_candidates import evaluate_task
from selector_task_specs import COMMIT, REPO, ROOT, TASKS

SELECTION = ROOT / "task_selector_selection.json"
PROOF = ROOT / "artifacts/selector_reference_validation.json"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_selection():
    selection = json.loads(SELECTION.read_text())
    if (selection["cooperbench_commit"] != COMMIT
            or selection["status"] != "selection_fixed_before_reference_checks"
            or selection["reference_or_hidden_test_contents_read_before_selection"] is not False
            or selection["historical_scores_used_for_selection"] is not False
            or selection["task_specs"]["sha256"] != sha256(ROOT / "selector_task_specs.py")):
        raise ValueError("Fixed task selection or metadata source differs")
    selected = {row["key"]: row for row in selection["selected_tasks"]}
    if set(selected) != set(TASKS):
        raise ValueError("Selected task identities differ")
    for key, task in TASKS.items():
        row = selected[key]
        if any(row[field] != value for field, value in task.as_dict().items()):
            raise ValueError("Task spec differs from selection: " + key)
        evidence = [row["dockerfile"], row["setup"], row["runner"], *row["feature_evidence"]]
        for record in evidence:
            relative = Path(record["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid requirement evidence path")
            if sha256(ROOT / relative) != record["sha256"]:
                raise ValueError("Public requirements changed: " + str(relative))
    actual = subprocess.check_output(
        ["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise ValueError("Unexpected CooperBench commit")
    subprocess.run(
        ["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
         "src/cooperbench/eval", "src/cooperbench/utils.py",
         *(str(task.task_dir.relative_to(REPO)) for task in TASKS.values())],
        check=True, capture_output=True)
    return selection


def task_receipt(key, result, directory):
    task = TASKS[key]
    features = result.get("features_results", [])
    complete = len(features) == 2 and {r["feature"] for r in features} == set(task.features)

    def satisfied(check, expected):
        return complete and all(
            row.get("checks", {}).get(check, {}).get("passed") is expected
            and (expected or (row["checks"][check].get("tests_failed") or 0) > 0)
            for row in features)

    base_failed = satisfied("base", False)
    individual_passed = satisfied("individual_reference", True)
    combined_passed = satisfied("combined_reference", True)
    image_path = directory / "task_image.json"
    image_record = None
    if image_path.exists():
        image_record = json.loads(image_path.read_text())
        target = task.image_record_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if json.loads(target.read_text()) != image_record:
                raise ValueError("Refusing to replace a different pinned image: " + key)
        else:
            write_json(target, image_record)
    return {
        "task": task.as_dict(),
        "base_fails_both": base_failed,
        "individual_gold_passes_both": individual_passed,
        "combined_gold_passes_both": combined_passed,
        "mandatory_checks_passed": base_failed and individual_passed,
        "passed": base_failed and individual_passed and combined_passed,
        "image_id": result.get("image_id") or (image_record or {}).get("Id"),
        "image_record_path": str(task.image_record_path) if image_record else None,
        "image_record_sha256": sha256(task.image_record_path) if image_record else None,
        "reference_directory": str(directory),
        "result_path": str(directory / "result.json") if (directory / "result.json").exists() else None,
        "reference_feature_results": features,
        "status": result["status"],
        "error": result.get("error"),
        "interpretation": "Individual references are tested only against their own feature tests. The supplied combined reference is tested separately; individual gold patches are never merged or repaired.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    selection = validate_selection()
    if args.validate_only:
        print(json.dumps({"status": "fixed_selection_verified", "tasks": list(TASKS),
                          "selection_sha256": sha256(SELECTION), "model_api_calls": 0,
                          "docker_calls": 0}))
        return 0
    if PROOF.exists():
        raise ValueError("Refusing to replace an existing selector validation proof")
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = ROOT / "artifacts/selector_reference_checks" / stamp
    output.mkdir(parents=True)
    selection_hash = sha256(SELECTION)
    sources = {name: sha256(ROOT / name) for name in (
        "preflight_selector_tasks.py", "selector_task_specs.py", "preflight_task_candidates.py",
        "evaluate_solo_smoke.py", "task_specs.py")}
    proof = {
        "schema": "selector_reference_validation_v1", "status": "running",
        "passed": False, "mandatory_checks_passed": False,
        "cooperbench_commit": COMMIT, "selection_path": str(SELECTION),
        "selection_sha256": selection_hash, "selection_was_fixed_before_reference_checks": True,
        "source_sha256": sources, "output": str(output), "tasks": {},
        "model_api_calls": 0, "reference_data_exposed_to_agents": False,
        "task_reselection_based_on_results": False,
    }
    write_json(output / "selection_snapshot.json", selection)
    print(json.dumps({"stage": "reference_validation_started", "output": str(output)}), flush=True)
    for key, task in TASKS.items():
        config = {"repo": task.repo, "task_id": task.task_id, "features": list(task.features),
                  "image": task.image, "base_commit": task.base}
        try:
            result = evaluate_task(key, config, output, clean)
        except Exception as exc:
            result = {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}
            print(json.dumps({"task": key, **result}), flush=True)
            previous = output / key / "result.json"
            if previous.exists():
                saved = json.loads(previous.read_text())
                result = {**saved, **result}
        proof["tasks"][key] = task_receipt(key, result, output / key)
        write_json(output / "summary.json", proof)
    validate_selection()
    if sha256(SELECTION) != selection_hash or any(sha256(ROOT / name) != value for name, value in sources.items()):
        raise ValueError("Selection or evaluator sources changed during validation")
    proof["mandatory_checks_passed"] = all(r["mandatory_checks_passed"] for r in proof["tasks"].values())
    proof["passed"] = all(r["passed"] for r in proof["tasks"].values())
    proof["status"] = "completed" if all(r["status"] in ("passed", "failed") for r in proof["tasks"].values()) else "environment_error"
    proof["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "summary.json", proof)
    write_json(PROOF, proof)
    print(json.dumps({"status": proof["status"], "passed": proof["passed"],
                      "mandatory_checks_passed": proof["mandatory_checks_passed"],
                      "proof": str(PROOF), "model_api_calls": 0}), flush=True)
    return 0 if proof["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
