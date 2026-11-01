"""Offline accounting tests. No provider, credentials, Docker or network."""
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from budget_guard import MODEL_PROFILES
from luna_accounting import LunaAccounting, MODEL, UsageAccountingError, calculate_usage


def usage(prompt=1000, completion=100, cached=600, written=200):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written}}


class LunaAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "usage_ledger.json"
        self.profiles = copy.deepcopy(MODEL_PROFILES)
        self.guard = LunaAccounting(self.path, "r1", model=MODEL, enforce_limits=False,
                                    max_output_tokens=16384)

    def tearDown(self):
        self.assertEqual(MODEL_PROFILES, self.profiles)

    def test_disjoint_cache_buckets_write_is_not_a_surcharge(self):
        result = calculate_usage(usage())
        self.assertEqual(result["ordinary_input_tokens"], 200)
        self.assertEqual(result["cached_input_tokens"], 600)
        self.assertEqual(result["cache_write_tokens"], 200)
        self.assertAlmostEqual(result["actual_cost_usd"], .000101, places=12)
        self.assertAlmostEqual(calculate_usage(usage(), cache_write_ttl="12h")["actual_cost_usd"], .000126, places=12)

    def test_long_input_threshold_prices_entire_request(self):
        short = calculate_usage(usage(prompt=272000, cached=100000, written=100000))
        long = calculate_usage(usage(prompt=272001, cached=100000, written=100000))
        self.assertFalse(short["long_context_pricing"])
        self.assertTrue(long["long_context_pricing"])
        self.assertEqual(long["rates_usd_per_million"], {
            "ordinary_input": .2, "cached_input": .02, "cache_write": .25, "output": .75})
        self.assertAlmostEqual(long["actual_cost_usd"], .0144002 + .002 + .025 + .000075, places=12)

    def test_output_details_are_already_in_completion_total(self):
        u = usage()
        u["completion_tokens_details"] = {"reasoning_tokens": 30, "accepted_prediction_tokens": 20,
                                          "rejected_prediction_tokens": 10, "audio_tokens": 0}
        self.assertEqual(calculate_usage(u)["actual_cost_usd"], calculate_usage(usage())["actual_cost_usd"])

    def test_missing_invalid_or_unknown_usage_stays_unknown_and_persisted(self):
        bad = [{}, usage(cached=900, written=200), usage(prompt=True),
               {**usage(), "unrecognized_cost": .1},
               {**usage(), "total_tokens": 1},
               {**usage(), "prompt_tokens_details": {"cache_write_12h_tokens": 12}},
               {**usage(), "prompt_tokens_details": {"cached_tokens": -1}},
               {**usage(), "prompt_tokens_details": {"audio_tokens": 1}},
               {**usage(), "completion_tokens_details": {"future_billable_tokens": 0}}]
        for item in bad:
            with self.subTest(usage=item):
                row = self.guard.reserve("solo:solo")
                with self.assertRaises(UsageAccountingError):
                    self.guard.record(row, item, "stop")
                self.assertEqual(row["status"], "error")
                self.assertIsNone(row["actual_cost_usd"])
                self.assertEqual(row["usage"], item)
        saved = json.loads(self.path.read_text())
        self.assertEqual(len(saved["requests"]), len(bad))
        self.assertEqual(self.guard.summary()["unknown_usage_requests"], len(bad))

    def test_optional_null_details_are_not_spurious_unknown_categories(self):
        u = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": None,
             "completion_tokens_details": None, "future_optional": None}
        self.assertAlmostEqual(calculate_usage(u)["actual_cost_usd"], .000015)

    def test_unknown_tier_model_or_ttl_rejected(self):
        with self.assertRaises(UsageAccountingError):
            calculate_usage(usage(), service_tier="priority")
        with self.assertRaises(UsageAccountingError):
            calculate_usage(usage(), cache_write_ttl="24h")
        row = self.guard.reserve("solo:solo")
        with self.assertRaises(UsageAccountingError):
            self.guard.record(row, usage(), "stop", response_model="different-model")
        self.assertIsNone(row["actual_cost_usd"])

    def test_idempotent_record_conflict_and_fail_cannot_erase_charge(self):
        row = self.guard.reserve("solo:solo")
        first = self.guard.record(row, usage(), "tool_calls")
        self.assertEqual(first, self.guard.record(row, copy.deepcopy(usage()), "tool_calls"))
        with self.assertRaises(ValueError):
            self.guard.record(row, usage(completion=101), "tool_calls")
        with self.assertRaises(ValueError):
            self.guard.fail(row, "TimeoutError")
        self.assertEqual(self.guard.summary()["request_count"], 1)
        self.assertEqual(self.guard.summary()["known_cost_usd"], first)
        with self.assertRaises(ValueError):
            self.guard.record(dict(row), usage(), "tool_calls")

    def test_resume_preserves_unknown_and_request_numbers(self):
        row = self.guard.reserve("solo:solo")
        self.guard.record(row, usage(), "tool_calls")
        self.guard.fail(self.guard.reserve("solo:solo"), "APITimeoutError")
        self.guard.reserve("solo:solo")
        resumed = LunaAccounting(self.path, "r1")
        self.assertEqual(resumed.reserve("solo:solo")["request_number"], 4)
        self.assertEqual(resumed.summary()["unknown_usage_requests"], 3)
        self.assertEqual(resumed.summary()["known_cost_usd"], row["actual_cost_usd"])
        self.assertGreater(resumed.summary()["budget_occupied_usd"], resumed.summary()["known_cost_usd"])

    def test_incompatible_ledger_never_rewritten(self):
        self.path.write_text(json.dumps({"model": "Qwen/Qwen3.5-9B", "requests": []}))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            LunaAccounting(self.path, "r1")
        self.assertEqual(before, self.path.read_bytes())

    def test_threaded_shared_guard_no_caps_or_duplicate_rows(self):
        def charge(i):
            row = self.guard.reserve(f"strongshare:agent{i % 2 + 1}")
            return self.guard.record(row, usage(), "tool_calls")
        with ThreadPoolExecutor(max_workers=6) as pool:
            costs = list(pool.map(charge, range(48)))
        rows = json.loads(self.path.read_text())["requests"]
        self.assertEqual(sorted(r["request_number"] for r in rows), list(range(1, 49)))
        self.assertAlmostEqual(sum(costs), self.guard.summary()["known_cost_usd"])

    def test_native_usage_reader_accepts_ledger_and_exclusion(self):
        from run_baselines import usage as native_usage
        self.guard.record(self.guard.reserve("solo:solo"), usage(), "tool_calls")
        row = self.guard.reserve("solo:solo")
        self.guard.fail(row, "APIStatusError", 500)
        row["excluded_from_baseline"] = True
        # The supervisor owns exclusion markers, as in the old accounting path.
        self.guard._persist()
        self.assertEqual(native_usage(self.path.parent)["requests"], 2)
        self.assertEqual(native_usage(self.path.parent, phase="solo")["requests"], 1)
        self.assertEqual(native_usage(self.path.parent, excluded_only=True)["unknown_usage_requests"], 1)


if __name__ == "__main__":
    unittest.main()
