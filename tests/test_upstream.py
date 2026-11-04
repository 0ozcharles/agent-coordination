import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import upstream


def pair(f1, f2, conflict=False, naive=(None, None), union=(None, None)):
    return {"feature1_id": f1, "feature2_id": f2, "has_naive_merge_conflict": conflict,
            "feature1_naive_merge_test_passed": naive[0], "feature2_naive_merge_test_passed": naive[1],
            "feature1_union_merge_test_passed": union[0], "feature2_union_merge_test_passed": union[1]}


def setup(root, exclude=()):
    published = root / "out/published"
    (published / "coop/typst/task1").mkdir(parents=True)
    (published / "coop/dspy_task/task2").mkdir(parents=True)
    (published / "solo/typst/task1").mkdir(parents=True)
    (published / "index.json").write_text(json.dumps(
        {"coop": ["coop/typst/task1", "coop/dspy_task/task2"], "solo": ["solo/typst/task1"]}))
    (published / "coop/typst/task1/aggregated_results_gpt5_k1.json").write_text(json.dumps({"detailed_results": [
        pair(1, 2, naive=(True, True)),
        pair(1, 3, conflict=True, union=(True, True)),
        pair(2, 3, conflict=True, union=(False, True))]}))
    (published / "coop/dspy_task/task2/aggregated_results_gpt5_k1.json").write_text(json.dumps({"detailed_results": [
        pair(1, 2, naive=(True, False))]}))
    (published / "solo/typst/task1/aggregated_results_gpt5_k1.json").write_text(json.dumps({"detailed_results": [
        {"feature1_id": f1, "feature2_id": f2, "both_tests_passed": True} for f1, f2 in ((1, 2), (1, 3), (2, 3))]}))
    for repo, task in (("typst_task", 1), ("dspy_task", 2)):
        (root / "experiments/m0/CooperBench/dataset" / repo / f"task{task}").mkdir(parents=True)
    return {"study": "u", "question": "q", "decision_rule": "d", "output": "out",
            "source": {"dataset": "x/y", "revision": "abc"}, "models": {"gpt5": "GPT-5"},
            "exclude_repos": list(exclude), "sets": {"calibration": {"sample": 5, "seed": 1}, "remerge": {}},
            "remerge": {"from": "configs/merge_test.json", "method": "llm"}}


class UpstreamTests(unittest.TestCase):
    def test_sets_follow_the_paper_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = setup(root)
            remerge = upstream.select(config, root, "remerge")
            self.assertEqual([(j["repo"], j["task_id"], j["features"]) for j in remerge], [("typst_task", 1, [2, 3])])
            calibration = upstream.select(config, root, "calibration")
            self.assertEqual(sorted((j["repo"], j["features"][1]) for j in calibration),
                             [("dspy_task", 2), ("typst_task", 2)])
            config["exclude_repos"] = ["dspy_task"]
            self.assertEqual(len(upstream.select(config, root, "calibration")), 1)

    def test_remerge_result_closes_the_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = setup(root, exclude=("dspy_task",))
            job = upstream.select(config, root, "remerge")[0]
            target = upstream.job_dir(config, root, job)
            target.mkdir(parents=True)
            (target / "merge.json").write_text(json.dumps(
                {"methods": {"llm": {"status": "resolved", "2": True, "3": True, "cost_usd": 0.001}}}))
            rows, calibration = upstream.collect(config, root)
            summary = upstream.summarize(config, rows, calibration)["gpt5"]
            self.assertEqual((summary["pairs"], summary["remerge"]["recovered"]), (3, 1))
            self.assertAlmostEqual(summary["with_solo"]["paper"], 2 / 3)
            self.assertAlmostEqual(summary["with_solo"]["gap_closed"], 1.0)
            self.assertTrue(summary["claim_holds"])


if __name__ == "__main__":
    unittest.main()
