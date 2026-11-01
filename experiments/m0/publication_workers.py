"""Publication-parity adapters around the frozen selector workers.

Only voluntary-exit publication reminders change. The original worker receipt,
published-patch selection, histories, model transport and scoring stay intact.
An additional immutable receipt binds the guard log to this execution.
"""
from contextlib import contextmanager
import importlib.util
import math
from pathlib import Path
import re

from run_repeats import read_json
from run_task_expansion import file_hash, write_fixed
from selector_workers import module_bindings, registry_context
from task_specs import ROOT

RUNS = ROOT / "artifacts/publication_runs"
TASK_KEY = "click2068_f4_f10"
CONDITIONS = ("strongshare", "integrator", "solo")
RECEIPT = "publication_execution_receipt.json"


def load_private(filename):
    if filename not in {"selector_workers.py", "selector_evaluator.py"}:
        raise ValueError("Unknown publication adapter dependency")
    spec = importlib.util.spec_from_file_location("_publication_" + Path(filename).stem, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_task(key):
    from selector_task_specs import get_task as registered
    if key != TASK_KEY:
        raise ValueError("Publication pilot is restricted to its fixed Click task")
    return registered(key)


def check_task(task):
    if task is None or task.as_dict() != get_task(task.key).as_dict():
        raise ValueError("Task differs from the frozen publication task")


def validate_phase(value):
    original = Path(value).expanduser().absolute()
    phase = original.resolve(strict=True)
    try:
        parts = phase.relative_to(RUNS.resolve()).parts
    except ValueError:
        parts = ()
    if (len(parts) != 5 or parts[-1] not in CONDITIONS
            or parts[1] != TASK_KEY
            or parts[2] != ("solo_jobs" if parts[-1] == "solo" else "jobs")
            or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts)
            or original != phase or not phase.is_dir()):
        raise ValueError("Expected publication_runs/<suite>/<fixed task>/<jobs|solo_jobs>/<job>/<condition>")
    return phase


def regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.absolute() != path.resolve(strict=True):
        raise ValueError("Missing or linked publication evidence: " + str(path))
    return path


def read_guard(phase):
    from publication_guard import GUARD_VERSION
    phase = validate_phase(phase)
    config = read_json(regular(phase.parent / "task_config.json"))
    guard = read_json(regular(phase / "publication_guard.json"))
    expected = {"schema": "publication_guard_v1", "guard_version": GUARD_VERSION,
                "task_key": TASK_KEY, "condition": phase.name,
                "run_id": phase.parent.name, "max_nudges_per_agent": 2,
                "context_closed": True}
    if (any(guard.get(k) != v for k, v in expected.items())
            or config.get("publication_guard_version") != GUARD_VERSION
            or config.get("task_key") != TASK_KEY or config.get("condition") != phase.name
            or not isinstance(guard.get("events"), list) or not isinstance(guard.get("counts"), dict)):
        raise ValueError("Publication guard is absent, open, or belongs to another execution")
    active = {"solo"} if phase.name == "solo" else {"agent1", "agent2"}
    if any(a not in active or type(n) is not int or not 0 <= n <= 2 for a, n in guard["counts"].items()):
        raise ValueError("Invalid publication reminder counts")
    return guard


def guard_summary(phase):
    guard = read_guard(phase)
    return {k: guard[k] for k in ("schema", "guard_version", "context_closed", "counts", "events")}


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=1200):
    check_task(task)
    if (condition not in CONDITIONS or type(offline) is not bool
            or isinstance(execution_seconds, bool) or not isinstance(execution_seconds, (int, float))
            or not math.isfinite(execution_seconds) or execution_seconds <= 0):
        raise ValueError("Invalid publication phase or execution allowance")
    run_dir = Path(run_dir).expanduser().absolute()
    # The guard is archived with the condition, never left at the reusable job root.
    (run_dir / condition).mkdir(parents=True, exist_ok=True)
    phase = validate_phase(run_dir / condition)
    config = read_json(regular(run_dir / "task_config.json"))
    from publication_guard import GUARD_VERSION, publication_guard
    if (config.get("task") != task.as_dict() or config.get("condition") != condition
            or config.get("offline_selftest") is not offline
            or config.get("execution_seconds") != execution_seconds
            or config.get("publication_guard_version") != GUARD_VERSION):
        raise ValueError("Worker arguments differ from the fixed publication configuration")
    if (phase / "publication_guard.json").exists() or (phase / "publication_guard.json").is_symlink():
        raise RuntimeError("Previous guard evidence must be archived before a new attempt")
    worker = load_private("selector_workers.py")
    worker.check_task = check_task
    from run_solo_smoke import configure
    configure()
    with publication_guard(run_dir):
        code = worker.phase_worker(run_dir, condition, task, offline=offline,
                                   execution_seconds=execution_seconds)
    # Does not freeze a completed receipt or alter any incomplete-attempt status.
    read_guard(phase)
    return code


def _additional_receipt(phase, task):
    guard = read_guard(phase)
    names = ["execution_receipt.json", "publication_guard.json", "status.json", "source_manifest.json"]
    names += ["solo.traj.json"] if phase.name == "solo" else ["agent1.traj.json", "agent2.traj.json"]
    paths = [phase.parent / "task_config.json"] + [phase / name for name in names]
    return {"schema": "publication_execution_v1", "task_key": task.key,
            "condition": phase.name, "guard_version": guard["guard_version"],
            "native_execution_receipt_unchanged": True,
            "protected_files_sha256": {str(p.relative_to(phase.parent)): file_hash(regular(p)) for p in paths}}


def verify_execution(phase, task, condition, freeze=False):
    """Return the ORIGINAL receipt; the additional guard binding is separate."""
    check_task(task)
    phase = validate_phase(phase)
    if condition != phase.name:
        raise ValueError("Publication execution condition differs")
    read_guard(phase)
    worker = load_private("selector_workers.py")
    worker.check_task = check_task
    original = worker.verify_execution(phase, task, condition, freeze=freeze)
    extra = _additional_receipt(phase, task)
    if freeze:
        write_fixed(phase / RECEIPT, extra)
    elif read_json(regular(phase / RECEIPT)) != extra:
        raise RuntimeError("Publication guard, configuration, or execution traces changed")
    return original


def verify_worker_artifacts(phase, task):
    """Native Integrator evaluator compatibility; never writes evidence."""
    return verify_execution(phase, task, "integrator", freeze=False)


def extend_input_manifest(manifest, phase):
    """Include parity evidence in native pre/post scoring integrity checks."""
    from evaluate_solo_expansion import file_info
    phase = validate_phase(phase)
    result = dict(manifest)
    result["protected_files"] = dict(manifest.get("protected_files", {}))
    for name in ("publication_guard.json", RECEIPT):
        path = regular(phase / name)
        result["protected_files"][str(path)] = file_info(path)
    return result


@contextmanager
def evaluator_context():
    import publication_workers
    with registry_context(), module_bindings(luna_integrator_worker=publication_workers):
        yield
