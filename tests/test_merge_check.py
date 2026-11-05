from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import merge_check

AGENT1 = "diff --git a/core.py b/core.py\n+++ b/core.py\n+def encode(text, max_tokens=None):\n+    check(max_tokens)\n"
AGENT4 = "diff --git a/core.py b/core.py\n+++ b/core.py\n+def encode(text, chunk_size=None):\n+    split(chunk_size)\n"


def row(set_name, passed, unreproduced=True, empty=False, side=True, same=None):
    return {"set": set_name, "model": "gpt5", "repo": "r", "task_id": hash(set_name + str(passed)) % 5, "tested": True,
            "error": None, "unreproduced": unreproduced, "merged_empty": empty, "side_present": side,
            "published_passed": passed, "same_error": same}


class MergeCheckTests(unittest.TestCase):
    def test_side_check_finds_a_dropped_agent(self):
        lines = set(merge_check.added(AGENT4))
        self.assertFalse(merge_check.present(AGENT1, lines))
        self.assertTrue(merge_check.present(AGENT4, lines))
        self.assertIsNone(merge_check.present("", lines))

    def test_first_error_reads_pytest_and_plain_errors(self):
        log = "collected 1 item\nE       TypeError: Encoding.encode() got an unexpected keyword argument 'max_tokens'\n"
        self.assertEqual(merge_check.first_error(log),
                         "TypeError: Encoding.encode() got an unexpected keyword argument 'max_tokens'")
        self.assertEqual(merge_check.first_error("ReferenceError: options is not defined\n"),
                         "ReferenceError: options is not defined")
        self.assertEqual(merge_check.first_error("8 passed in 0.4s\n"), "")

    def test_decision_follows_the_rule(self):
        controls = [row("pass_both_sides", True) for _ in range(10)]
        reproduced = [row("unreproduced", False) for _ in range(9)] + [row("unreproduced", True)]
        self.assertEqual(merge_check.summarize({}, controls + reproduced)["decision"],
                         "the paper's merged code explains the non-reproduction")
        flaky = [row("unreproduced", True) for _ in range(6)] + [row("unreproduced", False) for _ in range(4)]
        self.assertEqual(merge_check.summarize({}, controls + flaky)["decision"],
                         "environment or flaky tests: repeat the tests")
        bad = [row("pass_both_sides", False) for _ in range(3)] + controls[:7]
        self.assertTrue(merge_check.summarize({}, bad + reproduced)["decision"].startswith("control failed"))
        empty = [row("unreproduced", False, empty=True)] + reproduced
        self.assertEqual(merge_check.summarize({}, controls + empty)["primary_features"], 10)


if __name__ == "__main__":
    unittest.main()
