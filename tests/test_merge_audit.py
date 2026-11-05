from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import merge_audit

AGENT1 = "+++ b/core.py\n+def encode(text, max_tokens=None):\n+    check(max_tokens)\n"
AGENT2 = "+++ b/core.py\n+def encode(text, chunk_size=None):\n+    split(chunk_size)\n"


class MergeAuditTests(unittest.TestCase):
    def test_classify_names_the_agents_in_the_tested_patch(self):
        self.assertEqual(merge_audit.classify(AGENT1, AGENT2, AGENT2), "agent 2 only")
        self.assertEqual(merge_audit.classify(AGENT1, AGENT2, AGENT1 + AGENT2), "both")
        self.assertEqual(merge_audit.classify(AGENT1, AGENT2, AGENT1), "agent 1 only")
        self.assertEqual(merge_audit.classify(AGENT1, AGENT2, "+++ b/x.py\n+pass\n"), "neither")
        self.assertEqual(merge_audit.classify(AGENT1, AGENT2, ""), "no published patch")
        self.assertEqual(merge_audit.classify("", AGENT2, AGENT2), "agent 1 empty")


if __name__ == "__main__":
    unittest.main()
