"""Pinned native evaluation for cross-task handoff and isolated fresh Solo jobs.

CLI: --run-dir artifacts/handoff_expansion_runs/<suite>/<task>/jobs/<job>/<condition>
Fresh Solo uses <task>/solo_jobs/<job>/solo. Complete benchmark failure exits 0;
invalid input or infrastructure failure exits 2. No agent or model is invoked.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from pathlib import Path

from cross_task_workers import load_isolated
from evaluate_solo_smoke import ROOT, REPO

RUNS = ROOT / "artifacts/handoff_expansion_runs"
CONDITIONS = ("strongshare", "consolidate", "solo")


def get_task(key):
    from cross_task_specs import get_task as registered_task
    return registered_task(key)


def validate_run_dir(value):
    original = Path(value).expanduser().absolute()
    path = original.resolve(strict=True)
    try:
        parts = path.relative_to(RUNS.resolve()).parts
    except ValueError:
        parts = ()
    valid = (len(parts) == 5 and parts[4] in CONDITIONS
             and parts[2] == ("solo_jobs" if parts[4] == "solo" else "jobs")
             and all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts))
    if not valid or original != path or not path.is_dir():
        raise ValueError("Expected a cross-task jobs/<job>/<condition> or solo_jobs/<job>/solo directory")
    return path


def assert_fresh_job(run_dir, *records):
    """Inspect only this Solo job; sibling task prefixes are never inherited."""
    from evaluate_solo_expansion import FORBIDDEN_NAMES, FORBIDDEN_KEYS
    for path in run_dir.parent.rglob("*"):
        name = path.name.lower()
        if (path.is_symlink() or name in FORBIDDEN_NAMES or "checkpoint" in name
                or "provenance" in name or name.startswith(("restore_proof", "intervention", "inherited_"))):
            raise ValueError("Fresh Solo job contains a link or inherited artifact: " + str(path))

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                key = str(key).lower()
                if (key in FORBIDDEN_KEYS or "checkpoint" in key
                        or key.startswith(("inherited_", "restore_proof", "intervention"))):
                    raise ValueError("Fresh Solo metadata declares inherited state: " + key)
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    for record in records:
        inspect(record)


def source_manifest():
    from evaluate_solo_expansion import file_info
    paths = [Path(__file__).resolve()]
    paths.extend(ROOT / name for name in (
        "cross_task_workers.py", "cross_task_specs.py", "task_specs.py",
        "evaluate_solo_expansion.py", "evaluate_task_expansion.py", "evaluate_solo_smoke.py"))
    paths.append(REPO / "src/cooperbench/utils.py")
    paths.extend(sorted((REPO / "src/cooperbench/eval").rglob("*.py")))
    return {str(path.relative_to(ROOT)): file_info(path)["sha256"] for path in paths}


def make_evaluator(condition):
    if condition not in CONDITIONS:
        raise ValueError("Unknown cross-task evaluation condition")
    solo = condition == "solo"
    module = load_isolated("evaluate_solo_expansion.py" if solo else "evaluate_task_expansion.py")
    module.__file__ = str(Path(__file__).resolve())  # Supervisor children return to this CLI.
    module.RUNS = RUNS
    module.get_task = get_task
    module.validate_run_dir = validate_run_dir
    module.source_manifest = source_manifest
    if solo:
        module.assert_fresh_provenance = assert_fresh_job
    old_load, old_patches = module.load_task, module.patch_info
    old_manifest, old_summary = module.input_manifest, module.summarize

    def load_task(run_dir):
        validate_run_dir(run_dir)
        task, config = old_load(run_dir)
        if config.get("condition") != condition or run_dir.name != condition:
            raise ValueError("Saved condition differs from the selected evaluator")
        image = json.loads(module.ordinary_file(task.image_record_path).read_text())
        if image.get("expected_base") != task.base or image.get("image", task.image) != task.image:
            raise ValueError("Image record does not describe the registered task base")
        return task, config

    def patch_info(run_dir, task):
        patches = old_patches(run_dir, task)
        receipt = json.loads(module.ordinary_file(run_dir / "execution_receipt.json").read_text())
        expected = {name: record["sha256"] for name, record in patches.items()}
        if receipt.get("patch_sha256") != expected:
            raise ValueError("Published patches differ from the execution receipt")
        if solo:
            assert_fresh_job(run_dir, receipt)
        elif condition == "consolidate":
            status = json.loads(module.ordinary_file(run_dir / "status.json").read_text())
            if status.get("final_patch_sha256") != expected["final.patch"]:
                raise ValueError("Consolidate patch differs from the completed worker status")
        return patches

    def input_manifest(run_dir, task, patches):
        manifest = old_manifest(run_dir, task, patches)
        if not solo:
            paths = [run_dir.parent / name for name in (
                "checkpoint.json", "shared_git.tar", "checkpoint_provenance.json")]
            paths += [run_dir / name for name in (
                "handoff_packet.json", "handoff_packet.txt", "handoff_delivery.json")]
            paths += [run_dir / f"first_request_{agent}.json" for agent in
                      (("agent1", "agent2") if condition == "strongshare" else ("agent1",))]
            manifest["protected_files"].update({str(path): module.file_info(path) for path in paths})
        return manifest

    def summarize(result, status, run_dir, attempt_dir, elapsed, task=None, config=None):
        from evaluate_solo_expansion import feature_metrics
        summary = old_summary(result, status, run_dir, attempt_dir, elapsed, task, config)
        summary["both_passed"] = status == "passed" and bool(result.get("both_passed"))
        if task is not None:
            summary["feature_results"] = {
                str(feature): feature_metrics(result.get(f"feature{number}") or {})
                for number, feature in enumerate(task.features, 1)}
            for feature in task.features:
                summary[f"feature_{feature}_passed"] = summary["feature_results"][str(feature)]["passed"]
        summary["condition_version"] = (config or {}).get("condition_version")
        return summary

    module.load_task, module.patch_info = load_task, patch_info
    module.input_manifest, module.summarize = input_manifest, summarize
    module.resume_completed = lambda run_dir, task, config: resume_completed(module, run_dir, task, config)
    if not solo:
        original_backend = module.make_backend

        def make_backend(backend_module, client, expected_id, run_dir, attempt_dir, expected_image=None):
            backend = original_backend(backend_module, client, expected_id, run_dir, attempt_dir,
                                       expected_image=expected_image)
            create = backend.create_sandbox
            task, _ = load_task(run_dir)

            def create_sandbox(image, timeout=600, workdir="/workspace"):
                sandbox = create(image, timeout=timeout, workdir=workdir)
                try:
                    check = sandbox.exec("git", "-C", "/workspace/repo", "rev-parse", "HEAD")
                    base = check.stdout_read().strip()
                    if check.returncode or base != task.base:
                        raise RuntimeError("Evaluator sandbox does not contain the registered base commit")
                    module.write_json(attempt_dir / "image_base_validation.json", {
                        "image_id": expected_id, "base_commit": base, "matched": True})
                    return sandbox
                except BaseException:
                    sandbox.terminate()
                    raise

            backend.create_sandbox = create_sandbox
            return backend

        module.make_backend = make_backend
    return module


def resume_completed(module, run_dir, task, config):
    path = run_dir / "evaluation_summary.json"
    if not path.exists() and not path.is_symlink():
        return None
    summary = json.loads(module.ordinary_file(path).read_text())
    if not summary.get("evaluation_completed"):
        return None
    if (summary.get("status") not in ("passed", "failed")
            or summary.get("condition") != run_dir.name or summary.get("task") != task.as_dict()
            or summary.get("offline_selftest") != config["offline_selftest"]
            or summary.get("protocol_sha256") != config["protocol_sha256"]):
        raise ValueError("Saved evaluation metadata does not describe this job")
    original = Path(summary.get("attempt_directory", ""))
    attempt = original.resolve(strict=True)
    if original != attempt or attempt.parent != run_dir / "evaluation_attempts" or not attempt.is_dir():
        raise ValueError("Invalid saved evaluator attempt path")
    load = lambda p: json.loads(module.ordinary_file(p).read_text())
    entry, integrity = load(attempt / "evaluation_input.json"), load(attempt / "integrity.json")
    current = module.input_manifest(run_dir, task, module.patch_info(run_dir, task))
    if (entry.get("task") != task.as_dict() or entry.get("protocol_sha256") != config["protocol_sha256"]
            or entry.get("input_manifest") != current or not integrity.get("unchanged")
            or integrity.get("before") != current or integrity.get("after") != current):
        raise ValueError("Saved evaluation inputs or sources changed; refusing stale score reuse")
    module.verify_upstream(task)
    native, accepted = load(attempt / "official_evaluation.json"), load(attempt / "evaluation.json")
    if (accepted != native or load(run_dir / "evaluation.json") != accepted
            or accepted.get("error") or accepted.get("cleanup_error")):
        raise ValueError("Saved result differs from the accepted native evaluation")
    expected = module.summarize(native, "passed" if native.get("both_passed") else "failed",
                                run_dir, attempt, summary.get("elapsed_seconds", 0), task, config)
    fields = ("condition", "fresh_solo", "task_key", "task", "features", "offline_selftest",
              "protocol_sha256", "condition_version", "status", "evaluation_completed", "both_passed",
              "feature_results", "native_setting", "single_patch_scoring_adapter", "merge_status",
              "merge_strategy", "lead_patch_fallback_used", "model_calls")
    if (any(summary.get(key) != expected.get(key) for key in fields)
            or any(summary.get(f"feature_{f}_passed") != expected.get(f"feature_{f}_passed") for f in task.features)):
        raise ValueError("Saved evaluation summary differs from the native result")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--worker-attempt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        run_dir = validate_run_dir(args.run_dir)
        module = make_evaluator(run_dir.name)
        if args.worker_attempt:
            original = Path(args.worker_attempt).absolute()
            attempt = original.resolve(strict=True)
            if (original != attempt or attempt.parent != run_dir / "evaluation_attempts"
                    or not attempt.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]+", attempt.name)):
                raise ValueError("Invalid evaluator attempt directory")
            return module.worker(run_dir, attempt)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        task, config = module.load_task(run_dir)
        prior = resume_completed(module, run_dir, task, config)
        if prior is not None:
            print(json.dumps({**prior, "reused_completed_evaluation": True}, ensure_ascii=False, indent=2))
            return 0
        return module.supervise(run_dir)
    except Exception as exc:
        print(json.dumps({"status": "error", "evaluation_completed": False,
                          "error": f"{type(exc).__name__}: {exc}", "model_calls": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
