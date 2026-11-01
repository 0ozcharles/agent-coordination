"""Registry, evidence and scoring adapter checks without Docker or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import selector_workers as worker
import selector_evaluator as evaluator
import selector_task_specs as registry
import task_specs
from run_baselines import digest
from test_cross_task_packet import PacketFixture


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.task = registry.get_task("click2068_f4_f10")

    def test_worker_routes_new_feature_ids_and_exact_time_without_changing_old_scope(self):
        import luna_collaboration_worker as old
        old_scope = old.TASK_KEYS
        for condition in worker.CONDITIONS:
            fake = SimpleNamespace(phase_worker=mock.Mock(return_value=7))
            seconds = 1200 if condition == "prefix" else 1734.5
            with mock.patch.object(worker, "load_private", return_value=fake), mock.patch.object(worker, "frozen_packet_context") as packet:
                self.assertEqual(worker.phase_worker(self.root, condition, self.task, True, seconds), 7)
                args, kwargs = fake.phase_worker.call_args
                if condition == "solo":
                    self.assertEqual(args, (self.root, self.task))
                    self.assertEqual(kwargs, {"offline": True, "execution_seconds": seconds})
                    packet.assert_not_called()
                else:
                    self.assertEqual(args, (self.root, condition, self.task, True, seconds))
                    self.assertEqual(fake.TASK_KEYS, tuple(registry.TASKS))
                    if condition != "prefix":
                        packet.assert_called_once_with(self.root, self.task)
        self.assertEqual(old.TASK_KEYS, old_scope)

    def test_bad_scope_or_allowance_rejected_before_loading_a_worker(self):
        with mock.patch.object(worker, "load_private") as load:
            for condition, seconds, offline in (("prefix", 1199, True), ("integrator", 0, True),
                    ("solo", float("nan"), True), ("solo", True, True), ("keep", 20, True), ("solo", 10, 1)):
                with self.subTest(condition=condition, seconds=seconds, offline=offline), self.assertRaises(ValueError):
                    worker.phase_worker(self.root, condition, self.task, offline, seconds)
            fake = copy.copy(self.task)
            object.__setattr__(fake, "features", (1, 2))
            with self.assertRaises(ValueError):
                worker.phase_worker(self.root, "solo", fake)
            load.assert_not_called()

    def test_registry_context_restores_ordinary_modules_on_exception(self):
        import cross_task_specs
        original = cross_task_specs.TASKS
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with worker.registry_context():
                self.assertIs(sys.modules["cross_task_specs"], registry)
                raise RuntimeError("failure")
        self.assertIs(sys.modules["cross_task_specs"], cross_task_specs)
        self.assertIs(cross_task_specs.TASKS, original)
        import run_handoff_expansion
        self.assertIs(run_handoff_expansion.TASKS, original)

    def test_new_paths_registered_tasks_and_symlink_rejection(self):
        with mock.patch.object(evaluator, "RUNS", self.root):
            for condition in evaluator.CONDITIONS:
                phase = self.root / "suite" / self.task.key / ("solo_jobs" if condition == "solo" else "jobs") / "r1" / condition
                phase.mkdir(parents=True)
                self.assertEqual(evaluator.validate_run_dir(phase), phase)
            foreign = self.root / "suite/unknown/jobs/r1/integrator"
            foreign.mkdir(parents=True)
            with self.assertRaises(ValueError):
                evaluator.validate_run_dir(foreign)
            link = self.root / "linked"
            link.symlink_to(self.root / "suite", target_is_directory=True)
            with self.assertRaises(ValueError):
                evaluator.validate_run_dir(link / self.task.key / "jobs/r1/integrator")

    def test_solo_receipt_readonly_requires_freeze_and_detects_later_change(self):
        phase = self.root / "solo"
        phase.mkdir()
        write(self.root / "task_config.json", {"task": self.task.as_dict(), "condition": "solo", "fresh_solo": True})
        patch = b"complete published implementation"
        (phase / "final.patch").write_bytes(patch)
        write(phase / "status.json", {"status": "completed", "task": self.task.as_dict(), "fresh_solo": True,
            "final_patch_sha256": hashlib.sha256(patch).hexdigest()})
        for name, value in (("initial_state.json", {"fresh_solo": True}), ("source_manifest.json", {}), ("solo.traj.json", {"messages": []})):
            write(phase / name, value)
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            worker.verify_execution(phase, self.task, "solo")
        self.assertFalse((phase / "execution_receipt.json").exists())
        receipt = worker.verify_execution(phase, self.task, "solo", freeze=True)
        before = (phase / "execution_receipt.json").read_bytes()
        self.assertEqual(worker.verify_execution(phase, self.task, "solo"), receipt)
        self.assertEqual((phase / "execution_receipt.json").read_bytes(), before)
        write(phase / "solo.traj.json", {"messages": ["tampered"]})
        with self.assertRaises(RuntimeError):
            worker.verify_execution(phase, self.task, "solo")

    def test_all_evaluator_children_and_source_checks_route_to_new_adapter(self):
        import luna_integrator_evaluator as historical
        original = historical.TASK_KEYS
        with mock.patch.object(evaluator, "source_manifest", return_value={"frozen": "sha"}):
            for condition in evaluator.CONDITIONS:
                adapter = evaluator.make_evaluator(condition)
                self.assertEqual(Path(adapter.__file__).name, "selector_evaluator.py")
                self.assertEqual(Path(adapter._implementation.__file__).name, "selector_evaluator.py")
                self.assertEqual(adapter.source_manifest(), {"frozen": "sha"})
        self.assertEqual(historical.TASK_KEYS, original)

    def test_integrator_native_feature_mapping_keeps_agent1_authority(self):
        private = worker.load_private("luna_integrator_evaluator.py")
        self.assertEqual(private.patch_names(self.task), ("agent1.patch", "agent2.patch"))
        patches = {"agent2.patch": {"path": "peer"}, "agent1.patch": {"path": "lead"}}
        with mock.patch.object(private, "read", return_value={}):
            contributions = private.contributions(self.root, self.task, patches)
        self.assertEqual(contributions["agent1"]["feature"], 4)
        self.assertEqual(contributions["agent2"]["feature"], 10)
        self.assertEqual(contributions["agent1"]["patch"]["path"], "lead")
        self.assertTrue(contributions["agent1"]["graded"])
        self.assertFalse(contributions["agent2"]["graded"])

    def test_failed_solo_score_is_reused_but_source_change_rejected(self):
        phase = self.root / "solo"
        attempt = phase / "evaluation_attempts/one"
        attempt.mkdir(parents=True)
        adapter = evaluator.make_evaluator("solo")
        module = adapter._implementation
        config = {"offline_selftest": True, "protocol_sha256": "a" * 64,
                  "condition_version": "luna_fresh_solo_v1"}
        native = {"both_passed": False, "feature1": {"passed": False}, "feature2": {"passed": True}}
        current = {"frozen_sources": {"test": "1"}}
        summary = module.summarize(native, "failed", phase, attempt, 1.0, self.task, config)
        for path in (phase / "evaluation.json", attempt / "evaluation.json", attempt / "official_evaluation.json"):
            write(path, native)
        write(phase / "evaluation_summary.json", summary)
        write(attempt / "evaluation_summary.json", summary)
        write(attempt / "evaluation_input.json", {"task": self.task.as_dict(), "protocol_sha256": config["protocol_sha256"], "input_manifest": current})
        write(attempt / "integrity.json", {"unchanged": True, "before": current, "after": current})
        with mock.patch.object(module, "input_manifest", return_value=current) as manifest, mock.patch.object(module, "patch_info", return_value={}), mock.patch.object(module, "verify_upstream"):
            self.assertEqual(adapter.resume_completed(phase, self.task, config), summary)
            manifest.return_value = {"frozen_sources": {"test": "2"}}
            with self.assertRaisesRegex(ValueError, "inputs or sources changed"):
                adapter.resume_completed(phase, self.task, config)


class PreparationTests(PacketFixture):
    def setUp(self):
        super().setUp()
        root_patch = mock.patch.object(task_specs, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        patcher = mock.patch.object(registry, "get_task", return_value=self.task)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exact_packet_required_before_any_branch_dispatch(self):
        expected = self.build()
        with mock.patch.object(worker, "decision_packet", return_value=expected):
            with worker.frozen_packet_context(self.root, self.task):
                import cross_task_packet
                self.assertEqual(cross_task_packet.build_packet(self.checkpoint, self.task, self.changes, self.scratch), expected)
                altered = copy.deepcopy(expected)
                altered["agents"]["agent1"]["original_task"] += " changed"
                with mock.patch.object(cross_task_packet, "_SOURCE_VALIDATOR") as source:
                    # Use a distinct otherwise valid packet through the original
                    # build function to prove mismatch fails before model setup.
                    source.build_packet.return_value = {
                        "checkpoint_sha256": "b" * 64, "task_key": self.task.key, "base_commit": self.task.base,
                        "agents": {a: {"original_task": self.requirements[a], "source_changes": self.changes[a], "scratch_files": self.scratch[a]} for a in self.task.agents}}
                    with self.assertRaises((RuntimeError, ValueError)):
                        cross_task_packet.build_packet(self.checkpoint, self.task, self.changes, self.scratch)

    def collect(self, fail_manifest=False):
        import docker
        import baseline_runtime
        import cross_task_packet
        prefix = self.root / "prefix"
        self.checkpoint["git_archive_sha256"] = hashlib.sha256(b"git archive").hexdigest()
        for a in self.task.agents:
            self.checkpoint["agents"][a]["image"] = "sha256:" + ("1" if a == "agent1" else "2") * 64
        write(prefix / "checkpoint.json", self.checkpoint)
        write(prefix / "prefix/status.json", {"status": "completed"})
        (prefix / "shared_git.tar").write_bytes(b"git archive")
        write(self.task.image_record_path, {"Id": "image-id"})
        envs = {a: SimpleNamespace(agent=a, cleanup=mock.Mock()) for a in self.task.agents}
        client = mock.Mock()
        client.images.get.return_value.id = "image-id"
        with mock.patch("wip_checkpoint.validate_checkpoint"), mock.patch.object(docker, "from_env", return_value=client), mock.patch.object(baseline_runtime, "restore_volume") as restore, mock.patch.object(baseline_runtime, "SnapshotDockerEnvironment", side_effect=lambda **kw: envs[kw["agent_id"]]), mock.patch.object(baseline_runtime, "verify_workspace_manifest", side_effect=RuntimeError("snapshot drift") if fail_manifest else None) as verify, mock.patch.object(cross_task_packet, "collect_source_changes", side_effect=lambda env, task: self.changes[env.agent]), mock.patch.object(cross_task_packet, "collect_scratch_files", side_effect=lambda env, saved: self.scratch[env.agent]):
            destination = self.root / "decision/packet.json"
            if fail_manifest:
                with self.assertRaisesRegex(RuntimeError, "snapshot drift"):
                    worker.collect_checkpoint_packet(prefix, self.task, destination)
                self.assertFalse(destination.exists())
                envs["agent1"].cleanup.assert_called_once()
            else:
                result = worker.collect_checkpoint_packet(prefix, self.task, destination)
                self.assertEqual(result, self.build())
                self.assertEqual(json.loads(destination.read_text()), result)
                self.assertEqual(verify.call_count, 2)
                for env in envs.values():
                    env.cleanup.assert_called_once()
            self.assertEqual(restore.call_args.kwargs["expected_sha256"], self.checkpoint["git_archive_sha256"])
            client.volumes.get.return_value.remove.assert_called_once()
            client.close.assert_called_once()

    def test_collector_restores_both_snapshots_and_writes_only_after_cleanup(self):
        self.collect()

    def test_changed_snapshot_cannot_publish_packet_and_resources_are_cleaned(self):
        self.collect(fail_manifest=True)


if __name__ == "__main__":
    unittest.main()
