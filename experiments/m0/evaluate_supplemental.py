"""Post-run common scoring without model calls or changes to primary results.

Usage: python evaluate_supplemental.py --condition-dir <keep|strongshare|consolidate>
Exit 0: completed, including failed probes or no deliverable after merge conflict.
Exit 2: input, integrity, infrastructure, timeout, or cleanup error.
"""
import argparse
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT / "CooperBench"
SUITE = ROOT / "supplemental_probe_v1.py"
COMMIT = "63b9d44d9f39a02fccf5bf0052db48a917a011fd"
BASE = "a2920752fd111c2d52c88eb57487667b3cf0ea7b"
TIMEOUT = 180
LABEL = "agent-rsi-supplemental-attempt"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def clean_environment():
    result = {k: os.environ[k] for k in ("HOME", "USER", "TMPDIR", "LANG", "LC_ALL") if k in os.environ}
    result.update(PATH="/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                  DOCKER_HOST="unix://" + str(Path.home() / ".docker/run/docker.sock"),
                  PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                  LITELLM_LOCAL_MODEL_COST_MAP="True", MSWEA_GLOBAL_CONFIG_DIR=str(ROOT / "config/mini-swe-agent"))
    return result


def validate_condition(value):
    path = Path(value).expanduser().resolve(strict=True)
    if path.name not in {"keep", "strongshare", "consolidate"} or not path.is_dir() or not path.is_relative_to(ROOT / "artifacts"):
        raise ValueError("--condition-dir must be a completed keep/strongshare/consolidate directory under artifacts")
    return path


def ordinary_file(path, boundary):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(boundary):
        raise ValueError("Expected an ordinary artifact file within the condition: " + str(path))
    return path


def inspect_inputs(condition):
    summary_path = ordinary_file(condition / "evaluation_summary.json", condition)
    result_path = ordinary_file(condition / "evaluation.json", condition)
    status_path = ordinary_file(condition / "status.json", condition)
    summary = json.loads(summary_path.read_text())
    result = json.loads(result_path.read_text())
    if json.loads(status_path.read_text()).get("status") != "completed":
        raise ValueError("Model execution is not completed; supplemental tests must not feed back into it")
    if not summary.get("evaluation_completed") or summary.get("error") or summary.get("cleanup_error") or result.get("error"):
        raise ValueError("A completed, valid official evaluation is required")
    if bool(result.get("both_passed")) != bool(summary.get("both_passed")):
        raise ValueError("Official result and summary disagree")
    attempt_result = ordinary_file(summary["full_evaluator_result"], condition / "evaluation_attempts")
    if json.loads(attempt_result.read_text()) != result:
        raise ValueError("Top-level official result differs from its recorded attempt")
    input_path = ordinary_file(attempt_result.parent / "evaluation_input.json", condition)
    inputs = json.loads(input_path.read_text())
    expected_image = json.loads((ROOT / "artifacts/task_image.json").read_text())
    if inputs.get("image_id") != expected_image["Id"] or inputs.get("cooperbench_commit") != COMMIT or inputs.get("features") != [1, 6]:
        raise ValueError("Official evaluator image, code, or task scope is not the pinned experiment")
    protected = [summary_path, result_path, status_path, attempt_result, input_path]
    raw = attempt_result.parent / "official_evaluation.json"
    if raw.exists():
        protected.append(ordinary_file(raw, condition))
    patches = {}
    names = ["final.patch"] if condition.name == "consolidate" else ["agent1.patch", "agent6.patch"]
    for name in names:
        path = ordinary_file(condition / name, condition)
        saved = inputs.get("patch", {}) if name == "final.patch" else inputs.get("patches", {}).get(path.stem, {})
        if saved.get("sha256") != sha(path):
            raise ValueError("Accepted patch differs from official evaluator input: " + name)
        patches[name] = {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}
        protected.append(path)
    return {"official": summary, "result": result, "patches": patches, "image": expected_image,
            "protected_hashes": {str(p): sha(p) for p in protected}}


