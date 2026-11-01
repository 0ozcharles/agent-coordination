"""Grade a predesignated agent1 submission with unchanged native feature tests.

Primary: --run-dir <.../jobs/<job>/integrator>
Optional post-run diagnostic: --run-dir <.../strongshare> --diagnose-strongshare-lead
The diagnostic writes only strongshare/lead_only_diagnostic/. Neither mode
merges, repairs, selects by score, or substitutes the peer's patch. test_solo is
only the shared single-patch scoring adapter, not a claim about active agents.
"""
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

from evaluate_solo_expansion import feature_metrics, file_info, ordinary_file
from evaluate_solo_smoke import (ROOT, REPO, COMMIT, DEADLINE_SECONDS, EXEC_TIMEOUT_SECONDS,
    evaluator_environment, make_backend, cleanup, stop_process, write_json)
from cross_task_specs import get_task

RUNS = ROOT / "artifacts/handoff_expansion_runs"
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
VARIANT = "designated_integrator_v1"
CAPTURE_SCHEMA = "authoritative_agent1_native_delivery_v1"
DIAGNOSTIC_DIR = "lead_only_diagnostic"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path):
    return json.loads(ordinary_file(Path(path)).read_text())


def source_manifest():
    from run_luna_timing import source_manifest as frozen_sources
    return {**frozen_sources(), **{name: file_info(ROOT / name)["sha256"] for name in
        ("run_luna_integrator.py", "luna_integrator_worker.py", "luna_integrator_evaluator.py")}}


def policy(diagnostic=False):
    return "diagnostic_agent1_only" if diagnostic else "authoritative_agent1_only"


def output_dir(phase, diagnostic=False):
    return phase / DIAGNOSTIC_DIR if diagnostic else phase


def patch_names(task, diagnostic=False):
    return ((f"agent{task.features[0]}.patch", f"agent{task.features[1]}.patch")
            if diagnostic else ("agent1.patch", "agent2.patch"))


def validate_run_dir(value, diagnostic=False):
    original = Path(value).expanduser().absolute()
    path = original.resolve(strict=True)
    try:
        parts = path.relative_to(RUNS.resolve()).parts
    except ValueError:
        parts = ()
    expected = "strongshare" if diagnostic else "integrator"
    if (original != path or not path.is_dir() or len(parts) != 5 or parts[2] != "jobs"
            or parts[4] != expected or parts[1] not in TASK_KEYS
            or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts)):
        raise ValueError("Expected a flat registered-task jobs/<job>/" + expected + " directory")
    return path


def check_protected_receipt(phase, receipt):
    protected = receipt.get("protected_files_sha256")
    if not isinstance(protected, dict) or not protected:
        raise ValueError("Execution receipt has no protected evidence")
    for relative, expected in protected.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid execution evidence path")
        if file_info(phase.parent / relative)["sha256"] != expected:
            raise ValueError("Execution evidence changed: " + relative)


def load_task(phase, diagnostic=False):
    validate_run_dir(phase, diagnostic)
    config, status = read(phase.parent / "task_config.json"), read(phase / "status.json")
    task = get_task(config.get("task_key"))
    if (task.key not in TASK_KEYS or config.get("task") != task.as_dict()
            or phase.parent.parent.parent.name != task.key or config.get("condition") != phase.name
            or config.get("condition_version") != ("source_handoff_diff_v1" if diagnostic else VARIANT)
            or config.get("submission_policy") != ("native_cooperative_merge" if diagnostic else "authoritative_agent1_only")
            or type(config.get("offline_selftest")) is not bool
            or not re.fullmatch(r"[0-9a-f]{64}", str(config.get("protocol_sha256", "")))
            or status.get("status") != "completed" or status.get("task") != task.as_dict()
            or status.get("offline_selftest") != config["offline_selftest"]):
        raise ValueError("Completed integrator-study task/config identity is inconsistent")
    image = read(task.image_record_path)
    if (image.get("expected_base") != task.base or image.get("image", task.image) != task.image
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(image.get("Id", "")))):
        raise ValueError("Image record does not identify the immutable registered task image")
    if diagnostic:
        # Validate the original native score without changing it or choosing code from it.
        from luna_collaboration_evaluator import make_evaluator, resume_completed
        module = make_evaluator("strongshare")
        saved_task, saved_config = module.load_task(phase)
        if saved_task != task or saved_config != config or resume_completed(module, phase, task, config) is None:
            raise ValueError("The fixed SS lead diagnostic requires a completed unchanged primary evaluation")
        check_protected_receipt(phase, read(phase / "execution_receipt.json"))
    else:
        from luna_integrator_worker import verify_worker_artifacts
        receipt = verify_worker_artifacts(phase, task)
        if receipt != read(phase / "execution_receipt.json"):
            raise ValueError("Worker verification did not return the saved execution receipt")
        check_protected_receipt(phase, receipt)
    return task, config


