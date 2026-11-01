"""Isolated, persistent GPT-6 Luna Standard accounting (no API calls).

Prices verified against the model page and prompt-caching guide, 2026-10-31:
https://developers.openai.com/api/docs/models/gpt-6-luna
https://developers.openai.com/api/docs/guides/prompt-caching

Cache writes replace ordinary input charges; they are not an added surcharge.
One instance is shared across threads. As with BudgetGuard, the caller must
serialize processes/instances that use the same ledger. Old ledgers and global
MODEL_PROFILES are never changed. Reservations are diagnostic, not spending caps.
"""
import copy
import json
import time
from decimal import Decimal
from functools import wraps
from pathlib import Path
from threading import RLock

from budget_guard import write_json

MODEL = "gpt-6-luna"
MAX_OUTPUT = 16384
LONG_INPUT_THRESHOLD = 272000
PROFILE = {
    "input_rate": 0.10 / 1_000_000,
    "cached_input_rate": 0.01 / 1_000_000,
    "cache_write_30m_rate": 0.125 / 1_000_000,
    "cache_write_12h_rate": 0.25 / 1_000_000,
    "output_rate": 0.50 / 1_000_000,
    "max_context": 1_050_000,
    "price_source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
}


class UsageAccountingError(RuntimeError):
    """The received usage cannot safely be assigned a known price."""


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise UsageAccountingError(f"Missing or invalid {name}")
    return value


def _check_fields(data, allowed, name):
    if not isinstance(data, dict):
        raise UsageAccountingError(f"Invalid {name} object")
    # Unknown fields may introduce a new pricing category. Even zero is not
    # silently accepted; null SDK placeholder fields are harmless.
    if any(key not in allowed and value is not None for key, value in data.items()):
        raise UsageAccountingError(f"Unrecognized non-null field in {name}")


