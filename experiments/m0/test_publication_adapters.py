"""Publication adapter integrity/routing tests; no Docker or model requests."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import publication_evaluator as evaluator
import publication_guard as guard
import publication_workers as workers
from budget_guard import write_json


class PublicationAdapters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name).resolve() / "publication_runs"
        self.task = workers.get_task(workers.TASK_KEY)
        replacement = patch.object(workers, "RUNS", self.runs)
        replacement.start()
        self.addCleanup(replacement.stop)

    def fixture(self, condition, receipt=True):
        group = "solo_jobs" if condition == "solo" else "jobs"
        phase = self.runs / "suite" / self.task.key / group / ("job_" + condition) / condition
        phase.mkdir(parents=True)
        config = {"task_key": self.task.key, "task": self.task.as_dict(), "condition": condition,
                  "execution_seconds": 55.25, "offline_selftest": True,
                  "publication_guard_version": guard.GUARD_VERSION}
        write_json(phase.parent / "task_config.json", config)
        write_json(phase / "status.json", {"status": "completed", "task": self.task.as_dict()})
        write_json(phase / "source_manifest.json", {"fixture": "a" * 64})
        for agent in (["solo"] if condition == "solo" else ["agent1", "agent2"]):
            write_json(phase / f"{agent}.traj.json", {"messages": [{"role": "system", "content": "fixture"}]})
        if receipt:
            self.write_guard(phase)
        return phase

    def write_guard(self, phase, **changes):
        record = {"schema": "publication_guard_v1", "guard_version": guard.GUARD_VERSION,
                  "run_id": phase.parent.name, "task_key": self.task.key, "condition": phase.name,
                  "max_nudges_per_agent": 2, "counts": {}, "events": [], "context_closed": True}
        record.update(changes)
        write_json(phase / "publication_guard.json", record)
        return record

    def native_verifier(self, phase, task, condition, freeze=False):
        record = {"fixture_original_receipt": condition}
        if freeze:
            write_json(phase / "execution_receipt.json", record)
        else:
            self.assertEqual(json.loads((phase / "execution_receipt.json").read_text()), record)
        return record

    def test_scope_rejects_old_root_other_task_and_symlink(self):
        phase = self.fixture("solo")
        self.assertEqual(workers.validate_phase(phase), phase)
        with self.assertRaises(ValueError):
            workers.get_task("dspy8563_f2_f3")
        link = phase.parent / "linked"
        link.symlink_to(phase, target_is_directory=True)
        with self.assertRaises(ValueError):
            workers.validate_phase(link)
        with patch.object(workers, "RUNS", self.runs / "other"):
            with self.assertRaises(ValueError):
                workers.validate_phase(phase)

    def test_every_condition_inside_guard_with_same_budget_and_original_worker(self):
        for condition in workers.CONDITIONS:
            phase = self.fixture(condition, receipt=False)
            sequence = []

            @contextmanager
            def context(run):
                self.assertEqual(run, phase.parent)
                sequence.append("enter")
                try:
                    yield
                finally:
                    self.write_guard(phase)
                    sequence.append("close")

            def invoke(run, selected, task, **kwargs):
                self.assertEqual(sequence, ["enter"])
                self.assertEqual((run, selected, task), (phase.parent, condition, self.task))
                self.assertEqual(kwargs, {"offline": True, "execution_seconds": 55.25})
                sequence.append("native")
                return 0

            with patch.object(guard, "publication_guard", context), \
                    patch.object(workers, "load_private", return_value=SimpleNamespace(phase_worker=invoke)):
                self.assertEqual(workers.phase_worker(phase.parent, condition, self.task, True, 55.25), 0)
            self.assertEqual(sequence, ["enter", "native", "close"])

    def test_worker_exception_unwinds_guard_without_fabricating_completion(self):
        phase = self.fixture("solo", receipt=False)
        calls = []

        @contextmanager
        def context(run):
            try:
                yield
            finally:
                calls.append("closed")
                self.write_guard(phase)

        with patch.object(guard, "publication_guard", context), patch.object(workers, "load_private",
                return_value=SimpleNamespace(phase_worker=Mock(side_effect=RuntimeError("fixture")))):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                workers.phase_worker(phase.parent, "solo", self.task, True, 55.25)
        self.assertEqual(calls, ["closed"])
        self.assertFalse((phase / "execution_receipt.json").exists())

    def test_no_retry_over_existing_guard_or_argument_drift(self):
        phase = self.fixture("solo")
        with patch.object(workers, "load_private") as dispatch:
            with self.assertRaisesRegex(RuntimeError, "archived"):
                workers.phase_worker(phase.parent, "solo", self.task, True, 55.25)
            with self.assertRaises(ValueError):
                workers.phase_worker(phase.parent, "solo", self.task, True, 56)
            with self.assertRaises(ValueError):
                workers.phase_worker(phase.parent, "prefix", self.task, True, 55.25)
            dispatch.assert_not_called()

    def test_native_receipt_unchanged_and_readonly_verification_protects_all_three_traces(self):
        fake = SimpleNamespace(verify_execution=self.native_verifier)
        for condition in workers.CONDITIONS:
            phase = self.fixture(condition)
            with patch.object(workers, "load_private", return_value=fake):
                native = workers.verify_execution(phase, self.task, condition, freeze=True)
                native_bytes = (phase / "execution_receipt.json").read_bytes()
                self.assertEqual(workers.verify_execution(phase, self.task, condition), native)
                self.assertEqual((phase / "execution_receipt.json").read_bytes(), native_bytes)
                extra = json.loads((phase / workers.RECEIPT).read_text())
                names = set(extra["protected_files_sha256"])
                agents = ["solo"] if condition == "solo" else ["agent1", "agent2"]
                for agent in agents:
                    self.assertIn(f"{condition}/{agent}.traj.json", names)
                trajectory = phase / f"{agents[-1]}.traj.json"
                trajectory.write_text('{"messages": ["tampered"]}')
                with self.assertRaises(RuntimeError):
                    workers.verify_execution(phase, self.task, condition)

    def test_guard_mutation_is_detected_and_missing_extra_not_created_readonly(self):
        phase = self.fixture("solo")
        with patch.object(workers, "load_private", return_value=SimpleNamespace(verify_execution=self.native_verifier)):
            self.native_verifier(phase, self.task, "solo", True)
            with self.assertRaises(ValueError):
                workers.verify_execution(phase, self.task, "solo")
            self.assertFalse((phase / workers.RECEIPT).exists())
            workers.verify_execution(phase, self.task, "solo", True)
            self.write_guard(phase, events=[{"reason": "altered"}])
            with self.assertRaises(RuntimeError):
                workers.verify_execution(phase, self.task, "solo")

    def test_open_or_cross_run_guard_rejected(self):
        phase = self.fixture("solo")
        for change in ({"context_closed": False}, {"run_id": "another"}, {"counts": {"agent1": 1}},
                       {"counts": {"solo": 3}}):
            self.write_guard(phase, **change)
            with self.assertRaises(ValueError):
                workers.read_guard(phase)

    def test_extra_manifest_protection_is_copy_and_detects_new_bytes(self):
        phase = self.fixture("solo")
        write_json(phase / workers.RECEIPT, {"fixture": True})
        original = {"protected_files": {"old": {"sha256": "a"}}, "patches": {"same": True}}
        prior = copy.deepcopy(original)
        before = workers.extend_input_manifest(original, phase)
        self.assertEqual(original, prior)
        self.assertEqual(before["patches"], original["patches"])
        self.assertIn(str(phase / workers.RECEIPT), before["protected_files"])
        self.write_guard(phase, counts={"solo": 1})
        after = workers.extend_input_manifest(original, phase)
        self.assertNotEqual(before, after)

    def test_all_evaluators_route_children_to_new_cli_and_preserve_native_paths(self):
        import selector_evaluator as old
        original_root, original_file = old.RUNS, old.__file__
        for condition in workers.CONDITIONS:
            result = evaluator.make_evaluator(condition)
            self.assertEqual(result.__file__, evaluator.__file__)
            self.assertEqual(result._implementation.__file__, evaluator.__file__)
        self.assertEqual((old.RUNS, old.__file__), (original_root, original_file))

    def test_integrator_internal_manifest_not_only_public_facade_is_extended(self):
        phase = self.fixture("integrator")
        write_json(phase / workers.RECEIPT, {"fixture": True})
        result = evaluator.make_evaluator("integrator")
        internal = result._implementation.input_manifest.__globals__
        fake_info = lambda p: {"path": str(p), "sha256": "a" * 64}
        with patch.dict(internal, {"file_info": fake_info, "source_manifest": lambda: {}}):
            # The native worker/cache calls this global function directly.
            manifest = internal["input_manifest"](phase, self.task, {"agent1.patch": {}}, False)
        self.assertIn(str(phase / workers.RECEIPT), manifest["protected_files"])
        self.assertIn(str(phase / "publication_guard.json"), manifest["protected_files"])

    def test_integrator_alias_restored_on_exception(self):
        sentinel = SimpleNamespace(original=True)
        with patch.dict(sys.modules, {"luna_integrator_worker": sentinel}):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with workers.evaluator_context():
                    self.assertIs(sys.modules["luna_integrator_worker"], workers)
                    raise RuntimeError("fixture")
            self.assertIs(sys.modules["luna_integrator_worker"], sentinel)


if __name__ == "__main__":
    unittest.main()
