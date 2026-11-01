"""Together transport for baseline agents, using a caller-owned shared ledger.

make_model performs no preflight or model request. The caller supplies one shared
BudgetGuard(enforce_limits=False, max_output_tokens=16384) for all baseline agents
and sets TOGETHER_API_KEY in the child process. No screenshot/OCR credential path
is used here. Requests and diagnostic files live under artifact_dir/responses/agent_id.
"""
import os
import re
import time
from pathlib import Path

from budget_guard import write_json
from run_solo_smoke import configure

MAX_OUTPUT = 16384
TOKEN_PATTERN = re.compile(r"(?:tgp_|sk-)[A-Za-z0-9_\-]+")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.\-]+")
SECRET_FIELDS = {"api_key", "apikey", "authorization", "access_token", "refresh_token", "secret"}


def _redact(value, key):
    if isinstance(value, dict):
        return {
            str(name): "[REDACTED]" if str(name).lower() in SECRET_FIELDS else _redact(item, key)
            for name, item in value.items()
        }
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
    """Return a LitellmModel subclass instance; only query() makes model requests.

    Transport: OpenAI SDK -> https://api.together.ai/v1, no SDK retries, at most
    one retry for an HTTP 5xx per query. Timeouts/4xx/usage/parse errors are not
    retried here. Usage is settled before any tool-call parsing, including a
    response with missing choices or invalid tool arguments.
    """
    if guard.model != model_name:
        raise ValueError("Shared guard model does not match the requested model")
    if guard.enforce_limits:
        raise ValueError("Baseline transport requires an accounting-only shared guard")
    if guard.max_output_tokens != MAX_OUTPUT:
        raise ValueError("Shared guard must use max_output_tokens=16384")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id must be a simple directory name")
    if not isinstance(phase, str) or not phase:
        raise ValueError("phase must be a nonempty string")

    configure()
    from openai import OpenAI
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel

    key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TOGETHER_API_KEY is absent; no API request made")
    raw_dir = Path(artifact_dir).resolve() / "responses" / agent_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=key, base_url="https://api.together.ai/v1", max_retries=0, timeout=90.0)
    rates = dict(guard.profile)
    ledger_phase = f"{phase}:{agent_id}"

    def save(number, suffix, value):
        write_json(raw_dir / f"request_{number:05d}{suffix}.json", _redact(value, key))

    class TogetherBaselineModel(LitellmModel):
        # Prevent a second retry layer even if another component changes the
        # process-global mini-SWE retry configuration after model construction.
        abort_exceptions = list(LitellmModel.abort_exceptions) + [Exception]

        def __init__(self):
            super().__init__(model_name=model_name)
            self.last_request_number = None
            self.last_stage = None
            self.response_directory = raw_dir

        def _query(self, messages, **kwargs):
            allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
            clean_messages = [{k: v for k, v in message.items() if k in allowed} for message in messages]
            payload = {
                "model": model_name, "messages": clean_messages, "tools": self._tools,
                "temperature": 0.7, "top_p": 0.8, "max_tokens": MAX_OUTPUT,
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            }
            for attempt in range(2):
                self.last_stage = "reserve"
                row = guard.reserve(ledger_phase)
                number = row["request_number"]
                self.last_request_number = number
                save(number, "_input", {
                    "phase": phase, "agent_id": agent_id, "transport_attempt": attempt + 1,
                    "request": payload,
                })
                self.last_stage = "transport"
                try:
                    response = client.chat.completions.create(**payload)
                    break
                except Exception as exc:
                    code = getattr(exc, "status_code", None)
                    guard.fail(row, type(exc).__name__, code)
                    save(number, "_error", {
                        "phase": phase, "agent_id": agent_id, "stage": "transport",
                        "http_status": code, "error_type": type(exc).__name__,
                        "body": getattr(exc, "body", None),
                        "request_id": getattr(exc, "request_id", None),
                    })
                    if attempt == 0 and isinstance(code, int) and 500 <= code < 600:
                        print(f"{ledger_phase}: HTTP {code}; retrying once", flush=True)
                        time.sleep(2)
                        continue
                    raise RuntimeError(
                        f"Together request failed: {type(exc).__name__}, HTTP {code}"
                    ) from None

            self.last_stage = "usage_accounting"
            usage = response.usage.model_dump() if response.usage else {}
            finish_reason = response.choices[0].finish_reason if response.choices else None
            try:
                cost = guard.record(row, usage, finish_reason)
            except Exception as exc:
                save(number, "", response.model_dump())
                save(number, "_error", {
                    "phase": phase, "agent_id": agent_id, "stage": self.last_stage,
                    "error_type": type(exc).__name__, "message": str(exc),
                })
                raise

            self.last_stage = "response_validation"
            raw = response.model_dump()
            save(number, "", raw)
            # Do not let an unexpectedly echoed credential reach an agent's
            # transcript, tools, or separate runner logs.
            import json
            if key in json.dumps(raw, ensure_ascii=False, default=str):
                raise RuntimeError("Credential unexpectedly present in response; stopped after recording usage")
            print(
                f"request {number}: {ledger_phase}, finish={finish_reason}, cost=${cost:.6f}",
                flush=True,
            )
            self.last_stage = "tool_call_parsing"
            return response

        def query(self, messages, **kwargs):
            try:
                return super().query(messages, **kwargs)
            except Exception as exc:
                if self.last_request_number is not None:
                    save(self.last_request_number, "_query_error", {
                        "phase": phase, "agent_id": agent_id, "stage": self.last_stage,
                        "error_type": type(exc).__name__, "message": str(exc),
                    })
                raise

        def _calculate_cost(self, response):
            # guard.record() already validated and persisted this usage. The
            # return value feeds mini-SWE's statistics; it is not a second charge.
            return {"cost": (
                response.usage.prompt_tokens * rates["input_rate"]
                + response.usage.completion_tokens * rates["output_rate"]
            )}

        def summarize_context(self, *args, **kwargs):
            raise RuntimeError("Context compaction is disabled in the baseline smoke run")

        def close(self):
            client.close()

    return TogetherBaselineModel()
