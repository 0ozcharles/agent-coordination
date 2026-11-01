"""Task-parameterized adapters for the frozen prefix, handoff and fresh Solo workers.

Agent protocol, model settings and publication semantics come from the existing
workers. Private import contexts select the new diff packet and source-scoped
peer evidence. The offline probe is parameterized by task source directories.
"""
from __future__ import annotations

import importlib.util
import importlib
import json
import math
import shlex
import sys
import threading
from contextlib import contextmanager
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HANDOFF_SECONDS = 1200
CONDITIONS = ("prefix", "strongshare", "consolidate", "solo")
VARIANT = "source_handoff_diff_v1"
_PACKET_LOCK = threading.RLock()


@contextmanager
def packet_module_context(task=None):
    """Bind private packet/publisher adapters, then restore prior import state."""
    with _PACKET_LOCK:
        missing = object()
        previous = sys.modules.get("handoff_packet", missing)
        previous_evidence = sys.modules.get("baseline_evidence", missing)
        replacement = importlib.import_module("cross_task_packet")
        if task is not None:
            from cross_task_evidence import SourceScopedPublisher
            evidence_module = load_isolated("baseline_evidence.py")
            evidence_module.StrongSharePublisher = partial(SourceScopedPublisher, task=task)
            sys.modules["baseline_evidence"] = evidence_module
        sys.modules["handoff_packet"] = replacement
        try:
            yield
        finally:
            if previous is missing:
                sys.modules.pop("handoff_packet", None)
            else:
                sys.modules["handoff_packet"] = previous
            if task is not None:
                if previous_evidence is missing:
                    sys.modules.pop("baseline_evidence", None)
                else:
                    sys.modules["baseline_evidence"] = previous_evidence


def load_isolated(filename):
    """Load a fixed local dependency without mutating its ordinary import."""
    if filename not in ("handoff_worker.py", "baseline_evidence.py",
                        "evaluate_task_expansion.py", "evaluate_solo_expansion.py"):
        raise ValueError("Unsupported isolated dependency")
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location("_cross_task_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load frozen worker dependency")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def offline_commands(task, agent_id):
    if agent_id not in ("agent1", "agent2") or not task.code_roots:
        raise ValueError("Offline handoff requires two agents and explicit source roots")
    peer = "agent2" if agent_id == "agent1" else "agent1"
    checks = [f"test -s /shared-evidence/{peer}/latest/task.md",
              f"test -s /shared-evidence/{peer}/latest/history.json"]
    for root in task.code_roots:
        path = Path(root)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe source root")
        checks.append("test -d " + shlex.quote(f"/shared-evidence/{peer}/latest/worktree/{root}"))
    checks.extend(["test ! -e /patches",
                   "if touch /shared-evidence/write-probe; then exit 88; else echo handoff_read_only_ok; fi"])
    return [" && ".join(checks),
            "printf 'synthetic handoff infrastructure check, not a solution\\n' > "
            f"handoff_offline_{agent_id}.txt && git add -A && "
            "git commit -m 'Offline explicit handoff check' && git push",
            "gh pr create --title 'Offline handoff check' --body 'Synthetic infrastructure check'",
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]


def make_offline_model(task, agent_id, condition):
    if condition not in ("strongshare", "consolidate"):
        raise ValueError("Unknown handoff condition")
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    from run_consolidate import validate_message_layout
    commands = offline_commands(task, agent_id)

    class OfflineModel(LitellmModel):
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]

        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            validate_message_layout(messages)
            if self.index >= len(commands):
                raise RuntimeError("Unexpected extra offline handoff turn")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-handoff-{agent_id}-{self.index}", "object": "chat.completion", "created": 0,
                "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline infrastructure check.", "tool_calls": [{
                    "id": f"call_handoff_{agent_id}_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def phase_worker(run_dir, condition, task, offline=False, execution_seconds=HANDOFF_SECONDS):
    if condition not in CONDITIONS or task is None:
        raise ValueError("An explicit registered task and condition are required")
    if (isinstance(execution_seconds, bool) or not math.isfinite(execution_seconds)
            or execution_seconds <= 0):
        raise ValueError("execution_seconds must be finite and positive")
    if condition == "solo":
        from solo_expansion_worker import phase_worker as fresh_solo
        return fresh_solo(Path(run_dir), task, offline=offline, execution_seconds=execution_seconds)
    if execution_seconds != HANDOFF_SECONDS:
        raise ValueError("Prefix and handoff workers use the frozen 1200-second rule")
    if condition == "prefix":
        from run_baselines import phase_worker as prefix
        return prefix(Path(run_dir), "prefix", offline=offline, task=task)
    module = load_isolated("handoff_worker.py")
    module.VARIANT = VARIANT
    module.make_offline_model = lambda agent_id, phase: make_offline_model(task, agent_id, phase)
    with packet_module_context(task):
        return module.phase_worker(Path(run_dir), condition, offline=offline, task=task)
