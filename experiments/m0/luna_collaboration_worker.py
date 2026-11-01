"""Isolated Luna binding for the frozen WIP-prefix and handoff workers.

This module substitutes only transport/accounting in private worker modules.
The supervisor owns same-suite Luna checkpoint provenance, process watchdogs,
source freezing and scoring. One worker runs per process; the import context is
held until both native agent loops and their cleanup have finished.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import math
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parent
MODEL = "gpt-6-luna"
MAX_OUTPUT = 16384
EXECUTION_SECONDS = 1200
VARIANT = "source_handoff_diff_v1"
CONDITIONS = ("prefix", "strongshare", "consolidate")
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
_IMPORT_LOCK = threading.RLock()


def load_isolated(filename):
    """Load a fixed frozen dependency without replacing its ordinary import."""
    if filename not in ("run_baselines.py", "luna_handoff_worker.py", "run_consolidate.py"):
        raise ValueError("Unsupported Luna collaboration dependency")
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location("_luna_collaboration_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load frozen collaboration dependency")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def luna_transport_context():
    """Temporarily select Luna for function-local baseline_model imports."""
    import luna_model

    with _IMPORT_LOCK:
        missing = object()
        previous = sys.modules.get("baseline_model", missing)
        sys.modules["baseline_model"] = luna_model
        try:
            yield
        finally:
            if previous is missing:
                sys.modules.pop("baseline_model", None)
            else:
                sys.modules["baseline_model"] = previous


def make_timed_apply_handoff():
    """Keep the native role change, with only its remaining-time text updated."""
    consolidate = load_isolated("run_consolidate.py")
    original_messages = consolidate.handoff_messages

    def timed_messages(checkpoint):
        combined, messages = original_messages(checkpoint)
        seconds = consolidate.CONTINUATION_SECONDS
        old = f"You have {seconds} seconds from this continuation, with no monetary or model-turn limit. "
        new = (f"You have {seconds:.3f} seconds remaining in this continuation's total budget, "
               "including restore and handoff, with no monetary or model-turn limit. ")
        if len(messages) != 2 or messages[1]["content"].count(old) != 1:
            raise RuntimeError("Frozen Consolidate timing prompt changed")
        messages[1]["content"] = messages[1]["content"].replace(old, new)
        return combined, messages

    consolidate.handoff_messages = timed_messages

    def apply(agent, broker, checkpoint, *, remaining_seconds):
        if (isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float))
                or not math.isfinite(remaining_seconds) or remaining_seconds < 0):
            raise ValueError("Handoff remaining time must be finite and nonnegative")
        consolidate.CONTINUATION_SECONDS = float(remaining_seconds)
        return consolidate.apply_handoff(agent, broker, checkpoint)

    return apply


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=EXECUTION_SECONDS):
    """Run a fresh Luna prefix or a same-model checkpoint continuation.

    Prefix keeps the existing 1200-second paired-step WIP gate. Continuations
    use the positive remaining task allowance supplied by the supervisor,
    including restore/evidence/handoff. No model state is converted or fabricated.
    """
    if condition not in CONDITIONS:
        raise ValueError("Luna collaboration condition must be prefix, strongshare or consolidate")
    if (isinstance(execution_seconds, bool) or not isinstance(execution_seconds, (int, float))
            or not math.isfinite(execution_seconds) or execution_seconds <= 0):
        raise ValueError("Luna collaboration execution time must be finite and positive")
    if condition == "prefix" and execution_seconds != EXECUTION_SECONDS:
        raise ValueError("Luna prefix preserves the frozen 1200-second WIP horizon")
    if not isinstance(offline, bool):
        raise ValueError("offline must be a boolean")
    if task is None or getattr(task, "key", None) not in TASK_KEYS:
        raise ValueError("Task is outside the authorized Luna collaboration scope")
    from cross_task_specs import get_task
    if task.as_dict() != get_task(task.key).as_dict():
        raise ValueError("Task metadata differs from the pinned task registry")

    from luna_accounting import LunaAccounting
    from cross_task_workers import make_offline_model, packet_module_context

    with luna_transport_context():
        worker = load_isolated("run_baselines.py" if condition == "prefix" else "luna_handoff_worker.py")
        worker.MODEL = MODEL
        worker.MAX_OUTPUT = MAX_OUTPUT
        worker.BudgetGuard = LunaAccounting
        if condition == "prefix":
            # The task-aware native branch invokes drive_prefix with the frozen
            # paired AST gate; it does not use legacy fixed PREFIX_STEPS.
            return worker.phase_worker(Path(run_dir), "prefix", offline=offline, task=task)

        worker.VARIANT = VARIANT
        worker.CONTINUATION_SECONDS = float(execution_seconds)
        if condition == "consolidate":
            worker.apply_handoff = make_timed_apply_handoff()
        worker.make_offline_model = lambda agent_id, phase: make_offline_model(task, agent_id, phase)
        with packet_module_context(task):
            return worker.phase_worker(Path(run_dir), condition, offline=offline, task=task)
