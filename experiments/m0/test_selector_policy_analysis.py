"""Outcome-independent rollout reuse and physical-bill accounting, no API/Docker."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_luna_selector as supervisor


def usage(cost, requests=1, unknown=0):
    return {"requests": requests, "known_cost_usd": cost,
            "unknown_usage_requests": unknown, "prompt_tokens": 0,
            "completion_tokens": 0}


def row(task, condition, repeat=1, success=True, cost=1.0):
    return {"task": task, "condition": condition, "repeat": repeat,
            "official_success": success, "full_task_known_cost_usd": cost,
            "full_task_unknown_usage_requests": 0,
            "full_task_execution_seconds": 10.0,
            "full_task_time_unknown_attempts": 0}


def decisions(task, action):
    return {task: {"decision": {"selected_action": action}}}


class SelectorPolicyAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.task = next(iter(supervisor.TASKS))

    def test_frozen_choice_cannot_be_replaced_by_success_or_cheaper_action(self):
        rows = [row(self.task, "strongshare", success=True, cost=0.01),
                row(self.task, "integrator", success=False, cost=5.0),
                row(self.task, "solo", success=True, cost=0.001)]
        chosen = supervisor.policy_rows(rows, decisions(self.task, "integrator"))
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["condition"], "integrator")
        self.assertFalse(chosen[0]["official_success"])
        self.assertEqual(chosen[0]["full_task_known_cost_usd"], 5.0)
        self.assertTrue(chosen[0]["reused_counterfactual"])
        self.assertEqual(chosen[0]["additional_model_requests"], 0)
        self.assertEqual(chosen[0]["additional_model_cost_usd"], 0)

    def test_all_prespecified_chosen_repeats_are_retained_no_best_of(self):
        rows = [row(self.task, "strongshare", 2, True, 0.001),
                row(self.task, "integrator", 2, False, 4.0),
                row(self.task, "integrator", 1, True, 2.0),
                row(self.task, "strongshare", 1, True, 0.001)]
        before = copy.deepcopy(rows)
        selected = supervisor.policy_rows(rows, decisions(self.task, "integrator"))
        self.assertEqual({r["repeat"] for r in selected}, {1, 2})
        self.assertEqual(sum(r["official_success"] for r in selected), 1)
        self.assertEqual(rows, before)
        groups = supervisor.groups_for(rows, selected)
        policy = next(g for g in groups if g["task"] == self.task and g["policy"] == "selector")
        fixed = next(g for g in groups if g["task"] == self.task and g["policy"] == "integrator")
        for field in ("n", "official_passes", "full_task_known_cost_mean",
                      "full_task_execution_seconds_mean", "unknown_usage_requests"):
            self.assertEqual(policy[field], fixed[field])
        self.assertEqual(policy["n"], 2)
        self.assertEqual(policy["official_passes"], 1)

    def test_unfinished_selected_action_does_not_borrow_finished_counterfactual(self):
        rows = [row(self.task, "strongshare", 1, True, 0.1),
                row(self.task, "solo", 1, True, 0.1)]
        self.assertEqual(supervisor.policy_rows(rows, decisions(self.task, "integrator")), [])

    def test_changed_observed_scores_do_not_change_chosen_run_identities(self):
        tasks = list(supervisor.TASKS)[:2]
        fixed = {tasks[0]: {"decision": {"selected_action": "integrator"}},
                 tasks[1]: {"decision": {"selected_action": "strongshare"}}}
        rows = [row(task, condition, repeat, success=(condition == "strongshare"))
                for task in tasks for repeat in (1, 2)
                for condition in ("strongshare", "integrator", "solo")]
        identity = lambda rs: {(r["task"], r["repeat"], r["condition"]) for r in rs}
        before = identity(supervisor.policy_rows(rows, fixed))
        for i, result in enumerate(rows):
            result["official_success"] = not result["official_success"]
            result["full_task_known_cost_usd"] = i * 100
            result["full_task_unknown_usage_requests"] = 2
        self.assertEqual(before, identity(supervisor.policy_rows(rows, fixed)))

    def test_actual_bill_counts_physical_ledgers_once_including_unknown_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory)
            ledgers = {
                suite / self.task / "prefix": [(0.1, False), (None, True)],
                suite / self.task / "jobs/ss": [(0.2, False)],
                suite / self.task / "jobs/i": [(0.3, False)],
                suite / self.task / "solo_jobs/s": [(0.4, False)],
            }
            for folder, entries in ledgers.items():
                folder.mkdir(parents=True)
                records = [{"actual_cost_usd": cost, "excluded_from_baseline": excluded}
                           for cost, excluded in entries]
                (folder / "usage_ledger.json").write_text(json.dumps({"requests": records}))
            rows = [row(self.task, "strongshare", cost=0.3),
                    row(self.task, "integrator", cost=0.4), row(self.task, "solo", cost=0.4)]
            protocol = {"jobs": [{"task": self.task, "repeat": 1, "condition": c}
                                 for c in supervisor.CONDITIONS], "policy_evaluation": "test"}
            result = supervisor.summary_for(suite, protocol, rows, [], {},
                                            decisions(self.task, "integrator"))
            self.assertTrue(result["pipeline_completed"])
            self.assertEqual(result["actual_new_usage"], usage(1.0, requests=5, unknown=1))
            self.assertEqual(len(result["selector_reused_rows"]), 1)
            self.assertFalse(result["selector_has_independent_runs"])
            self.assertEqual(result["selector_additional_model_requests"], 0)
            other = supervisor.summary_for(suite, protocol, rows, [], {},
                                           decisions(self.task, "strongshare"))
            self.assertEqual(other["actual_new_usage"], result["actual_new_usage"])

    def coverage(self, rows, skipped, frozen):
        protocol = {"jobs": [{"task": self.task, "repeat": repeat, "condition": condition}
                             for repeat in (1, 2) for condition in supervisor.CONDITIONS]}
        results = supervisor.policy_coverage(protocol, rows, skipped, frozen)
        return {r["policy"]: r for r in results if r["task"] == self.task}

    def test_budget_terminal_stays_in_eligible_policy_denominator(self):
        rows = [row(self.task, "integrator", 1, True)]
        for reason in (*supervisor.EXHAUSTED_STATES, "total_budget_exhausted"):
            with self.subTest(reason=reason):
                skipped = [{"task": self.task, "repeat": 2, "condition": "integrator",
                            "reason": reason}]
                coverage = self.coverage(rows, skipped, decisions(self.task, "integrator"))
                for policy in ("integrator", "selector"):
                    result = coverage[policy]
                    self.assertEqual(result["planned"], 2)
                    self.assertEqual(result["scored"], 1)
                    self.assertEqual(result["successes"], 1)
                    self.assertEqual(result["budget_failures"], 1)
                    self.assertEqual(result["completed_eligible"], 2)
                    self.assertEqual(result["pending"], 0)
                    self.assertEqual(result["budget_aware_success_rate"], 0.5)
                    self.assertIsNone(result["official_scores_for_budget_failures"])
                self.assertEqual(rows[0]["official_success"], True)

    def test_pending_chosen_repeat_cannot_report_completed_success_rate(self):
        rows = [row(self.task, "integrator", 1, True),
                row(self.task, "strongshare", 1, True),
                row(self.task, "strongshare", 2, True)]
        coverage = self.coverage(rows, [], decisions(self.task, "integrator"))
        self.assertEqual(coverage["selector"]["pending"], 1)
        self.assertIsNone(coverage["selector"]["budget_aware_success_rate"])
        self.assertEqual(coverage["strongshare"]["budget_aware_success_rate"], 1.0)

    def test_no_checkpoint_is_ineligible_not_functional_failure_and_solo_remains(self):
        rows = [row(self.task, "solo", 1, True), row(self.task, "solo", 2, False)]
        skipped = [{"task": self.task, "repeat": repeat, "condition": condition,
                    "reason": "checkpoint_not_reached"}
                   for repeat in (1, 2) for condition in ("strongshare", "integrator")]
        coverage = self.coverage(rows, skipped, {})
        for policy in ("strongshare", "integrator", "selector"):
            result = coverage[policy]
            self.assertEqual(result["ineligible"], 2)
            self.assertEqual(result["pending"], 0)
            self.assertEqual(result["completed_eligible"], 0)
            self.assertEqual(result["budget_failures"], 0)
            self.assertIsNone(result["budget_aware_success_rate"])
        self.assertIsNone(coverage["selector"]["selected_action"])
        self.assertEqual(coverage["solo"]["scored"], 2)
        self.assertEqual(coverage["solo"]["budget_aware_success_rate"], 0.5)

    def test_decision_cannot_be_created_after_a_fresh_solo_job(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory)
            task = supervisor.TASKS[self.task]
            (suite / self.task / "solo_jobs/already_started").mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                supervisor.prepare_decision(suite, task, {"checkpoint_sha256": "a" * 64})


if __name__ == "__main__":
    unittest.main()
