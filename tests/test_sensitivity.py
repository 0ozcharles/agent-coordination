from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import sensitivity


def pair(task, solo, l0, l1, l2, l3, solo_ours=None, ours=None):
    row = {"model": "gpt5", "repo": "r", "task_id": task, "solo": solo, "L0": l0, "L1": l1, "L2": l2, "L3": l3,
           "in_sample": solo_ours is not None, "union_tested": False, "llm_tested": False}
    if solo_ours is not None:
        row.update(solo_ours=solo_ours, ours_L1=ours[0], ours_L2=ours[1], ours_L3=ours[2])
    return row


class SensitivityTests(unittest.TestCase):
    def test_closure_is_the_share_of_the_paper_gap_removed(self):
        rows = [pair(1, 1, 0, 0, 1, 1), pair(2, 1, 0, 0, 0, 1), pair(3, 1, 1, 1, 1, 1), pair(4, 0, 0, 0, 0, 0)]
        self.assertEqual(sensitivity.closure(rows, "L1")["mean"], 0)
        self.assertEqual(sensitivity.closure(rows, "L2")["mean"], 0.5)
        self.assertEqual(sensitivity.closure(rows, "L3")["mean"], 1)

    def test_same_harness_closure_counts_a_lower_solo(self):
        # Paper gap 2 (solo 2, coop 0). In our harness solo passes once and coop once: remaining gap 0.
        rows = [pair(1, 1, 0, 0, 0, 0, solo_ours=1, ours=(1, 1, 1)), pair(2, 1, 0, 0, 0, 0, solo_ours=0, ours=(0, 0, 0))]
        self.assertEqual(sensitivity.closure(rows, "ours_L1", "solo_ours")["mean"], 1)

    def test_decision_uses_l2_and_flags_a_divergent_same_harness_figure(self):
        rows = [pair(t, 1, 0, 0, int(t < 5), 1, solo_ours=1, ours=(0, 0, 1)) for t in range(10)]
        summary = sensitivity.summarize({}, rows)
        self.assertEqual(summary["models"]["gpt5"]["closed"]["L2"]["mean"], 0.5)
        self.assertTrue(summary["decision"]["gpt5"].startswith("holds"))
        self.assertIn("same-harness", summary["decision"]["gpt5"])


if __name__ == "__main__":
    unittest.main()
