"""Evaluate an unchanged solo patch with CooperBench; no model or credential use.

Usage (with the CooperBench virtual environment's Python):
    python evaluate_solo_smoke.py --run-dir artifacts/solo_runs/solo_smoke_<id>

Completed benchmark failures exit 0; infrastructure errors/timeouts exit 2.
Read evaluation_summary.json to distinguish passing from failing tests.
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

ROOT = Path(__file__).resolve().parent
REPO = ROOT / "CooperBench"
RUNS = ROOT / "artifacts/solo_runs"
IMAGE = "akhatua/cooperbench-pallets-jinja:task1621"
COMMIT = "63b9d44d9f39a02fccf5bf0052db48a917a011fd"
DEADLINE_SECONDS = 660
EXEC_TIMEOUT_SECONDS = 600
RUN_LABEL = "agent-rsi-eval-run"
ATTEMPT_LABEL = "agent-rsi-eval-attempt"


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def evaluator_environment(source=None):
    """An allowlist avoids passing model keys, tokens, or proxy credentials."""
    source = os.environ if source is None else source
    clean = {k: source[k] for k in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL") if k in source}
    clean.update({
        "PATH": "/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "DOCKER_HOST": "unix://" + str(Path.home() / ".docker/run/docker.sock"),
        "MSWEA_GLOBAL_CONFIG_DIR": str(ROOT / "config/mini-swe-agent"),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return clean


def validate_run_dir(value):
    path = Path(value).expanduser().resolve(strict=True)
    if path.parent != RUNS.resolve() or not re.fullmatch(r"solo_smoke_[A-Za-z0-9_-]+", path.name):
        raise ValueError("--run-dir must be a solo_smoke_* directory directly under artifacts/solo_runs")
    if not path.is_dir():
        raise ValueError("--run-dir is not a directory")
    return path


def patch_info(run_dir):
    patch = run_dir / "solo/pallets_jinja_task/1621/f1_f6/solo.patch"
    if not patch.is_file() or patch.is_symlink() or patch.resolve().parent != patch.parent:
        raise ValueError("An ordinary saved solo.patch is required; no patch was evaluated")
    content = patch.read_bytes()
    if not content.strip():
        raise ValueError("solo.patch is empty; no patch was evaluated")
    return patch, hashlib.sha256(content).hexdigest()


def make_backend(backend_module, client, expected_id, run_dir, attempt_dir, expected_image=None):
    """Add image pinning/labels and record exec bytes; keep official test commands."""
    logs = attempt_dir / "exec_logs"
    logs.mkdir(exist_ok=True)
    entries = []

    class RecordingSandbox(backend_module.DockerSandbox):
        def exec(self, *args):
            number = len(entries) + 1
            logfile = logs / f"exec_{number:03d}.log"
            entry = {"number": number, "output_file": str(logfile), "status": "running"}
            entries.append(entry)
            write_json(attempt_dir / "exec_index.json", entries)
            # Streaming preserves all bytes already produced if the watchdog stops
            # a hung test. stdout/stderr remain merged, as in DockerSandbox.exec.
            execution = client.api.exec_create(
                self._container.id, cmd=list(args), workdir=self._workdir,
                stdout=True, stderr=True,
            )
            chunks = []
            try:
                with logfile.open("wb") as output:
                    for chunk in client.api.exec_start(execution["Id"], stream=True, demux=False):
                        chunks.append(chunk)
                        output.write(chunk)
                        output.flush()
                code = client.api.exec_inspect(execution["Id"])["ExitCode"]
                code = code if isinstance(code, int) else -1
                entry.update(status="completed", returncode=code, output_bytes=sum(map(len, chunks)))
                write_json(attempt_dir / "exec_index.json", entries)
                return backend_module.DockerExecResult(code, b"".join(chunks))
            except Exception as exc:
                entry.update(status="error", error_type=type(exc).__name__)
                write_json(attempt_dir / "exec_index.json", entries)
                raise

    class LabelledBackend(backend_module.DockerBackend):
        def create_sandbox(self, image, timeout=600, workdir="/workspace"):
            if image != (expected_image or IMAGE):
                raise ValueError("Unexpected task image requested by evaluator")
            container = client.containers.run(
                image=expected_id, entrypoint="", command="sleep infinity",
                detach=True, working_dir=workdir, remove=False, stop_signal="SIGTERM",
                labels={RUN_LABEL: run_dir.name, ATTEMPT_LABEL: attempt_dir.name},
            )
            write_json(attempt_dir / "container.json", {"id": container.id, "image_id": expected_id})
            sandbox = RecordingSandbox(container, workdir, timeout)
            sandbox.exec("mkdir", "-p", "/patches")
            return sandbox

    return LabelledBackend()


def worker(run_dir, attempt_dir):
    # Do this before importing CooperBench: its package imports load_dotenv().
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    import docker
    import cooperbench.eval.backends.docker as backend_module
    import cooperbench.eval.sandbox as evaluation_module

    patch, before = patch_info(run_dir)
    expected = json.loads((ROOT / "artifacts/task_image.json").read_text())
    client = docker.from_env(timeout=EXEC_TIMEOUT_SECONDS)
    try:
        actual = client.images.get(IMAGE)
        if actual.id != expected["Id"] or actual.attrs["Architecture"] != expected["Architecture"]:
            raise RuntimeError("Task image differs from the verified reference image")
        head = subprocess.check_output(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
        if head != COMMIT:
            raise RuntimeError("CooperBench commit differs from the verified environment")
        write_json(attempt_dir / "evaluation_input.json", {
            "patch": str(patch), "patch_sha256": before, "image_id": expected["Id"],
            "cooperbench_commit": head, "features": [1, 6],
            "model_calls": 0, "credentials_inherited": False,
            "patch_modified_by_evaluator": False,
            "upstream_test_file_filter": True,
        })
        backend = make_backend(backend_module, client, expected["Id"], run_dir, attempt_dir)

        def get_backend(name="docker"):
            if name != "docker":
                raise ValueError("Only the local Docker evaluator is permitted")
            return backend

        evaluation_module.get_backend = get_backend
        result = evaluation_module.test_solo(
            repo_name="pallets_jinja_task", task_id=1621, feature1_id=1, feature2_id=6,
            patch=patch, timeout=EXEC_TIMEOUT_SECONDS, backend="docker", dataset_dir=REPO / "dataset",
        )
        if hashlib.sha256(patch.read_bytes()).hexdigest() != before:
            result["error"] = "Saved agent patch changed during evaluation; result cannot be accepted"
            result["both_passed"] = False
        write_json(attempt_dir / "evaluation.json", result)
        return 2 if result.get("error") else 0
    finally:
        client.close()


def cleanup(run_dir, attempt_dir, env):
    result = subprocess.run([
        "docker", "ps", "-aq", "--filter", f"label={RUN_LABEL}={run_dir.name}",
        "--filter", f"label={ATTEMPT_LABEL}={attempt_dir.name}",
    ], env=env, capture_output=True, text=True, timeout=20, check=True)
    ids = [line for line in result.stdout.splitlines() if re.fullmatch(r"[a-f0-9]{12,64}", line)]
    for container_id in ids:
        subprocess.run(["docker", "rm", "-f", container_id], env=env,
                       capture_output=True, timeout=20, check=True)
    write_json(attempt_dir / "cleanup.json", {"removed_remaining_containers": ids})


def stop_process(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def summarize(result, status, attempt_dir, elapsed):
    return {
        "status": status, "feature_1_passed": result.get("feature1", {}).get("passed"),
        "feature_6_passed": result.get("feature2", {}).get("passed"),
        "both_passed": result.get("both_passed", False), "error": result.get("error"),
        "cleanup_error": result.get("cleanup_error"),
        "elapsed_seconds": round(elapsed, 3), "watchdog_seconds": DEADLINE_SECONDS,
        "model_calls": 0, "attempt_directory": str(attempt_dir),
        "full_test_output": str(attempt_dir / "evaluation.json"),
        "incremental_exec_output": str(attempt_dir / "exec_logs"),
    }


def supervise(run_dir):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    attempt_dir = run_dir / "evaluation_attempts" / stamp
    attempt_dir.mkdir(parents=True)
    started = time.monotonic()
    env = evaluator_environment()
    result = None
    proc = None
    code = 2
    status = "error"
    try:
        patch_info(run_dir)  # Reject absent/empty patches before any Docker work.
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
        summary = summarize(result, status, attempt_dir, time.monotonic() - started)
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
    # Translate SIGTERM into the same cleanup path as keyboard interruption.
    def interrupted(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    return supervise(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