def calculate_usage(usage, *, cache_write_ttl="30m", service_tier="default"):
    """Return validated token buckets and exact-decimal-derived USD cost.

    Chat Completions currently supports only 30m. 12h is supported here for
    explicit accounting callers, not enabled by the Chat transport. Mixed TTLs
    or new provider fields are rejected rather than priced by assumption.
    """
    if service_tier not in (None, "default"):
        raise UsageAccountingError("Unexpected service tier; Standard pricing not applicable")
    if cache_write_ttl not in ("30m", "12h"):
        raise UsageAccountingError("Unrecognized cache write lifetime")
    _check_fields(usage, {"prompt_tokens", "completion_tokens", "total_tokens",
                         "prompt_tokens_details", "completion_tokens_details"}, "usage")
    prompt = _integer(usage.get("prompt_tokens"), "prompt_tokens")
    completion = _integer(usage.get("completion_tokens"), "completion_tokens")
    if usage.get("total_tokens") is not None:
        if _integer(usage["total_tokens"], "total_tokens") != prompt + completion:
            raise UsageAccountingError("Token total does not match prompt plus completion")
    details = usage.get("prompt_tokens_details")
    details = {} if details is None else details
    _check_fields(details, {"cached_tokens", "cache_write_tokens", "audio_tokens",
                            "image_tokens", "text_tokens"}, "prompt_tokens_details")
    output = usage.get("completion_tokens_details")
    output = {} if output is None else output
    _check_fields(output, {"reasoning_tokens", "accepted_prediction_tokens",
                           "rejected_prediction_tokens", "audio_tokens", "text_tokens"},
                  "completion_tokens_details")
    for name, obj, bound in (("prompt_tokens_details", details, prompt),
                              ("completion_tokens_details", output, completion)):
        for key, value in obj.items():
            if value is None:
                continue
            if _integer(value, name + "." + key) > bound:
                raise UsageAccountingError("Token detail exceeds its parent total")
        if any(obj.get(key) not in (None, 0) for key in ("audio_tokens", "image_tokens")):
            raise UsageAccountingError("Non-text usage requires a separately verified price")
    cached = details.get("cached_tokens") or 0
    written = details.get("cache_write_tokens") or 0
    if cached + written > prompt:
        raise UsageAccountingError("Cached plus cache-write tokens exceed prompt tokens")
    ordinary = prompt - cached - written
    long_input = prompt > LONG_INPUT_THRESHOLD
    input_multiplier = Decimal(2 if long_input else 1)
    output_multiplier = Decimal("1.5" if long_input else "1")
    per_million = Decimal(1_000_000)
    rates = {"ordinary_input": Decimal("0.10") * input_multiplier,
             "cached_input": Decimal("0.01") * input_multiplier,
             "cache_write": Decimal("0.125" if cache_write_ttl == "30m" else "0.25") * input_multiplier,
             "output": Decimal("0.50") * output_multiplier}
    components = {"ordinary_input": Decimal(ordinary) * rates["ordinary_input"] / per_million,
                  "cached_input": Decimal(cached) * rates["cached_input"] / per_million,
                  "cache_write": Decimal(written) * rates["cache_write"] / per_million,
                  "output": Decimal(completion) * rates["output"] / per_million}
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "ordinary_input_tokens": ordinary, "cached_input_tokens": cached,
            "cache_write_tokens": written, "cache_write_ttl": cache_write_ttl,
            "long_context_pricing": long_input,
            "rates_usd_per_million": {k: float(v) for k, v in rates.items()},
            "cost_components_usd": {k: float(v) for k, v in components.items()},
            "actual_cost_usd": float(sum(components.values()))}


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class LunaAccounting:
    """BudgetGuard-compatible accounting-only ledger, with idempotent settlement."""

    def __init__(self, ledger_path, run_id, model=MODEL, enforce_limits=False,
                 max_output_tokens=MAX_OUTPUT, *, cache_write_ttl="30m"):
        if model != MODEL or enforce_limits is not False:
            raise ValueError("LunaAccounting only supports accounting-only gpt-6-luna")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if cache_write_ttl not in ("30m", "12h"):
            raise ValueError("Unrecognized cache write lifetime")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be nonempty")
        self.path, self.run_id = Path(ledger_path), run_id
        self.model, self.profile = model, dict(PROFILE)
        self.max_output_tokens, self.cache_write_ttl = max_output_tokens, cache_write_ttl
        self.enforce_limits, self.enforcement_mode = False, "accounting_only"
        self._lock = RLock()
        self.request_reserve = (self.profile["max_context"] * self.profile[f"cache_write_{cache_write_ttl}_rate"] * 2
                                + max_output_tokens * self.profile["output_rate"] * 1.5)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if (self.data.get("accounting_schema") != "luna_standard_v1"
                    or self.data.get("model") != model
                    or self.data.get("cache_write_ttl") != cache_write_ttl
                    or self.data.get("max_output_tokens") != max_output_tokens):
                raise ValueError("Refusing incompatible existing ledger")
            if not isinstance(self.data.get("requests"), list):
                raise ValueError("Invalid ledger requests")
            for row in self.data["requests"]:
                if row.get("model") != model:
                    raise ValueError("Refusing mixed-model ledger")
        else:
            self.data = {"accounting_schema": "luna_standard_v1", "model": model,
                         "requests": [], "price_checked_date": "2026-10-31",
                         "price_source": PROFILE["price_source"],
                         "cache_price_source": "https://developers.openai.com/api/docs/guides/prompt-caching",
                         "input_usd_per_million": 0.10, "cached_input_usd_per_million": 0.01,
                         "cache_write_30m_usd_per_million": 0.125,
                         "cache_write_12h_usd_per_million": 0.25, "output_usd_per_million": 0.50,
                         "long_input_threshold": LONG_INPUT_THRESHOLD,
                         "max_context": PROFILE["max_context"], "cache_write_ttl": cache_write_ttl,
                         "service_tier": "default", "request_reservation_usd": self.request_reserve,
                         "max_output_tokens": max_output_tokens, "enforcement_mode": self.enforcement_mode,
                         "round_limit_usd": None}

    def _owned(self, row):
        if not any(candidate is row for candidate in self.data["requests"]):
            raise ValueError("Request row does not belong to this accounting instance")
        if row["run_id"] != self.run_id:
            raise ValueError("Request row belongs to a different run")

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self.data)

    @_locked
    def reserve(self, phase):
        if not isinstance(phase, str) or not phase:
            raise ValueError("phase must be nonempty")
        numbers = [r["request_number"] for r in self.data["requests"] if r["run_id"] == self.run_id]
        row = {"run_id": self.run_id, "phase": phase, "request_number": max(numbers, default=0) + 1,
               "started_at_unix": time.time(), "reserved_usd": self.request_reserve,
               "model": self.model, "input_rate": self.profile["input_rate"],
               "output_rate": self.profile["output_rate"], "max_output_tokens": self.max_output_tokens,
               "cache_write_ttl": self.cache_write_ttl, "enforcement_mode": self.enforcement_mode,
               "status": "in_flight", "actual_cost_usd": None}
        self.data["requests"].append(row)
        self._persist()
        return row

    @_locked
    def fail(self, row, kind, status_code=None):
        self._owned(row)
        if row["status"] == "completed":
            raise ValueError("Cannot erase settled usage with fail()")
        # Only identifiers/codes, never raw exception bodies or credentials.
        if not isinstance(kind, str) or not kind.replace("_", "").isalnum():
            raise ValueError("Error kind must be an identifier")
        row.update({"status": "error", "error_type": kind, "http_status": status_code,
                    "finished_at_unix": time.time()})
        self._persist()

    @_locked
    def record(self, row, usage, finish_reason, *, service_tier="default", response_model=MODEL):
        self._owned(row)
        if row["status"] == "completed":
            if (row["usage"] == usage and row["finish_reason"] == finish_reason
                    and row["response_service_tier"] == service_tier
                    and row["response_model"] == response_model):
                return row["actual_cost_usd"]
            raise ValueError("Conflicting duplicate settlement")
        if row["status"] != "in_flight":
            raise ValueError("Cannot settle a failed request row")
        # The transport passes already-redacted usage. Persist it even when a
        # new field cannot yet be priced; such requests remain unknown, not $0.
        row.update({"usage": copy.deepcopy(usage), "finish_reason": finish_reason,
                    "response_service_tier": service_tier, "response_model": response_model})
        try:
            if not isinstance(response_model, str) or not (response_model == MODEL or response_model.startswith(MODEL + "-")):
                raise UsageAccountingError("Unexpected response model; verified price not applicable")
            result = calculate_usage(usage, cache_write_ttl=row["cache_write_ttl"], service_tier=service_tier)
        except UsageAccountingError as exc:
            row["accounting_error"] = str(exc)
            self.fail(row, "MissingOrUnrecognizedUsage")
            raise
        row.update(result)
        row.update({"status": "completed", "finished_at_unix": time.time()})
        if result["prompt_tokens"] > PROFILE["max_context"] or result["completion_tokens"] > self.max_output_tokens:
            row["usage_bound_exceeded"] = True
        self._persist()
        return row["actual_cost_usd"]

    @_locked
    def summary(self):
        rows = [r for r in self.data["requests"] if r["run_id"] == self.run_id]
        return {"request_count": len(rows), "enforcement_mode": self.enforcement_mode,
                "known_cost_usd": sum(r.get("actual_cost_usd") or 0 for r in rows),
                "reserved_upper_bound_usd": sum(r["reserved_usd"] for r in rows),
                "budget_occupied_usd": sum(r["reserved_usd"] if r.get("actual_cost_usd") is None
                                           else r["actual_cost_usd"] for r in rows),
                "unknown_usage_requests": sum(r.get("actual_cost_usd") is None for r in rows),
                "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
                "completion_tokens": sum(r.get("completion_tokens", 0) for r in rows),
                "cached_input_tokens": sum(r.get("cached_input_tokens", 0) for r in rows),
                "cache_write_tokens": sum(r.get("cache_write_tokens", 0) for r in rows)}


# Convenient private-module binding for unchanged worker imports.
BudgetGuard = LunaAccounting
