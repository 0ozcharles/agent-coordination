import unittest

from agent_coordination import merge, synthesis


def patch(*lines):
    return "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n" + "\n".join(lines) + "\n"


class SynthesisTest(unittest.TestCase):
    def test_consensus_picks_the_most_shared_patch(self):
        cands = [{"patch": patch("+x = 1", "+y = 2")}, {"patch": patch("+x = 1", "+y = 2", "+z = 3")},
                 {"patch": patch("+q = 9")}]
        self.assertEqual(synthesis.consensus(cands), 0)
        self.assertEqual(synthesis.changed_lines(cands[2]["patch"]), {"+q = 9"})

    def test_consensus_breaks_ties_by_order(self):
        cands = [{"patch": patch("+a")}, {"patch": patch("+a")}]
        self.assertEqual(synthesis.consensus(cands), 0)

    def test_parse_choice_takes_the_last_valid_line(self):
        self.assertEqual(synthesis.parse_choice("Candidate 2 is close.\nCHOICE: 3", 8), 3)
        self.assertEqual(synthesis.parse_choice("CHOICE: **5**", 8), 5)
        with self.assertRaises(ValueError):
            synthesis.parse_choice("I pick candidate 2", 8)
        with self.assertRaises(ValueError):
            synthesis.parse_choice("CHOICE: 9", 8)

    def test_aware_prompt_labels_sides_by_request_and_keeps_blind_render(self):
        text = "a\n<<<<<<< side_a\nmine\n||||||| base\nold\n=======\ntheirs\n>>>>>>> side_b\nz\n"
        parts = merge.blocks(text)
        instance = {"agentA_problem_statement": "Do A.", "agentB_problem_statement": "Do B."}
        conversation = [{"from": "agent1", "message": "I take f."}, {"from": "agent2", "message": "Fine."}]
        prompt = synthesis.aware_prompt("f.py", parts, instance, conversation, 6000)
        self.assertIn("<<<<<<< developer A\nmine\n", prompt)
        self.assertIn("=======\ntheirs\n>>>>>>> developer B", prompt)
        self.assertIn("Agent 1 (request A): I take f.", prompt)
        self.assertIn("Request B (developer B):\nDo B.", prompt)
        self.assertIn("<<<<<<< developer B\nmine\n", merge.prompt_for("f.py", parts))  # the blind prompt is unchanged

    def test_messages_are_cut_to_the_limit(self):
        text = synthesis.messages_text([{"from": "agent2", "message": "x" * 50}], 20)
        self.assertTrue(text.endswith("(later messages cut)"))
        self.assertEqual(synthesis.messages_text([], 20), "(none)")

    def test_file_prompt_shows_the_whole_file_and_parse_file_checks_markers(self):
        text = "head\n<<<<<<< side_a\nmine\n||||||| base\nold\n=======\ntheirs\n>>>>>>> side_b\ntail\n"
        parts = merge.blocks(text)
        instance = {"agentA_problem_statement": "Do A.", "agentB_problem_statement": "Do B."}
        blind = synthesis.file_prompt("f.py", parts, instance, [], 100, aware=False)
        aware = synthesis.file_prompt("f.py", parts, instance, [], 100, aware=True)
        self.assertIn("head\n<<<<<<< developer A\nmine", blind)
        self.assertIn(">>>>>>> developer B\ntail\n```", blind)
        self.assertNotIn("Do A.", blind)
        self.assertIn("Request A (developer A):\nDo A.", aware)
        self.assertEqual(synthesis.parse_file("Here:\n```python\nhead\nboth\ntail\n```"), "head\nboth\ntail\n")
        with self.assertRaises(ValueError):
            synthesis.parse_file("```\n<<<<<<< developer A\nx\n```")
        with self.assertRaises(ValueError):
            synthesis.parse_file("no code here")


if __name__ == "__main__":
    unittest.main()
