"""Task-scoped adapters for the frozen Luna workers; no historical module edits.

One worker runs per process. Import bindings are held until all agent threads
finish, then restored even on failure. Packet preparation creates no model.
"""
from contextlib import contextmanager
import importlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import threading
import uuid

from run_baselines import digest
from run_repeats import read_json
from run_task_expansion import file_hash, write_fixed
from task_specs import ROOT

CONDITIONS = ("prefix", "strongshare", "integrator", "solo")
_LOCK = threading.RLock()


def load_private(filename):
    allowed = {"luna_collaboration_worker.py", "luna_integrator_worker.py", "luna_solo_worker.py",
               "run_handoff_pilot.py", "run_handoff_expansion.py", "cross_task_packet.py",
               "evaluate_cross_task.py", "luna_collaboration_evaluator.py", "luna_integrator_evaluator.py"}
    if filename not in allowed:
        raise ValueError("Unknown selector adapter dependency")
    spec = importlib.util.spec_from_file_location("_selector_" + Path(filename).stem, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_task(task):
    from selector_task_specs import get_task
    if task is None or task.as_dict() != get_task(task.key).as_dict():
        raise ValueError("Task differs from the frozen selector registry")


@contextmanager
def module_bindings(**modules):
    with _LOCK:
        missing = object()
        previous = {name: sys.modules.get(name, missing) for name in modules}
        sys.modules.update(modules)
        try:
            yield
        finally:
            for name, old in previous.items():
                if old is missing:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old


@contextmanager
def registry_context():
    # Initialize ordinary dependency globals before a temporary registry can be
    # observed by their first import. Private copies below are the only modules
    # whose task scope is widened.
    for name in ("cross_task_specs", "run_handoff_expansion", "run_luna_integrator",
                 "luna_integrator_evaluator", "luna_collaboration_evaluator"):
        importlib.import_module(name)
    with module_bindings(cross_task_specs=importlib.import_module("selector_task_specs")):
        yield


def integrator_module():
    from selector_task_specs import TASKS
    module = load_private("luna_integrator_worker.py")
    module.TASK_KEYS = tuple(TASKS)
    return module


def decision_packet(run_dir, task):
    """Read the outcome-independent packet frozen before any action branch."""
    from selector_rule import verify_decision
    folder = Path(run_dir).parent.parent / "decision"
    packet = read_json(folder / "packet.json")
    decision = read_json(folder / "decision.json")
    receipt = read_json(folder / "preparation.json")
    verify_decision(packet, decision)
    if (packet.get("task_key") != task.key or packet.get("checkpoint_sha256") != digest(read_json(Path(run_dir) / "checkpoint.json"))
            or receipt.get("packet_sha256") != digest(packet) or receipt.get("decision_sha256") != digest(decision)
            or receipt.get("checkpoint_sha256") != packet["checkpoint_sha256"]
            or receipt.get("task") != task.key or receipt.get("model_api_calls") != 0
            or receipt.get("before_continuation_outcomes") is not True):
        raise ValueError("Missing or changed pre-continuation selector packet")
    return packet


@contextmanager
def frozen_packet_context(run_dir, task):
    expected = decision_packet(run_dir, task)
    packet_module = load_private("cross_task_packet.py")
    original = packet_module.build_packet

    def build(*args, **kwargs):
        packet = original(*args, **kwargs)
        if packet != expected:
            raise RuntimeError("Restored checkpoint packet differs from the pre-outcome decision packet")
        return packet

    packet_module.build_packet = build
    with module_bindings(cross_task_packet=packet_module):
        yield


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=1200):
    check_task(task)
    if (condition not in CONDITIONS or type(offline) is not bool
            or isinstance(execution_seconds, bool) or not isinstance(execution_seconds, (int, float))
            or not math.isfinite(execution_seconds) or execution_seconds <= 0
            or condition == "prefix" and execution_seconds != 1200):
        raise ValueError("Invalid selector worker condition or execution allowance")
    from selector_task_specs import TASKS
    with registry_context():
        if condition == "solo":
            return load_private("luna_solo_worker.py").phase_worker(
                Path(run_dir), task, offline=offline, execution_seconds=execution_seconds)
        module = integrator_module() if condition == "integrator" else load_private("luna_collaboration_worker.py")
        module.TASK_KEYS = tuple(TASKS)
        if condition == "prefix":
            return module.phase_worker(Path(run_dir), condition, task, offline, execution_seconds)
        with frozen_packet_context(run_dir, task):
            return module.phase_worker(Path(run_dir), condition, task, offline, execution_seconds)