def patch_info(phase, task, diagnostic=False):
    names = patch_names(task, diagnostic)
    patches = {name: {**file_info(phase / name), "empty": not (phase / name).read_bytes().strip()} for name in names}
    receipt = read(phase / "execution_receipt.json")
    hashes = {name: record["sha256"] for name, record in patches.items()}
    if receipt.get("patch_sha256") != hashes:
        raise ValueError("Both saved contributions must match the execution receipt")
    if not diagnostic:
        declaration = read(phase / "final_delivery.json")
        expected = {"schema": "predesignated_integrator_delivery_v1", "authoritative_agent": "agent1",
                    "scored_patch": "agent1.patch", "peer_patch": "agent2.patch",
                    "selection": "predesignated_agent1", "automatic_merge": False, "posthoc_selection": False,
                    "active_agents": ["agent1", "agent2"], "patch_sha256": hashes[names[0]],
                    "peer_patch_sha256": hashes[names[1]]}
        if any(declaration.get(key) != value for key, value in expected.items()):
            raise ValueError("Final-delivery declaration differs from fixed agent1 ownership")
    return patches


def contributions(phase, task, patches, diagnostic=False):
    names = patch_names(task, diagnostic)
    declaration = read(phase / "final_delivery.json") if not diagnostic else None
    return {"agent1": {"feature": task.features[0], "patch": patches[names[0]], "graded": True},
            "agent2": {"feature": task.features[1], "patch": patches[names[1]], "graded": False},
            "worker_delivery_declaration": declaration,
            "meaning": "Recorded publications are contribution evidence, not proof that the peer work was reused."}


def input_manifest(phase, task, patches, diagnostic=False):
    paths = [phase.parent / name for name in
             ("task_config.json", "checkpoint.json", "checkpoint_provenance.json", "shared_git.tar")]
    paths += [phase / name for name in ("status.json", "source_manifest.json", "restore_proof.json",
              "execution_receipt.json", "handoff_packet.json", "handoff_packet.txt", "handoff_delivery.json",
              "first_request_agent1.json", "first_request_agent2.json")]
    if diagnostic:
        paths += [phase / "evaluation_summary.json", phase / "evaluation.json"]
    else:
        paths.append(phase / "final_delivery.json")
    paths.append(task.image_record_path)
    return {"submission_policy": policy(diagnostic), "primary_outcome": not diagnostic,
            "authoritative_agent": "agent1", "patches": patches,
            "protected_files": {str(path): file_info(path) for path in paths},
            "evaluator_sources": source_manifest()}


def verify_upstream(task):
    head = subprocess.check_output(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        raise ValueError("Pinned CooperBench revision changed")
    subprocess.run(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                    "src/cooperbench/eval", "src/cooperbench/utils.py", str(task.task_dir.relative_to(REPO))],
                   check=True, capture_output=True)


