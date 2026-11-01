"""Run the frozen fresh-Solo worker with a process-local Luna transport.

Only the model transport/accounting are substituted. The upstream agent prompt,
sandbox, tool behavior, publication and initial-state checks are unchanged.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def phase_worker(run_dir, task, offline=False, execution_seconds=1200):
    import luna_model
    from luna_accounting import LunaAccounting

    spec = importlib.util.spec_from_file_location("_luna_fresh_solo", ROOT / "solo_expansion_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    worker.MODEL = "gpt-6-luna"
    worker.MAX_OUTPUT = 16384
    worker.BudgetGuard = LunaAccounting
    missing = object()
    previous = sys.modules.get("baseline_model", missing)
    sys.modules["baseline_model"] = luna_model
    try:
        return worker.phase_worker(Path(run_dir), task, offline=offline,
                                   execution_seconds=execution_seconds)
    finally:
        if previous is missing:
            sys.modules.pop("baseline_model", None)
        else:
            sys.modules["baseline_model"] = previous
