import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import paper_census, upstream


def pair(f1, f2, conflict=False, naive=(None, None), union=(None, None), llm=(None, None)):
    entry = {"feature1_id": f1, "feature2_id": f2, "has_naive_merge_conflict": conflict}
    for stage, values in (("naive", naive), ("union", union), ("llm", llm)):
        for n, value in zip((1, 2), values):
            entry[f"feature{n}_{stage}_merge_test_passed"] = value
    return entry


def setup(root):
    published = root / "out/published"
    for path in ("coop/typst/task1", "solo/typst/task1"):
        (published / path).mkdir(parents=True)
    (published / "index.json").write_text(json.dumps({"coop": ["coop/typst/task1"], "solo": ["solo/typst/task1"]}))
    (published / "coop/typst/task1/aggregated_results_gpt5_k1.json").write_text(json.dumps({"detailed_results": [
        pair(1, 2, naive=(True, True)),                                 # passes: not in the census
        pair(1, 3, naive=(False, True)),                                # clean, feature 1 fails
        pair(2, 3, conflict=True, union=(True, False), llm=(False, False)),
        pair(1, 4)]}))                                                  # clean, no recorded result
    (published / "solo/typst/task1/aggregated_results_gpt5_k1.json").write_text(json.dumps({"detailed_results": [
        {"feature1_id": f1, "feature2_id": f2, "feature1_test_passed": p1, "feature2_test_passed": p2,
         "both_tests_passed": p1 and p2}
        for f1, f2, p1, p2 in ((1, 2, True, True), (1, 3, True, True), (2, 3, False, True), (1, 4, True, False))]}))
    (root / "experiments/m0/CooperBench/dataset/typst_task/task1").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "configs/base.json").write_text(json.dumps(
        {"study": "u", "question": "q", "decision_rule": "d", "output": "out",
         "source": {"dataset": "x/y", "revision": "abc"}, "models": {"gpt5": "GPT-5"}, "exclude_repos": [],
         "sets": {}, "remerge": {}}))
    (root / "census.json").write_text(json.dumps(
        {"study": "c", "question": "q", "from": "configs/base.json", "labels": {}, "decision_rule": "d",
         "output": "out"}))
    return paper_census.load_config(root / "census.json", root)


def record(config, root, job, own, retest=None):
    target = upstream.job_dir(config, root, job)
    target.mkdir(parents=True, exist_ok=True)
    f1, f2 = (str(f) for f in job["features"])
    (target / "agent_eval.json").write_text(json.dumps(
        {f: {f1: f1 in own[f], f2: f2 in own[f], "error": None} for f in (f1, f2)}))
    for f in (f1, f2):
        (target / f"agent{f}.patch").write_text(f"diff --git a/src{f}.rs b/src{f}.rs\n")
    if retest is not None:
        (target / "calibration.json").write_text(json.dumps(retest))


class PaperCensusTests(unittest.TestCase):
    def test_labels_follow_s1_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, base = setup(root)
            jobs = {tuple(j["features"]): j for j in paper_census.select(base, root)}
            self.assertEqual(sorted(jobs), [(1, 3), (1, 4), (2, 3)])
            # Clean pair: agent 1's own patch passes feature 1, the merged code fails it.
            record(base, root, jobs[1, 3], {"1": ["1"], "3": ["3"]}, {"merge": "clean", "1": False, "3": True})
            # Conflicted pair: the last stage (llm) fails both; agent 2 fails its own feature,
            # which solo passes in 1 of 2 pairs; agent 3's own patch passes.
            record(base, root, jobs[2, 3], {"2": [], "3": ["3"]})
            # Clean pair without a recorded result: our re-test merges cleanly and fails feature 4,
            # which solo never passes; agent 1's patch passes feature 4 (a handoff).
            record(base, root, jobs[1, 4], {"1": ["1", "4"], "4": []}, {"merge": "clean", "1": True, "4": False})
            rows = {tuple(int(f) for f in r["features"].split()): r for r in paper_census.collect(base, root)}
            self.assertEqual((rows[1, 3]["feature_labels"], rows[1, 3]["label"]), ("1:integration", "integration"))
            self.assertEqual(rows[2, 3]["merged_source"], "llm")
            self.assertEqual(rows[2, 3]["feature_labels"], "2:context 3:integration")
            self.assertEqual(rows[1, 4]["merged_source"], "retest")
            self.assertEqual((rows[1, 4]["label"], rows[1, 4]["refined_label"]), ("capability", "handoff"))
            self.assertTrue(rows[1, 4]["coordination"])
            summary = paper_census.summarize(config, base, list(rows.values()))["models"]["gpt5"]
            self.assertEqual(summary["integration"], {"semantic": 1, "conflict": 1})
            self.assertEqual(summary["gap"]["pairs"], 1)  # only (1, 3) has a solo run passing both
            self.assertEqual(summary["gap"]["integration"]["mean"], 1.0)
            self.assertEqual(summary["retest"], {"clean_pairs": 2, "merge_clean": 2, "failed_features": 1,
                                                 "reproduced": 1})

    def test_unreproduced_failures_leave_the_robust_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, base = setup(root)
            job = next(j for j in paper_census.select(base, root) if j["features"] == [1, 3])
            record(base, root, job, {"1": ["1"], "3": ["3"]}, {"merge": "clean", "1": True, "3": True})
            row = next(r for r in paper_census.collect(base, root) if r["features"] == "1 3")
            self.assertEqual((row["label"], row["robust_label"]), ("integration", ""))
            record(base, root, job, {"1": ["1"], "3": ["3"]}, {"merge": "conflicts", "1": True, "3": True})
            row = next(r for r in paper_census.collect(base, root) if r["features"] == "1 3")
            self.assertEqual(row["robust_label"], "")
            self.assertFalse(paper_census.done(base, root, next(
                j for j in paper_census.select(base, root) if j["features"] == [1, 4])))


if __name__ == "__main__":
    unittest.main()