def capture_evaluation(native, phase, attempt, task, patches, diagnostic=False):
    """Test agent1 once per feature and retain the exact native filtered bytes."""
    lead = Path(patches[patch_names(task, diagnostic)[0]]["path"])
    expected_bytes = native._filter_test_files(native._load_patch(lead) or "").encode()
    capture_dir = attempt / "evaluated_patches"
    capture_dir.mkdir()
    calls = []
    original_test = native._run_tests

    def recorded(sb, tests_name, patch_name, base):
        number = len(calls) + 1
        if (number not in (1, 2) or tests_name != f"tests{number}.patch"
                or patch_name != "solo.patch" or base != task.base):
            raise ValueError("Unexpected native single-patch scoring invocation")
        result = sb.exec("python", "-c", "import base64,pathlib;print(base64.b64encode(pathlib.Path('/patches/solo.patch').read_bytes()).decode())")
        if result.returncode:
            raise RuntimeError("Could not capture native agent1 test input")
        data = base64.b64decode(result.stdout_read().strip(), validate=True)
        if data != expected_bytes:
            raise ValueError("Native test input is not exactly the fixed agent1 patch after upstream test filtering")
        path = capture_dir / f"call_{number:03d}.patch"
        path.write_bytes(data)
        call = {"index": number, "feature": task.features[number - 1], "native_test_name": tests_name,
                "native_patch_name": patch_name, "base_commit": base, "patch": file_info(path)}
        calls.append(call)
        outcome = original_test(sb, tests_name, patch_name, base)
        call.update(passed=outcome.get("passed"), exit_code=outcome.get("exit_code"),
                    test_output_sha256=hashlib.sha256(outcome.get("output", "").encode()).hexdigest())
        return outcome

    native._run_tests = recorded
    try:
        result = native.test_solo(repo_name=task.repo, task_id=task.task_id,
            feature1_id=task.features[0], feature2_id=task.features[1], patch=lead,
            timeout=EXEC_TIMEOUT_SECONDS, backend="docker", dataset_dir=REPO / "dataset")
    finally:
        native._run_tests = original_test
    delivered = None
    if not result.get("error"):
        validate_calls(calls, result, task)
        path = attempt / "delivered.patch"
        path.write_bytes(expected_bytes)
        delivered = file_info(path)
    write_json(attempt / "evaluated_delivery.json", {
        "schema": CAPTURE_SCHEMA, "condition": phase.name, "submission_policy": policy(diagnostic),
        "primary_outcome": not diagnostic, "authoritative_agent": "agent1", "selection": "predesignated_agent1",
        "automatic_merge": False, "posthoc_selection": False, "peer_fallback": False,
        "task": task.as_dict(), "base_commit": task.base, "input_patches": patches,
        "contributions": contributions(phase, task, patches, diagnostic),
        "official_result_sha256": digest(result), "calls": calls, "delivered_patch": delivered,
        "model_calls": 0, "official_scoring_modified": False, "submitted_patch_bytes_modified": False,
        "native_test_file_filter_applied": True})
    return result


def validate_calls(calls, native, task):
    if len(calls) != 2:
        raise ValueError("Both official feature invocations must be captured")
    for i, call in enumerate(calls, 1):
        outcome = native.get(f"feature{i}") or {}
        output = outcome.get("test_output", outcome.get("output", ""))
        if (call["index"] != i or call["feature"] != task.features[i - 1]
                or call["native_test_name"] != f"tests{i}.patch" or call["native_patch_name"] != "solo.patch"
                or call["base_commit"] != task.base or call["passed"] != outcome.get("passed")
                or call["test_output_sha256"] != hashlib.sha256(output.encode()).hexdigest()):
            raise ValueError("Captured invocation differs from the native feature result")
    if calls[0]["patch"]["sha256"] != calls[1]["patch"]["sha256"]:
        raise ValueError("The two feature tests received different patches")


def delivery_summary(attempt):
    receipt = read(attempt / "evaluated_delivery.json")
    return {"schema": CAPTURE_SCHEMA, "submission_policy": receipt["submission_policy"],
            "selection": receipt["selection"], "authoritative_agent": "agent1",
            "manifest": file_info(attempt / "evaluated_delivery.json"), "delivered_patch": receipt["delivered_patch"]}


