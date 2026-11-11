from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import conflict_states as cs


def send(agent, step, text, delivered=True):
    return {"agent": agent, "step": step, "kind": "send", "text": text, "delivered": delivered}


def recv(agent, step, text):
    return {"agent": agent, "step": step, "kind": "recv", "text": text}


LABEL = {"claims_incompatible": True,
         "types": {"resource": True, "responsibility": True, "interface": False, "requirement": False},
         "primary_type": "resource",
         "agent1": {"assertiveness": 2, "cooperativeness": 1, "evidence": "I'm taking cache.py"},
         "agent2": {"assertiveness": 0, "cooperativeness": 2, "evidence": "I'll avoid cache.py"},
         "agreement": "explicit", "shared_code_owner": "agent1", "interface_exact": True,
         "handoff": True, "unanswered_question": False, "notes": "x"}


class ConflictStateTests(unittest.TestCase):
    def test_files_in_keeps_basenames(self):
        self.assertEqual(cs.files_in("I'm taking dspy/clients/cache.py and `__init__.py`."), {"cache.py", "__init__.py"})

    def test_sends_in_reads_heredoc_unterminated_and_quoted_forms(self):
        self.assertEqual(cs.sends_in("send_message agent2 <<'MSG'\nI'm taking core.py\nMSG\necho done"), [("agent2", "I'm taking core.py")])
        self.assertEqual(cs.sends_in("send_message --wait agent1 <<'MSG'\nno terminator here"), [("agent1", "no terminator here")])
        self.assertEqual(cs.sends_in('send_message agent2 "I said \\"core.py\\""'), [("agent2", 'I said "core.py"')])
        self.assertEqual(cs.sends_in("send_message agent1 'plain text'"), [("agent1", "plain text")])

    def test_agent_conflict_needs_a_shared_claim(self):
        events = [send("agent1", 2, "I'm taking core.py"), recv("agent1", 5, "I'm editing utils.py"),
                  recv("agent1", 7, "Actually I also need core.py")]
        self.assertEqual(cs.agent_conflict(events), (7, {"core.py"}))
        self.assertEqual(cs.agent_conflict(events[:2]), (None, set()))

    def test_agent_conflict_accepts_a_reference_to_the_same_file(self):
        events = [send("agent1", 2, "I'm taking types.py"), recv("agent1", 6, "Yes, I need the same file.")]
        self.assertEqual(cs.agent_conflict(events)[0], 6)

    def test_first_conflict_message(self):
        sends = [send("agent1", 2, "I'm taking ext.py"), send("agent2", 2, "I'm taking loaders.py"),
                 send("agent2", 9, "I also need ext.py for the alias")]
        self.assertEqual(cs.first_conflict_message(sends), 3)
        self.assertIsNone(cs.first_conflict_message(sends[:2]))

    def test_order_puts_receives_after_sends_and_lost_messages_after_exit(self):
        a1 = [send("agent1", 2, "I'm taking core.py"), recv("agent1", 6, "core.py is mine too"),
              {"agent": "agent1", "step": 9, "kind": "exit"}]
        a2 = [recv("agent2", 1, "I'm taking core.py"), send("agent2", 3, "core.py is mine too"),
              send("agent2", 12, "please rename the keys", delivered=False)]
        ordered = cs.order({"agent1": a1, "agent2": a2})
        kinds = [(e["agent"], e["kind"], e.get("text", "")[:12]) for e in ordered]
        self.assertLess(kinds.index(("agent1", "send", "I'm taking c")), kinds.index(("agent2", "recv", "I'm taking c")))
        self.assertLess(kinds.index(("agent1", "exit", "")), kinds.index(("agent2", "send", "please renam")))

    def test_style_table(self):
        self.assertEqual(cs.style(2, 2), "collaborating")
        self.assertEqual(cs.style(2, 0), "competing")
        self.assertEqual(cs.style(2, 1), "compromising")   # a split of the shared file
        self.assertEqual(cs.style(0, 2), "accommodating")
        self.assertEqual(cs.style(0, 0), "avoiding")
        self.assertEqual(cs.style(1, 1), "compromising")
        self.assertEqual(set(cs.STYLE), {(a, c) for a in range(3) for c in range(3)})

    def test_parse_label_adds_styles_and_rejects_bad_scores(self):
        parsed = cs.parse_label("Here it is:\n" + json.dumps(LABEL))
        self.assertEqual(parsed["agent1"]["style"], "compromising")
        self.assertEqual(parsed["agent2"]["style"], "accommodating")
        bad = json.loads(json.dumps(LABEL))
        bad["agent1"]["assertiveness"] = 3
        with self.assertRaises(ValueError):
            cs.parse_label(json.dumps(bad))
        bad = json.loads(json.dumps(LABEL))
        bad["primary_type"] = "turf"
        with self.assertRaises(ValueError):
            cs.parse_label(json.dumps(bad))

    def test_kappa(self):
        self.assertEqual(cs.kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]), 1.0)
        self.assertAlmostEqual(cs.kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]), 0.0)

    def test_trajectory_reads_sends_replies_and_writes(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "send_message --wait agent2 <<'MSG'\nI'm taking core.py\nMSG"})}}]},
                {"role": "tool", "content": json.dumps({"returncode": 0, "output": "Message sent to agent2\n\n[Reply from agent2]: core.py is mine too"})},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "python - <<'PY'\nopen('core.py','w').write('x')\nPY"})}}]},
                {"role": "user", "content": "[Message from agent2]: also utils.py"},
                {"role": "exit", "content": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"messages": msgs}))
            events, n = cs.trajectory(path, "agent1")
        self.assertEqual(n, 6)
        self.assertEqual([e["kind"] for e in events], ["send", "recv", "write", "recv", "exit"])
        self.assertTrue(events[1].get("inline"))


if __name__ == "__main__":
    unittest.main()