def selection(info, condition_name):
    if condition_name == "consolidate":
        return "single_patch", None
    merge = info["result"].get("merge") or {}
    status, strategy = merge.get("status"), merge.get("strategy")
    if strategy == "solo-agent1" and info["official"]["both_passed"]:
        return "native_lead_fallback", None
    if status == "conflicts":
        return None, "not_evaluable_due_to_merge_conflict"
    if status == "missing_input":
        return None, "not_evaluable_due_to_missing_input"
    if status == "identical" and strategy == "skip-merge-identical":
        return "identical_normalized", None
    if status == "clean" and strategy == "naive":
        return "native_naive_merge", None
    raise ValueError("Unrecognized official delivery strategy; no replacement artifact will be invented")


def base_summary(condition, info):
    mode, reason = selection(info, condition.name)
    return {"condition": condition.name, "condition_directory": str(condition), "suite_id": "jinja-task1621-supplemental-v1",
            "suite_sha256": sha(SUITE), "official_both_passed": bool(info["official"]["both_passed"]),
            "official_merge_status": info["official"].get("merge_status"),
            "official_merge_strategy": info["official"].get("merge_strategy"),
            "delivery_mode": mode, "native_lead_fallback": mode == "native_lead_fallback",
            "patch_provenance": info["patches"], "protected_hashes_before": info["protected_hashes"],
            "model_api_calls": 0, "gold_patches_used": False, "model_feedback": False,
            "supplemental_evaluation_completed": bool(reason), "supplemental_passed": None,
            "enhanced_passed": False, "status": reason or "initializing", "tests": [],
            "tests_total": 0, "tests_passed": 0, "timeout_seconds": TIMEOUT}


def upload(container, names):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name, data in names.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            bundle.addfile(entry, io.BytesIO(data))
    if not container.put_archive("/patches", archive.getvalue()):
        raise RuntimeError("Docker refused supplemental input archive")


class Sandbox:
    """Small recording adapter for the pinned native merge helper interface."""
    def __init__(self, container, attempt):
        self.container, self.attempt, self.number = container, attempt, 0

    def exec(self, *args):
        from cooperbench.eval.backends.docker import DockerExecResult
        self.number += 1
        response = self.container.exec_run(list(args), workdir="/workspace/repo")
        (self.attempt / f"exec_{self.number:03d}.log").write_bytes(response.output)
        write_json(self.attempt / f"exec_{self.number:03d}.json", {"argv": args, "returncode": response.exit_code})
        return DockerExecResult(response.exit_code, response.output)

    def checked(self, *args):
        result = self.exec(*args)
        if result.returncode:
            raise RuntimeError("Supplemental sandbox command failed; see exec logs")
        return result.stdout_read()


def delivered_patch(sb, native, patches, mode, info):
    filtered = {name: native._filter_test_files(native._load_patch(Path(value["path"])) or "")
                for name, value in patches.items()}
    if mode == "single_patch":
        return filtered["final.patch"], {"normalization": "native_sanitize_and_test_file_filter"}
    if mode == "native_lead_fallback":
        return filtered["agent1.patch"], {"normalization": "native_sanitize_and_test_file_filter", "selected_by": "official_evaluator"}
    upload(sb.container, {"patch1.patch": filtered["agent1.patch"].encode(), "patch2.patch": filtered["agent6.patch"].encode()})
    setup = native._setup_branches(sb)
    if setup.get("error") or setup.get("base_sha") != BASE:
        raise RuntimeError("Could not reconstruct official merge setup")
    if mode == "native_naive_merge":
        if setup["apply_status"] != info["result"].get("apply_status"):
            raise RuntimeError("Reconstructed patch application differs from official evaluation")
        merge = native._merge_naive(sb, BASE)
        if merge["conflict"] or "MERGE_STATUS=clean" not in merge["output"]:
            raise RuntimeError("Previously accepted clean merge cannot be reconstructed")
        if merge["diff"][:5000] != info["result"]["merge"].get("diff", ""):
            raise RuntimeError("Reconstructed merged diff differs from official recorded diff")
        return merge["diff"], {"normalization": "pinned_native_setup_branches_and_merge_naive", "apply_status": setup["apply_status"]}
    if not filtered["agent1.patch"] or filtered["agent1.patch"] != filtered["agent6.patch"]:
        raise RuntimeError("Official identical strategy no longer has identical nonempty inputs")
    normalized = sb.checked("bash", "-c", f"""
cd /workspace/repo
git checkout {BASE} 2>&1 >/dev/null
git checkout -b identical-merge 2>&1 >/dev/null
if git apply /patches/patch1.patch 2>/dev/null || git apply --recount /patches/patch1.patch 2>/dev/null; then
    git add -A
    git commit -m 'merged' --allow-empty >/dev/null 2>&1
    git diff {BASE} HEAD > /patches/merged.patch
    echo NORMALIZED
else
    cp /patches/patch1.patch /patches/merged.patch
    echo RAW
fi
""")
    return sb.checked("cat", "/patches/merged.patch"), {"normalization": normalized.strip(), "policy": "pinned_native_identical_patch_normalization"}


