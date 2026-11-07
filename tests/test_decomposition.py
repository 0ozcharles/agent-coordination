from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import decomposition


def row(task, solo_rec, coop_rec, solo, l1, l2, l3, remaining=""):
    return {"model": "gpt5", "repo": "r", "task_id": task, "features": "1 2", "solo_recorded": solo_rec,
            "coop_recorded": coop_rec, "merge": "clean", "solo": solo, "L1": l1, "L2": l2, "L3": l3,
            "remaining": remaining, "complete": True}


class DecompositionTests(unittest.TestCase):
    def test_components_sum_to_the_paper_gap(self):
        cases = [row(1, 1, 0, 1, 0, 0, 1),                         # resolved by the llm re-merge
                 row(2, 1, 0, 1, 1, 1, 1),                         # false failure: our clean merge passes
                 row(3, 1, 1, 1, 0, 0, 0, "integration"),          # false pass, conflict nothing resolves
                 row(4, 1, 0, 1, 0, 0, 0, "division_of_labor"),
                 row(5, 0, 0, 0, 0, 1, 1)]                         # coop beats solo after union
        for r in cases:
            c = decomposition.contributions(r)
            parts = sum(c[k] for k in decomposition.COMPONENTS)
            self.assertEqual(parts, c["G0"], r)

    def test_summary_shares_and_verdict(self):
        rows = [row(t, 1, 0, 1, 0, 0, 0, "division_of_labor") for t in range(3)] + [row(9, 1, 0, 1, 0, 1, 1)]
        config = {"models": {"gpt5": "GPT-5"}}
        s = decomposition.summarize(config, rows)["models"]["gpt5"]
        self.assertEqual(s["totals"]["G0"], 4)
        self.assertEqual(s["shares"]["division_of_labor"]["mean"], 0.75)
        self.assertEqual(s["shares"]["resolvable"]["mean"], 0.25)
        self.assertEqual(s["verdict"], "division of labor")


if __name__ == "__main__":
    unittest.main()
