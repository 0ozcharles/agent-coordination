import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import s1, s2


def config(root, repeats=1, tasks=(1, 2)):
    (root / "configs").mkdir(exist_ok=True)
    (root / "configs/tasks.json").write_text(json.dumps({
        "study": "t", "question": "q", "model": "m", "repeats": 1, "output": "x",
        "tasks": [{"repo": "r", "task_id": i, "features": [1, 2]} for i in tasks]}))
    return {"study": "s2t", "question": "q", "decision_rule": "d", "model": "m", "baseline": "base",
            "arms": {"base": {"setting": "coop", "agent_config": "a.yaml"},
                     "own": {"setting": "coop", "agent_config": "b.yaml"},
                     "git": {"setting": "coop", "agent_config": "a.yaml", "git": True},
                     "solo": {"setting": "solo", "agent_config": "a.yaml"}},
            "tasks_from": "configs/tasks.json", "repeats": repeats,
            "remerge": {"from": "configs/merge_test.json", "method": "llm"}, "output": "out"}


def write(root, cfg, arm, task_id, merged, own=None, status="clean", remerged=None, repeat=1):
    setting = cfg["arms"][arm]["setting"]
    job = {"repeat": repeat, "setting": setting, "repo": "r", "task_id": task_id, "features": [1, 2]}
    path = s1.log_dir(root / s2.arm_config(cfg, root, arm)["output"], job)
    path.mkdir(parents=True)
    (path / "eval.json").write_text(json.dumps({
        "feature1": {"passed": "1" in merged}, "feature2": {"passed": "2" in merged},
        "both_passed": merged == "12", "merge": {"status": status}}))
    agents = ({"agent": {"status": "Submitted"}} if setting == "solo"
              else {"agents": {"agent1": {"status": "Submitted"}, "agent2": {"status": "Submitted"}}})
    (path / "result.json").write_text(json.dumps({"total_cost": 0.01, **agents}))
    if own is not None:
        (path / "agent_eval.json").write_text(json.dumps({f: {g: g in own[f] for g in "12"} for f in "12"}))
    if remerged is not None:
        (path / "merge.json").write_text(json.dumps(
            {"methods": {"llm": {"status": "resolved", "1": "1" in remerged, "2": "2" in remerged}}}))


class S2Tests(unittest.TestCase):
    def test_config_validation_and_plan_mix_arms_across_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root, repeats=2, tasks=range(1, 7))
            jobs = s2.plan(cfg, root)
            self.assertEqual(len(jobs), 4 * 6 * 2)
            self.assertEqual([j["repeat"] for _, j in jobs], sorted(j["repeat"] for _, j in jobs))
            for worker in range(4):
                self.assertGreater(len({name for name, _ in jobs[worker::4]}), 1)
            self.assertEqual(s2.arm_config(cfg, root, "git")["git"], True)
            self.assertEqual(s2.arm_config(cfg, root, "solo")["settings"], ["solo"])
            path = root / "bad.json"
            path.write_text(json.dumps({**cfg, "baseline": "nope"}))
            with self.assertRaises(ValueError):
                s2.load_config(path)

    def test_labels_use_the_remerged_result_and_paired_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root)
            for arm in ("base", "own", "git"):
                # Task 1 conflicted; the re-merge passes both only in "own" and "git".
                write(root, cfg, arm, 1, "", own={"1": "1", "2": "2"}, status="conflicts",
                      remerged="1" if arm == "base" else "12")
                # Task 2: feature 2 fails on its own, and solo can do it -> context.
                write(root, cfg, arm, 2, "1", own={"1": "1", "2": ""})
            write(root, cfg, "solo", 1, "12")
            write(root, cfg, "solo", 2, "12")
            rows = s2.collect(cfg, root)
            got = {(r["arm"], r["task_id"]): (r["label"], r["official_success"], r["success"]) for r in rows}
            self.assertEqual(got[("base", 1)], ("integration", False, False))
            self.assertEqual(got[("own", 1)], ("passed", False, True))
            self.assertEqual(got[("own", 2)], ("context", False, False))
            self.assertEqual(got[("solo", 1)], ("passed", True, True))
            summary = s2.summarize(cfg, rows)
            self.assertEqual((summary["base"]["passed"], summary["own"]["passed"], summary["own"]["official_passed"]),
                             (0, 1, 0))
            diff = summary["own"]["vs_baseline"]
            self.assertEqual((diff["tasks"], diff["difference"]), (2, 0.5))
            self.assertNotIn("vs_baseline", summary["base"])


if __name__ == "__main__":
    unittest.main()
