"""Model routing: a provider prefix in a config's model name picks the endpoint and the key.

- "gpt-6-luna" or "openai/gpt-6-luna": OpenAI with OPENAI_API_KEY (the default, as before).
- "ai2s/claude-sonnet-5-5": the University of Arizona AI Assistant, an OpenAI-compatible LiteLLM proxy,
  with AI2S_BASE_URL and AI2S_API_KEY from the gitignored .env (litellm loads .env on import).
- "together/<org>/<model>": Together with TOGETHER_API_KEY (litellm's together_ai provider).

Costs: the proxy reports each call's cost in a response header, which is taken when present; otherwise
litellm's price list is consulted under the bare model name.
"""
import os

PROVIDERS = {
    "openai": {"prefix": "openai/", "key": "OPENAI_API_KEY"},
    "ai2s": {"prefix": "openai/", "key": "AI2S_API_KEY", "base": "AI2S_BASE_URL"},
    "together": {"prefix": "together_ai/", "key": "TOGETHER_API_KEY"},
}
COST_HEADERS = ("llm_provider-x-litellm-response-cost", "x-litellm-response-cost")


def route(model):
    """(litellm model string, extra completion kwargs, bare model name) for a possibly provider-prefixed name."""
    provider, _, name = model.partition("/")
    if not name or provider not in PROVIDERS:  # no prefix, or an org/model name that is not a provider
        provider, name = "openai", model
    spec = PROVIDERS[provider]
    kwargs = {}
    if spec.get("base"):
        base, key = os.environ.get(spec["base"]), os.environ.get(spec["key"])
        if not base or not key:
            raise RuntimeError(f"{spec['base']} and {spec['key']} must be set (in .env) to use {model}; no request was made.")
        kwargs = {"api_base": base, "api_key": key}
    return spec["prefix"] + name, kwargs, name


def provider_of(model):
    provider, _, name = model.partition("/")
    return provider if name and provider in PROVIDERS else "openai"


def cost_of(response, name):
    """Dollars for one response: the proxy's cost header when present, else litellm's price for the model."""
    hidden = getattr(response, "_hidden_params", None) or {}
    headers = hidden.get("additional_headers") or {}
    for key in COST_HEADERS:
        if headers.get(key) not in (None, ""):
            try:
                return float(headers[key])
            except (TypeError, ValueError):
                pass
    try:
        import litellm
        return litellm.completion_cost(completion_response=response, model=name)
    except Exception:
        return None
