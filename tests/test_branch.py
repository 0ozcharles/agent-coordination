from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import branch


def step(command, output="ok", text="thinking"):
    """One assistant step with a single bash call and its tool result."""
    return [{"role": "assistant", "content": text, "tool_calls": [{"id": "c", "function": {"arguments": json.dumps(
                {"command": command})}}]},
            {"role": "tool", "content": json.dumps({"returncode": 0, "output": output}), "tool_call_id": "c"}]


def claim(to, text):
    return step(f"send_message {to} <<'MSG'\n{text}\nMSG", f"Message sent to {to}")


def write_run(tmp, agent1, agent2, conversation):
    d = Path(tmp) / "r1" / "coop" / "repo" / "7" / "f1_f2"
    d.mkdir(parents=True)
    (d / "agent1_traj.json").write_text(json.dumps({"messages": agent1}))
    (d / "agent2_traj.json").write_text(json.dumps({"messages": agent2}))
    (d / "conversation.json").write_text(json.dumps(conversation))
    return d


HEAD = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
CONVERSATION = [{"from": "agent1", "message": "I'm taking core.py"}, {"from": "agent2", "message": "I need core.py too"}]


class BranchTest(unittest.TestCase):
    def test_crossed_claims_are_cut_before_either_reacts(self):
        a1 = HEAD + claim("agent2", "I'm taking core.py") + [
            {"role": "user", "content": "[Message from agent2]: I need core.py too"}] + step("sed -n 1,5p core.py")
        a2 = HEAD + claim("agent1", "I need core.py too") + [
            {"role": "user", "content": "[Message from agent1 — agent1 is BLOCKED waiting]: I'm taking core.py"}]
        with tempfile.TemporaryDirectory() as tmp:
            state = branch.cut(write_run(tmp, a1, a2, CONVERSATION))
        self.assertEqual(state["conflict_message"], 2)
        self.assertEqual(state["cut"], {"agent1": 4, "agent2": 4})
        self.assertEqual(state["inbox"]["agent1"], [{"from": "agent2", "content": "I need core.py too"}])
        self.assertEqual(state["inbox"]["agent2"], [{"from": "agent1", "content": "I'm taking core.py"}])

    def test_a_write_before_the_cut_blocks_branching(self):
        a1 = HEAD + step("sed -i 's/a/b/' core.py") + claim("agent2", "I'm taking core.py") + [
            {"role": "user", "content": "[Message from agent2]: I need core.py too"}]
        a2 = HEAD + claim("agent1", "I need core.py too") + [
            {"role": "user", "content": "[Message from agent1]: I'm taking core.py"}]
        with tempfile.TemporaryDirectory() as tmp:
            state = branch.cut(write_run(tmp, a1, a2, CONVERSATION))
        self.assertEqual(state, {"reason": "file written before the cut"})

    def test_a_reply_sent_after_the_cut_is_not_restored(self):
        a1 = HEAD + claim("agent2", "I'm taking core.py") + [
            {"role": "user", "content": "[Message from agent2]: I need core.py too"}]
        a2 = HEAD + claim("agent1", "I need core.py too") + [
            {"role": "user", "content": "[Message from agent1]: I'm taking core.py"}] + claim("agent1", "ok, utils.py is yours")
        conversation = CONVERSATION + [{"from": "agent2", "message": "ok, utils.py is yours"}]
        with tempfile.TemporaryDirectory() as tmp:
            state = branch.cut(write_run(tmp, a1, a2, conversation))
        self.assertEqual(state["cut"]["agent2"], 4)
        self.assertEqual(len(state["inbox"]["agent1"]), 1)

    def test_prefix_restores_empty_tool_call_content_as_none(self):
        a1 = HEAD + step("ls", text="")
        with tempfile.TemporaryDirectory() as tmp:
            d = write_run(tmp, a1, HEAD, [])
            history = branch.prefix(d, {"features": {"agent1": "1"}, "cut": {"agent1": 4}}, "agent1")
        self.assertIsNone(history[2]["content"])
        self.assertEqual(history[3]["role"], "tool")

    def test_tool_end_skips_parallel_tool_results(self):
        msgs = [{"role": "assistant"}, {"role": "tool"}, {"role": "tool"}, {"role": "user"}]
        self.assertEqual(branch.tool_end(msgs, 0), 3)

    def test_instruction_uses_the_prefix_and_per_agent_overrides(self):
        config = {"instruction_prefix": "[Guidance] ", "arms": {
            "none": {"note": "a description, not an instruction"},
            "split": {"instruction": "Split it.", "instruction_agent2": "Take the rest."}}}
        self.assertIsNone(branch.instruction(config, "none", "agent1"))
        self.assertEqual(branch.instruction(config, "split", "agent1"), "[Guidance] Split it.")
        self.assertEqual(branch.instruction(config, "split", "agent2"), "[Guidance] Take the rest.")

    def test_sign_test_is_two_sided(self):
        self.assertEqual(branch.exact_p(0, 0), None)
        self.assertEqual(branch.exact_p(5, 5), 0.0625)
        self.assertEqual(branch.exact_p(2, 4), 1.0)


if __name__ == "__main__":
    unittest.main()
