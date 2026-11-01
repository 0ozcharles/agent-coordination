"""Direct OpenAI Chat transport for isolated Luna pilots; constructing is free.

Only query() calls the API. The caller owns OPENAI_API_KEY in process memory.
No environment values, headers, or unredacted response credentials are logged.
"""
import json
import os
import re
import time
from pathlib import Path

from budget_guard import write_json
from luna_accounting import MODEL, MAX_OUTPUT
from run_solo_smoke import configure

TOKEN_PATTERN = re.compile(r"(?:tgp_|sk-)[A-Za-z0-9_\-]+")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.\-]+")
SECRET_FIELDS = {"api_key", "apikey", "authorization", "access_token", "refresh_token",
                 "secret", "headers", "request_headers", "response_headers"}


def _redact(value, key):
    if isinstance(value, dict):
        return {_redact(str(name), key): "[REDACTED]" if str(name).lower() in SECRET_FIELDS
                else _redact(item, key) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if isinstance(value, str):
        if key:
            value = value.replace(key, "[REDACTED]")
        return BEARER_PATTERN.sub("Bearer [REDACTED]", TOKEN_PATTERN.sub("[REDACTED]", value))
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _redact(str(value), key)


def make_model(model_name, guard, phase, agent_id, artifact_dir):
    """Same interface as baseline_model.make_model, without global profile edits."""
    if model_name != MODEL or guard.model != model_name:
        raise ValueError("Luna transport requires matching gpt-6-luna accounting")
    if guard.enforce_limits or guard.max_output_tokens != MAX_OUTPUT:
        raise ValueError("Luna pilot requires accounting_only and 16384 max output")
    if guard.cache_write_ttl != "30m":
        raise ValueError("Chat Completions pilot supports only 30m cache writes")
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id must be a simple directory name")
    if not isinstance(phase, str) or not phase:
        raise ValueError("phase must be nonempty")
    configure()
    import httpx
    from openai import OpenAI
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is absent; no API request made")
    raw_dir = Path(artifact_dir).resolve() / "responses" / agent_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0,
                    timeout=90.0, http_client=httpx.Client(trust_env=False))
    ledger_phase = f"{phase}:{agent_id}"

    def save(number, suffix, value):
        write_json(raw_dir / f"request_{number:05d}{suffix}.json", _redact(value, key))

    class LunaModel(LitellmModel):
        # Block mini-SWE's independent retry layer for every Exception. Only
        # the explicit 5xx branch below may issue a second transport request.
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]

        def __init__(self):
            super().__init__(model_name=model_name, litellm_model_registry=None)
            self.last_request_number = None
            self.last_stage = None
            self.response_directory = raw_dir
            self._settled_response = None
            self._settled_cost = None

        def _query(self, messages, **kwargs):
            self.last_request_number = None
            self.last_stage = "input_validation"
            self._settled_response = None
            self._settled_cost = None
            allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
            clean = [{k: v for k, v in message.items() if k in allowed} for message in messages]
            payload = {"model": model_name, "messages": clean, "tools": self._tools,
                       "reasoning_effort": "none", "temperature": 0.7, "top_p": 0.8,
                       "max_completion_tokens": MAX_OUTPUT, "service_tier": "default",
                       # Official Chat field, forwarded because installed SDK
                       # 2.21 predates prompt_cache_options' typed parameter.
                       "extra_body": {"prompt_cache_options": {"mode": "implicit", "ttl": "30m"}}}
            if key in json.dumps(payload, ensure_ascii=False, default=str):
                raise RuntimeError("Credential present in input; no API request made")
            for attempt in range(2):
                self.last_stage = "reserve"
                row = guard.reserve(ledger_phase)
                number = self.last_request_number = row["request_number"]
                save(number, "_input", {"phase": phase, "agent_id": agent_id,
                                        "transport_attempt": attempt + 1, "request": payload})
                self.last_stage = "transport"
                try:
                    response = client.chat.completions.create(**payload)
                    break
                except Exception as exc:
                    code = getattr(exc, "status_code", None)
                    guard.fail(row, type(exc).__name__, code)
                    save(number, "_error", {"phase": phase, "agent_id": agent_id,
                                           "stage": "transport", "http_status": code,
                                           "error_type": type(exc).__name__,
                                           "body": getattr(exc, "body", None),
                                           "request_id": getattr(exc, "request_id", None)})
                    if attempt == 0 and type(code) is int and 500 <= code < 600:
                        print(f"{ledger_phase}: HTTP {code}; retrying once", flush=True)
                        time.sleep(2)
                        continue
                    raise RuntimeError(f"OpenAI request failed: {type(exc).__name__}, HTTP {code}") from None

            # Raw response is captured even if usage or tool JSON is malformed.
            # Do not call the tool parser before settling the charge.
            self.last_stage = "usage_accounting"
            raw = response.model_dump()
            safe_raw = _redact(raw, key)
            save(number, "", safe_raw)
            choices = raw.get("choices") or []
            finish_reason = choices[0].get("finish_reason") if choices else None
            try:
                cost = guard.record(row, safe_raw.get("usage") or {}, finish_reason,
                                    service_tier=safe_raw.get("service_tier", "default"),
                                    response_model=safe_raw.get("model"))
            except Exception as exc:
                save(number, "_error", {"phase": phase, "agent_id": agent_id,
                                       "stage": self.last_stage, "error_type": type(exc).__name__,
                                       "message": str(exc)})
                raise
            self._settled_response, self._settled_cost = response, cost
            self.last_stage = "response_validation"
            if key in json.dumps(raw, ensure_ascii=False, default=str):
                raise RuntimeError("Credential unexpectedly present in response; usage recorded")
            print(f"request {number}: {ledger_phase}, finish={finish_reason}, cost=${cost:.6f}", flush=True)
            self.last_stage = "tool_call_parsing"
            return response

        def query(self, messages, **kwargs):
            try:
                return super().query(messages, **kwargs)
            except Exception as exc:
                if self.last_request_number is not None:
                    save(self.last_request_number, "_query_error", {
                        "phase": phase, "agent_id": agent_id, "stage": self.last_stage,
                        "error_type": type(exc).__name__, "message": str(exc)})
                else:
                    write_json(raw_dir / "request_not_sent_error.json", _redact({
                        "phase": phase, "agent_id": agent_id, "stage": self.last_stage,
                        "api_call_made": False, "error_type": type(exc).__name__,
                        "message": str(exc)}, key))
                # Exceptions normally contain no request/headers. Sanitize the
                # final propagated error if an SDK/custom exception echoes key.
                if key in str(exc):
                    raise RuntimeError("Luna query failed; redacted details saved") from None
                raise

        def _calculate_cost(self, response):
            if response is not self._settled_response or self._settled_cost is None:
                raise RuntimeError("Response has no settled accounting record")
            # Mini-SWE statistics only: never charge the ledger twice.
            return {"cost": self._settled_cost}

        def summarize_context(self, *args, **kwargs):
            raise RuntimeError("Context compaction is disabled in the Luna pilot")

        def close(self):
            client.close()

    return LunaModel()