def _read_only_fixed(path, value):
    if Path(path).is_symlink() or read_json(path) != value:
        raise RuntimeError("Previously frozen execution evidence changed or is absent")


def verify_execution(phase, task, condition, freeze=False):
    """Recompute the original full receipt; only freeze=True may create it."""
    check_task(task)
    phase = Path(phase)
    if condition not in CONDITIONS[1:] or phase.name != condition:
        raise ValueError("Wrong selector execution phase")
    config, status = read_json(phase.parent / "task_config.json"), read_json(phase / "status.json")
    if (config.get("task") != task.as_dict() or status.get("task") != task.as_dict()
            or config.get("condition") != condition or status.get("status") != "completed"):
        raise ValueError("Completed selector task identity differs")
    if condition != "solo" and read_json(phase / "handoff_packet.json") != decision_packet(phase.parent, task):
        raise ValueError("Delivered packet differs from the selector input")
    with registry_context():
        if condition == "integrator":
            module = integrator_module()
            return (module.freeze_execution_receipt if freeze else module.verify_worker_artifacts)(phase, task)
        cross = load_private("run_handoff_expansion.py")
        cross.write_fixed = write_fixed if freeze else _read_only_fixed
        if condition == "solo":
            return cross.verify_execution_receipt(phase, task, condition)
        verifier = load_private("run_handoff_pilot.py")
        verifier.TASK = task
        verifier.SOURCE_CHECKPOINT = digest(read_json(phase.parent / "checkpoint.json"))
        verifier.verify_handoff = cross.verify_handoff
        verifier.write_fixed = write_fixed if freeze else _read_only_fixed
        return verifier.verify_execution_receipt(phase, task, condition)


def verify_worker_artifacts(phase, task):
    """Compatibility entry point used by the unchanged Integrator evaluator."""
    return verify_execution(phase, task, "integrator", freeze=False)


def collect_checkpoint_packet(prefix, task, output_path):
    """Restore and verify both immutable snapshots, collect evidence, then clean up.

    The caller measures the whole invocation, including cleanup, as common
    preparation time. The returned/written packet is exactly the branch packet.
    No agents, model clients, or provider requests are created.
    """
    check_task(task)
    from run_solo_smoke import configure
    configure()
    import docker
    from baseline_runtime import SnapshotDockerEnvironment, restore_volume, verify_workspace_manifest
    from cross_task_packet import build_packet, collect_source_changes, collect_scratch_files
    from wip_checkpoint import validate_checkpoint

    prefix, output_path = Path(prefix), Path(output_path)
    checkpoint = read_json(prefix / "checkpoint.json")
    validate_checkpoint(checkpoint, task)
    if (read_json(prefix / "prefix/status.json").get("status") != "completed"
            or file_hash(prefix / "shared_git.tar") != checkpoint["git_archive_sha256"]):
        raise ValueError("Checkpoint preparation requires a completed unchanged prefix")
    expected = read_json(task.image_record_path)
    label = "selector-prepare-" + uuid.uuid4().hex[:16]
    volume, envs, client, created = "rsi-" + label, {}, None, False
    packet, cleanup_errors = None, []
    try:
        client = docker.from_env(timeout=120)
        if client.images.get(task.image).id != expected["Id"]:
            raise RuntimeError("Registered task image changed")
        # Mark before restore so a partially created helper volume is also removed.
        created = True
        restore_volume(prefix / "shared_git.tar", volume, expected["Id"], label,
                       expected_sha256=checkpoint["git_archive_sha256"])
        for agent, saved in checkpoint["agents"].items():
            envs[agent] = SnapshotDockerEnvironment(image=saved["image"], run_label=label,
                agent_id=agent, shared_git_volume=volume, container_timeout="10m")
            verify_workspace_manifest(envs[agent], saved["manifest"])
        packet = build_packet(checkpoint, task,
            {a: collect_source_changes(envs[a], task) for a in task.agents},
            {a: collect_scratch_files(envs[a], checkpoint["agents"][a]) for a in task.agents})
    finally:
        for env in envs.values():
            try:
                env.cleanup()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        if client is not None:
            if created:
                try:
                    client.volumes.get(volume).remove()
                except docker.errors.NotFound:
                    pass
                except Exception as exc:
                    cleanup_errors.append(type(exc).__name__)
            client.close()
        if cleanup_errors:
            raise RuntimeError("Checkpoint preparation cleanup failed: " + ", ".join(cleanup_errors))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_fixed(output_path, packet)
    return packet