def worker(condition, attempt):
    safe = clean_environment()
    os.environ.clear()
    os.environ.update(safe)
    import docker
    import cooperbench.eval.sandbox as native
    info = inspect_inputs(condition)
    summary = base_summary(condition, info)
    if summary["supplemental_evaluation_completed"]:
        write_json(attempt / "worker_summary.json", summary)
        return 0
    head = subprocess.check_output(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        raise RuntimeError("Pinned native evaluator commit changed")
    subprocess.run(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--", "src/cooperbench/eval",
                    "dataset/pallets_jinja_task/task1621/runner.sh"], check=True, capture_output=True)
    summary["native_evaluator_sha256"] = sha(REPO / "src/cooperbench/eval/sandbox.py")
    client = docker.from_env(timeout=60)
    container = None
    try:
        image = client.images.get(info["image"]["Id"])
        if image.attrs["Architecture"] != info["image"]["Architecture"]:
            raise RuntimeError("Pinned image architecture differs")
        container = client.containers.run(image.id, entrypoint=["sleep"], command=[str(TIMEOUT + 60)],
                    detach=True, init=True, network_disabled=True, mem_limit="2g", nano_cpus=2_000_000_000,
                    pids_limit=128, labels={LABEL: attempt.name, "agent-rsi-run": condition.parent.name + "-supplemental"})
        write_json(attempt / "container.json", {"id": container.id, "image_id": image.id})
        sb = Sandbox(container, attempt)
        if sb.checked("git", "rev-parse", "HEAD").strip() != BASE:
            raise RuntimeError("Task container base differs")
        sb.checked("mkdir", "-p", "/patches")
        content, details = delivered_patch(sb, native, info["patches"], summary["delivery_mode"], info)
        (attempt / "delivered.patch").write_text(content)
        summary["delivery_reconstruction"] = details
        summary["delivered_patch_sha256"] = sha(attempt / "delivered.patch")
        upload(container, {"delivered.patch": content.encode(), "supplemental_probe_v1.py": SUITE.read_bytes()})
        sb.checked("bash", "-c", f"git checkout --force {BASE} && git reset --hard {BASE} && git clean -fdx")
        # These are the task runner's exact feature-patch application flags.
        if content.strip():
            sb.checked("bash", "-c", "git apply --ignore-whitespace --ignore-space-change /patches/delivered.patch || git apply --3way /patches/delivered.patch")
        summary["delivered_worktree_diff_sha256"] = hashlib.sha256(sb.checked("git", "diff", "--binary", BASE).encode()).hexdigest()
        output = sb.checked("python", "/patches/supplemental_probe_v1.py")
        (attempt / "probe_output.json").write_text(output)
        probe = json.loads(output)
        load_failed = bool(probe.get("artifact_load_error")) and probe.get("tests_total") == 0 and probe.get("passed") is False
        if probe.get("suite_id") != summary["suite_id"] or (probe.get("tests_total") != 5 and not load_failed):
            raise RuntimeError("Supplemental suite output is incomplete")
        if load_failed:
            summary["artifact_load_error"] = probe["artifact_load_error"]
        summary.update(supplemental_evaluation_completed=True, supplemental_passed=bool(probe["passed"]),
                       enhanced_passed=summary["official_both_passed"] and bool(probe["passed"]),
                       status="passed" if probe["passed"] else "failed", tests=probe["checks"],
                       tests_total=probe["tests_total"], tests_passed=probe["tests_passed"])
        return 0
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
        write_json(attempt / "worker_summary.json", summary)


