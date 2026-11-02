from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import merge

CONFLICTED = """def f(a,
<<<<<<< HEAD
      b=None,
||||||| base
=======
      c=1,
>>>>>>> agent1
      ):
    return a
<<<<<<< HEAD
x = 2
||||||| base
x = 1
=======
x = 3
>>>>>>> agent1
"""


class MergeTests(unittest.TestCase):
    def test_blocks_union_and_splice(self):
        parts = merge.blocks(CONFLICTED)
        found = merge.conflicts(parts)
        self.assertEqual([c["ours"] for c in found], ["      b=None,\n", "x = 2\n"])
        self.assertEqual([c["base"] for c in found], ["", "x = 1\n"])
        self.assertEqual(merge.union(parts),
                         "def f(a,\n      b=None,\n      c=1,\n      ):\n    return a\nx = 2\nx = 3\n")
        self.assertEqual(merge.splice(parts, ["      b=None, c=1,", ""]),
                         "def f(a,\n      b=None, c=1,\n      ):\n    return a\n")
        with self.assertRaises(ValueError):
            merge.blocks("<<<<<<< HEAD\nx\n")

    def test_excerpt_keeps_context_around_conflicts(self):
        text = "".join(f"line{i}\n" for i in range(100)) + CONFLICTED
        shown = merge.excerpt(merge.blocks(text), context=3)
        self.assertTrue(shown.startswith("...\nline98\nline99\ndef f(a,\n"))
        self.assertIn("[CONFLICT 1]\n<<<<<<< developer B\n", shown)
        self.assertIn("[CONFLICT 2]", shown)
        self.assertNotIn("line50", shown)

    def test_parse_resolutions(self):
        reply = "=== RESOLUTION 1 ===\n```python\n    b=None, c=1,\n```\n=== RESOLUTION 2 ===\nx = 3\n\n"
        self.assertEqual(merge.parse_resolutions(reply, 2), ["    b=None, c=1,\n", "x = 3\n"])
        with self.assertRaises(ValueError):
            merge.parse_resolutions(reply, 3)
        # A trailing empty section (the model copying the template too far) is ignored.
        self.assertEqual(merge.parse_resolutions("=== RESOLUTION 1 ===\nx = 3\n=== RESOLUTION 2 ===\n", 1),
                         ["x = 3\n"])
        self.assertEqual(merge.parse_resolutions("=== RESOLUTION 1 ===\n```py\nx = 3\n```\nDone.\n", 1), ["x = 3\n"])
        prompt = merge.prompt_for("a.py", merge.blocks(CONFLICTED))
        self.assertIn("There are 2 conflicts", prompt)
        self.assertNotIn("RESOLUTION 3", prompt)

    def test_summarize_counts_recovered_and_regressed_features(self):
        rows = [
            {"job": {"row": {"features": "1 2", "feature_labels": "1:integration 2:integration",
                             "merged_passed": "", "label": "integration"}},
             "record": {"methods": {"union": {"status": "resolved", "1": True, "2": False}}}},
            {"job": {"row": {"features": "3 4", "feature_labels": "", "merged_passed": "3 4", "label": "passed"}},
             "record": {"methods": {"union": {"status": "resolved", "3": True, "4": False}}}},
            {"job": {"row": {"features": "5 6", "feature_labels": "5:integration 6:capability",
                             "merged_passed": "", "label": "integration"}},
             "record": {"methods": {"union": {"status": "unresolved"}}}},
        ]
        got = merge.summarize({"methods": {"union": ""}}, rows)["union"]
        self.assertEqual((got["integration_recovered"], got["integration_features"], got["runs_passed"],
                          got["runs_passed_officially"], got["regressions"], got["unresolved"]),
                         (1, 3, 0, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
