import json
from pathlib import Path
import tempfile
import unittest

from agent_coordination import negotiation


def config(**over):
    base = {"study": "t", "question": "q", "model": "m", "control": "neutral", "labeler": {}, "output": "runs/t",
            "instances": {"n": 6, "seed": 3, "cost": [30, 60], "surplus": [10, 20], "focal_role": "alternate"},
            "instruction_prefix": "[G] ",
            "conditions": {"neutral": {"instruction": "Do as you like."},
                           "competing": {"target": "competing", "instruction": "Hold out."},
                           "default": {"instruction": None}}}
    base.update(over)
    return base


def turn(player, answer, price=None, message="hi"):
    return {"player": player, "answer": answer, "price": price, "message": message, "reasoning": ""}


class NegotiationTest(unittest.TestCase):
    def test_instances_are_deterministic_and_alternate_roles(self):
        a, b = negotiation.instances(config()), negotiation.instances(config())
        self.assertEqual(a, b)
        self.assertEqual([i["focal_role"] for i in a], ["seller", "buyer"] * 3)
        for inst in a:
            self.assertEqual(inst["limit"], inst["cost"] + inst["surplus"])
            self.assertTrue(30 <= inst["cost"] <= 60)

    def test_instruction_has_prefix_or_is_absent(self):
        self.assertEqual(negotiation.instruction(config(), "competing"), "[G] Hold out.")
        self.assertIsNone(negotiation.instruction(config(), "default"))

    def test_outcome_and_claims(self):
        inst = {"cost": 40, "limit": 60, "surplus": 20}
        turns = [turn("seller", "PROPOSAL", 70), turn("buyer", "PROPOSAL", 45), turn("seller", "PROPOSAL", 55),
                 turn("buyer", "ACCEPT")]
        out = negotiation.outcome(turns, inst)
        self.assertEqual((out["deal"], out["price"], out["accepted_by"], out["rejected_by"]), (True, 55, "buyer", None))
        self.assertAlmostEqual(out["shares"]["seller"], 0.75)
        self.assertAlmostEqual(out["shares"]["buyer"], 0.25)
        focal = negotiation.focal_metrics(turns, inst, "seller")
        self.assertEqual(focal["proposals"], [70, 55])
        self.assertAlmostEqual(focal["first_claim"], 1.5)
        self.assertAlmostEqual(focal["concession"], 0.75)
        self.assertEqual(focal["messages"], 2)
        rejected = negotiation.outcome([turn("seller", "PROPOSAL", 70), turn("buyer", "REJECT")], inst)
        self.assertEqual((rejected["deal"], rejected["price"], rejected["shares"]["seller"], rejected["rejected_by"]),
                         (False, None, 0.0, "buyer"))
        self.assertAlmostEqual(negotiation.claim("buyer", 45, inst), 0.75)

    def test_transcript_and_label_parsing(self):
        record = {"turns": [turn("seller", "PROPOSAL", 70, "Take it."), turn("buyer", "ACCEPT", None, "")]}
        text = negotiation.transcript(record)
        self.assertIn("#1 Agent 1 (seller, Player RED) proposes 70 ZUP: Take it.", text)
        self.assertIn("#2 Agent 2 (buyer, Player BLUE) ACCEPTS the last proposal: (no message)", text)
        reply = ('ok {"agent1": {"assertiveness": 2, "cooperativeness": 0, "asks_needs": false}, '
                 '"agent2": {"assertiveness": 0, "cooperativeness": 1, "asks_needs": false}, "first_concession": "none"}')
        label = negotiation.parse_style_label(reply)
        self.assertEqual((label["agent1"]["style"], label["agent2"]["style"]), ("competing", "accommodating"))
        with self.assertRaises(ValueError):
            negotiation.parse_style_label(reply.replace('"none"', '"both"'))
        with self.assertRaises(ValueError):
            negotiation.parse_style_label('{"agent1": {"assertiveness": 3, "cooperativeness": 0, "asks_needs": true}}')

    def test_sign_test(self):
        self.assertEqual(negotiation.sign_test([(1, 0)] * 5), {"higher": 5, "lower": 0, "p": 0.0625})
        self.assertEqual(negotiation.sign_test([(1, 1), (2, 2)]), {"higher": 0, "lower": 0, "p": 1.0})

    def test_rows_summary_and_curves_from_records(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            insts = negotiation.instances(cfg)
            for condition, share, style in (("neutral", 0.5, "compromising"), ("competing", 0.8, "competing")):
                for inst in insts[:2]:
                    role = inst["focal_role"]
                    price = inst["cost"] + inst["surplus"] * (share if role == "seller" else 1 - share)
                    turns = [turn("seller", "PROPOSAL", inst["limit"] + 5), turn("buyer", "PROPOSAL", inst["cost"]),
                             turn("seller", "PROPOSAL", price), turn("buyer", "ACCEPT")]
                    me = "agent1" if role == "seller" else "agent2"
                    label = {"agent1": {"assertiveness": 1, "cooperativeness": 1, "asks_needs": False, "style": "compromising"},
                             "agent2": {"assertiveness": 1, "cooperativeness": 1, "asks_needs": False, "style": "compromising"},
                             "first_concession": "agent2"}
                    label[me]["style"] = style
                    record = {"condition": condition, "instance": inst, "focal_role": role, "turns": turns,
                              **negotiation.outcome(turns, inst), "focal": negotiation.focal_metrics(turns, inst, role),
                              "cost_usd": 0.01, "error": None, "label": {"label": label, "cost_usd": 0.0}}
                    path = negotiation.game_path(cfg, root, condition, inst)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(record))
            table = negotiation.rows(cfg, root)
            self.assertEqual(len(table), 4)
            summary = negotiation.summarize(cfg, table)
            self.assertEqual(summary["competing"]["in_target_style"], 2)
            self.assertEqual(summary["competing"]["vs_control"]["focal_share"], {"higher": 2, "lower": 0, "p": 0.5})
            self.assertEqual(summary["competing"]["vs_control"]["in_target_style"], {"arm": 2, "control": 0})
            self.assertNotIn("vs_control", summary["neutral"])
            curve = negotiation.curves(cfg, table)
            self.assertEqual(curve["neutral"][1]["n"], 2)
            svg = negotiation.chart(cfg, summary, curve)
            self.assertTrue(svg.startswith("<svg") and "competing" in svg)


if __name__ == "__main__":
    unittest.main()
