"""Persistent accounting: settle valid usage, reserve unresolved HTTP requests."""
import json
import time
from functools import wraps
from pathlib import Path
from threading import RLock

MODEL = "openai/gpt-oss-120b"
MODEL_PROFILES = {
    MODEL: {
        "input_rate": 0.15 / 1_000_000,
        "output_rate": 0.60 / 1_000_000,
        "max_context": 131072,
        "price_source": "https://docs.together.ai/docs/gpt-oss",
    },
    "Qwen/Qwen3.5-9B": {
        "input_rate": 0.17 / 1_000_000,
        "output_rate": 0.25 / 1_000_000,
        "max_context": 262144,
        "price_source": "https://docs.together.ai/docs/serverless/models",
    },
}
# Default-model constants remain available to existing callers.
INPUT_RATE = MODEL_PROFILES[MODEL]["input_rate"]
OUTPUT_RATE = MODEL_PROFILES[MODEL]["output_rate"]
MAX_CONTEXT = MODEL_PROFILES[MODEL]["max_context"]
MAX_OUTPUT = 4096
# Reserve a full context plus full output before each request. Valid completed
# requests settle to actual cost; failed/ambiguous requests keep the reservation.
REQUEST_RESERVE = MAX_CONTEXT * INPUT_RATE + MAX_OUTPUT * OUTPUT_RATE
MAX_REQUESTS = 30
RUN_BUDGET = 1.0
ROUND_BUDGET = 5.0


class BudgetExceeded(RuntimeError):
    pass


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def budget_occupied(row):
    """Current budget use, with full reservations retained for unknown usage."""
    reserved = row["reserved_usd"]
    actual = row.get("actual_cost_usd")
    if row.get("status") != "completed" or actual is None:
        return reserved
    profile = MODEL_PROFILES[row.get("model", MODEL)]
    # Detect historical violations too, before the explicit row marker existed.
    exceeded = (row.get("usage_bound_exceeded")
                or row.get("prompt_tokens", 0) > profile["max_context"]
                or row.get("completion_tokens", 0) > row.get("max_output_tokens", MAX_OUTPUT)
                or actual > reserved)
    return max(actual, reserved) if exceeded else actual


