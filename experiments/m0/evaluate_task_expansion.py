"""Run pinned official CooperBench evaluation for a saved expansion submission.

Usage:
    python evaluate_task_expansion.py --run-dir \
        artifacts/expansion_runs/<suite>/<task>/jobs/<job>/<condition>

Keep/StrongShare retain native two-patch merge and lead fallback semantics.
Consolidate uses test_solo only as the single-patch scoring adapter. Completed
benchmark failures exit 0; invalid inputs, infrastructure and timeout errors
exit 2. No model, credential, patch repair, or reference solution is used.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from evaluate_solo_smoke import (
    COMMIT, DEADLINE_SECONDS, EXEC_TIMEOUT_SECONDS, REPO, ROOT,
    cleanup, evaluator_environment, make_backend, stop_process, write_json,
)
from task_specs import get_task

RUNS = ROOT / "artifacts/expansion_runs"
CONDITIONS = ("keep", "strongshare", "consolidate")


def ordinary_file(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.resolve() != path:
        raise ValueError("An ordinary saved file is required: " + str(path))
    return path


def file_info(path):
    path = ordinary_file(path)
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def validate_run_dir(value):
    original = Path(value).expanduser().absolute()
    path = original.resolve(strict=True)
    try:
        parts = path.relative_to(RUNS.resolve()).parts
    except ValueError:
        parts = ()
    if (len(parts) != 5 or parts[2] != "jobs" or parts[4] not in CONDITIONS
            or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts)
            or not path.is_dir() or original != path):
        raise ValueError("--run-dir must be artifacts/expansion_runs/<suite>/<task>/jobs/<job>/<condition>")
    return path


def load_task(run_dir):
    path = ordinary_file(run_dir.parent / "task_config.json")
    config = json.loads(path.read_text())
    task = get_task(config.get("task_key"))
    if config.get("task") != task.as_dict():
        raise ValueError("Saved task specification differs from the fixed task registry")
    if run_dir.parent.parent.parent.name != task.key:
        raise ValueError("Task directory does not match task_config.json")
    if type(config.get("offline_selftest")) is not bool:
        raise ValueError("task_config.json must declare offline_selftest")
    if not re.fullmatch(r"[0-9a-f]{64}", str(config.get("protocol_sha256", ""))):
        raise ValueError("task_config.json requires the frozen protocol hash")
    status_path = ordinary_file(run_dir / "status.json")
    status = json.loads(status_path.read_text())
    if status.get("status") != "completed":
        raise ValueError("Only completed condition artifacts may be evaluated")
    if status.get("task") is not None and status["task"] != task.as_dict():
        raise ValueError("Completed condition task differs from task_config.json")
    if status.get("offline_selftest") != config["offline_selftest"]:
        raise ValueError("Condition and task_config.json disagree on offline_selftest")
    return task, config


def patch_info(run_dir, task):
    names = ["final.patch"] if run_dir.name == "consolidate" else [f"agent{f}.patch" for f in task.features]
    patches = {}
    for name in names:
        info = file_info(run_dir / name)
        info["empty"] = not Path(info["path"]).read_bytes().strip()
        patches[name] = info
    return patches


def source_manifest():
    paths = [Path(__file__).resolve(), ROOT / "evaluate_solo_smoke.py", ROOT / "task_specs.py"]
    paths.extend(sorted((REPO / "src/cooperbench/eval").rglob("*.py")))
    return {str(p.relative_to(ROOT)): file_info(p)["sha256"] for p in paths}


def input_manifest(run_dir, task, patches):
    paths = [run_dir.parent / "task_config.json", run_dir / "status.json", task.image_record_path]
    # These are provenance records only; never use prior outcomes to choose code.
    for name in ("source_manifest.json", "restore_proof.json", "execution_receipt.json", "intervention.json"):
        path = run_dir / name
        if path.exists() or path.is_symlink():
            paths.append(path)
    for name in ("checkpoint.json", "checkpoint_provenance.json"):
        path = run_dir.parent / name
        if path.exists() or path.is_symlink():
            paths.append(path)
    return {
        "patches": patches,
        "protected_files": {str(p): file_info(p) for p in paths},
        "evaluator_sources": source_manifest(),
    }


def evaluate_saved_patches(evaluation_module, run_dir, task, patches):
    kwargs = dict(repo_name=task.repo, task_id=task.task_id,
                  feature1_id=task.features[0], feature2_id=task.features[1],
                  timeout=EXEC_TIMEOUT_SECONDS, backend="docker", dataset_dir=REPO / "dataset")
    if run_dir.name == "consolidate":
        return evaluation_module.test_solo(patch=Path(patches["final.patch"]["path"]), **kwargs)
    return evaluation_module.test_merged(
        patch1=Path(patches[f"agent{task.features[0]}.patch"]["path"]),
        patch2=Path(patches[f"agent{task.features[1]}.patch"]["path"]), **kwargs)


def verify_upstream(task):
    head = subprocess.check_output(
        ["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        raise RuntimeError("CooperBench commit differs from the verified environment")
    paths = ["src/cooperbench/eval", str(task.task_dir.relative_to(REPO))]
    subprocess.run(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--", *paths],
                   check=True, capture_output=True)
    return head


def worker(run_dir, attempt_dir):
    # CooperBench imports load_dotenv(); sanitize before importing that package.
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    import docker
    import cooperbench.eval.backends.docker as backend_module
    import cooperbench.eval.sandbox as evaluation_module

    task, config = load_task(run_dir)
    patches = patch_info(run_dir, task)
    before = input_manifest(run_dir, task, patches)
    expected = json.loads(ordinary_file(task.image_record_path).read_text())
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(expected.get("Id", ""))):
        raise ValueError("Verified task image record requires an immutable Docker image ID")
    client = docker.from_env(timeout=EXEC_TIMEOUT_SECONDS)
    previous_backend = evaluation_module.get_backend
    try:
        actual = client.images.get(task.image)
        if actual.id != expected["Id"] or actual.attrs["Architecture"] != expected["Architecture"]:
            raise RuntimeError("Task image differs from the verified reference image")
        head = verify_upstream(task)
        solo = run_dir.name == "consolidate"
        write_json(attempt_dir / "evaluation_input.json", {
            "condition": run_dir.name, "task_key": task.key, "task": task.as_dict(),
            "offline_selftest": config["offline_selftest"], "protocol_sha256": config["protocol_sha256"],
            "input_manifest": before, "image_id": expected["Id"], "cooperbench_commit": head,
            "features": list(task.features), "native_setting": "solo" if solo else "coop",
            "official_evaluator": "cooperbench.eval.sandbox." + ("test_solo" if solo else "test_merged"),
            "single_patch_scoring_adapter": solo,
            "ordered_patch_mapping": {agent: f"agent{f}.patch" for agent, f in task.agents.items()} if not solo else {"agent1": "final.patch"},
            "fallback_eligible_agent": None if solo else "agent1",
            "model_calls": 0, "credentials_inherited": False,
            "saved_patches_modified_by_evaluator": False, "reference_patch_substitution": False,
            "upstream_test_file_filter": True,
            "merge_policy": "not_applicable" if solo else "pinned upstream naive merge with native lead-only fallback",
        })
        backend = make_backend(backend_module, client, expected["Id"], run_dir, attempt_dir,
                               expected_image=task.image)

        def get_backend(name="docker"):
            if name != "docker":
                raise ValueError("Only the local Docker evaluator is permitted")
            return backend

        evaluation_module.get_backend = get_backend
        official = evaluate_saved_patches(evaluation_module, run_dir, task, patches)
        write_json(attempt_dir / "official_evaluation.json", official)
        result = copy.deepcopy(official)
        after = input_manifest(run_dir, task, patch_info(run_dir, task))
        integrity_ok = before == after
        try:
            verify_upstream(task)
        except Exception:
            integrity_ok = False
        write_json(attempt_dir / "integrity.json", {
            "unchanged": integrity_ok, "before": before, "after": after,
        })
        if not integrity_ok:
            result["error"] = "Evaluation inputs or scoring sources changed during evaluation"
            result["both_passed"] = False
        write_json(attempt_dir / "evaluation.json", result)
        return 2 if result.get("error") else 0
    finally:
        evaluation_module.get_backend = previous_backend
        client.close()


def summarize(result, status, run_dir, attempt_dir, elapsed, task=None, config=None):
    merge = result.get("merge") or {}
    solo = run_dir.name == "consolidate"
    features = list(task.features) if task is not None else []
    raw_path = attempt_dir / "official_evaluation.json"
    feature_results = {}
    for number, feature in enumerate(features, 1):
        native = result.get(f"feature{number}") or {}
        feature_results[str(feature)] = {"passed": native.get("passed"),
            "tests_passed": native.get("tests_passed"), "tests_failed": native.get("tests_failed"),
            "exit_code": native.get("exit_code")}
    summary = {
        "condition": run_dir.name, "task_key": task.key if task else None,
        "task": task.as_dict() if task else None, "features": features,
        "offline_selftest": (config or {}).get("offline_selftest"),
        "protocol_sha256": (config or {}).get("protocol_sha256"),
        "status": status, "evaluation_completed": status in ("passed", "failed"),
        "both_passed": bool(result.get("both_passed", False)), "feature_results": feature_results,
        "native_setting": "solo" if solo else "coop", "single_patch_scoring_adapter": solo,
        "apply_status": result.get("apply_status"),
        "merge_status": "not_applicable" if solo else merge.get("status"),
        "merge_strategy": "not_applicable" if solo else merge.get("strategy"),
        "lead_patch_fallback_used": not solo and merge.get("strategy") == "solo-agent1",
        "error": result.get("error"), "cleanup_error": result.get("cleanup_error"),
        "elapsed_seconds": round(elapsed, 3), "watchdog_seconds": DEADLINE_SECONDS,
        "model_calls": 0, "attempt_directory": str(attempt_dir),
        "official_raw_result": str(raw_path) if raw_path.is_file() else None,
        "full_evaluator_result": str(attempt_dir / "evaluation.json"),
        "incremental_exec_output": str(attempt_dir / "exec_logs"),
        "input_manifest": str(attempt_dir / "evaluation_input.json"),
        "integrity_check": str(attempt_dir / "integrity.json"),
        "interpretation": (
            "Official feature scores are unchanged. Consolidate's solo setting is a single-patch "
            "scoring adapter, not a fresh solo run. Cooperative conditions retain native merge "
            "and lead-only fallback rules. No model-assisted repair or reference patch substitution."
        ),
    }
    for feature in features:
        summary[f"feature_{feature}_passed"] = feature_results[str(feature)]["passed"]
    return summary


def supervise(run_dir):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    attempt_dir = run_dir / "evaluation_attempts" / stamp
    attempt_dir.mkdir(parents=True)
    started = time.monotonic()
    env = evaluator_environment()
    result, proc, task, config = None, None, None, None
    code, status = 2, "error"
    try:
        task, config = load_task(run_dir)
        patch_info(run_dir, task)
        with (attempt_dir / "console.log").open("wb") as log:
            proc = subprocess.Popen([
                sys.executable, "-u", str(Path(__file__).resolve()), "--run-dir", str(run_dir),
                "--worker-attempt", str(attempt_dir),
            ], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=DEADLINE_SECONDS)
            except subprocess.TimeoutExpired:
                status = "timeout"
                result = {"error": f"Evaluation exceeded {DEADLINE_SECONDS} seconds", "both_passed": False}
            finally:
                stop_process(proc)
        if result is None:
            saved = attempt_dir / "evaluation.json"
            result = json.loads(saved.read_text()) if saved.is_file() else {
                "error": f"Evaluator worker exited {code} without a result; see console.log", "both_passed": False}
            status = "error" if code or result.get("error") else ("passed" if result.get("both_passed") else "failed")
    except BaseException as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "both_passed": False}
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "error"
    finally:
        if proc is not None:
            stop_process(proc)
            try:
                cleanup(run_dir, attempt_dir, env)
            except Exception as exc:
                write_json(attempt_dir / "cleanup.json", {"error_type": type(exc).__name__, "error": str(exc)})
                result = result or {"both_passed": False}
                result["cleanup_error"] = str(exc)
                status = "error"
        result = result or {"error": "Evaluation did not produce a result", "both_passed": False}
        write_json(attempt_dir / "evaluation.json", result)
        summary = summarize(result, status, run_dir, attempt_dir, time.monotonic() - started, task, config)
        write_json(attempt_dir / "evaluation_summary.json", summary)
        write_json(run_dir / "evaluation.json", result)
        write_json(run_dir / "evaluation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status in ("passed", "failed") else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--worker-attempt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        run_dir = validate_run_dir(args.run_dir)
        if args.worker_attempt:
            original = Path(args.worker_attempt).absolute()
            attempt_dir = original.resolve(strict=True)
            if (attempt_dir.parent != run_dir / "evaluation_attempts" or original != attempt_dir
                    or not attempt_dir.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]+", attempt_dir.name)):
                raise ValueError("Invalid evaluator attempt directory")
            return worker(run_dir, attempt_dir)
        return supervise(run_dir)
    except Exception as exc:
        print(json.dumps({"status": "error", "evaluation_completed": False,
                          "error": f"{type(exc).__name__}: {exc}", "model_calls": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
