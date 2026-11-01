"""Score one Consolidate submission with the pinned CooperBench feature tests.

Use the CooperBench virtual environment's Python:
    python evaluate_consolidate_smoke.py --run-dir \
        artifacts/consolidate_runs/consolidate_smoke_<id>/consolidate

The condition directory must contain an ordinary final.patch file. An empty
patch is a valid unsuccessful submission; a missing file is an input error.
Exit 0 means evaluation completed, including a benchmark failure. Exit 2 means
an input, infrastructure, integrity, cleanup, or timeout error.

Native test_solo is only the single-patch scoring adapter: the experimental
condition is runtime Consolidate, not a fresh solo run. Both original feature
suites must pass. No merge, fallback, model call, or patch repair is performed.
"""
import argparse
import copy
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

RUNS = ROOT / "artifacts/consolidate_runs"
FEATURES = (1, 6)
CONDITION = "consolidate"


def validate_run_dir(value):
    path = Path(value).expanduser().resolve(strict=True)
    if (
        path.name != CONDITION
        or path.parent.parent != RUNS.resolve()
        or not re.fullmatch(r"consolidate_smoke_[A-Za-z0-9_-]+", path.parent.name)
        or not path.is_dir()
    ):
        raise ValueError(
            "--run-dir must be artifacts/consolidate_runs/consolidate_smoke_*/consolidate"
        )
    return path


def patch_info(run_dir):
    patch = run_dir / "final.patch"
    if not patch.is_file() or patch.is_symlink() or patch.resolve().parent != run_dir:
        raise ValueError("An ordinary saved final.patch is required; no patch was evaluated")
    content = patch.read_bytes()
    return {
        "path": str(patch), "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content), "empty": not content.strip(),
    }


def evaluate_saved_patch(evaluation_module, patch):
    """Evaluate the explicit saved submission; never substitute a reference patch."""
    return evaluation_module.test_solo(
        repo_name="pallets_jinja_task", task_id=1621,
        feature1_id=FEATURES[0], feature2_id=FEATURES[1],
        patch=Path(patch["path"]), timeout=EXEC_TIMEOUT_SECONDS,
        backend="docker", dataset_dir=REPO / "dataset",
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
        subprocess.run([
            "/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
            "src/cooperbench/eval", "dataset/pallets_jinja_task/task1621",
        ], check=True, capture_output=True)
        write_json(attempt_dir / "evaluation_input.json", {
            "condition": CONDITION, "patch": before,
            "image_id": expected["Id"], "cooperbench_commit": head,
            "repo": "pallets_jinja_task", "task_id": 1621, "features": list(FEATURES),
            "official_evaluator": "cooperbench.eval.sandbox.test_solo",
            "native_setting": "solo", "single_patch_scoring_adapter": True,
            "merge_status": "not_applicable", "lead_patch_fallback_used": False,
            "model_calls": 0, "credentials_inherited": False,
            "saved_patch_modified_by_evaluator": False,
            "upstream_test_file_filter": True, "reference_patch_substitution": False,
            "scoring_rule": "The one submitted final.patch must pass both original feature suites.",
        })
        backend = make_backend(backend_module, client, expected["Id"], run_dir, attempt_dir)

        def get_backend(name="docker"):
            if name != "docker":
                raise ValueError("Only the local Docker evaluator is permitted")
            return backend

        evaluation_module.get_backend = get_backend
        official_result = evaluate_saved_patch(evaluation_module, before)
        # Preserve the native result separately from wrapper integrity/cleanup errors.
        write_json(attempt_dir / "official_evaluation.json", official_result)
        result = copy.deepcopy(official_result)
        if patch_info(run_dir) != before:
            result["error"] = "Saved agent patch changed during evaluation; result cannot be accepted"
            result["both_passed"] = False
        write_json(attempt_dir / "evaluation.json", result)
        return 2 if result.get("error") else 0
    finally:
        evaluation_module.get_backend = previous_backend
        client.close()


def summarize(result, status, run_dir, attempt_dir, elapsed):
    raw_path = attempt_dir / "official_evaluation.json"
    return {
        "condition": CONDITION, "status": status,
        "evaluation_completed": status in ("passed", "failed"),
        "both_passed": result.get("both_passed", False),
        "feature_1_passed": result.get("feature1", {}).get("passed"),
        "feature_6_passed": result.get("feature2", {}).get("passed"),
        "native_setting": result.get("setting"), "single_patch_scoring_adapter": True,
        "merge_status": "not_applicable", "merge_strategy": "not_applicable",
        "lead_patch_fallback_used": False,
        "error": result.get("error"), "cleanup_error": result.get("cleanup_error"),
        "elapsed_seconds": round(elapsed, 3), "watchdog_seconds": DEADLINE_SECONDS,
        "model_calls": 0, "attempt_directory": str(attempt_dir),
        "final_patch": str(run_dir / "final.patch"),
        "official_raw_result": str(raw_path) if raw_path.is_file() else None,
        "full_evaluator_result": str(attempt_dir / "evaluation.json"),
        "incremental_exec_output": str(attempt_dir / "exec_logs"),
        "interpretation": (
            "Runtime Consolidate is scored as one final patch against the same two official feature suites. "
            "Native setting=solo describes only the scoring adapter, not a from-scratch solo experiment. "
            "No merge or lead fallback applies; no model-assisted repair is performed."
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
        patch_info(run_dir)  # Missing inputs fail before any Docker work; empty is valid.
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