def cleanup(attempt):
    import docker
    client = docker.from_env(timeout=20)
    try:
        removed = []
        for container in client.containers.list(all=True, filters={"label": LABEL + "=" + attempt.name}):
            removed.append(container.id)
            container.remove(force=True)
        if client.containers.list(all=True, filters={"label": LABEL + "=" + attempt.name}):
            raise RuntimeError("Supplemental containers remain after cleanup")
        write_json(attempt / "cleanup.json", {"removed_remaining_containers": removed, "all_removed": True})
    finally:
        client.close()


def supervise(condition):
    attempt = condition / "supplemental_attempts" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True)
    started, proc = time.monotonic(), None
    summary = {"condition": condition.name, "suite_sha256": sha(SUITE), "status": "error", "official_both_passed": None,
               "supplemental_evaluation_completed": False, "supplemental_passed": None, "enhanced_passed": False, "model_api_calls": 0}
    before = {}
    try:
        info = inspect_inputs(condition)
        before = info["protected_hashes"]
        summary = base_summary(condition, info)
        (attempt / SUITE.name).write_bytes(SUITE.read_bytes())
        write_json(attempt / "evaluation_input.json", {"suite_sha256": summary["suite_sha256"], "patches": info["patches"],
                   "official_protected_hashes": before, "delivery_mode": summary["delivery_mode"], "model_api_calls": 0})
        if not summary["supplemental_evaluation_completed"]:
            with (attempt / "console.log").open("wb") as output:
                proc = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--condition-dir", str(condition),
                        "--worker-attempt", str(attempt)], env=clean_environment(), stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=True)
                try:
                    code = proc.wait(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    raise TimeoutError(f"Supplemental evaluation exceeded {TIMEOUT} seconds")
            saved = attempt / "worker_summary.json"
            if saved.exists():
                summary = json.loads(saved.read_text())
            if code or not summary.get("supplemental_evaluation_completed"):
                raise RuntimeError(f"Supplemental worker exited {code}; see console.log")
    except BaseException as exc:
        summary.update(status="timeout" if isinstance(exc, TimeoutError) else "error", error=type(exc).__name__ + ": " + str(exc),
                       supplemental_evaluation_completed=False, enhanced_passed=False)
    finally:
        if proc is not None:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
            try:
                cleanup(attempt)
            except Exception as exc:
                summary.update(status="error", cleanup_error=str(exc), supplemental_evaluation_completed=False, enhanced_passed=False)
        try:
            unchanged = all(sha(path) == value for path, value in before.items()) and sha(SUITE) == summary["suite_sha256"]
        except OSError:
            unchanged = False
        summary.update(protected_artifacts_unchanged=unchanged, attempt_directory=str(attempt), elapsed_seconds=round(time.monotonic() - started, 3))
        if not unchanged:
            summary.update(status="error", error="An official artifact or frozen suite changed", supplemental_evaluation_completed=False, enhanced_passed=False)
        write_json(attempt / "supplemental_summary.json", summary)
        write_json(condition / "supplemental_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["supplemental_evaluation_completed"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition-dir", required=True)
    parser.add_argument("--worker-attempt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    safe = clean_environment()
    os.environ.clear()
    os.environ.update(safe)
    condition = validate_condition(args.condition_dir)
    if args.worker_attempt:
        attempt = Path(args.worker_attempt).resolve(strict=True)
        if attempt.parent != condition / "supplemental_attempts":
            raise ValueError("Invalid supplemental attempt directory")
        return worker(condition, attempt)
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    return supervise(condition)


if __name__ == "__main__":
    raise SystemExit(main())
