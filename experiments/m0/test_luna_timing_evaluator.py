"""Timing child-suite layout/cache compatibility; no Docker or model calls."""
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import evaluate_cross_task
import luna_collaboration_evaluator as evaluator
import task_specs
from task_specs import TaskSpec


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class TimingEvaluatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        self.task = TaskSpec("llama_index17070_f1_f3", "fixture", 17070, (1, 3), "fixture:17070", "a" * 40, ("src/pkg",))
        for patcher in (
            mock.patch.object(evaluate_cross_task, "RUNS", self.runs),
            mock.patch.object(evaluate_cross_task, "get_task", return_value=self.task),
            mock.patch.object(evaluator, "source_manifest", return_value={"frozen_evaluator.py": "b" * 64}),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        write(self.task.image_record_path, {"Id": "sha256:" + "c" * 64, "Architecture": "arm64",
              "expected_base": self.task.base, "image": self.task.image})

    def job(self, stage, condition, repeat=1):
        phase = (self.runs / f"luna_timing_v1_{stage}" / self.task.key / "jobs"
                 / f"{self.task.key}_{stage}_r{repeat:02d}_{condition}" / condition)
        phase.mkdir(parents=True)
        config = {"task_key": self.task.key, "task": self.task.as_dict(), "condition": condition,
                  "condition_version": "luna_timing_v1", "offline_selftest": True,
                  "protocol_sha256": "d" * 64, "checkpoint_stage": stage,
                  "checkpoint_sha256": ("e" if stage == "early" else "f") * 64}
        write(phase.parent / "task_config.json", config)
        status = {"status": "completed", "task": self.task.as_dict(), "offline_selftest": True}
        names = ["final.patch"] if condition == "consolidate" else ["agent1.patch", "agent3.patch"]
        hashes = {}
        for name in names:
            path = phase / name
            path.write_text("synthetic complete code patch\n")
            hashes[name] = evaluator.file_info(path)["sha256"]
        if condition == "consolidate":
            status["final_patch_sha256"] = hashes["final.patch"]
        write(phase / "status.json", status)
        write(phase / "execution_receipt.json", {"patch_sha256": hashes})
        write(phase.parent / "checkpoint.json", {"stage": stage})
        write(phase.parent / "checkpoint_provenance.json", {"stage": stage})
        (phase.parent / "shared_git.tar").write_bytes(b"synthetic shared state")
        for name in ("handoff_packet.json", "handoff_delivery.json"):
            write(phase / name, {"stage": stage})
        (phase / "handoff_packet.txt").write_text("synthetic evidence " + stage)
        for agent in (("agent1", "agent2") if condition == "strongshare" else ("agent1",)):
            write(phase / f"first_request_{agent}.json", {"verified": True})
        return phase, config

    def cache(self, phase, config):
        module = evaluator.make_evaluator(phase.name)
        module.verify_upstream = mock.Mock()
        task, loaded = module.load_task(phase)
        self.assertEqual(loaded, config)
        patches = module.patch_info(phase, task)
        attempt = phase / "evaluation_attempts/fixture_attempt"
        attempt.mkdir(parents=True)
        inputs = module.input_manifest(phase, task, patches)
        write(attempt / "evaluation_input.json", {"task": task.as_dict(), "protocol_sha256": config["protocol_sha256"],
              "input_manifest": inputs})
        write(attempt / "integrity.json", {"unchanged": True, "before": inputs, "after": inputs})
        sb = SimpleNamespace(exec=lambda *args: SimpleNamespace(returncode=0,
             stdout_read=lambda: base64.b64encode(b"synthetic tested patch\n").decode()))
        native = SimpleNamespace(_run_tests=lambda *args: {"passed": False, "exit_code": 1, "output": "synthetic failed test"})

        def invoke():
            name = "solo.patch" if phase.name == "consolidate" else "merged.patch"
            results = [native._run_tests(sb, f"tests{i}.patch", name, task.base) for i in (1, 2)]
            result = {"both_passed": False, "error": None,
                      **{f"feature{i}": {"passed": r["passed"], "test_output": r["output"]}
                         for i, r in enumerate(results, 1)}}
            if phase.name == "strongshare":
                result["merge"] = {"status": "clean", "strategy": "naive", "diff": "synthetic"}
            return result

        result = evaluator.capture_evaluation(native, invoke, phase.name, task, patches, attempt)
        for path in (attempt / "official_evaluation.json", attempt / "evaluation.json", phase / "evaluation.json"):
            write(path, result)
        summary = module.summarize(result, "failed", phase, attempt, 1.0, task, config)
        write(phase / "evaluation_summary.json", summary)
        return module, task, summary

    def test_both_child_suites_all_flat_job_names_are_accepted(self):
        for stage in ("early", "late"):
            for condition in ("strongshare", "consolidate"):
                for repeat in (1, 2):
                    phase, config = self.job(stage, condition, repeat)
                    self.assertEqual(evaluator.validate_run_dir(phase), phase)
                    module = evaluator.make_evaluator(condition)
                    self.assertEqual(module.load_task(phase), (self.task, config))
                    self.assertEqual(Path(module.__file__).name, "luna_collaboration_evaluator.py")

    def test_extra_stage_directory_is_rejected(self):
        for path in (
            self.runs / "luna_timing_v1" / self.task.key / "early/jobs/job/strongshare",
            self.runs / "luna_timing_v1_early" / self.task.key / "early/jobs/job/consolidate",
        ):
            path.mkdir(parents=True)
            with self.assertRaises(ValueError):
                evaluator.validate_run_dir(path)

    def test_task_level_packet_records_are_separate_for_early_and_late(self):
        early, _ = self.job("early", "strongshare")
        late, _ = self.job("late", "strongshare")
        early_record = early.parent.parent.parent / "packet_record.json"
        late_record = late.parent.parent.parent / "packet_record.json"
        self.assertNotEqual(early_record, late_record)
        write(early_record, {"stage": "early"})
        write(late_record, {"stage": "late"})
        self.assertEqual(json.loads(early_record.read_text()), {"stage": "early"})

    def test_stage_config_mutation_invalidates_completed_cache(self):
        phase, config = self.job("early", "strongshare")
        module, task, summary = self.cache(phase, config)
        self.assertEqual(evaluator.resume_completed(module, phase, task, config), summary)
        changed = dict(config, checkpoint_stage="late", checkpoint_sha256="f" * 64)
        write(phase.parent / "task_config.json", changed)
        _, loaded = module.load_task(phase)
        with self.assertRaisesRegex(ValueError, "inputs or sources changed"):
            evaluator.resume_completed(module, phase, task, loaded)

    def test_checkpoint_mutation_invalidates_late_cache(self):
        phase, config = self.job("late", "consolidate")
        module, task, summary = self.cache(phase, config)
        self.assertEqual(evaluator.resume_completed(module, phase, task, config), summary)
        write(phase.parent / "checkpoint.json", {"stage": "early", "wrong_state": True})
        with self.assertRaisesRegex(ValueError, "inputs or sources changed"):
            evaluator.resume_completed(module, phase, task, config)

    def test_late_sibling_does_not_change_early_evaluation_inputs(self):
        phase, config = self.job("early", "consolidate")
        module, task, summary = self.cache(phase, config)
        self.job("late", "consolidate")
        self.assertEqual(evaluator.resume_completed(module, phase, task, config), summary)


if __name__ == "__main__":
    unittest.main()
