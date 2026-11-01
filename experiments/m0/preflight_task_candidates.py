"""Check selected CooperBench task images and grading references without model calls.

References remain in evaluator-only containers. This script does not modify the
dataset, runner, or agent inputs. Run with CooperBench/.venv/bin/python.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from evaluate_solo_smoke import COMMIT, REPO, ROOT, evaluator_environment, write_json

DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
CASES = {
    "click": {
        "repo": "pallets_click_task", "task_id": 2800, "features": [1, 2],
        "image": "akhatua/cooperbench-pallets-click:task2800",
        "base_commit": "d8763b93021c416549b5f8b4b5497234619410db",
    },
    "dirty_equals": {
        "repo": "samuelcolvin_dirty_equals_task", "task_id": 43, "features": [3, 7],
        "image": "akhatua/cooperbench-samuelcolvin-dirty-equals:task43",
        "base_commit": "593bcccf738ab8b724d7cb860881d74344171f5f",
    },
}
EXEC_TIMEOUT = 300


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_task(name, config, output, env):
    import docker
    from cooperbench.eval.backends.docker import DockerExecResult
    from cooperbench.eval.sandbox import _run_tests, _write_patch
    from cooperbench.utils import get_image_name

    target = output / name
    target.mkdir()
    task = REPO / "dataset" / config["repo"] / f"task{config['task_id']}"
    result = {**config, "status": "preparing", "features_results": [], "model_calls": 0}
    if get_image_name(config["repo"], config["task_id"]) != config["image"]:
        raise RuntimeError("Image mapping differs from the pinned official source")
    print(json.dumps({"task": name, "stage": "pulling_image", "image": config["image"]}), flush=True)
    with (target / "image_pull.log").open("wb") as log:
        pull = subprocess.run([DOCKER, "pull", "--platform", "linux/arm64", config["image"]],
                              env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
    if pull.returncode:
        raise RuntimeError(f"Image pull failed with exit {pull.returncode}; see {target / 'image_pull.log'}")
    client = docker.from_env(timeout=120)
    try:
        image = client.images.get(config["image"])
        image_info = {k: image.attrs.get(k) for k in ("Id", "Architecture", "Os", "RepoDigests", "RepoTags", "Created")}
        image_info.update(image=config["image"], expected_base=config["base_commit"])
        if image_info["Architecture"] != "arm64" or image_info["Os"] != "linux":
            raise RuntimeError("Unexpected task image architecture/OS")
        write_json(target / "task_image.json", image_info)
        result["image_id"] = image.id
        print(json.dumps({"task": name, "stage": "image_ready", "image_id": image.id,
                          "image_record": str(target / "task_image.json")}), flush=True)

        class RecordingSandbox:
            def __init__(self, directory):
                self.directory = directory
                self.entries = []
                self.container = client.containers.run(
                    image=image.id, entrypoint="", command="sleep infinity", detach=True,
                    working_dir="/workspace", remove=False,
                    labels={"agent-rsi-candidate-preflight": output.name, "agent-rsi-candidate": name},
                )
                write_json(directory / "container.json", {"id": self.container.id, "image_id": image.id})

            def exec(self, *args):
                index = len(self.entries) + 1
                logfile = self.directory / f"exec_{index:03d}.log"
                entry = {"index": index, "output": str(logfile), "status": "running"}
                self.entries.append(entry)
                write_json(self.directory / "exec_index.json", self.entries)
                # A host subprocess timeout can remove the container even if Docker exec hangs.
                try:
                    with logfile.open("wb") as stream:
                        command = subprocess.run([DOCKER, "exec", self.container.id, *args], env=env,
                                                 stdout=stream, stderr=subprocess.STDOUT,
                                                 timeout=EXEC_TIMEOUT)
                    data = logfile.read_bytes()
                    entry.update(status="completed", exit_code=command.returncode, bytes=len(data))
                    return DockerExecResult(command.returncode, data)
                except subprocess.TimeoutExpired:
                    entry.update(status="timeout")
                    raise
                finally:
                    write_json(self.directory / "exec_index.json", self.entries)

            def terminate(self):
                self.container.remove(force=True)

        for feature in config["features"]:
            feature_dir = target / f"feature{feature}"
            feature_dir.mkdir()
            source = task / f"feature{feature}"
            sb = RecordingSandbox(feature_dir)
            feature_result = {"feature": feature, "checks": {}, "reference_source_hashes": {
                "task": sha256(source / "feature.md"), "tests": sha256(source / "tests.patch"),
                "individual_reference": sha256(source / "feature.patch"),
                "combined_reference": sha256(task / "combined.patch"),
            }}
            try:
                observed = sb.exec("git", "-C", "/workspace/repo", "rev-parse", "HEAD")
                base = observed.stdout_read().strip()
                if observed.returncode or base != config["base_commit"]:
                    raise RuntimeError(f"Image base commit mismatch for {name}: {base}")
                baked = sb.exec("sha256sum", "/usr/local/bin/runner.sh")
                feature_result["baked_runner_sha256"] = baked.stdout_read().split()[0]
                feature_result["source_runner_sha256"] = sha256(task / "runner.sh")
                feature_result["runner_matches_source"] = (
                    feature_result["baked_runner_sha256"] == feature_result["source_runner_sha256"]
                )
                if not feature_result["runner_matches_source"]:
                    raise RuntimeError("Image-baked runner differs from the pinned dataset runner")
                sb.exec("mkdir", "-p", "/patches")
                _write_patch(sb, "tests.patch", (source / "tests.patch").read_text())
                _write_patch(sb, "individual.patch", (source / "feature.patch").read_text())
                _write_patch(sb, "combined.patch", (task / "combined.patch").read_text())
                for check, reference in (("base", ""), ("individual_reference", "individual.patch"),
                                         ("combined_reference", "combined.patch")):
                    started = time.monotonic()
                    scored = _run_tests(sb, "tests.patch", reference, config["base_commit"])
                    log = feature_dir / f"{check}.log"
                    log.write_text(scored.pop("output"))
                    scored.update(log=str(log), elapsed_seconds=round(time.monotonic() - started, 3))
                    feature_result["checks"][check] = scored
                    write_json(feature_dir / "result.json", feature_result)
                    print(json.dumps({"task": name, "feature": feature, "check": check, **scored}), flush=True)
                checks = feature_result["checks"]
                # A base failure must reach pytest and observe feature failure, not fail in setup.
                feature_result["gradeable"] = (
                    not checks["base"]["passed"] and checks["base"]["tests_failed"] > 0
                    and checks["individual_reference"]["passed"] and checks["combined_reference"]["passed"]
                )
            finally:
                sb.terminate()
                write_json(feature_dir / "result.json", feature_result)
            result["features_results"].append(feature_result)
            write_json(target / "result.json", result)
        result["status"] = "passed" if all(f["gradeable"] for f in result["features_results"]) else "failed"
        result["combined_reference_supports_selected_pair"] = all(
            f["checks"]["combined_reference"]["passed"] for f in result["features_results"]
        )
        write_json(target / "result.json", result)
        return result
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=list(CASES))
    args = parser.parse_args()
    clean = evaluator_environment()
    os.environ.clear()
    os.environ.update(clean)
    actual = subprocess.check_output(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise RuntimeError("Unexpected CooperBench commit")
    subprocess.run(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                    "src/cooperbench/eval", "src/cooperbench/utils.py",
                    "dataset/pallets_click_task/task2800", "dataset/samuelcolvin_dirty_equals_task/task43"],
                   check=True, capture_output=True)
    output = ROOT / "artifacts/task_candidate_preflight" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True)
    summary = {"cooperbench_commit": COMMIT, "script_sha256": sha256(Path(__file__)),
               "model_calls": 0, "reference_data_exposed_to_agents": False, "results": {}, "output": str(output)}
    print(str(output), flush=True)
    for name, config in CASES.items():
        if args.only and args.only != name:
            continue
        try:
            summary["results"][name] = evaluate_task(name, config, output, clean)
        except Exception as exc:
            summary["results"][name] = {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}
            print(json.dumps({"task": name, **summary["results"][name]}), flush=True)
        write_json(output / "summary.json", summary)
    return 0 if all(r["status"] == "passed" for r in summary["results"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
