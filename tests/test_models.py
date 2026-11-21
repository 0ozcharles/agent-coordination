import os
import unittest
from unittest import mock

from agent_coordination import models


class Response:
    def __init__(self, headers=None):
        self._hidden_params = {"additional_headers": headers or {}}


class RouteTest(unittest.TestCase):
    def test_bare_and_openai_names_go_to_openai(self):
        self.assertEqual(models.route("gpt-6-luna"), ("openai/gpt-6-luna", {}, "gpt-6-luna"))
        self.assertEqual(models.route("openai/gpt-6.1-sol"), ("openai/gpt-6.1-sol", {}, "gpt-6.1-sol"))
        self.assertEqual(models.provider_of("gpt-6-luna"), "openai")

    def test_ai2s_uses_the_proxy_base_and_key(self):
        with mock.patch.dict(os.environ, {"AI2S_BASE_URL": "https://proxy.example/v1", "AI2S_API_KEY": "k"}):
            model, kwargs, name = models.route("ai2s/claude-sonnet-5-5")
        self.assertEqual((model, name), ("openai/claude-sonnet-5-5", "claude-sonnet-5-5"))
        self.assertEqual(kwargs, {"api_base": "https://proxy.example/v1", "api_key": "k"})
        self.assertEqual(models.provider_of("ai2s/claude-sonnet-5-5"), "ai2s")

    def test_ai2s_without_credentials_refuses_before_any_request(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AI2S_")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                models.route("ai2s/claude-haiku-5-5")

    def test_together_keeps_the_org_in_the_name(self):
        model, kwargs, name = models.route("together/meta-llama/Llama-3.3-70B-Instruct-Turbo")
        self.assertEqual(model, "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo")
        self.assertEqual((kwargs, name), ({}, "meta-llama/Llama-3.3-70B-Instruct-Turbo"))

    def test_an_org_name_that_is_not_a_provider_is_left_alone(self):
        self.assertEqual(models.route("some-org/some-model")[0], "openai/some-org/some-model")


class CostTest(unittest.TestCase):
    def test_proxy_header_wins(self):
        self.assertEqual(models.cost_of(Response({"llm_provider-x-litellm-response-cost": "4.18e-06"}), "claude-haiku-5-5"), 4.18e-06)
        self.assertEqual(models.cost_of(Response({"x-litellm-response-cost": 0.002}), "x"), 0.002)

    def test_no_header_falls_back_to_litellm_or_none(self):
        # a bare Response has no usage, so litellm cannot price it and the fallback returns None
        self.assertIsNone(models.cost_of(Response(), "claude-haiku-5-5"))


if __name__ == "__main__":
    unittest.main()