def validate_delivery(attempt, summary, native, phase, task, patches, diagnostic=False):
    if summary.get("evaluated_delivery") != delivery_summary(attempt):
        raise ValueError("Native delivery capture changed")
    receipt = read(attempt / "evaluated_delivery.json")
    expected = {"schema": CAPTURE_SCHEMA, "condition": phase.name, "submission_policy": policy(diagnostic),
                "primary_outcome": not diagnostic, "authoritative_agent": "agent1", "selection": "predesignated_agent1",
                "automatic_merge": False, "posthoc_selection": False, "peer_fallback": False,
                "task": task.as_dict(), "base_commit": task.base, "input_patches": patches,
                "contributions": contributions(phase, task, patches, diagnostic),
                "official_result_sha256": digest(native), "model_calls": 0, "official_scoring_modified": False,
                "submitted_patch_bytes_modified": False, "native_test_file_filter_applied": True}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Delivery metadata differs from fixed agent1 scoring")
    validate_calls(receipt["calls"], native, task)
    for i, call in enumerate(receipt["calls"], 1):
        if call["patch"] != file_info(attempt / "evaluated_patches" / f"call_{i:03d}.patch"):
            raise ValueError("Captured test-input patch changed")
    delivered = file_info(attempt / "delivered.patch")
    if receipt["delivered_patch"] != delivered or delivered["sha256"] != receipt["calls"][0]["patch"]["sha256"]:
        raise ValueError("Saved delivery is not the complete actually tested patch")


def worker(phase, attempt, diagnostic=False):
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    import docker
    import cooperbench.eval.backends.docker as backend_module
    import cooperbench.eval.sandbox as native
    task, config = load_task(phase, diagnostic)
    patches = patch_info(phase, task, diagnostic)
    before = input_manifest(phase, task, patches, diagnostic)
    image = read(task.image_record_path)
    client = docker.from_env(timeout=EXEC_TIMEOUT_SECONDS)
    old_backend = native.get_backend
    try:
        actual = client.images.get(task.image)
        if actual.id != image["Id"] or actual.attrs["Architecture"] != image["Architecture"]:
            raise ValueError("Task image differs from the pinned image")
        verify_upstream(task)
        write_json(attempt / "evaluation_input.json", {
            "condition": phase.name, "task": task.as_dict(), "protocol_sha256": config["protocol_sha256"],
            "input_manifest": before, "image_id": image["Id"], "base_commit": task.base,
            "native_setting": "solo", "single_patch_scoring_adapter": True,
            "official_evaluator": "cooperbench.eval.sandbox.test_solo", "submission_policy": policy(diagnostic),
            "primary_outcome": not diagnostic, "model_calls": 0, "credentials_inherited": False})
        backend = make_backend(backend_module, client, image["Id"], phase, attempt, expected_image=task.image)
        create = backend.create_sandbox

        def checked_create(image_name, timeout=600, workdir="/workspace"):
            sandbox = create(image_name, timeout=timeout, workdir=workdir)
            check = sandbox.exec("git", "-C", "/workspace/repo", "rev-parse", "HEAD")
            if check.returncode or check.stdout_read().strip() != task.base:
                sandbox.terminate()
                raise ValueError("Evaluator sandbox does not contain the pinned task base")
            write_json(attempt / "image_base_validation.json", {"matched": True, "base_commit": task.base, "image_id": image["Id"]})
            return sandbox

        backend.create_sandbox = checked_create

        def fixed_backend(name="docker"):
            if name != "docker":
                raise ValueError("Only the pinned local Docker evaluator is allowed")
            return backend

        native.get_backend = fixed_backend
        official = capture_evaluation(native, phase, attempt, task, patches, diagnostic)
        write_json(attempt / "official_evaluation.json", official)
        result = copy.deepcopy(official)
        after = input_manifest(phase, task, patch_info(phase, task, diagnostic), diagnostic)
        unchanged = before == after
        try:
            load_task(phase, diagnostic)
            verify_upstream(task)
        except Exception:
            unchanged = False
        write_json(attempt / "integrity.json", {"unchanged": unchanged, "before": before, "after": after})
        if not unchanged:
            result.update(error="Inputs or scoring sources changed during evaluation", both_passed=False)
        write_json(attempt / "evaluation.json", result)
        return 2 if result.get("error") else 0
    finally:
        native.get_backend = old_backend
        client.close()


