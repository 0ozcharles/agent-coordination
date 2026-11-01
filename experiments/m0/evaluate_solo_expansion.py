"""Evaluate a fresh solo expansion submission with the pinned official tests.

Usage:
    python evaluate_solo_expansion.py --run-dir \
        artifacts/solo_expansion_runs/<suite>/<task>/jobs/<job>/solo

One final.patch is tested against both task features using native test_solo.
The execution must declare fresh_solo=true and contain no inherited checkpoint
or intervention artifacts. Empty patches are valid unsuccessful submissions.
Completed benchmark outcomes exit 0; invalid inputs/infrastructure exit 2.
An unchanged completed evaluation is reused without launching a new evaluator.
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
    COMMIT, DEADLINE_SECONDS, EXEC_TIMEOUT_SECONDS, REPO, ROOT,
    cleanup, evaluator_environment, make_backend, stop_process, write_json,
)
from task_specs import get_task

RUNS = ROOT / "artifacts/solo_expansion_runs"
CONDITIONS = ("solo",)
FORBIDDEN_NAMES = {
    "prefix", "shared_git.tar", "shared-git.tar", "shared_evidence", "shared-evidence",
    "evidence_manifests", "restore_proof.json", "intervention.json", "inherited_state.json",
}
FORBIDDEN_KEYS = {
    "checkpoint", "checkpoint_sha256", "checkpoint_hash", "checkpoint_provenance",
    "source_checkpoint", "source_checkpoint_sha256", "source_run", "prefix_run",
    "prefix_directory", "inherited_from", "inherited_state", "restore_proof", "intervention",
}


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
    if (len(parts) != 5 or parts[2] != "jobs" or parts[4] != "solo"
            or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts)
            or not path.is_dir() or original != path):
        raise ValueError("--run-dir must be artifacts/solo_expansion_runs/<suite>/<task>/jobs/<job>/solo")
    return path


def assert_fresh_provenance(run_dir, *records):
    """Reject inherited runtime artifacts rather than treating them as harmless receipts."""
    def forbidden_name(name):
        name = name.lower()
        return (name in FORBIDDEN_NAMES or "checkpoint" in name
                or "provenance" in name or name.startswith("restore_proof")
                or name.startswith("intervention") or name.startswith("inherited_"))

    # Inspect this job recursively; also reject inherited state placed at suite/task level.
    paths = list(run_dir.parent.rglob("*"))
    for directory in (run_dir.parent.parent.parent, run_dir.parent.parent.parent.parent):
        paths.extend(directory.iterdir())
    for path in paths:
        if forbidden_name(path.name):
            raise ValueError("Fresh solo cannot contain checkpoint/inherited artifacts: " + str(path))

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                if (lowered in FORBIDDEN_KEYS or "checkpoint" in lowered
                        or lowered.startswith("inherited_") or lowered.startswith("restore_proof")
                        or lowered.startswith("intervention")):
                    raise ValueError("Fresh solo metadata cannot declare inherited state: " + str(key))
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    for record in records:
        inspect(record)


def load_task(run_dir):
    config = json.loads(ordinary_file(run_dir.parent / "task_config.json").read_text())
    task = get_task(config.get("task_key"))
    if config.get("task") != task.as_dict():
        raise ValueError("Saved task specification differs from the fixed task registry")
    if run_dir.parent.parent.parent.name != task.key:
        raise ValueError("Task directory does not match task_config.json")
    if type(config.get("offline_selftest")) is not bool:
        raise ValueError("task_config.json must declare offline_selftest")
    if config.get("fresh_solo") is not True:
        raise ValueError("task_config.json must declare fresh_solo=true")
    if not re.fullmatch(r"[0-9a-f]{64}", str(config.get("protocol_sha256", ""))):
        raise ValueError("task_config.json requires the frozen protocol hash")
    status = json.loads(ordinary_file(run_dir / "status.json").read_text())
    if status.get("status") != "completed":
        raise ValueError("Only completed solo artifacts may be evaluated")
    if status.get("task") != task.as_dict():
        raise ValueError("Completed solo task differs from task_config.json")
    if status.get("offline_selftest") != config["offline_selftest"]:
        raise ValueError("Condition and task_config.json disagree on offline_selftest")
    if status.get("fresh_solo") is not True:
        raise ValueError("Completed solo status must declare fresh_solo=true")
    if status.get("phase", "solo") != "solo":
        raise ValueError("Completed status is not a solo phase")
    assert_fresh_provenance(run_dir, config, status)
    return task, config


def patch_info(run_dir, task):
    info = file_info(run_dir / "final.patch")
    info["empty"] = not Path(info["path"]).read_bytes().strip()
    status = json.loads(ordinary_file(run_dir / "status.json").read_text())
    declared = status.get("final_patch_sha256")
    if declared is not None and declared != info["sha256"]:
        raise ValueError("Solo submission differs from the completed execution status")
    receipt = run_dir / "execution_receipt.json"
    if receipt.exists() or receipt.is_symlink():
        saved = json.loads(ordinary_file(receipt).read_text())
        assert_fresh_provenance(run_dir, saved)
        if saved.get("patch_sha256") != {"final.patch": info["sha256"]}:
            raise ValueError("Solo submission differs from its execution receipt")
    return {"final.patch": info}


def source_manifest():
    paths = [Path(__file__).resolve(), ROOT / "evaluate_solo_smoke.py", ROOT / "task_specs.py",
             REPO / "src/cooperbench/utils.py"]
    paths.extend(sorted((REPO / "src/cooperbench/eval").rglob("*.py")))
    return {str(p.relative_to(ROOT)): file_info(p)["sha256"] for p in paths}


def input_manifest(run_dir, task, patches):
    assert_fresh_provenance(run_dir)
    paths = [run_dir.parent / "task_config.json", run_dir / "status.json",
             run_dir / "initial_state.json", task.image_record_path]
    for name in ("source_manifest.json", "execution_receipt.json"):
        path = run_dir / name
        if path.exists() or path.is_symlink():
            paths.append(path)
    # Bind the exact task specifications, tests and baked-runner source, not reference answers.
    paths.extend(task.task_dir / f"feature{feature}" / name
                 for feature in task.features for name in ("feature.md", "tests.patch"))
    paths.append(task.task_dir / "runner.sh")
    return {"patches": patches, "protected_files": {str(p): file_info(p) for p in paths},
            "evaluator_sources": source_manifest()}


def evaluate_saved_patches(evaluation_module, run_dir, task, patches):
    return evaluation_module.test_solo(
        repo_name=task.repo, task_id=task.task_id,
        feature1_id=task.features[0], feature2_id=task.features[1],
        patch=Path(patches["final.patch"]["path"]), timeout=EXEC_TIMEOUT_SECONDS,
        backend="docker", dataset_dir=REPO / "dataset")


def verify_upstream(task):
    head = subprocess.check_output(
        ["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        raise RuntimeError("CooperBench commit differs from the verified environment")
    paths = ["src/cooperbench/eval", "src/cooperbench/utils.py", str(task.task_dir.relative_to(REPO))]
    subprocess.run(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--", *paths],
                   check=True, capture_output=True)
    return head


def worker(run_dir, attempt_dir):
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
    if expected.get("image", task.image) != task.image or expected.get("expected_base", task.base) != task.base:
        raise ValueError("Image record belongs to a different task")
    client = docker.from_env(timeout=EXEC_TIMEOUT_SECONDS)
    previous_backend = evaluation_module.get_backend
    try:
        actual = client.images.get(task.image)
        if actual.id != expected["Id"] or actual.attrs["Architecture"] != expected["Architecture"]:
            raise RuntimeError("Task image differs from the verified reference image")
        head = verify_upstream(task)
        write_json(attempt_dir / "evaluation_input.json", {
            "condition": "solo", "fresh_solo": True, "execution_mode": "fresh_solo",
            "task_key": task.key, "task": task.as_dict(), "offline_selftest": config["offline_selftest"],
            "protocol_sha256": config["protocol_sha256"], "input_manifest": before,
            "image_id": expected["Id"], "base_commit": task.base, "cooperbench_commit": head,
            "features": list(task.features), "native_setting": "solo",
            "official_evaluator": "cooperbench.eval.sandbox.test_solo",
            "single_patch_scoring_adapter": False, "ordered_patch_mapping": {"solo": "final.patch"},
            "fallback_eligible_agent": None, "model_calls": 0, "credentials_inherited": False,
            "saved_patches_modified_by_evaluator": False, "reference_patch_substitution": False,
            "upstream_test_file_filter": True, "merge_policy": "not_applicable",
        })
        backend = make_backend(backend_module, client, expected["Id"], run_dir, attempt_dir,
                               expected_image=task.image)
        original_create = backend.create_sandbox

        def create_sandbox(image, timeout=600, workdir="/workspace"):
            sandbox = original_create(image, timeout=timeout, workdir=workdir)
            try:
                found = sandbox.exec("git", "-C", "/workspace/repo", "rev-parse", "HEAD")
                base = found.stdout_read().strip()
                if found.returncode or base != task.base:
                    raise RuntimeError("Evaluator image does not contain the fixed task base commit")
                write_json(attempt_dir / "image_base_validation.json", {
                    "image_id": expected["Id"], "base_commit": base, "matched": True})
                return sandbox
            except BaseException:
                sandbox.terminate()
                raise

        backend.create_sandbox = create_sandbox

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
        write_json(attempt_dir / "integrity.json", {"unchanged": integrity_ok, "before": before, "after": after})
        if not integrity_ok:
            result["error"] = "Evaluation inputs or scoring sources changed during evaluation"
            result["both_passed"] = False
        write_json(attempt_dir / "evaluation.json", result)
        return 2 if result.get("error") else 0
    finally:
        evaluation_module.get_backend = previous_backend
        client.close()


def feature_metrics(native):
    """Extract only explicit native counts or a final pytest summary; never infer from pass/fail."""
    metrics = {"passed": native.get("passed"), "tests_passed": native.get("tests_passed"),
               "tests_failed": native.get("tests_failed"), "exit_code": native.get("exit_code"),
               "counts_source": None}
    if metrics["tests_passed"] is not None and metrics["tests_failed"] is not None:
        metrics["counts_source"] = "native_result"
        return metrics
    output = native.get("test_output") or native.get("output") or ""
    for line in reversed(output.splitlines()):
        if not re.match(r"^=+\s", line) or not re.search(r"\bin\s+[0-9.]+s\s*=+$", line):
            continue
        counts = {name: int(number) for number, name in re.findall(
            r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", line)}
        if not any(name in counts for name in ("passed", "failed", "error", "errors")):
            continue
        metrics.update(tests_passed=counts.get("passed", 0),
                       tests_failed=counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0),
                       counts_source="pytest_summary")
        break
    return metrics


def summarize(result, status, run_dir, attempt_dir, elapsed, task=None, config=None):
    features = list(task.features) if task is not None else []
    feature_results = {str(feature): feature_metrics(result.get(f"feature{number}") or {})
                       for number, feature in enumerate(features, 1)}
    raw_path = attempt_dir / "official_evaluation.json"
    summary = {
        "condition": "solo", "fresh_solo": True, "execution_mode": "fresh_solo",
        "task_key": task.key if task else None, "task": task.as_dict() if task else None,
        "features": features, "offline_selftest": (config or {}).get("offline_selftest"),
        "protocol_sha256": (config or {}).get("protocol_sha256"), "status": status,
        "evaluation_completed": status in ("passed", "failed"),
        "both_passed": status == "passed" and bool(result.get("both_passed", False)),
        "feature_results": feature_results, "native_setting": "solo", "single_patch_scoring_adapter": False,
        "merge_status": "not_applicable", "merge_strategy": "not_applicable", "lead_patch_fallback_used": False,
        "error": result.get("error"), "cleanup_error": result.get("cleanup_error"),
        "elapsed_seconds": round(elapsed, 3), "watchdog_seconds": DEADLINE_SECONDS,
        "model_calls": 0, "attempt_directory": str(attempt_dir),
        "official_raw_result": str(raw_path) if raw_path.is_file() else None,
        "full_evaluator_result": str(attempt_dir / "evaluation.json"),
        "incremental_exec_output": str(attempt_dir / "exec_logs"),
        "input_manifest": str(attempt_dir / "evaluation_input.json"),
        "integrity_check": str(attempt_dir / "integrity.json"),
        "interpretation": (
            "Fresh solo starts at the task base and implements both features in one submission. "
            "The pinned native solo evaluator scores the saved final.patch against both official suites. "
            "No resumed agent state, runtime consolidation, model-assisted repair or reference substitution."
        ),
    }
    for feature in features:
        summary[f"feature_{feature}_passed"] = feature_results[str(feature)]["passed"]
    return summary


def resume_completed(run_dir, task, config):
    path = run_dir / "evaluation_summary.json"
    if not path.exists() and not path.is_symlink():
        return None
    summary = json.loads(ordinary_file(path).read_text())
    if not summary.get("evaluation_completed"):
        return None
    if (summary.get("status") not in ("passed", "failed") or summary.get("condition") != "solo"
            or summary.get("fresh_solo") is not True or summary.get("task") != task.as_dict()
            or summary.get("offline_selftest") != config["offline_selftest"]
            or summary.get("protocol_sha256") != config["protocol_sha256"]):
        raise ValueError("Completed evaluation metadata does not match this fresh solo run")
    original = Path(summary.get("attempt_directory", ""))
    attempt = original.resolve(strict=True)
    if original != attempt or attempt.parent != run_dir / "evaluation_attempts" or not attempt.is_dir():
        raise ValueError("Completed evaluator attempt path is invalid")
    entry = json.loads(ordinary_file(attempt / "evaluation_input.json").read_text())
    integrity = json.loads(ordinary_file(attempt / "integrity.json").read_text())
    current = input_manifest(run_dir, task, patch_info(run_dir, task))
    if (entry.get("fresh_solo") is not True or entry.get("task") != task.as_dict()
            or entry.get("protocol_sha256") != config["protocol_sha256"]
            or entry.get("input_manifest") != current or not integrity.get("unchanged")
            or integrity.get("before") != current or integrity.get("after") != current):
        raise ValueError("Saved evaluation inputs or sources changed; refusing stale score reuse")
    verify_upstream(task)
    accepted = json.loads(ordinary_file(attempt / "evaluation.json").read_text())
    native = json.loads(ordinary_file(attempt / "official_evaluation.json").read_text())
    current_result = json.loads(ordinary_file(run_dir / "evaluation.json").read_text())
    if (accepted != native or current_result != accepted or accepted.get("error")
            or accepted.get("cleanup_error") or bool(accepted.get("both_passed")) != summary["both_passed"]
            or (summary["status"] == "passed") != bool(accepted.get("both_passed"))):
        raise ValueError("Saved evaluation result no longer matches the accepted native result")
    expected_features = {str(feature): feature_metrics(accepted.get(f"feature{number}") or {})
                         for number, feature in enumerate(task.features, 1)}
    if (summary.get("features") != list(task.features)
            or summary.get("feature_results") != expected_features
            or any(summary.get(f"feature_{feature}_passed") != expected_features[str(feature)]["passed"]
                   for feature in task.features)):
        raise ValueError("Saved feature summary no longer matches the accepted native result")
    return summary


def supervise(run_dir):
    task, config = load_task(run_dir)
    patch_info(run_dir, task)
    existing = resume_completed(run_dir, task, config)
    if existing is not None:
        print(json.dumps({**existing, "reused_completed_evaluation": True}, ensure_ascii=False, indent=2))
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    attempt_dir = run_dir / "evaluation_attempts" / stamp
    attempt_dir.mkdir(parents=True)
    started = time.monotonic()
    env = evaluator_environment()
    result, proc = None, None
    code, status = 2, "error"
    try:
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
            result = json.loads(ordinary_file(saved).read_text()) if saved.exists() else {
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

        def interrupted(*_):
            raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, interrupted)
        return supervise(run_dir)
    except Exception as exc:
        print(json.dumps({"status": "error", "evaluation_completed": False, "fresh_solo": True,
                          "error": f"{type(exc).__name__}: {exc}", "model_calls": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