def _locked(method):
    """Serialize state changes and ledger writes on one BudgetGuard instance."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class BudgetGuard:
    """Thread-safe operations on one shared instance.

    Caller must ensure no other instance or process accesses this ledger for
    this object's lifetime (including construction). The instance RLock does
    not coordinate separate instances. Treat returned request rows as opaque:
    update them through ``record`` and ``fail``, not by direct mutation.

    ``enforce_limits=False`` records costs without stopping for monetary,
    request-count, or reservation-bound limits. Invalid usage still raises.
    """

    def __init__(self, ledger_path, run_id, model=MODEL, enforce_limits=True,
                 max_output_tokens=MAX_OUTPUT):
        self._lock = RLock()
        self.path = Path(ledger_path)
        self.run_id = run_id
        self.enforce_limits = enforce_limits
        self.enforcement_mode = "enforced" if enforce_limits else "accounting_only"
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self.max_output_tokens = max_output_tokens
        if model not in MODEL_PROFILES:
            raise ValueError(f"No verified price profile for model: {model}")
        self.model = model
        self.profile = dict(MODEL_PROFILES[model])
        self.request_reserve = (self.profile["max_context"] * self.profile["input_rate"]
                                + self.max_output_tokens * self.profile["output_rate"])
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"requests": []}
        # Older ledgers identified the model only at the top level. Attach that
        # original identity before changing the active-model metadata; never
        # recalculate previously reserved amounts or recorded actual costs.
        legacy_model = self.data.get("model", MODEL)
        migrated = False
        for row in self.data["requests"]:
            if "max_output_tokens" not in row:
                row["max_output_tokens"] = MAX_OUTPUT
                migrated = True
            if "model" not in row:
                row["model"] = legacy_model
                migrated = True
            old_profile = MODEL_PROFILES[row["model"]]
            for field in ("input_rate", "output_rate"):
                if field not in row:
                    global_field = field.replace("_rate", "_usd_per_million")
                    old_global_rate = self.data.get(global_field)
                    row[field] = (old_global_rate / 1_000_000
                                  if row["model"] == legacy_model and old_global_rate is not None
                                  else old_profile[field])
                    migrated = True
        self.data.update({
            "model": model,
            "input_usd_per_million": self.profile["input_rate"] * 1_000_000,
            "output_usd_per_million": self.profile["output_rate"] * 1_000_000,
            "price_checked_date": "2026-10-29",
            "price_source": self.profile["price_source"],
            "request_reservation_usd": self.request_reserve,
            "max_output_tokens": self.max_output_tokens,
            "round_limit_usd": ROUND_BUDGET,
            "enforcement_mode": self.enforcement_mode,
        })
        if migrated:
            write_json(self.path, self.data)

    @_locked
    def reserve(self, phase):
        rows = self.data["requests"]
        current = [r for r in rows if r["run_id"] == self.run_id]
        if self.enforce_limits:
            if self.data.get("halt_reason"):
                raise BudgetExceeded("Ledger is halted: " + self.data["halt_reason"])
            if len(current) >= MAX_REQUESTS:
                raise BudgetExceeded("30-request limit reached (preflight included)")
            if sum(budget_occupied(r) for r in current) + self.request_reserve > RUN_BUDGET:
                raise BudgetExceeded("Per-run reservation would exceed $1")
            if sum(budget_occupied(r) for r in rows) + self.request_reserve > ROUND_BUDGET:
                raise BudgetExceeded("Round reservation would exceed $5")
        row = {"run_id": self.run_id, "phase": phase, "request_number": len(current) + 1,
               "started_at_unix": time.time(), "reserved_usd": self.request_reserve,
               "model": self.model, "input_rate": self.profile["input_rate"],
               "output_rate": self.profile["output_rate"],
               "max_output_tokens": self.max_output_tokens,
               "enforcement_mode": self.enforcement_mode,
               "status": "in_flight", "actual_cost_usd": None}
        rows.append(row)
        write_json(self.path, self.data)
        return row

    @_locked
    def fail(self, row, kind, status_code=None):
        row.update({"status": "error", "error_type": kind, "http_status": status_code})
        write_json(self.path, self.data)

    @_locked
    def record(self, row, usage, finish_reason):
        # Missing usage must stop the run; it must never become a zero-cost call.
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
            self.fail(row, "MissingOrInvalidUsage")
            raise RuntimeError("Provider did not return valid token usage")
        cost = (prompt * self.profile["input_rate"]
                + completion * self.profile["output_rate"])
        row.update({"status": "completed", "prompt_tokens": prompt,
                    "completion_tokens": completion, "usage": usage,
                    "actual_cost_usd": cost, "finish_reason": finish_reason})
        if (prompt > self.profile["max_context"]
                or completion > row.get("max_output_tokens", MAX_OUTPUT)
                or cost > row["reserved_usd"]):
            row["usage_bound_exceeded"] = True
            self.data["halt_reason"] = "Provider usage exceeded the reserved request bound"
            write_json(self.path, self.data)
            # Keep the violation for any future enforced run, but accounting-only
            # runs continue and report the measured cost instead of interrupting.
            if self.enforce_limits:
                raise RuntimeError("Provider usage exceeded the reserved request bound; stopped")
            return cost
        write_json(self.path, self.data)
        return cost

    @_locked
    def summary(self):
        """Summarize this run's actual charges and current budget occupation.

        ``reserved_upper_bound_usd`` is retained for compatibility: it is the
        historical sum of reservations, not the current spending upper bound
        or remaining budget occupation. Use ``budget_occupied_usd`` for the
        settled costs plus reservations still held for unresolved requests.
        """
        rows = [r for r in self.data["requests"] if r["run_id"] == self.run_id]
        return {"request_count": len(rows),
                "enforcement_mode": self.enforcement_mode,
                "known_cost_usd": sum(r.get("actual_cost_usd") or 0 for r in rows),
                "reserved_upper_bound_usd": sum(r["reserved_usd"] for r in rows),
                "budget_occupied_usd": sum(budget_occupied(r) for r in rows),
                "unknown_usage_requests": sum(r.get("actual_cost_usd") is None for r in rows),
                "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
                "completion_tokens": sum(r.get("completion_tokens", 0) for r in rows)}