def summarize(result, status, phase, attempt, elapsed, task=None, config=None, diagnostic=False):
    features = list(task.features) if task else []
    metrics = {str(f): feature_metrics(result.get(f"feature{i}") or {}) for i, f in enumerate(features, 1)}
    summary = {"condition": phase.name, "task_key": task.key if task else None,
        "task": task.as_dict() if task else None, "features": features,
        "offline_selftest": (config or {}).get("offline_selftest"), "protocol_sha256": (config or {}).get("protocol_sha256"),
        "condition_version": (config or {}).get("condition_version"), "submission_policy": policy(diagnostic),
        "primary_outcome": not diagnostic, "fresh_solo": False, "active_agent_count": 2,
        "authoritative_agent": "agent1", "peer_patch_graded": False, "automatic_merge": False, "posthoc_selection": False,
        "status": status, "evaluation_completed": status in ("passed", "failed"),
        "both_passed": status == "passed" and bool(result.get("both_passed")), "feature_results": metrics,
        "native_setting": "solo", "single_patch_scoring_adapter": True,
        "merge_status": "not_applicable", "merge_strategy": "not_applicable", "lead_patch_fallback_used": False,
        "error": result.get("error"), "cleanup_error": result.get("cleanup_error"),
        "elapsed_seconds": round(elapsed, 3), "watchdog_seconds": DEADLINE_SECONDS, "model_calls": 0,
        "attempt_directory": str(attempt), "official_raw_result": str(attempt / "official_evaluation.json"),
        "input_manifest": str(attempt / "evaluation_input.json"), "integrity_check": str(attempt / "integrity.json"),
        "interpretation": ("Prespecified SS agent1-only sidecar; native SS primary score remains unchanged. Never choose the better result."
            if diagnostic else "Two agents cooperate; predesignated agent1 alone owns the graded final artifact. Same native single-patch feature tests as Consolidate, with no merge, repair, or peer fallback.")}
    for f in features:
        summary[f"feature_{f}_passed"] = metrics[str(f)]["passed"]
    if (attempt / "evaluated_delivery.json").is_file():
        summary["evaluated_delivery"] = delivery_summary(attempt)
        summary["contributions"] = read(attempt / "evaluated_delivery.json")["contributions"]
    elif status in ("passed", "failed"):
        raise ValueError("Completed official scoring lacks its complete native input capture")
    return summary


def resume_completed(module, phase, task, config):
    diagnostic = module.diagnostic
    output = output_dir(phase, diagnostic)
    path = output / "evaluation_summary.json"
    if not path.exists() and not path.is_symlink():
        return None
    summary = read(path)
    if not summary.get("evaluation_completed"):
        return None
    original = Path(summary.get("attempt_directory", ""))
    attempt = original.resolve(strict=True)
    if original != attempt or attempt.parent != output / "evaluation_attempts":
        raise ValueError("Saved evaluator attempt is outside this scoring policy")
    patches = patch_info(phase, task, diagnostic)
    current = input_manifest(phase, task, patches, diagnostic)
    entry, integrity = read(attempt / "evaluation_input.json"), read(attempt / "integrity.json")
    if (entry.get("input_manifest") != current or entry.get("task") != task.as_dict()
            or entry.get("protocol_sha256") != config["protocol_sha256"]
            or entry.get("submission_policy") != policy(diagnostic) or entry.get("primary_outcome") is not (not diagnostic)
            or integrity != {"unchanged": True, "before": current, "after": current}):
        raise ValueError("Saved score inputs, policy or sources changed")
    verify_upstream(task)
    native = read(attempt / "official_evaluation.json")
    if (native != read(attempt / "evaluation.json") or native != read(output / "evaluation.json")
            or native.get("error") or native.get("cleanup_error")):
        raise ValueError("Saved result differs from the unchanged native score")
    expected = summarize(native, "passed" if native.get("both_passed") else "failed", phase, attempt,
                         summary.get("elapsed_seconds", 0), task, config, diagnostic)
    if summary != expected or read(attempt / "evaluation_summary.json") != summary:
        raise ValueError("Saved summary differs from its native score or artifact policy")
    validate_delivery(attempt, summary, native, phase, task, patches, diagnostic)
    return summary


