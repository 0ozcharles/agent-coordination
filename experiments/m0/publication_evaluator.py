"""Unchanged native scoring for publication-parity jobs, with bound guard evidence.

StrongShare keeps native merge/fallback; Integrator scores predesignated agent1;
Solo scores its published patch. No local-worktree rescue or alternate scoring.
"""
import argparse
import json
from pathlib import Path
import re
import signal
import sys

from publication_workers import (CONDITIONS, RUNS, TASK_KEY, evaluator_context,
    extend_input_manifest, get_task, load_private, validate_phase, verify_execution)


def source_manifest():
    from run_luna_publication import source_manifest as frozen_sources
    return frozen_sources()


def validate_run_dir(value, diagnostic=False):
    if diagnostic:
        raise ValueError("Publication pilot has no diagnostic scoring mode")
    return validate_phase(value)


def make_evaluator(condition):
    if condition not in CONDITIONS:
        raise ValueError("Unknown publication evaluation condition")
    adapter = load_private("selector_evaluator.py")
    adapter.RUNS, adapter.get_task = RUNS, get_task
    adapter.validate_run_dir, adapter.source_manifest = validate_run_dir, source_manifest
    adapter.verify_execution, adapter.adapter_context = verify_execution, evaluator_context
    adapter.__file__ = __file__
    old_private = adapter.load_private

    def dependencies(filename):
        owner = old_private(filename)
        if filename == "luna_integrator_evaluator.py":
            # Its worker and cache verifier call module-global input_manifest,
            # rather than the SimpleNamespace returned by make_evaluator.
            prior = owner.input_manifest

            def manifest(phase, task, patches, diagnostic=False):
                return extend_input_manifest(prior(phase, task, patches, diagnostic), phase)

            owner.input_manifest = manifest
        return owner

    adapter.load_private = dependencies
    result = adapter.make_evaluator(condition)
    if condition != "integrator":
        implementation = result._implementation
        prior = implementation.input_manifest

        def manifest(phase, task, patches):
            return extend_input_manifest(prior(phase, task, patches), phase)

        implementation.input_manifest = manifest
        # The public function was captured before replacing the module global.
        def protected_manifest(*args, **kwargs):
            with evaluator_context():
                return manifest(*args, **kwargs)
        result.input_manifest = protected_manifest
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
                raise ValueError("Invalid publication evaluator attempt directory")
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
