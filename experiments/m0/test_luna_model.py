"""Exercise real mini-SWE parsing with mocked OpenAI SDK; zero API calls."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import luna_model
from luna_accounting import LunaAccounting, MODEL

luna_model.configure()
from openai.types.chat import ChatCompletion

TEST_KEY = "sk-proj-UNIT_TEST_SENTINEL_NEVER_A_REAL_CREDENTIAL"
MESSAGES = [{"role": "system", "content": "You can use bash."},
            {"role": "user", "content": "Inspect this synthetic fixture.", "extra": {"hidden": True}}]


def response(*, args='{"command":"echo synthetic"}', usage=None, choices=True, content="ok", model=MODEL):
    data = {"id": "offline-response", "created": 0, "object": "chat.completion", "model": model,
            "service_tier": "default", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": content, "tool_calls": [{"id": "tool-1",
            "type": "function", "function": {"name": "bash", "arguments": args}}]}}] if choices else [],
            "usage": usage if usage is not None else {"prompt_tokens": 1000, "completion_tokens": 100,
            "total_tokens": 1100, "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 200}}}
    return ChatCompletion.model_validate(data)


class TransportError(Exception):
    def __init__(self, code=None):
        self.status_code = code
        self.body = {"message": TEST_KEY, "authorization": "Bearer " + TEST_KEY,
                     "headers": {"X-Secret": TEST_KEY}, TEST_KEY: "echo"}
        self.request_id = "test-request"
        super().__init__(TEST_KEY)


class LunaModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.guard = LunaAccounting(self.root / "usage_ledger.json", "run")
        self.client = MagicMock()
        self.client.chat.completions.create.return_value = response()
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": TEST_KEY})
        self.env.start(); self.addCleanup(self.env.stop)
        self.sdk = patch("openai.OpenAI", return_value=self.client).start()
        self.addCleanup(patch.stopall)
        patch("httpx.Client").start()
        self.sleep = patch("luna_model.time.sleep").start()
        # Fail loudly if a test accidentally escapes the mocked SDK.
        patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in unit tests")).start()
        self.model = luna_model.make_model(MODEL, self.guard, "solo", "solo", self.root)
        self.addCleanup(self.model.close)

    def all_saved_text(self):
        return "\n".join(p.read_text() for p in self.root.rglob("*.json"))

    def test_factory_no_request_native_query_payload_and_cached_cost(self):
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(self.sdk.call_args.kwargs["max_retries"], 0)
        self.assertEqual(self.sdk.call_args.kwargs["base_url"], "https://api.openai.com/v1")
        result = self.model.query(MESSAGES)
        p = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(p["model"], MODEL)
        self.assertEqual(p["reasoning_effort"], "none")
        self.assertEqual(p["max_completion_tokens"], 16384)
        self.assertNotIn("max_tokens", p)
        self.assertEqual((p["temperature"], p["top_p"], p["service_tier"]), (.7, .8, "default"))
        self.assertEqual(p["extra_body"], {"prompt_cache_options": {"mode": "implicit", "ttl": "30m"}})
        self.assertNotIn("extra", p["messages"][1])
        self.assertEqual(result["extra"]["actions"][0]["command"], "echo synthetic")
        self.assertAlmostEqual(result["extra"]["cost"], .000101)
        self.assertAlmostEqual(self.guard.summary()["known_cost_usd"], .000101)
        self.assertEqual(self.guard.summary()["request_count"], 1)
        self.assertNotIn(TEST_KEY, self.all_saved_text())
        self.assertEqual(self.model.serialize()["info"]["config"]["model"]["model_name"], MODEL)

    def test_only_one_http_5xx_retry_and_each_attempt_counted(self):
        self.client.chat.completions.create.side_effect = [TransportError(503), response()]
        self.model.query(MESSAGES)
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.sleep.assert_called_once_with(2)
        self.assertEqual(self.guard.summary()["request_count"], 2)
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], 1)
        self.assertAlmostEqual(self.guard.summary()["known_cost_usd"], .000101)
        self.assertNotIn(TEST_KEY, self.all_saved_text())

    def test_second_5xx_does_not_trigger_native_retry(self):
        self.client.chat.completions.create.side_effect = TransportError(500)
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            self.model.query(MESSAGES)
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], 2)

    def test_4xx_and_timeout_never_retry_or_log_secret(self):
        for code in (None, 400, 401, 402, 429):
            with self.subTest(code=code):
                self.client.chat.completions.create.reset_mock()
                self.client.chat.completions.create.side_effect = TransportError(code)
                with self.assertRaises(RuntimeError) as exc:
                    self.model.query(MESSAGES)
                self.assertNotIn(TEST_KEY, str(exc.exception))
                self.assertEqual(self.client.chat.completions.create.call_count, 1)
                self.assertNotIn(TEST_KEY, self.all_saved_text())
        self.sleep.assert_not_called()

    def test_real_sdk_timeout_is_not_retried(self):
        import httpx
        from openai import APITimeoutError
        self.client.chat.completions.create.side_effect = APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
        with self.assertRaisesRegex(RuntimeError, "APITimeoutError"):
            self.model.query(MESSAGES)
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], 1)

    def test_tool_parse_failure_is_billed_before_parsing(self):
        self.client.chat.completions.create.return_value = response(args="not json")
        with self.assertRaises(Exception):
            self.model.query(MESSAGES)
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertEqual(self.guard.data["requests"][0]["status"], "completed")
        self.assertAlmostEqual(self.guard.summary()["known_cost_usd"], .000101)
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], 0)
        self.assertTrue((self.root / "responses/solo/request_00001_query_error.json").exists())

    def test_empty_choices_still_billed_once(self):
        self.client.chat.completions.create.return_value = response(choices=False)
        with self.assertRaises(IndexError):
            self.model.query(MESSAGES)
        self.assertAlmostEqual(self.guard.summary()["known_cost_usd"], .000101)
        self.assertEqual(self.guard.summary()["request_count"], 1)

    def test_missing_usage_and_unknown_cache_field_pause_with_raw_response(self):
        self.client.chat.completions.create.return_value = response(usage={"prompt_tokens": 100,
            "completion_tokens": 10, "total_tokens": 110,
            "prompt_tokens_details": {"future_cache_charge_tokens": 20}})
        with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
            self.model.query(MESSAGES)
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], 1)
        self.assertIsNone(self.guard.data["requests"][0]["actual_cost_usd"])
        self.assertTrue((self.root / "responses/solo/request_00001.json").exists())

    def test_response_credential_echo_is_redacted_and_never_reaches_agent(self):
        self.client.chat.completions.create.return_value = response(content=TEST_KEY)
        with self.assertRaisesRegex(RuntimeError, "Credential unexpectedly"):
            self.model.query(MESSAGES)
        self.assertAlmostEqual(self.guard.summary()["known_cost_usd"], .000101)
        self.assertNotIn(TEST_KEY, self.all_saved_text())
        self.assertIn("[REDACTED]", self.all_saved_text())

    def test_input_credential_echo_rejected_before_api(self):
        with self.assertRaisesRegex(RuntimeError, "Credential present in input"):
            self.model.query([{"role": "user", "content": TEST_KEY}])
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(self.guard.summary()["request_count"], 0)
        self.assertFalse(json.loads((self.root / "responses/solo/request_not_sent_error.json").read_text())["api_call_made"])
        self.assertNotIn(TEST_KEY, self.all_saved_text())

    def test_unexpected_model_is_not_silently_priced_as_luna(self):
        self.client.chat.completions.create.return_value = response(model="unexpected-model")
        with self.assertRaisesRegex(RuntimeError, "Unexpected response model"):
            self.model.query(MESSAGES)
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], 1)
        self.assertEqual(self.guard.summary()["known_cost_usd"], 0)

    def test_missing_key_and_disabled_compaction(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "absent"):
                luna_model.make_model(MODEL, self.guard, "solo", "other", self.root)
        with self.assertRaisesRegex(RuntimeError, "compaction"):
            self.model.summarize_context([])
        self.client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
