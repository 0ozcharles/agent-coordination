"""Pinned native scoring and cache validation for the new selector task registry.

Private copies retain native merge/fallback for StrongShare, predetermined
agent1-only delivery for Integrator, and fresh single-patch scoring for Solo.
This adapter changes registry, artifact root, and evaluator-child routing only.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import signal
import sys
from types import SimpleNamespace

from selector_workers import load_private, module_bindings, registry_context, verify_execution
from task_specs import ROOT

RUNS = ROOT / "artifacts/selector_runs"
CONDITIONS = ("strongshare", "integrator", "solo")


def source_manifest():
    from run_luna_selector import source_manifest as frozen_sources
    return frozen_sources()


def get_task(key):
    from selector_task_specs import get_task as registered
    return registered(key)


def validate_run_dir(value, diagnostic=False):
    if diagnostic:
        raise ValueError("Selector pilot has no diagnostic scoring mode")
    original = Path(value).expanduser().absolute()
    path = original.resolve(strict=True)
    try:
        parts = path.relative_to(RUNS.resolve()).parts
    except ValueError:
        parts = ()
    if (len(parts) != 5 or parts[-1] not in CONDITIONS
            or parts[2] != ("solo_jobs" if parts[-1] == "solo" else "jobs")
            or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts)
            or path != original or not path.is_dir()):
        raise ValueError("Expected a regular selector_runs/<suite>/<task>/<jobs|solo_jobs>/<job>/<condition> directory")
    get_task(parts[1])
    return path


@contextmanager
def adapter_context():
    import selector_workers
    with registry_context(), module_bindings(luna_integrator_worker=selector_workers):
        yield


def make_evaluator(condition):
    if condition not in CONDITIONS:
        raise ValueError("Unknown selector evaluation condition")
    with adapter_context():
        if condition == "integrator":
            from selector_task_specs import TASKS
            owner = load_private("luna_integrator_evaluator.py")
            owner.RUNS, owner.TASK_KEYS = RUNS, tuple(TASKS)
            owner.get_task, owner.validate_run_dir = get_task, validate_run_dir
            owner.source_manifest, owner.__file__ = source_manifest, __file__
            implementation = owner.make_evaluator(condition)
            implementation.worker = lambda phase, attempt: owner.worker(phase, attempt, False)
            implementation.resume_completed = lambda phase, task, config: owner.resume_completed(implementation, phase, task, config)
        else:
            base = load_private("evaluate_cross_task.py")
            base.RUNS, base.get_task = RUNS, get_task
            base.validate_run_dir, base.source_manifest, base.__file__ = validate_run_dir, source_manifest, __file__
            if condition == "strongshare":
                owner = load_private("luna_collaboration_evaluator.py")
                owner.original = base
                owner.validate_run_dir, owner.source_manifest, owner.__file__ = validate_run_dir, source_manifest, __file__
                implementation = owner.make_evaluator(condition)
            else:
                implementation = base.make_evaluator(condition)
        implementation.__file__ = __file__
        implementation.source_manifest = source_manifest
        old_load = implementation.load_task

        def checked_load(phase):
            validate_run_dir(phase)
            task, config = old_load(phase)
            verify_execution(phase, task, condition, freeze=False)
            return task, config

        implementation.load_task = checked_load

    # Each public operation holds registry bindings for its full nested call,
    # including a worker's native capture and a cached-result integrity check.
    def guarded(function):
        def call(*args, **kwargs):
            with adapter_context():
                return function(*args, **kwargs)
        return call

    result = SimpleNamespace(__file__=__file__, _implementation=implementation)
    for name in ("load_task", "patch_info", "input_manifest", "source_manifest", "worker", "supervise", "resume_completed"):
        setattr(result, name, guarded(getattr(implementation, name)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
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
                raise ValueError("Invalid selector evaluator attempt directory")
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
