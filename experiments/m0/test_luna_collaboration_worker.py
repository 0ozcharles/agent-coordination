"""Offline binding checks: no Docker, credentials or model requests."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

import cross_task_workers
from cross_task_specs import get_task
from luna_accounting import LunaAccounting
import luna_collaboration_worker as wrapper
import luna_model


class LunaCollaborationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.task = get_task("llama_index17070_f1_f3")
        self.original = sys.modules.get("baseline_model")
        self.had_original = "baseline_model" in sys.modules
        self.old_marker = SimpleNamespace(marker="ordinary baseline import")
        sys.modules["baseline_model"] = self.old_marker
        self.addCleanup(self.restore_import)

    def restore_import(self):
        if self.had_original:
            sys.modules["baseline_model"] = self.original
        else:
            sys.modules.pop("baseline_model", None)

    def test_prefix_private_binding_and_argument_preservation(self):
        worker = SimpleNamespace(MODEL="original", MAX_OUTPUT=1, BudgetGuard=object)

        def phase(run_dir, condition, **kwargs):
            self.assertIs(sys.modules["baseline_model"], luna_model)
            self.assertEqual((worker.MODEL, worker.MAX_OUTPUT), ("gpt-6-luna", 16384))
            self.assertIs(worker.BudgetGuard, LunaAccounting)
            self.assertEqual((run_dir, condition), (Path("/tmp/luna-prefix"), "prefix"))
            self.assertEqual(kwargs, {"offline": True, "task": self.task})
            return 0

        worker.phase_worker = phase
        with patch.object(wrapper, "load_isolated", return_value=worker) as load, \
                patch.object(cross_task_workers, "packet_module_context") as packet:
            self.assertEqual(wrapper.phase_worker("/tmp/luna-prefix", "prefix", self.task, offline=True), 0)
            load.assert_called_once_with("run_baselines.py")
            packet.assert_not_called()
        self.assertIs(sys.modules["baseline_model"], self.old_marker)

    def test_both_handoff_conditions_keep_packet_context_and_offline_task(self):
        for condition in ("strongshare", "consolidate"):
            with self.subTest(condition=condition):
                worker = SimpleNamespace()
                active = []

                @contextmanager
                def context(task):
                    self.assertIs(task, self.task)
                    active.append(True)
                    try:
                        yield
                    finally:
                        active.pop()

                def phase(run_dir, selected, **kwargs):
                    self.assertEqual(selected, condition)
                    self.assertEqual(kwargs, {"offline": True, "task": self.task})
                    self.assertTrue(active)
                    self.assertIs(sys.modules["baseline_model"], luna_model)
                    self.assertEqual(worker.VARIANT, "source_handoff_diff_v1")
                    self.assertEqual(worker.CONTINUATION_SECONDS, 739.25)
                    self.assertIs(worker.BudgetGuard, LunaAccounting)
                    self.assertEqual(worker.make_offline_model("agent1", selected), "scripted")
                    return 0

                worker.phase_worker = phase
                with patch.object(wrapper, "load_isolated", return_value=worker) as load, \
                        patch.object(wrapper, "make_timed_apply_handoff", return_value="timed") as handoff, \
                        patch.object(cross_task_workers, "packet_module_context", context), \
                        patch.object(cross_task_workers, "make_offline_model", return_value="scripted") as fake:
                    self.assertEqual(wrapper.phase_worker("/tmp/luna-job", condition, self.task, offline=True,
                                                          execution_seconds=739.25), 0)
                    load.assert_called_once_with("luna_handoff_worker.py")
                    fake.assert_called_once_with(self.task, "agent1", condition)
                    if condition == "consolidate":
                        handoff.assert_called_once_with()
                        self.assertEqual(worker.apply_handoff, "timed")
                    else:
                        handoff.assert_not_called()
                self.assertFalse(active)
                self.assertIs(sys.modules["baseline_model"], self.old_marker)

    def test_exception_restores_existing_and_absent_imports(self):
        for absent in (False, True):
            with self.subTest(absent=absent):
                if absent:
                    sys.modules.pop("baseline_model", None)
                else:
                    sys.modules["baseline_model"] = self.old_marker
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    with wrapper.luna_transport_context():
                        self.assertIs(sys.modules["baseline_model"], luna_model)
                        raise RuntimeError("injected")
                if absent:
                    self.assertNotIn("baseline_model", sys.modules)
                else:
                    self.assertIs(sys.modules["baseline_model"], self.old_marker)

    def test_nested_context_restores_outer_binding(self):
        with wrapper.luna_transport_context():
            with wrapper.luna_transport_context():
                self.assertIs(sys.modules["baseline_model"], luna_model)
            self.assertIs(sys.modules["baseline_model"], luna_model)
        self.assertIs(sys.modules["baseline_model"], self.old_marker)

    def test_invalid_scope_budget_and_metadata_fail_before_worker_load(self):
        bad_task = SimpleNamespace(key=self.task.key, as_dict=lambda: {**self.task.as_dict(), "base": "changed"})
        cases = [dict(condition="solo"), dict(condition="keep"), dict(task=None),
                 dict(task=get_task("hf_datasets3997_f2_f3")), dict(task=bad_task),
                 dict(execution_seconds=True), dict(execution_seconds=float("nan")),
                 dict(execution_seconds=1199), dict(execution_seconds="1200"), dict(offline="yes")]
        with patch.object(wrapper, "load_isolated") as load:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    kwargs = {"condition": "prefix", "task": self.task, **changes}
                    wrapper.phase_worker("/tmp/luna-invalid", **kwargs)
            load.assert_not_called()

    def test_private_loader_leaves_ordinary_module_globals_unchanged(self):
        import run_baselines
        import luna_handoff_worker
        for filename, ordinary in (("run_baselines.py", run_baselines), ("luna_handoff_worker.py", luna_handoff_worker)):
            with self.subTest(filename=filename):
                original = (ordinary.MODEL, ordinary.MAX_OUTPUT, ordinary.BudgetGuard)
                private = wrapper.load_isolated(filename)
                self.assertIsNot(private, ordinary)
                private.MODEL, private.MAX_OUTPUT, private.BudgetGuard = "test-only", 1, object
                self.assertEqual((ordinary.MODEL, ordinary.MAX_OUTPUT, ordinary.BudgetGuard), original)
        with self.assertRaises(ValueError):
            wrapper.load_isolated("../outside.py")

    def test_timed_consolidate_preserves_all_non_timing_handoff_behavior(self):
        import run_consolidate
        from unittest.mock import Mock
        checkpoint = {"task": {"features": [1, 3]}, "agents": {
            "agent1": {"state": {"extra_template_vars": {"task": "feature one"}}},
            "agent2": {"state": {"extra_template_vars": {"task": "feature three"}}},
        }}
        private = wrapper.load_isolated("run_consolidate.py")
        original_seconds = run_consolidate.CONTINUATION_SECONDS
        original_function = run_consolidate.handoff_messages
        called = Mock(side_effect=lambda agent, broker, cp: private.handoff_messages(cp))
        private.apply_handoff = called
        with patch.object(wrapper, "load_isolated", return_value=private):
            bound = wrapper.make_timed_apply_handoff()
        combined, messages = bound("agent", "broker", checkpoint, remaining_seconds=684.125)
        self.assertEqual(combined, original_function(checkpoint)[0])
        expected = original_function(checkpoint)[1]
        old = f"You have {original_seconds} seconds from this continuation, with no monetary or model-turn limit. "
        new = ("You have 684.125 seconds remaining in this continuation's total budget, "
               "including restore and handoff, with no monetary or model-turn limit. ")
        expected[1]["content"] = expected[1]["content"].replace(old, new)
        self.assertEqual(messages, expected)
        self.assertEqual(run_consolidate.CONTINUATION_SECONDS, original_seconds)
        self.assertIs(run_consolidate.handoff_messages, original_function)
        called.assert_called_once_with("agent", "broker", checkpoint)

    def test_new_copy_uses_total_budget_and_no_literal_twenty_minute_prompt(self):
        import ast
        source = (wrapper.ROOT / "luna_handoff_worker.py").read_text()
        tree = ast.parse(source)
        deadline = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "deadline" for t in n.targets)]
        self.assertEqual(len(deadline), 1)
        self.assertEqual(ast.unparse(deadline[0].value), "started + CONTINUATION_SECONDS")
        self.assertIn('"initialization_included_in_budget": True', source)
        self.assertNotIn("You have 1200 seconds", source)
        self.assertIn("remaining_seconds=max(0.0, started + CONTINUATION_SECONDS - time.monotonic())", source)

    def test_partial_first_query_gap_is_terminal_only_for_normal_deadline(self):
        from luna_handoff_worker import unqueried_budget_agents
        active = ("agent1", "agent2")
        with TemporaryDirectory() as tmp:
            phase = Path(tmp)
            (phase / "first_request_agent1.json").write_text('{}')
            outcomes = {"agent1": {"status": "Submitted", "infrastructure_error": False},
                        "agent2": {"status": "deadline", "infrastructure_error": False}}
            self.assertEqual(unqueried_budget_agents(phase, active, outcomes), ["agent2"])
            outcomes["agent2"]["status"] = "Submitted"
            self.assertEqual(unqueried_budget_agents(phase, active, outcomes), [])
            outcomes["agent2"].update(status="deadline", infrastructure_error=True)
            self.assertEqual(unqueried_budget_agents(phase, active, outcomes), [])
            outcomes["agent2"]["infrastructure_error"] = False
            (phase / "first_request_agent2.json").write_text('{}')
            self.assertEqual(unqueried_budget_agents(phase, active, outcomes), [])

    def test_no_query_expiry_check_precedes_parallel_drive(self):
        source = (wrapper.ROOT / "luna_handoff_worker.py").read_text()
        expiry = source.index('status="time_budget_exhausted_before_first_query"')
        executor = source.index('with ThreadPoolExecutor(max_workers=len(active))')
        self.assertLess(expiry, executor)
        self.assertIn('continuation_model_requests=0', source[expiry:executor])
        self.assertIn('except ContinuationBudgetExhausted:\n        exit_code = 0', source)


if __name__ == "__main__":
    unittest.main()
