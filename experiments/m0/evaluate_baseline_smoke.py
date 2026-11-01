"""Score Keep or StrongShare saved patches with the pinned CooperBench evaluator.

Use the CooperBench virtual environment's Python:
    python evaluate_baseline_smoke.py --run-dir \
        artifacts/baseline_runs/baseline_smoke_<id>/keep

The condition directory must contain agent1.patch and agent6.patch. Existing empty
patches are valid submissions; missing files are an input error. Exit 0 means an
evaluation completed (including failed tests or merge conflicts), not necessarily
that the benchmark passed. Read evaluation_summary.json for the score.

This wrapper makes no model calls. It reuses the solo wrapper's clean environment,
image pinning, recording Docker backend, watchdog cleanup, and atomic JSON writes.
It leaves CooperBench's merge, lead-only fallback, and scoring rules unchanged.
"""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from evaluate_solo_smoke import (
    COMMIT, DEADLINE_SECONDS, EXEC_TIMEOUT_SECONDS, IMAGE, REPO, ROOT,
    cleanup, evaluator_environment, make_backend, stop_process, write_json,
)

RUNS = ROOT / "artifacts/baseline_runs"
CONDITIONS = ("keep", "strongshare")
FEATURES = (1, 6)


def validate_run_dir(value):
    path = Path(value).expanduser().resolve(strict=True)
    if (
        path.name not in CONDITIONS
        or path.parent.parent != RUNS.resolve()
        or not re.fullmatch(r"baseline_smoke_[A-Za-z0-9_-]+", path.parent.name)
        or not path.is_dir()
    ):
        raise ValueError(
            "--run-dir must be artifacts/baseline_runs/baseline_smoke_*/{keep,strongshare}"
        )
    return path


def patch_info(run_dir):
    patches = {}
    for feature in FEATURES:
        patch = run_dir / f"agent{feature}.patch"
        if not patch.is_file() or patch.is_symlink() or patch.resolve().parent != run_dir:
            raise ValueError(f"An ordinary saved agent{feature}.patch is required; no patch was evaluated")
        content = patch.read_bytes()
        patches[f"agent{feature}"] = {
            "path": str(patch), "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content), "empty": not content.strip(),
        }
    return patches


def evaluate_saved_patches(evaluation_module, patches):
    """The same ordered feature-to-patch mapping as native coop evaluation."""
    return evaluation_module.test_merged(
        repo_name="pallets_jinja_task", task_id=1621,
        feature1_id=1, feature2_id=6,
        patch1=Path(patches["agent1"]["path"]),
        patch2=Path(patches["agent6"]["path"]),
        timeout=EXEC_TIMEOUT_SECONDS, backend="docker", dataset_dir=REPO / "dataset",
    )


def worker(run_dir, attempt_dir):
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    import docker
    import cooperbench.eval.backends.docker as backend_module
    import cooperbench.eval.sandbox as evaluation_module

    before = patch_info(run_dir)
    expected = json.loads((ROOT / "artifacts/task_image.json").read_text())
    client = docker.from_env(timeout=EXEC_TIMEOUT_SECONDS)
    previous_backend = evaluation_module.get_backend
    try:
        actual = client.images.get(IMAGE)
        if actual.id != expected["Id"] or actual.attrs["Architecture"] != expected["Architecture"]:
            raise RuntimeError("Task image differs from the verified reference image")
        head = subprocess.check_output(
            ["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True,
        ).strip()
        if head != COMMIT:
            raise RuntimeError("CooperBench commit differs from the verified environment")
        # Check the scoring code and task data, without rejecting unrelated runner instrumentation.
        subprocess.run([
            "/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
            "src/cooperbench/eval", "dataset/pallets_jinja_task/task1621",
        ], check=True, capture_output=True)
        write_json(attempt_dir / "evaluation_input.json", {
            "condition": run_dir.name, "patches": before,
            "image_id": expected["Id"], "cooperbench_commit": head,
            "features": list(FEATURES), "setting": "coop",
            "official_evaluator": "cooperbench.eval.sandbox.test_merged",
            "patch1_agent": "agent1", "patch2_agent": "agent6",
            "fallback_eligible_agent": "agent1",
            "model_calls": 0, "credentials_inherited": False,
            "saved_patches_modified_by_evaluator": False,
            "upstream_test_file_filter": True,
            "merge_policy": "pinned upstream naive merge with lead-only fallback; no model-assisted merge",
        })
        backend = make_backend(backend_module, client, expected["Id"], run_dir, attempt_dir)

        def get_backend(name="docker"):
            if name != "docker":
                raise ValueError("Only the local Docker evaluator is permitted")
            return backend

        evaluation_module.get_backend = get_backend
        result = evaluate_saved_patches(evaluation_module, before)
        if patch_info(run_dir) != before:
            result["error"] = "Saved agent patch changed during evaluation; result cannot be accepted"
            result["both_passed"] = False
        write_json(attempt_dir / "evaluation.json", result)
        return 2 if result.get("error") else 0
    finally:
        evaluation_module.get_backend = previous_backend
        client.close()


def summarize(result, status, run_dir, attempt_dir, elapsed):
    merge = result.get("merge") or {}
    return {
        "condition": run_dir.name, "status": status,
        "evaluation_completed": status in ("passed", "failed"),
        "both_passed": result.get("both_passed", False),
        "feature_1_passed": result.get("feature1", {}).get("passed"),
        "feature_6_passed": result.get("feature2", {}).get("passed"),
        "apply_status": result.get("apply_status"),
        "merge_status": merge.get("status"), "merge_strategy": merge.get("strategy"),
        "lead_patch_fallback_used": merge.get("strategy") == "solo-agent1",
        "error": result.get("error"), "cleanup_error": result.get("cleanup_error"),
        "elapsed_seconds": round(elapsed, 3), "watchdog_seconds": DEADLINE_SECONDS,
        "model_calls": 0, "attempt_directory": str(attempt_dir),
        "full_evaluator_result": str(attempt_dir / "evaluation.json"),
        "incremental_exec_output": str(attempt_dir / "exec_logs"),
        "interpretation": (
            "Official coop score. Conflicts/missing_input are benchmark outcomes, not infrastructure errors. "
            "A solo-agent1 strategy means the pre-existing agent1 patch passed both suites; "
            "it is not a model-assisted repair or a runtime Consolidate intervention."
        ),
    }


def supervise(run_dir):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    attempt_dir = run_dir / "evaluation_attempts" / stamp
    attempt_dir.mkdir(parents=True)
    started = time.monotonic()
    env = evaluator_environment()
    result, proc = None, None
    code, status = 2, "error"
    try:
        patch_info(run_dir)
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
            result = json.loads(saved.read_text()) if saved.exists() else {
                "error": f"Evaluator worker exited {code} without a result; see console.log", "both_passed": False,
            }
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
        summary = summarize(result, status, run_dir, attempt_dir, time.monotonic() - started)
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
    run_dir = validate_run_dir(args.run_dir)
    if args.worker_attempt:
        attempt_dir = Path(args.worker_attempt).resolve(strict=True)
        if attempt_dir.parent != run_dir / "evaluation_attempts":
            raise ValueError("Invalid evaluator attempt directory")
        return worker(run_dir, attempt_dir)

    def interrupted(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    return supervise(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
