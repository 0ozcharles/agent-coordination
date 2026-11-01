"""Independent, evaluator-only Pillow task68 metadata checks. No model calls.

Run with the host Python (stdlib only):
  python pillow_metadata_probe_v1.py --validate-references
  python pillow_metadata_probe_v1.py --patch /absolute/path/final.patch --label qwen_solo_r1

The saved patch is applied unchanged in a disposable, network-disabled pinned
container. Nothing is mounted into an agent or written into an existing run.
Official scores are not read or changed. A completed supplemental failure exits
0; setup failures exit 2. --inside and --test-inside are evaluator internals.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts/pillow_metadata_probe_v1"
REPO = ROOT / "CooperBench"
TASK = REPO / "dataset/pillow_task/task68"
COMMIT = "63b9d44d9f39a02fccf5bf0052db48a917a011fd"
BASE = "5d52ede584c5137f3a0b00be97dea8764404306b"
IMAGE = "akhatua/cooperbench-pillow:task68"
IMAGE_RECORD = ROOT / "artifacts/task_images/pillow68_f1_f2.json"
DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
SUITE_ID = "pillow-task68-metadata-supplemental-v1"
RESULT_MARKER = "RSI_METADATA_PROBE_JSON="
ORIENTATIONS = (("missing", None), ("identity", 1), ("rotate90", 6))
MODES = ("strip", "default_omitted", "explicit_false")


def protocol():
    return {
        "suite_id": SUITE_ID, "task": "pillow68_f1_f2", "target_feature": 2,
        "requirement": "strip_metadata=True removes all metadata after the orientation operation; default behavior remains unchanged.",
        "cases": {"orientation": [name for name, _ in ORIENTATIONS],
                  "in_place": [False, True], "strip_argument": list(MODES)},
        "checks_per_case": ["correct pixels, size and mode", "existing copy/in_place return contract",
                            "copy mode leaves input pixels and metadata unchanged",
                            "strip: empty EXIF and no remaining info metadata",
                            "default/False: preserve non-orientation metadata and base orientation behavior"],
        "empty_exif_representation_allowed": True,
        "metadata_fixture": ["EXIF Make/Software/DateTime", "comment", "icc_profile", "xmp", "custom_metadata"],
        "interpretation": "18 synthetic cases exercise specified metadata paths, not every format or all Pillow behavior. They do not replace official feature scores or test tuple-XMP feature1.",
        "reference_policy": "A reference patch is evaluated unchanged. Its failure is reported as a reference/requirement gap; expectations are not relaxed to make it pass.",
        "model_calls": 0, "agent_visibility": "evaluator only, after submission",
    }


def _fixture(Image, orientation):
    image = Image.new("RGB", (3, 2))
    image.putdata([(5, 10, 15), (20, 25, 30), (35, 40, 45),
                   (50, 55, 60), (65, 70, 75), (80, 85, 90)])
    exif = Image.Exif()
    exif[0x010F] = "Probe camera"
    exif[0x0131] = "Probe software"
    exif[0x0132] = "2026:01:02 03:04:05"
    if orientation is not None:
        exif[0x0112] = orientation
    image.info.update({"exif": exif.tobytes(), "comment": b"Private note",
                       "icc_profile": b"probe-profile-marker", "xmp": b"<xmp>private</xmp>",
                       "custom_metadata": "private-marker"})
    return image


def _snapshot(image):
    return {"size": image.size, "mode": image.mode, "pixels": image.tobytes(),
            "info": copy.deepcopy(image.info), "exif": dict(image.getexif())}


def _metadata_observation(Image, image):
    exif = dict(image.getexif())
    encoded_exif = image.info.get("exif")
    encoded_tags = None
    encoded_error = None
    if encoded_exif is not None:
        try:
            encoded = Image.Exif()
            encoded.load(encoded_exif)
            encoded_tags = list(encoded)
        except Exception as exc:
            encoded_error = {"type": type(exc).__name__, "message": str(exc)}
    return {"info_keys": sorted(image.info), "exif_tags": sorted(exif),
            "encoded_exif_tags": encoded_tags, "encoded_exif_error": encoded_error}


def _run_case(Image, operation, orientation_name, orientation, in_place, mode):
    image = _fixture(Image, orientation)
    before = _snapshot(image)
    expected_pixels = (image.transpose(Image.Transpose.ROTATE_270)
                       if orientation == 6 else image.copy())
    expected_exif = dict(before["exif"])
    if orientation == 6:
        expected_exif.pop(0x0112, None)
    case = {"id": f"{mode}_{orientation_name}_{'in_place' if in_place else 'copy'}",
            "group": "strip" if mode == "strip" else "default_preservation",
            "orientation": orientation, "in_place": in_place, "argument_mode": mode}
    try:
        kwargs = {"in_place": in_place}
        if mode != "default_omitted":
            kwargs["strip_metadata"] = mode == "strip"
        returned = operation(image, **kwargs)
        output = image if in_place else returned
        checks = {"return_contract": returned is None if in_place else isinstance(returned, Image.Image) and returned is not image}
        if not isinstance(output, Image.Image):
            raise TypeError("Operation did not return an Image in copy mode")
        checks["pixels_size_mode"] = (output.mode == expected_pixels.mode
            and output.size == expected_pixels.size and output.tobytes() == expected_pixels.tobytes())
        checks["input_unchanged_in_copy_mode"] = in_place or _snapshot(image) == before
        observation = _metadata_observation(Image, output)
        if mode == "strip":
            checks["all_exif_removed"] = len(output.getexif()) == 0
            checks["all_info_metadata_removed"] = not (set(output.info) - {"exif"})
            checks["encoded_exif_empty_or_absent"] = (observation["encoded_exif_error"] is None
                and observation["encoded_exif_tags"] in (None, []))
        else:
            checks["non_orientation_exif_and_base_orientation_preserved"] = dict(output.getexif()) == expected_exif
            checks["info_metadata_preserved"] = ({k: v for k, v in output.info.items() if k != "exif"}
                == {k: v for k, v in before["info"].items() if k != "exif"})
        case.update(passed=all(checks.values()), checks=checks, observed=observation)
    except Exception as exc:
        case.update(passed=False, checks={}, exception={"type": type(exc).__name__, "message": str(exc)})
    finally:
        expected_pixels.close()
        image.close()
    return case


def run_suite(operation=None):
    """Run the fixed 18 cases inside the evaluator; injectable operator is for unit controls only."""
    try:
        sys.path.insert(0, "/workspace/repo/src")
        import PIL
        from PIL import Image, ImageOps
        if not str(Path(PIL.__file__).resolve()).startswith("/workspace/repo/src/PIL/"):
            raise RuntimeError("Pillow is not imported from the evaluated workspace")
        operation = ImageOps.exif_transpose if operation is None else operation
        checks = [_run_case(Image, operation, name, orientation, in_place, mode)
                  for mode in MODES for name, orientation in ORIENTATIONS for in_place in (False, True)]
        return {"suite_id": SUITE_ID, "pillow_source": PIL.__file__, "tests_total": len(checks),
                "tests_passed": sum(c["passed"] for c in checks), "passed": all(c["passed"] for c in checks),
                "groups": {group: {"passed": sum(c["passed"] for c in checks if c["group"] == group),
                                   "total": sum(c["group"] == group for c in checks)}
                           for group in ("strip", "default_preservation")}, "checks": checks}
    except Exception as exc:
        return {"suite_id": SUITE_ID, "passed": False, "tests_total": 0, "tests_passed": 0,
                "checks": [], "artifact_load_error": {"type": type(exc).__name__, "message": str(exc)}}


def clean_environment(source=None):
    source = os.environ if source is None else source
    env = {k: source[k] for k in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL") if k in source}
    env.update(PATH="/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin",
               DOCKER_HOST="unix://" + str(Path.home() / ".docker/run/docker.sock"),
               PYTHON_DOTENV_DISABLED="1", PYTHONUNBUFFERED="1")
    return env


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _saved_patch(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = path.absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("A saved ordinary patch file is required")
    return path.resolve(), file_hash(path)


def evaluate_patch(patch=None, label="base", validate_probe=False):
    """Return a supplemental receipt; only writes under this module's artifact root."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", label):
        raise ValueError("label must contain only letters, digits, underscores or hyphens")
    patch, patch_sha = _saved_patch(patch) if patch is not None else (None, None)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = ARTIFACTS / (stamp + "_" + label + "_" + uuid.uuid4().hex[:6])
    target.mkdir(parents=True)
    env, container, entries = clean_environment(), None, []
    result = {"suite_id": SUITE_ID, "label": label, "status": "preparing", "artifact_directory": str(target),
              "model_calls": 0, "official_scores_modified": False, "probe_exposed_to_agents": False}
    expected = json.loads(IMAGE_RECORD.read_text())
    receipt = {"patch": str(patch) if patch else None, "patch_sha256": patch_sha,
               "image_record_sha256": file_hash(IMAGE_RECORD), "expected_image_id": expected["Id"],
               "base_commit": BASE, "cooperbench_commit": COMMIT,
               "probe_sha256": file_hash(__file__), "test_sha256": file_hash(ROOT / "test_pillow_metadata_probe_v1.py"),
               "source_hashes": {str(path.relative_to(ROOT)): file_hash(path) for path in (
                   TASK / "feature2/feature.md", TASK / "feature2/feature.patch", TASK / "feature2/tests.patch",
                   TASK / "combined.patch")},
               "credentials_inherited": False, "network_mode": "none", "host_mounts": []}
    write_json(target / "input.json", receipt)
    write_json(target / "protocol.json", protocol())
    started = time.monotonic()

    def command(args, timeout=120, check=True):
        index = len(entries) + 1
        path = target / f"exec_{index:03d}.log"
        entry = {"index": index, "argv": args, "output": path.name}
        entries.append(entry)
        try:
            proc = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            path.write_bytes(proc.stdout)
            entry.update(returncode=proc.returncode, output_sha256=file_hash(path))
            if check and proc.returncode:
                raise RuntimeError(f"Command {index} failed (exit {proc.returncode}); see {path}")
            return proc.returncode, proc.stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            path.write_bytes(exc.stdout or b"")
            entry.update(timed_out=True)
            raise
        finally:
            write_json(target / "exec_index.json", entries)

    try:
        _, head = command(["/usr/bin/git", "-C", str(REPO), "rev-parse", "HEAD"])
        if head.strip() != COMMIT:
            raise RuntimeError("Pinned CooperBench revision differs")
        command(["/usr/bin/git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--", "dataset/pillow_task/task68"])
        _, image_json = command([DOCKER, "image", "inspect", IMAGE])
        image = json.loads(image_json)[0]
        if any(image[k] != expected[k] for k in ("Id", "Architecture", "Os")):
            raise RuntimeError("Pinned Pillow task image differs")
        _, created = command([DOCKER, "create", "--network", "none", "--cpus", "2", "--memory", "2g",
            "--label", "agent-rsi-pillow-metadata-probe=" + target.name, "--entrypoint", "sleep", expected["Id"], "infinity"])
        container = created.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise RuntimeError("Docker returned an invalid container ID")
        command([DOCKER, "start", container])
        _, base = command([DOCKER, "exec", "-w", "/workspace/repo", container, "git", "rev-parse", "HEAD"])
        _, state = command([DOCKER, "exec", "-w", "/workspace/repo", container, "git", "status", "--porcelain"])
        if base.strip() != BASE or state.strip():
            raise RuntimeError("Evaluator workspace did not start at the clean task base")
        command([DOCKER, "cp", str(Path(__file__).resolve()), container + ":/tmp/pillow_metadata_probe_v1.py"])
        if patch and patch.read_bytes().strip():
            command([DOCKER, "cp", str(patch), container + ":/tmp/submitted.patch"])
            code, _ = command([DOCKER, "exec", "-w", "/workspace/repo", container, "git", "apply", "--check", "/tmp/submitted.patch"], check=False)
            if code:
                result.update(status="completed", passed=False, patch_apply_failed=True, tests_total=0, tests_passed=0)
            else:
                command([DOCKER, "exec", "-w", "/workspace/repo", container, "git", "apply", "/tmp/submitted.patch"])
        if not result.get("patch_apply_failed"):
            if validate_probe:
                command([DOCKER, "cp", str(ROOT / "test_pillow_metadata_probe_v1.py"), container + ":/tmp/test_pillow_metadata_probe_v1.py"])
                code, _ = command([DOCKER, "exec", "-w", "/tmp", "-e", "PYTHONPATH=/workspace/repo/src:/tmp",
                    container, "python", "-m", "unittest", "test_pillow_metadata_probe_v1", "-v"], check=False)
                result.update(status="completed", probe_selftests_passed=code == 0, passed=code == 0)
            else:
                _, text = command([DOCKER, "exec", "-w", "/workspace/repo", "-e", "PYTHONPATH=/workspace/repo/src",
                    container, "python", "/tmp/pillow_metadata_probe_v1.py", "--inside"])
                lines = [line[len(RESULT_MARKER):] for line in text.splitlines() if line.startswith(RESULT_MARKER)]
                if len(lines) != 1:
                    raise RuntimeError("Evaluator output did not contain exactly one probe result")
                result.update(status="completed", **json.loads(lines[0]))
    except Exception as exc:
        result.update(status="error", passed=False, error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        if container:
            try:
                command([DOCKER, "rm", "-f", container], timeout=30)
                result["container_removed"] = True
            except Exception as exc:
                result.update(status="error", cleanup_error={"type": type(exc).__name__, "message": str(exc)})
        result["patch_unchanged"] = patch is None or file_hash(patch) == patch_sha
        result["probe_unchanged"] = file_hash(__file__) == receipt["probe_sha256"]
        if not result["patch_unchanged"] or not result["probe_unchanged"]:
            result.update(status="error", passed=False, input_mutation_detected=True)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(target / "result.json", result)
    return result


def validate_references():
    records = [evaluate_patch(label="probe_selftests", validate_probe=True),
               evaluate_patch(label="base"),
               evaluate_patch(TASK / "feature2/feature.patch", label="feature2_reference"),
               evaluate_patch(TASK / "combined.patch", label="combined_reference")]
    summary = {"suite_id": SUITE_ID, "model_calls": 0, "official_scores_modified": False,
               "records": [{k: r.get(k) for k in ("label", "status", "passed", "tests_total", "tests_passed", "groups", "artifact_directory")}
                           for r in records], "base_fails": records[1].get("passed") is False,
               "probe_selftests_passed": records[0].get("probe_selftests_passed") is True,
               "feature2_reference_passes": records[2].get("passed") is True,
               "combined_reference_passes": records[3].get("passed") is True,
               "reference_gap_not_repaired": not all(r.get("passed") for r in records[2:]),
               "checks_fixed_from_requirement": True}
    path = ARTIFACTS / ("reference_validation_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    write_json(path, summary)
    return summary, path, 0 if all(r["status"] == "completed" for r in records) and summary["probe_selftests_passed"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choices = parser.add_mutually_exclusive_group(required=True)
    choices.add_argument("--patch", type=Path)
    choices.add_argument("--base", action="store_true")
    choices.add_argument("--validate-references", action="store_true")
    choices.add_argument("--inside", action="store_true")
    parser.add_argument("--label", default="submitted")
    args = parser.parse_args()
    if args.inside:
        print(RESULT_MARKER + json.dumps(run_suite(), ensure_ascii=False))
        return 0
    if args.validate_references:
        _, path, code = validate_references()
        print(json.dumps({"reference_validation": str(path), "model_calls": 0}))
        return code
    result = evaluate_patch(args.patch, "base" if args.base else args.label)
    print(json.dumps({k: result.get(k) for k in ("status", "passed", "tests_passed", "tests_total", "artifact_directory", "model_calls")}))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