def supervise(phase, diagnostic=False):
    output = output_dir(phase, diagnostic)
    attempt = output / "evaluation_attempts" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True)
    started, env = time.monotonic(), evaluator_environment()
    result, proc, task, config, status = None, None, None, None, "error"
    try:
        task, config = load_task(phase, diagnostic)
        patch_info(phase, task, diagnostic)
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--run-dir", str(phase), "--worker-attempt", str(attempt)]
        if diagnostic:
            command.append("--diagnose-strongshare-lead")
        with (attempt / "console.log").open("wb") as log:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=DEADLINE_SECONDS)
            except subprocess.TimeoutExpired:
                code, status = 2, "timeout"
                result = {"error": f"Evaluation exceeded {DEADLINE_SECONDS} seconds", "both_passed": False}
            finally:
                stop_process(proc)
        if result is None:
            result = read(attempt / "evaluation.json") if (attempt / "evaluation.json").is_file() else {
                "error": f"Worker exited {code} without a result; see console.log", "both_passed": False}
            status = "error" if code or result.get("error") else "passed" if result.get("both_passed") else "failed"
    except BaseException as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "both_passed": False}
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "error"
    finally:
        if proc is not None:
            stop_process(proc)
            try:
                cleanup(phase, attempt, env)
            except Exception as exc:
                write_json(attempt / "cleanup.json", {"error_type": type(exc).__name__, "error": str(exc)})
                result = result or {"both_passed": False}
                result["cleanup_error"] = str(exc)
                status = "error"
        result = result or {"error": "No evaluator result", "both_passed": False}
        write_json(attempt / "evaluation.json", result)
        summary = summarize(result, status, phase, attempt, time.monotonic() - started, task, config, diagnostic)
        write_json(attempt / "evaluation_summary.json", summary)
        write_json(output / "evaluation.json", result)
        write_json(output / "evaluation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status in ("passed", "failed") else 2


def make_evaluator(condition="integrator", *, diagnostic=False):
    if condition != ("strongshare" if diagnostic else "integrator"):
        raise ValueError("Evaluator mode and condition disagree")
    return SimpleNamespace(diagnostic=diagnostic, __file__=str(Path(__file__).resolve()),
        load_task=lambda phase: load_task(phase, diagnostic),
        patch_info=lambda phase, task: patch_info(phase, task, diagnostic),
        input_manifest=lambda phase, task, patches: input_manifest(phase, task, patches, diagnostic),
        source_manifest=source_manifest, supervise=lambda phase: supervise(phase, diagnostic))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", "--phase-dir", dest="run_dir", required=True)
    parser.add_argument("--diagnose-strongshare-lead", action="store_true")
    parser.add_argument("--worker-attempt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        diagnostic = args.diagnose_strongshare_lead
        phase = validate_run_dir(args.run_dir, diagnostic)
        if args.worker_attempt:
            original = Path(args.worker_attempt).absolute()
            attempt = original.resolve(strict=True)
            if (original != attempt or not attempt.is_dir()
                    or attempt.parent != output_dir(phase, diagnostic) / "evaluation_attempts"
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", attempt.name)):
                raise ValueError("Invalid evaluator attempt path")
            return worker(phase, attempt, diagnostic)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        module = make_evaluator(phase.name, diagnostic=diagnostic)
        task, config = module.load_task(phase)
        saved = resume_completed(module, phase, task, config)
        if saved is not None:
            print(json.dumps({**saved, "reused_completed_evaluation": True}, ensure_ascii=False))
            return 0
        return module.supervise(phase)
    except Exception as exc:
        print(json.dumps({"status": "error", "evaluation_completed": False,
                          "error": f"{type(exc).__name__}: {exc}", "model_calls": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
