"""Native collaboration evaluation with an exact, evaluator-only delivery capture.

CLI: --run-dir (or --phase-dir) <.../jobs/<job>/strongshare|consolidate>
The native test/merge/fallback decisions are unchanged. Full patch bytes are
copied immediately before each native test invocation, never from merge.diff.
No model is called and no supplemental test is exposed to an agent.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import signal
import sys

import evaluate_cross_task as original
from evaluate_solo_expansion import file_info, ordinary_file
from evaluate_solo_smoke import ROOT, write_json

SCHEMA = "native_evaluated_delivery_v1"
CONDITIONS = ("strongshare", "consolidate")
ALLOWED_PATCHES = {"merged.patch", "patch1.patch", "solo.patch"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_manifest():
    result = original.source_manifest()
    result[str(Path(__file__).resolve().relative_to(ROOT))] = file_info(Path(__file__).resolve())["sha256"]
    return result


def validate_run_dir(value):
    path = original.validate_run_dir(value)
    if path.name not in CONDITIONS:
        raise ValueError("This evaluator accepts StrongShare or Consolidate only")
    return path


def select_calls(condition, native, calls):
    """Select only the deliverable that produced the official feature results."""
    if native.get("error") or native.get("cleanup_error"):
        return "native_error", []
    if condition == "consolidate":
        mode, filename = "single_patch", "solo.patch"
    else:
        merge = native.get("merge") or {}
        pair = merge.get("status"), merge.get("strategy")
        if pair == ("identical", "skip-merge-identical"):
            mode, filename = "identical_normalized", "merged.patch"
        elif pair == ("clean", "naive"):
            mode, filename = "native_naive_merge", "merged.patch"
        elif pair[0] in ("conflicts", "missing_input") and pair[1] == "solo-agent1" and native.get("both_passed") is True:
            mode, filename = "native_lead_fallback", "patch1.patch"
        elif pair[0] in ("conflicts", "missing_input") and pair[1] == "naive" and not native.get("both_passed"):
            return "no_deliverable_" + pair[0], []
        else:
            raise ValueError("Unknown official merge/fallback decision")
    selected = [c for c in calls if c["native_patch_name"] == filename]
    if len(selected) != 2 or [c["native_test_name"] for c in selected] != ["tests1.patch", "tests2.patch"]:
        raise ValueError("Official deliverable must have two captured feature-test invocations")
    if selected[0]["patch"]["sha256"] != selected[1]["patch"]["sha256"]:
        raise ValueError("Official feature tests did not receive identical patch bytes")
    for i, call in enumerate(selected, 1):
        feature = native.get(f"feature{i}") or {}
        output = feature.get("test_output", feature.get("output", ""))
        if (call.get("passed") != feature.get("passed")
                or call.get("test_output_sha256") != hashlib.sha256(output.encode()).hexdigest()):
            raise ValueError("Captured invocation does not match its official feature result")
    return mode, selected


def capture_evaluation(native_module, invoke, condition, task, patches, attempt):
    """Wrap one native evaluation; invoke() still calls its original scorer."""
    capture_dir = attempt / "evaluated_patches"
    capture_dir.mkdir()
    calls = []
    original_run_tests = native_module._run_tests

    def recorded_test(sb, tests_patch, feature_patch, base_sha):
        if (tests_patch not in ("tests1.patch", "tests2.patch")
                or feature_patch not in ALLOWED_PATCHES or base_sha != task.base):
            raise ValueError("Unexpected native feature test or task base")
        result = sb.exec("python", "-c",
                         "import base64,pathlib,sys; print(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode())",
                         "/patches/" + feature_patch)
        if result.returncode:
            raise RuntimeError("Could not capture the complete native test input patch")
        data = base64.b64decode(result.stdout_read().strip(), validate=True)
        path = capture_dir / f"call_{len(calls) + 1:03d}.patch"
        path.write_bytes(data)
        call = {"index": len(calls) + 1, "native_test_name": tests_patch,
                "feature": task.features[0 if tests_patch == "tests1.patch" else 1],
                "native_patch_name": feature_patch, "base_commit": base_sha,
                "patch": file_info(path)}
        calls.append(call)
        outcome = original_run_tests(sb, tests_patch, feature_patch, base_sha)
        call.update(passed=outcome.get("passed"), exit_code=outcome.get("exit_code"),
                    test_output_sha256=hashlib.sha256(outcome.get("output", "").encode()).hexdigest())
        return outcome

    native_module._run_tests = recorded_test
    try:
        native = invoke()
    finally:
        native_module._run_tests = original_run_tests
    mode, selected = select_calls(condition, native, calls)
    delivered = None
    if selected:
        path = attempt / "delivered.patch"
        path.write_bytes(Path(selected[0]["patch"]["path"]).read_bytes())
        delivered = file_info(path)
    receipt = {"schema": SCHEMA, "condition": condition, "task": task.as_dict(),
               "base_commit": task.base, "input_patches": patches,
               "official_result_sha256": digest(native), "selection": mode,
               "calls": calls, "selected_call_indices": [c["index"] for c in selected],
               "delivered_patch": delivered, "model_calls": 0,
               "official_scoring_modified": False, "patch_bytes_modified": False,
               "interpretation": "Full native feature-patch bytes captured before scoring. No reconstruction from the truncated merge.diff, no member fallback, and no reference replacement."}
    write_json(attempt / "evaluated_delivery.json", receipt)
    return native


def delivery_summary(attempt):
    path = attempt / "evaluated_delivery.json"
    receipt = json.loads(ordinary_file(path).read_text())
    return {"schema": SCHEMA, "manifest": file_info(path), "selection": receipt["selection"],
            "delivered_patch": receipt["delivered_patch"]}


def validate_delivery(attempt, summary, native, condition, task, patches):
    """Check complete saved captures before accepting a cached official score."""
    saved = summary.get("evaluated_delivery")
    if not isinstance(saved, dict) or saved != delivery_summary(attempt):
        raise ValueError("Delivery capture manifest changed or is missing")
    receipt = json.loads((attempt / "evaluated_delivery.json").read_text())
    if (receipt.get("schema") != SCHEMA or receipt.get("condition") != condition
            or receipt.get("task") != task.as_dict() or receipt.get("base_commit") != task.base
            or receipt.get("input_patches") != patches
            or receipt.get("official_result_sha256") != digest(native)
            or receipt.get("model_calls") != 0
            or receipt.get("official_scoring_modified") is not False
            or receipt.get("patch_bytes_modified") is not False):
        raise ValueError("Delivery capture does not describe this native evaluation")
    calls = receipt.get("calls", [])
    if not isinstance(calls, list):
        raise ValueError("Invalid captured native invocations")
    for index, call in enumerate(calls, 1):
        expected = attempt / "evaluated_patches" / f"call_{index:03d}.patch"
        if (call.get("index") != index or call.get("base_commit") != task.base
                or call.get("patch") != file_info(expected)):
            raise ValueError("Captured native patch changed")
    mode, selected = select_calls(condition, native, calls)
    if receipt.get("selection") != mode or receipt.get("selected_call_indices") != [c["index"] for c in selected]:
        raise ValueError("Delivery selection differs from the official decision")
    delivered = file_info(attempt / "delivered.patch") if selected else None
    if receipt.get("delivered_patch") != delivered:
        raise ValueError("Selected delivery patch changed")
    if selected and delivered["sha256"] != selected[0]["patch"]["sha256"]:
        raise ValueError("Selected delivery differs from the actual tested bytes")
    if not selected and (attempt / "delivered.patch").exists():
        raise ValueError("No deliverable was selected, but a delivery patch is present")
    return receipt


def make_evaluator(condition):
    if condition not in CONDITIONS:
        raise ValueError("Only StrongShare and Consolidate are supported")
    module = original.make_evaluator(condition)
    module.__file__ = str(Path(__file__).resolve())
    module.source_manifest = source_manifest
    module.validate_run_dir = validate_run_dir
    old_worker, old_evaluate, old_summary = module.worker, module.evaluate_saved_patches, module.summarize
    state = {}

    def worker(run_dir, attempt_dir):
        state["attempt"] = attempt_dir
        try:
            return old_worker(run_dir, attempt_dir)
        finally:
            state.clear()

    def evaluate_saved_patches(native, run_dir, task, patches):
        return capture_evaluation(native, lambda: old_evaluate(native, run_dir, task, patches),
                                  condition, task, patches, state["attempt"])

    def summarize(result, status, run_dir, attempt_dir, elapsed, task=None, config=None):
        summary = old_summary(result, status, run_dir, attempt_dir, elapsed, task, config)
        if (attempt_dir / "evaluated_delivery.json").is_file():
            summary["evaluated_delivery"] = delivery_summary(attempt_dir)
        elif status in ("passed", "failed"):
            raise ValueError("Completed native evaluation has no delivery capture")
        return summary

    module.worker, module.evaluate_saved_patches, module.summarize = worker, evaluate_saved_patches, summarize
    module.resume_completed = lambda run_dir, task, config: resume_completed(module, run_dir, task, config)
    return module


def resume_completed(module, run_dir, task, config):
    summary = original.resume_completed(module, run_dir, task, config)
    if summary is not None:
        attempt = Path(summary["attempt_directory"])
        native = json.loads(ordinary_file(attempt / "official_evaluation.json").read_text())
        validate_delivery(attempt, summary, native, run_dir.name, task, module.patch_info(run_dir, task))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", "--phase-dir", dest="run_dir", required=True)
    parser.add_argument("--worker-attempt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        run_dir = validate_run_dir(args.run_dir)
        module = make_evaluator(run_dir.name)
        if args.worker_attempt:
            original_path = Path(args.worker_attempt).absolute()
            attempt = original_path.resolve(strict=True)
            if (original_path != attempt or attempt.parent != run_dir / "evaluation_attempts"
                    or not attempt.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]+", attempt.name)):
                raise ValueError("Invalid evaluator attempt directory")
            return module.worker(run_dir, attempt)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        task, config = module.load_task(run_dir)
        prior = resume_completed(module, run_dir, task, config)
        if prior is not None:
            print(json.dumps({**prior, "reused_completed_evaluation": True}, ensure_ascii=False))
            return 0
        return module.supervise(run_dir)
    except Exception as exc:
        print(json.dumps({"status": "error", "evaluation_completed": False,
                          "error": f"{type(exc).__name__}: {exc}", "model_calls": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
