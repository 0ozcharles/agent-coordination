"""Offline checks for persistent, conservative smoke-run cost accounting."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from budget_guard import (
    BudgetExceeded,
    BudgetGuard,
    INPUT_RATE,
    MODEL,
    MODEL_PROFILES,
    MAX_CONTEXT,
    MAX_OUTPUT,
    MAX_REQUESTS,
    OUTPUT_RATE,
    REQUEST_RESERVE,
    ROUND_BUDGET,
    RUN_BUDGET,
)


class BudgetGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger_path = Path(self.temp.name) / "ledger.json"

    def persisted_rows(self):
        return json.loads(self.ledger_path.read_text())["requests"]

    def test_preflight_counts_toward_30_request_limit_after_restart(self):
        guard = BudgetGuard(self.ledger_path, "solo-1")
        for index in range(MAX_REQUESTS):
            row = guard.reserve("preflight" if index == 0 else "model")
            guard.record(row, {"prompt_tokens": 1, "completion_tokens": 1}, "stop")
        before = self.ledger_path.read_bytes()

        # A restart cannot reset this run's counter or spend another request.
        restarted = BudgetGuard(self.ledger_path, "solo-1")
        with self.assertRaisesRegex(BudgetExceeded, "30-request"):
            restarted.reserve("model")
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(restarted.summary()["request_count"], MAX_REQUESTS)
        self.assertEqual([r["request_number"] for r in self.persisted_rows()],
                         list(range(1, MAX_REQUESTS + 1)))

    def test_five_dollar_limit_persists_across_runs_with_unknown_usage(self):
        affordable_requests = math.floor(ROUND_BUDGET / REQUEST_RESERVE)
        for index in range(affordable_requests):
            # Reload from disk every time and rotate run IDs before the run cap.
            guard = BudgetGuard(self.ledger_path, f"solo-{index // MAX_REQUESTS}")
            row = guard.reserve("model")
            guard.fail(row, "TimeoutError")

        rows = self.persisted_rows()
        self.assertEqual(len(rows), affordable_requests)
        self.assertGreater(len({r["run_id"] for r in rows}), 1)
        self.assertLessEqual(sum(r["reserved_usd"] for r in rows), ROUND_BUDGET)
        self.assertTrue(all(r["actual_cost_usd"] is None for r in rows))
        before = self.ledger_path.read_bytes()

        new_run = BudgetGuard(self.ledger_path, "fresh-run")
        with self.assertRaisesRegex(BudgetExceeded, r"\$5"):
            new_run.reserve("preflight")
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(new_run.summary()["request_count"], 0)

    def test_timeout_http_error_and_interrupted_call_keep_reservations(self):
        guard = BudgetGuard(self.ledger_path, "solo-errors")
        timeout = guard.reserve("model")
        guard.fail(timeout, "TimeoutError")
        rejected = guard.reserve("model")
        guard.fail(rejected, "HTTPStatusError", status_code=429)
        guard.reserve("model")  # Simulate interruption before response accounting.

        restarted = BudgetGuard(self.ledger_path, "solo-errors")
        summary = restarted.summary()
        self.assertEqual(summary["request_count"], 3)
        self.assertEqual(summary["known_cost_usd"], 0)
        self.assertEqual(summary["unknown_usage_requests"], 3)
        self.assertAlmostEqual(summary["reserved_upper_bound_usd"], 3 * REQUEST_RESERVE)
        self.assertAlmostEqual(summary["budget_occupied_usd"], 3 * REQUEST_RESERVE)
        rows = self.persisted_rows()
        self.assertEqual([r["status"] for r in rows], ["error", "error", "in_flight"])
        self.assertEqual(rows[1]["http_status"], 429)
        self.assertTrue(all(r["reserved_usd"] == REQUEST_RESERVE for r in rows))
        self.assertEqual(restarted.reserve("model")["request_number"], 4)

    def test_failed_requests_also_exhaust_request_limit(self):
        guard = BudgetGuard(self.ledger_path, "solo-errors")
        for _ in range(MAX_REQUESTS):
            guard.fail(guard.reserve("model"), "TimeoutError")
        with self.assertRaises(BudgetExceeded):
            guard.reserve("model")
        self.assertEqual(guard.summary()["unknown_usage_requests"], MAX_REQUESTS)

    def test_missing_or_invalid_usage_stops_and_persists_unknown_cost(self):
        invalid_usage = [
            {},
            {"prompt_tokens": 10},
            {"completion_tokens": 10},
            {"prompt_tokens": None, "completion_tokens": 10},
            {"prompt_tokens": 10, "completion_tokens": None},
            {"prompt_tokens": -1, "completion_tokens": 10},
            {"prompt_tokens": 10, "completion_tokens": -1},
            {"prompt_tokens": 1.5, "completion_tokens": 10},
            {"prompt_tokens": 10, "completion_tokens": "2"},
            {"prompt_tokens": True, "completion_tokens": 10},
            {"prompt_tokens": 10, "completion_tokens": False},
        ]
        for index, usage in enumerate(invalid_usage):
            with self.subTest(usage=usage):
                guard = BudgetGuard(self.ledger_path, f"invalid-{index}")
                row = guard.reserve("model")
                with self.assertRaisesRegex(RuntimeError, "valid token usage"):
                    guard.record(row, usage, "stop")
                persisted = self.persisted_rows()[-1]
                self.assertEqual(persisted["status"], "error")
                self.assertEqual(persisted["error_type"], "MissingOrInvalidUsage")
                self.assertIsNone(persisted["actual_cost_usd"])
                self.assertEqual(persisted["reserved_usd"], REQUEST_RESERVE)
                self.assertEqual(guard.summary()["unknown_usage_requests"], 1)
                self.assertEqual(guard.summary()["budget_occupied_usd"], REQUEST_RESERVE)

    def test_token_bounds_stop_but_actual_usage_and_cost_remain_on_disk(self):
        cases = [
            {"prompt_tokens": MAX_CONTEXT + 1, "completion_tokens": 1},
            {"prompt_tokens": 1, "completion_tokens": MAX_OUTPUT + 1},
            {"prompt_tokens": MAX_CONTEXT + 1, "completion_tokens": MAX_OUTPUT + 1},
        ]
        for index, usage in enumerate(cases):
            with self.subTest(usage=usage):
                run_id = f"oversized-{index}"
                ledger_path = Path(self.temp.name) / f"oversized-{index}.json"
                guard = BudgetGuard(ledger_path, run_id)
                row = guard.reserve("model")
                with self.assertRaisesRegex(RuntimeError, "exceeded.*bound"):
                    guard.record(row, usage, "length")
                expected_cost = (usage["prompt_tokens"] * INPUT_RATE
                                 + usage["completion_tokens"] * OUTPUT_RATE)
                ledger = json.loads(ledger_path.read_text())
                persisted = ledger["requests"][-1]
                self.assertEqual(persisted["usage"], usage)
                self.assertAlmostEqual(persisted["actual_cost_usd"], expected_cost)
                self.assertEqual(persisted["finish_reason"], "length")
                self.assertEqual(persisted["reserved_usd"], REQUEST_RESERVE)
                reloaded = BudgetGuard(ledger_path, run_id).summary()
                self.assertAlmostEqual(reloaded["known_cost_usd"], expected_cost)
                self.assertAlmostEqual(reloaded["budget_occupied_usd"],
                                       max(expected_cost, REQUEST_RESERVE))
                self.assertEqual(reloaded["unknown_usage_requests"], 0)
                self.assertIn("exceeded", ledger["halt_reason"])

                # A provider-bound violation invalidates reservations for the
                # whole round. Neither this process nor a fresh run may proceed.
                before = ledger_path.read_bytes()
                for blocked_guard in (guard, BudgetGuard(ledger_path, "new-run")):
                    with self.assertRaisesRegex(BudgetExceeded, "halted"):
                        blocked_guard.reserve("model")
                self.assertEqual(ledger_path.read_bytes(), before)

    def test_exact_token_bounds_are_accepted(self):
        guard = BudgetGuard(self.ledger_path, "at-boundary")
        actual = guard.record(guard.reserve("model"),
                              {"prompt_tokens": MAX_CONTEXT, "completion_tokens": MAX_OUTPUT},
                              "length")
        self.assertAlmostEqual(actual, REQUEST_RESERVE)
        self.assertEqual(self.persisted_rows()[0]["status"], "completed")

    def test_normal_cost_and_summary_are_scoped_to_current_run(self):
        guard = BudgetGuard(self.ledger_path, "solo-1")
        first_usage = {"prompt_tokens": 1000, "completion_tokens": 200,
                       "total_tokens": 1200}
        actual = guard.record(guard.reserve("preflight"), first_usage, "stop")
        self.assertAlmostEqual(actual, 0.00027)
        guard.record(guard.reserve("model"),
                     {"prompt_tokens": 2000, "completion_tokens": 300}, "tool_calls")

        another_run = BudgetGuard(self.ledger_path, "solo-2")
        another_run.fail(another_run.reserve("model"), "TimeoutError")
        summary = BudgetGuard(self.ledger_path, "solo-1").summary()
        self.assertEqual(summary["request_count"], 2)
        self.assertAlmostEqual(summary["known_cost_usd"], 0.00075)
        self.assertAlmostEqual(summary["budget_occupied_usd"], 0.00075)
        self.assertAlmostEqual(summary["reserved_upper_bound_usd"], 2 * REQUEST_RESERVE)
        self.assertEqual(summary["unknown_usage_requests"], 0)
        self.assertEqual(summary["prompt_tokens"], 3000)
        self.assertEqual(summary["completion_tokens"], 500)
        self.assertEqual(self.persisted_rows()[0]["usage"], first_usage)

    def test_qwen_settled_usage_releases_unused_reservation_until_request_cap(self):
        model = "Qwen/Qwen3.5-9B"
        guard = BudgetGuard(self.ledger_path, "qwen", model=model)
        self.assertAlmostEqual(guard.request_reserve, 0.04558848)
        for index in range(MAX_REQUESTS):
            row = guard.reserve("preflight" if index == 0 else "model")
            cost = guard.record(row, {"prompt_tokens": 1000, "completion_tokens": 200}, "stop")
            self.assertAlmostEqual(cost, 0.00022)
            self.assertEqual(row["model"], model)
            self.assertEqual(row["input_rate"], MODEL_PROFILES[model]["input_rate"])
            self.assertEqual(row["output_rate"], MODEL_PROFILES[model]["output_rate"])
        restarted = BudgetGuard(self.ledger_path, "qwen", model=model)
        with self.assertRaisesRegex(BudgetExceeded, "30-request"):
            restarted.reserve("model")
        summary = restarted.summary()
        self.assertEqual(summary["request_count"], MAX_REQUESTS)
        self.assertGreater(summary["reserved_upper_bound_usd"], RUN_BUDGET)
        self.assertAlmostEqual(summary["budget_occupied_usd"], MAX_REQUESTS * 0.00022)

    def test_qwen_unknown_usage_retains_full_reservation_and_stops_at_run_budget(self):
        guard = BudgetGuard(self.ledger_path, "qwen-unknown", model="Qwen/Qwen3.5-9B")
        maximum_requests = math.floor(RUN_BUDGET / guard.request_reserve)
        self.assertEqual(maximum_requests, 21)
        for _ in range(maximum_requests):
            guard.fail(guard.reserve("model"), "TimeoutError")
        with self.assertRaisesRegex(BudgetExceeded, r"Per-run.*\$1"):
            guard.reserve("model")
        self.assertAlmostEqual(guard.summary()["budget_occupied_usd"],
                               maximum_requests * guard.request_reserve)

    def test_run_budget_checks_settled_cost_plus_next_full_reservation(self):
        guard = BudgetGuard(self.ledger_path, "qwen-expensive", model="Qwen/Qwen3.5-9B")
        for _ in range(29):
            actual = guard.record(guard.reserve("model"),
                                  {"prompt_tokens": 200000, "completion_tokens": 0}, "stop")
            self.assertAlmostEqual(actual, 0.034)
        occupied = guard.summary()["budget_occupied_usd"]
        self.assertAlmostEqual(occupied, 0.986)
        self.assertLess(occupied, RUN_BUDGET)
        self.assertGreater(occupied + guard.request_reserve, RUN_BUDGET)
        before = self.ledger_path.read_bytes()
        with self.assertRaisesRegex(BudgetExceeded, r"Per-run.*\$1"):
            BudgetGuard(self.ledger_path, "qwen-expensive", model="Qwen/Qwen3.5-9B").reserve("model")
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_settled_usage_releases_round_budget_across_runs(self):
        for index in range(250):
            guard = BudgetGuard(self.ledger_path, f"qwen-{index // MAX_REQUESTS}",
                                model="Qwen/Qwen3.5-9B")
            guard.record(guard.reserve("model"),
                         {"prompt_tokens": 1000, "completion_tokens": 200}, "stop")
        rows = self.persisted_rows()
        self.assertGreater(sum(row["reserved_usd"] for row in rows), ROUND_BUDGET)
        self.assertAlmostEqual(sum(row["actual_cost_usd"] for row in rows), 0.055)
        another = BudgetGuard(self.ledger_path, "next-run", model="Qwen/Qwen3.5-9B")
        self.assertEqual(another.reserve("preflight")["request_number"], 1)

    def test_qwen_accepts_its_own_context_boundary_and_halts_above_it(self):
        model = "Qwen/Qwen3.5-9B"
        context = MODEL_PROFILES[model]["max_context"]
        self.assertEqual(context, 262144)
        self.assertGreater(context, MAX_CONTEXT)
        guard = BudgetGuard(self.ledger_path, "qwen", model=model)
        cost = guard.record(guard.reserve("model"),
                            {"prompt_tokens": context, "completion_tokens": MAX_OUTPUT}, "length")
        self.assertAlmostEqual(cost, guard.request_reserve)
        row = guard.reserve("model")
        with self.assertRaisesRegex(RuntimeError, "exceeded.*bound"):
            guard.record(row, {"prompt_tokens": context + 1, "completion_tokens": 1}, "stop")
        with self.assertRaisesRegex(BudgetExceeded, "halted"):
            BudgetGuard(self.ledger_path, "different-model", model=MODEL).reserve("model")

    def test_legacy_rows_keep_original_model_reservation_and_cost_after_switch(self):
        # Model-less rows are the format produced before mixed-model support.
        old_row = {"run_id": "old-run", "request_number": 1, "phase": "model",
                   "reserved_usd": REQUEST_RESERVE, "actual_cost_usd": 0.00027,
                   "status": "completed", "prompt_tokens": 1000, "completion_tokens": 200}
        self.ledger_path.write_text(json.dumps({
            "model": MODEL, "input_usd_per_million": 0.15,
            "output_usd_per_million": 0.60, "requests": [old_row],
        }))
        model = "Qwen/Qwen3.5-9B"
        qwen = BudgetGuard(self.ledger_path, "new-run", model=model)
        migrated = self.persisted_rows()[0]
        for field, original_value in old_row.items():
            self.assertEqual(migrated[field], original_value)
        self.assertEqual(migrated["model"], MODEL)
        self.assertEqual(migrated["input_rate"], INPUT_RATE)
        self.assertEqual(migrated["output_rate"], OUTPUT_RATE)
        qwen.record(qwen.reserve("model"), {"prompt_tokens": 1000, "completion_tokens": 200}, "stop")

        reloaded = BudgetGuard(self.ledger_path, "old-run", model=MODEL)
        self.assertAlmostEqual(reloaded.summary()["known_cost_usd"], 0.00027)
        rows = self.persisted_rows()
        self.assertEqual([row["model"] for row in rows], [MODEL, model])
        self.assertAlmostEqual(rows[1]["actual_cost_usd"], 0.00022)
        self.assertEqual(rows[0]["reserved_usd"], REQUEST_RESERVE)
        self.assertEqual(rows[1]["reserved_usd"], qwen.request_reserve)

    def test_same_run_model_switch_sums_original_reservations(self):
        first = BudgetGuard(self.ledger_path, "same-run")
        for _ in range(10):
            first.fail(first.reserve("model"), "TimeoutError")
        qwen = BudgetGuard(self.ledger_path, "same-run", model="Qwen/Qwen3.5-9B")
        remaining_calls = math.floor((RUN_BUDGET - 10 * REQUEST_RESERVE) / qwen.request_reserve)
        self.assertEqual(remaining_calls, 17)
        for _ in range(remaining_calls):
            qwen.reserve("model")
        with self.assertRaisesRegex(BudgetExceeded, r"Per-run.*\$1"):
            qwen.reserve("model")
        summary = qwen.summary()
        self.assertEqual(summary["request_count"], 27)
        self.assertAlmostEqual(summary["reserved_upper_bound_usd"],
                               10 * REQUEST_RESERVE + 17 * qwen.request_reserve)

    def test_round_budget_is_shared_across_models_and_new_runs(self):
        initial = BudgetGuard(self.ledger_path, "gpt-initial")
        initial.record(initial.reserve("model"),
                       {"prompt_tokens": 1000, "completion_tokens": 200}, "stop")
        qwen = BudgetGuard(self.ledger_path, "qwen-0", model="Qwen/Qwen3.5-9B")
        settled_initial = initial.summary()["budget_occupied_usd"]
        affordable_qwen = math.floor((ROUND_BUDGET - settled_initial) / qwen.request_reserve)
        per_run = math.floor(RUN_BUDGET / qwen.request_reserve)
        for index in range(affordable_qwen):
            qwen = BudgetGuard(self.ledger_path, f"qwen-{index // per_run}",
                               model="Qwen/Qwen3.5-9B")
            qwen.fail(qwen.reserve("model"), "TimeoutError")
        next_run = BudgetGuard(self.ledger_path, "fresh-qwen", model="Qwen/Qwen3.5-9B")
        before = self.ledger_path.read_bytes()
        with self.assertRaisesRegex(BudgetExceeded, r"Round.*\$5"):
            next_run.reserve("preflight")
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertLessEqual(settled_initial + affordable_qwen * qwen.request_reserve, ROUND_BUDGET)
        self.assertEqual(self.persisted_rows()[0]["model"], MODEL)
        self.assertAlmostEqual(self.persisted_rows()[0]["actual_cost_usd"], 0.00027)

    def test_accounting_only_crosses_money_and_request_limits_and_persists_totals(self):
        model = "Qwen/Qwen3.5-9B"
        guard = BudgetGuard(self.ledger_path, "unlimited", model=model, enforce_limits=False)
        usage = {"prompt_tokens": MODEL_PROFILES[model]["max_context"],
                 "completion_tokens": MAX_OUTPUT}
        for _ in range(120):
            guard.record(guard.reserve("model"), usage, "stop")
        guard = BudgetGuard(self.ledger_path, "unlimited", model=model, enforce_limits=False)
        summary = guard.summary()
        self.assertEqual(summary["request_count"], 120)
        self.assertGreater(summary["request_count"], MAX_REQUESTS)
        self.assertGreater(summary["known_cost_usd"], ROUND_BUDGET)
        self.assertGreater(summary["known_cost_usd"], RUN_BUDGET)
        self.assertAlmostEqual(summary["known_cost_usd"], 120 * guard.request_reserve)
        self.assertAlmostEqual(summary["budget_occupied_usd"], summary["known_cost_usd"])
        self.assertEqual(summary["enforcement_mode"], "accounting_only")
        self.assertEqual(summary["unknown_usage_requests"], 0)
        self.assertEqual(summary["prompt_tokens"], 120 * usage["prompt_tokens"])
        self.assertEqual(summary["completion_tokens"], 120 * usage["completion_tokens"])
        self.assertTrue(all(row["enforcement_mode"] == "accounting_only"
                            for row in self.persisted_rows()))
        # A different run also proceeds despite the shared round total.
        next_run = BudgetGuard(self.ledger_path, "unlimited-next", model=model, enforce_limits=False)
        self.assertEqual(next_run.reserve("model")["request_number"], 1)

    def test_accounting_only_records_bound_violation_and_continues(self):
        guard = BudgetGuard(self.ledger_path, "unlimited", enforce_limits=False)
        usage = {"prompt_tokens": MAX_CONTEXT + 1, "completion_tokens": MAX_OUTPUT + 1}
        actual = guard.record(guard.reserve("model"), usage, "length")
        self.assertAlmostEqual(actual, usage["prompt_tokens"] * INPUT_RATE
                               + usage["completion_tokens"] * OUTPUT_RATE)
        self.assertTrue(self.persisted_rows()[0]["usage_bound_exceeded"])
        restarted = BudgetGuard(self.ledger_path, "unlimited", enforce_limits=False)
        self.assertEqual(restarted.reserve("model")["request_number"], 2)
        # The historical violation is still available if enforcement is restored.
        with self.assertRaisesRegex(BudgetExceeded, "halted"):
            BudgetGuard(self.ledger_path, "enforced-again").reserve("model")

    def test_accounting_only_preserves_unknown_usage_and_still_rejects_missing_usage(self):
        guard = BudgetGuard(self.ledger_path, "unlimited", enforce_limits=False)
        guard.fail(guard.reserve("model"), "TimeoutError")
        guard.reserve("model")
        row = guard.reserve("model")
        with self.assertRaisesRegex(RuntimeError, "valid token usage"):
            guard.record(row, {}, "stop")
        summary = BudgetGuard(self.ledger_path, "unlimited", enforce_limits=False).summary()
        self.assertEqual(summary["unknown_usage_requests"], 3)
        self.assertEqual(summary["known_cost_usd"], 0)
        self.assertAlmostEqual(summary["budget_occupied_usd"], 3 * REQUEST_RESERVE)
        self.assertEqual(summary["enforcement_mode"], "accounting_only")
        self.assertEqual(BudgetGuard(self.ledger_path, "default-run").summary()["enforcement_mode"],
                         "enforced")

    def test_custom_16k_output_limit_records_cost_without_bound_violation(self):
        model = "Qwen/Qwen3.5-9B"
        guard = BudgetGuard(self.ledger_path, "long-output", model=model,
                            enforce_limits=False, max_output_tokens=16384)
        row = guard.reserve("model")
        actual = guard.record(row, {"prompt_tokens": 1000, "completion_tokens": 16384}, "stop")
        self.assertAlmostEqual(guard.request_reserve, 262144 * 0.17e-6 + 16384 * 0.25e-6)
        self.assertAlmostEqual(actual, 1000 * 0.17e-6 + 16384 * 0.25e-6)
        self.assertEqual(row["max_output_tokens"], 16384)
        self.assertFalse(row.get("usage_bound_exceeded", False))
        self.assertNotIn("halt_reason", guard.data)
        # Reloading with the default constructor must not shrink historical limits.
        reloaded = BudgetGuard(self.ledger_path, "long-output", model=model)
        self.assertAlmostEqual(reloaded.summary()["budget_occupied_usd"], actual)
        self.assertEqual(self.persisted_rows()[0]["max_output_tokens"], 16384)

    def test_legacy_output_limit_remains_4096_when_new_run_allows_16k(self):
        old = BudgetGuard(self.ledger_path, "legacy")
        old.reserve("model")
        legacy_data = json.loads(self.ledger_path.read_text())
        del legacy_data["requests"][0]["max_output_tokens"]
        self.ledger_path.write_text(json.dumps(legacy_data))

        changed = BudgetGuard(self.ledger_path, "legacy", max_output_tokens=16384)
        row = changed.data["requests"][0]
        self.assertEqual(row["max_output_tokens"], 4096)
        self.assertEqual(row["reserved_usd"], REQUEST_RESERVE)
        with self.assertRaisesRegex(RuntimeError, "exceeded.*bound"):
            changed.record(row, {"prompt_tokens": 1, "completion_tokens": 4097}, "length")
        self.assertTrue(self.persisted_rows()[0]["usage_bound_exceeded"])
        self.assertAlmostEqual(changed.summary()["budget_occupied_usd"], REQUEST_RESERVE)

    def test_concurrent_shared_instance_preserves_every_request_and_settlement(self):
        guard = BudgetGuard(self.ledger_path, "parallel", model="Qwen/Qwen3.5-9B",
                            enforce_limits=False, max_output_tokens=16384)
        start = Barrier(8)

        def worker(worker_id):
            start.wait(timeout=5)
            for offset in range(8):
                task_id = worker_id * 8 + offset
                row = guard.reserve(f"task-{task_id}")
                if task_id % 2:
                    guard.fail(row, "TimeoutError")
                else:
                    guard.record(row, {"prompt_tokens": task_id + 1, "completion_tokens": 5000}, "stop")
                snapshot = guard.summary()
                self.assertGreaterEqual(snapshot["request_count"], row["request_number"])
                self.assertLessEqual(snapshot["unknown_usage_requests"], snapshot["request_count"])
                # Readers only ever see a complete old or new JSON document.
                self.assertIn("requests", json.loads(self.ledger_path.read_text()))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(worker, range(8)))

        rows = self.persisted_rows()
        self.assertEqual(len(rows), 64)
        self.assertEqual([r["request_number"] for r in rows], list(range(1, 65)))
        self.assertEqual({r["phase"] for r in rows}, {f"task-{i}" for i in range(64)})
        self.assertEqual(sum(r["status"] == "completed" for r in rows), 32)
        self.assertEqual(sum(r["status"] == "error" for r in rows), 32)
        self.assertTrue(all(r["max_output_tokens"] == 16384 for r in rows))
        expected_prompt_tokens = sum(i + 1 for i in range(0, 64, 2))
        expected_cost = expected_prompt_tokens * 0.17e-6 + 32 * 5000 * 0.25e-6
        summary = guard.summary()
        self.assertEqual(summary["prompt_tokens"], expected_prompt_tokens)
        self.assertEqual(summary["completion_tokens"], 32 * 5000)
        self.assertEqual(summary["unknown_usage_requests"], 32)
        self.assertAlmostEqual(summary["known_cost_usd"], expected_cost)
        self.assertAlmostEqual(summary["budget_occupied_usd"],
                               expected_cost + 32 * guard.request_reserve)
        reloaded = BudgetGuard(self.ledger_path, "parallel", model="Qwen/Qwen3.5-9B",
                               enforce_limits=False, max_output_tokens=16384)
        self.assertEqual(reloaded.summary(), summary)

    def test_concurrent_reservations_cannot_race_past_request_cap(self):
        guard = BudgetGuard(self.ledger_path, "parallel-enforced")
        start = Barrier(8)

        def worker(_):
            start.wait(timeout=5)
            accepted = []
            for _ in range(8):
                try:
                    accepted.append(guard.reserve("model")["request_number"])
                except BudgetExceeded:
                    pass
            return accepted

        with ThreadPoolExecutor(max_workers=8) as executor:
            accepted = [number for batch in executor.map(worker, range(8)) for number in batch]
        self.assertEqual(sorted(accepted), list(range(1, MAX_REQUESTS + 1)))
        self.assertEqual(len(self.persisted_rows()), MAX_REQUESTS)
        self.assertEqual(guard.summary()["request_count"], MAX_REQUESTS)


if __name__ == "__main__":
    unittest.main()
