"""Verify the fixed three task images and native scoring, without model calls.

Reference patches are used only inside disposable evaluator containers. The
output is an environment/scoring proof; references never enter agent inputs.
Task selection must already be recorded before this program is started.
"""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cross_task_specs import COMMIT, REPO, ROOT, TASKS
from evaluate_solo_smoke import evaluator_environment, write_json
from preflight_task_candidates import evaluate_task, sha256


PROOF = ROOT / "artifacts/cross_task_reference_validation.json"
SELECTION = ROOT / "task_handoff_expansion_selection.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=list(TASKS))
    args = parser.parse_args()
    if not SELECTION.is_file():
        raise RuntimeError("Record task selection before environment/reference checks")
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    head = subprocess.check_output(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        raise RuntimeError("Pinned CooperBench commit differs")
    task_paths = [str(t.task_dir.relative_to(REPO)) for t in TASKS.values()]
    subprocess.run(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                    "src/cooperbench/eval", "src/cooperbench/utils.py", *task_paths],
                   check=True, capture_output=True)
    output = ROOT / "artifacts/cross_task_preflight" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True)
    source_hashes = {name: sha256(ROOT / name) for name in (
        "cross_task_specs.py", "preflight_cross_tasks.py", "preflight_task_candidates.py",
        "evaluate_solo_smoke.py", "task_handoff_expansion_selection.json")}
    summary = {"cooperbench_commit": COMMIT, "source_hashes": source_hashes,
               "model_api_calls": 0, "reference_data_exposed_to_agents": False,
               "results": {}, "output": str(output)}
    print(str(output), flush=True)
    for key, task in TASKS.items():
        if args.only and key != args.only:
            continue
        config = {"repo": task.repo, "task_id": task.task_id, "features": list(task.features),
                  "image": task.image, "base_commit": task.base}
        try:
            result = evaluate_task(key, config, output, clean)
            if result["status"] == "passed":
                image = json.loads((output / key / "task_image.json").read_text())
                task.image_record_path.parent.mkdir(parents=True, exist_ok=True)
                if task.image_record_path.exists() and json.loads(task.image_record_path.read_text()) != image:
                    raise RuntimeError("Existing immutable task image record differs")
                write_json(task.image_record_path, image)
            summary["results"][key] = result
        except Exception as exc:
            summary["results"][key] = {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}
            print(json.dumps({"task": key, **summary["results"][key]}), flush=True)
        write_json(output / "summary.json", summary)
    tasks = {}
    for key, result in summary["results"].items():
        features = result.get("features_results", [])
        tasks[key] = {
            "task": TASKS[key].as_dict(), "image_id": result.get("image_id"),
            "passed": result.get("status") == "passed", "details": str(output / key / "result.json"),
            "base_fails_both": len(features) == 2 and all(not f["checks"]["base"]["passed"] and f["checks"]["base"]["tests_failed"] > 0 for f in features),
            "individual_gold_passes_both": len(features) == 2 and all(f["checks"]["individual_reference"]["passed"] for f in features),
            "combined_gold_passes_both": len(features) == 2 and all(f["checks"]["combined_reference"]["passed"] for f in features),
            "source_hashes": {str(TASKS[key].task_dir.relative_to(ROOT) / f"feature{f['feature']}" / name): sha256(TASKS[key].task_dir / f"feature{f['feature']}" / name)
                              for f in features for name in ("feature.md", "tests.patch", "feature.patch")},
        }
    passed = len(tasks) == len(TASKS) and all(t["passed"] for t in tasks.values())
    proof = {"passed": passed, "cooperbench_commit": COMMIT, "model_api_calls": 0,
             "reference_data_exposed_to_agents": False, "source_hashes": source_hashes,
             "tasks": tasks, "output": str(output)}
    write_json(output / "proof.json", proof)
    if not args.only:
        write_json(PROOF, proof)
    print(json.dumps({"passed": passed, "proof": str(output / "proof.json"), "model_api_calls": 0}), flush=True)
    return 0 if passed or (args.only and all(t["passed"] for t in tasks.values())) else 2


if __name__ == "__main__":
    raise SystemExit(main())
