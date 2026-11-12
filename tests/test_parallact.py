import json
from pathlib import Path
import tempfile
import unittest

from agent_coordination import parallact


def instance(iid, repo="NodeBB/NodeBB", family="contradictory"):
    return {"instance_id": iid, "repo": repo, "family": family, "base_commit": "abc",
            "runtime": {"image_uri": f"img:{iid}"},
            "agentA_problem_statement": "Request A\n", "agentB_problem_statement": "Request B",
            "agentA_fail_to_pass": ["a1", "a2"], "agentB_fail_to_pass": ["b1"],
            "agentA_test_patch": "ta", "agentB_test_patch": "tb", "combined_test_patch": "tc"}


class ParallactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        data = self.root / "src" / "dataset"
        layout = {"v1/seeds/contradictory": ["s1", "s2"], "v1/seeds/complementary": ["c1"],
                  "v2/generated/contradictory": ["g1", "g2", "g3"]}
        for folder, ids in layout.items():
            (data / folder).mkdir(parents=True)
            for iid in ids:
                family = folder.rsplit("/", 1)[-1]
                (data / folder / f"instance_{iid}.json").write_text(json.dumps(instance(iid, family=family)))
        self.config = {"source": {"path": "src"}, "family": "contradictory", "repeats": 2,
                       "sample": {"corpora": {"v1/seeds": "all", "v2/generated": 2}, "seed": 1}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_name(self):
        self.assertEqual(parallact.repo_name({"repo": "element-hq/element-web"}), "parallact_element_web")

    def test_select_takes_family_and_sample(self):
        chosen = [i["instance_id"] for i in parallact.select(self.config, self.root)]
        self.assertEqual(chosen[:2], ["s1", "s2"])
        self.assertEqual(len(chosen), 4)
        self.assertTrue(set(chosen[2:]) <= {"g1", "g2", "g3"})
        self.assertEqual(chosen, [i["instance_id"] for i in parallact.select(self.config, self.root)])

    def test_plan_task_ids_are_stable_across_families(self):
        jobs = parallact.plan(self.config, self.root)
        self.assertEqual(len(jobs), 8)
        ids = {j["instance"]: j["task_id"] for j in jobs}
        # positions among all six instance ids, sorted: c1 g1 g2 g3 s1 s2
        self.assertEqual(ids["s1"], 4)
        self.assertEqual({j["repeat"] for j in jobs}, {1, 2})

    def test_write_dataset_maps_requests_to_features(self):
        job = {"repo": "parallact_nodebb", "task_id": 7}
        dataset = parallact.write_dataset(self.root / "out", instance("x"), job)
        task = dataset / "parallact_nodebb" / "task7"
        self.assertEqual((task / "feature1" / "feature.md").read_text(), "Request A\n")
        self.assertEqual((task / "feature2" / "feature.md").read_text(), "Request B\n")

    def test_side_passed_needs_every_test(self):
        result = {"fail_to_pass": {"a1": "PASSED", "a2": "FAILED", "b1": "PASSED"}}
        self.assertFalse(parallact.side_passed(result, ["a1", "a2"]))
        self.assertTrue(parallact.side_passed(result, ["b1"]))
        self.assertFalse(parallact.side_passed(result, []))

    def test_gold_checks(self):
        inst = instance("x")
        ok = {"fail_to_pass": {"a1": "PASSED", "a2": "PASSED", "b1": "PASSED"}, "n_pass": 3}
        record = {"base_combined": {"fail_to_pass": {}, "n_pass": 0}, "A_alone": ok, "B_alone": ok, "reconciled": ok}
        self.assertTrue(all(parallact.gold_checks(record, inst).values()))
        record["naive_combined"] = ok
        self.assertFalse(parallact.gold_checks(record, inst)["naive_fails"])

    def test_combined_tests_hold_both_requests(self):
        sets = parallact.test_sets(instance("x"))
        self.assertEqual(sets["combined"]["fail_to_pass"], ["a1", "a2", "b1"])
        self.assertEqual(sets["combined"]["patches"], ["tc"])

    def test_solo_jobs_and_errors(self):
        config = {**self.config, "setting": "solo", "repeats": 1}
        job = parallact.plan(config, self.root)[0]
        self.assertEqual(job["setting"], "solo")
        self.assertEqual(parallact.job_dir(self.root, job).parts[-4], "solo")
        target = self.root / "job"
        target.mkdir()
        (target / "result.json").write_text(json.dumps({"agent": {"status": "Error"}}))
        self.assertEqual(parallact.agent_errors(target), ["solo"])
        (target / "result.json").write_text(json.dumps({"agents": {"agent1": {"status": "Submitted"}}}))
        self.assertEqual(parallact.agent_errors(target), [])


if __name__ == "__main__":
    unittest.main()
