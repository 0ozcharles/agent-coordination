"""Identical native scoring of the predesignated agent1 publication after conflict.

Both StrongShare and Integrator use one already published, complete patch from
agent1 relative to the task base. Peer contributions are protected evidence only.
No merge, worktree rescue, alternate submission, model call, or score selection
is performed here. The existing single-patch scorer is loaded privately.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys
from types import SimpleNamespace

from evaluate_solo_expansion import file_info, ordinary_file
from evaluate_solo_smoke import ROOT

RUNS = ROOT / "artifacts/conflict_runs"
TASK_KEYS = ("llama_index17070_f1_f3", "click2068_f4_f10")
CONDITIONS = ("strongshare", "integrator")
VERSION = "luna_conflict_v1"
CONDITION_VERSIONS = {"strongshare": "conflict_strongshare_v1", "integrator": "conflict_integrator_v1"}
SUBMISSION_POLICY = "authoritative_agent1_only"
DELIVERY_SCHEMA = "conflict_authoritative_delivery_v1"


def read(path):
    return json.loads(ordinary_file(Path(path)).read_text())


def get_task(key):
    if key == TASK_KEYS[0]:
        from cross_task_specs import get_task as registered
    elif key == TASK_KEYS[1]:
        from selector_task_specs import get_task as registered
    else:
        raise ValueError("Unregistered conflict-pilot task")
    return registered(key)


def source_manifest():
    from run_luna_conflict import source_manifest as frozen_sources
    return frozen_sources()


def validate_run_dir(value, diagnostic=False):
    if diagnostic:
        raise ValueError("Conflict pilot has no alternate scoring mode")
    original = Path(value).expanduser().absolute()
    phase = original.resolve(strict=True)
    try:
        parts = phase.relative_to(RUNS.resolve()).parts
    except ValueError:
        parts = ()
    if (original != phase or not phase.is_dir() or len(parts) != 5
            or parts[1] not in TASK_KEYS or parts[2] != "jobs" or parts[4] not in CONDITIONS
            or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts)):
        raise ValueError("Expected conflict_runs/<suite>/<task>/jobs/<job>/<strongshare|integrator>")
    return phase


def verify_worker(phase, task):
    from conflict_runtime import verify_worker_artifacts
    receipt = verify_worker_artifacts(phase, task)
    if receipt != read(phase / "execution_receipt.json"):
        raise ValueError("Conflict runtime verifier did not return the saved receipt")
    protected = receipt.get("protected_files_sha256")
    if not isinstance(protected, dict) or not protected:
        raise ValueError("Conflict execution receipt has no protected artifacts")
    for relative, expected in protected.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid conflict evidence path")
        if file_info(phase.parent / relative)["sha256"] != expected:
            raise ValueError("Conflict execution evidence changed: " + relative)
    return receipt


def load_task(phase, diagnostic=False):
    validate_run_dir(phase, diagnostic)
    config, status = read(phase.parent / "task_config.json"), read(phase / "status.json")
    task = get_task(config.get("task_key"))
    if (config.get("task") != task.as_dict() or phase.parent.parent.parent.name != task.key
            or config.get("condition") != phase.name
            or config.get("condition_version") != CONDITION_VERSIONS[phase.name]
            or config.get("experiment_version") != VERSION
            or config.get("submission_policy") != SUBMISSION_POLICY
            or type(config.get("offline_selftest")) is not bool
            or not re.fullmatch(r"[0-9a-f]{64}", str(config.get("protocol_sha256", "")))
            or status.get("status") != "completed" or status.get("task") != task.as_dict()
            or status.get("offline_selftest") != config["offline_selftest"]):
        raise ValueError("Completed conflict task/config identity is inconsistent")
    image = read(task.image_record_path)
    if (image.get("expected_base") != task.base or image.get("image", task.image) != task.image
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(image.get("Id", "")))):
        raise ValueError("Image record does not identify the pinned conflict task image")
    from conflict_checkpoint import validate_conflict_checkpoint
    validate_conflict_checkpoint(read(phase.parent / "checkpoint.json"), task)
    verify_worker(phase, task)
    return task, config


def patch_info(phase, task, diagnostic=False):
    if diagnostic or phase.name not in CONDITIONS:
        raise ValueError("Only the primary common agent1 scoring contract is allowed")
    names = ("agent1.patch", "agent2.patch")
    patches = {name: {**file_info(phase / name), "empty": not (phase / name).read_bytes().strip()} for name in names}
    receipt = read(phase / "execution_receipt.json")
    hashes = {name: info["sha256"] for name, info in patches.items()}
    if receipt.get("patch_sha256") != hashes:
        raise ValueError("Both published patches must match the conflict execution receipt")
    declaration = read(phase / "final_delivery.json")
    expected = {"schema": DELIVERY_SCHEMA, "condition": phase.name,
                "submission_policy": SUBMISSION_POLICY, "authoritative_agent": "agent1",
                "scored_patch": "agent1.patch", "peer_patch": "agent2.patch",
                "selection": "predesignated_agent1", "active_agents": ["agent1", "agent2"],
                "automatic_merge": False, "posthoc_selection": False,
                "patch_sha256": hashes["agent1.patch"], "peer_patch_sha256": hashes["agent2.patch"]}
    if any(declaration.get(key) != value for key, value in expected.items()):
        raise ValueError("Conflict delivery differs from the common predesignated agent1 contract")
    return patches


def contributions(phase, task, patches, diagnostic=False):
    if diagnostic:
        raise ValueError("Conflict pilot has no alternate score")
    return {"agent1": {"feature": task.features[0], "patch": patches["agent1.patch"], "graded": True},
            "agent2": {"feature": task.features[1], "patch": patches["agent2.patch"], "graded": False},
            "worker_delivery_declaration": read(phase / "final_delivery.json"),
            "meaning": "Both conditions had the same agent1-only publication contract before conflict collection. Peer publication is contribution evidence, not an alternate graded submission or proof of reuse."}


def input_manifest(phase, task, patches, diagnostic=False):
    if diagnostic:
        raise ValueError("Conflict pilot has no alternate score")
    receipt = verify_worker(phase, task)
    paths = {phase.parent / relative for relative in receipt["protected_files_sha256"]}
    paths.update(phase.parent / name for name in ("task_config.json", "checkpoint.json", "shared_git.tar"))
    paths.update(phase / name for name in ("status.json", "source_manifest.json", "execution_receipt.json", "final_delivery.json"))
    paths.add(task.image_record_path)
    # Evaluator-only hashes bind the original requirements, tests and runner;
    # these files are never sent to the execution agents by this adapter.
    paths.update(task.task_dir / f"feature{feature}" / name
                 for feature in task.features for name in ("feature.md", "tests.patch"))
    paths.add(task.task_dir / "runner.sh")
    return {"experiment_version": VERSION, "condition": phase.name,
            "condition_version": read(phase.parent / "task_config.json")["condition_version"],
            "submission_policy": SUBMISSION_POLICY, "primary_outcome": True,
            "authoritative_agent": "agent1", "patches": patches,
            "protected_files": {str(path): file_info(path) for path in sorted(paths)},
            "evaluator_sources": source_manifest()}


def make_evaluator(condition):
    if condition not in CONDITIONS:
        raise ValueError("Unknown conflict intervention condition")
    spec = importlib.util.spec_from_file_location("_conflict_single_patch_evaluator", ROOT / "luna_integrator_evaluator.py")
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    owner.__file__ = __file__
    owner.RUNS, owner.TASK_KEYS = RUNS, TASK_KEYS
    owner.get_task, owner.validate_run_dir = get_task, validate_run_dir
    def bound(function):
        def call(phase, *args, **kwargs):
            if Path(phase).name != condition:
                raise ValueError("Evaluator condition differs from the requested intervention")
            return function(phase, *args, **kwargs)
        return call
    owner.source_manifest, owner.load_task = source_manifest, bound(load_task)
    owner.patch_info, owner.input_manifest, owner.contributions = bound(patch_info), bound(input_manifest), bound(contributions)
    old_summary = owner.summarize

    def summarize(*args, **kwargs):
        summary = old_summary(*args, **kwargs)
        summary.update(experiment_version=VERSION, common_delivery_contract=True,
            patch_origin="actual_agent1_published_full_patch_relative_to_original_task_base",
            interpretation="Both active-agent conditions use the same predesignated agent1 published artifact and unchanged official feature tests. test_solo is only a single-patch scoring adapter. No patch merge, unsubmitted-worktree rescue, peer fallback, repair or post-hoc selection.")
        return summary

    owner.summarize = summarize
    public = SimpleNamespace(__file__=__file__, diagnostic=False, _implementation=owner,
        load_task=lambda phase: owner.load_task(phase), patch_info=lambda phase, task: owner.patch_info(phase, task),
        input_manifest=lambda phase, task, patches: owner.input_manifest(phase, task, patches),
        source_manifest=source_manifest, worker=bound(lambda phase, attempt: owner.worker(phase, attempt, False)),
        supervise=bound(lambda phase: owner.supervise(phase, False)))
    public.resume_completed = bound(lambda phase, task, config: owner.resume_completed(public, phase, task, config))
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", "--phase-dir", dest="run_dir", required=True)
    parser.add_argument("--worker-attempt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        phase = validate_run_dir(args.run_dir)
        evaluator = make_evaluator(phase.name)
        if args.worker_attempt:
            original = Path(args.worker_attempt).absolute()
            attempt = original.resolve(strict=True)
            if (original != attempt or not attempt.is_dir() or attempt.parent != phase / "evaluation_attempts"
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", attempt.name)):
                raise ValueError("Invalid conflict evaluator attempt directory")
            return evaluator.worker(phase, attempt)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        task, config = evaluator.load_task(phase)
        saved = evaluator.resume_completed(phase, task, config)
        if saved is not None:
            print(json.dumps({**saved, "reused_completed_evaluation": True}, ensure_ascii=False))
            return 0
        return evaluator.supervise(phase)
    except Exception as exc:
        print(json.dumps({"status": "error", "evaluation_completed": False,
                          "error": f"{type(exc).__name__}: {exc}", "model_calls": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
