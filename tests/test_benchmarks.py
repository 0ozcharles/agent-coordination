import json
import tempfile
import unittest
from pathlib import Path

from agent_coordination import bargain, benchmarks

ENV = {"moves": 10, "visible_weights": False, "protocol_note": "There are {T} moves in total."}


def config_for(path):
    return {"study": "t", "question": "q", "model": "m", "environment": ENV, "instances": {"path": str(path), "first_mover": "alternate"},
            "strategies": {"shared": {"neutral": "Handle this in whatever way you think best."}, "prompted": {}, "rules": {}},
            "cells": [], "control": "neutral-neutral", "output": "runs/t"}


class CasinoTest(unittest.TestCase):
    def setUp(self):
        self.specs = benchmarks.casino_instances()

    def test_full_factorial_of_priority_orders(self):
        self.assertEqual(len(self.specs), 36)
        self.assertEqual(len({s["source"] for s in self.specs}), 36)

    def test_points_follow_the_benchmark(self):
        s = next(x for x in self.specs if x["priorities"] == {"p1": ["Food", "Water", "Firewood"], "p2": ["Firewood", "Water", "Food"]})
        # Camper 1: food 5 a package, water 4, firewood 3; options run from 3 packages to Camper 1 down to 0
        self.assertEqual(s["weights"]["p1"], [[15, 10, 5, 0], [12, 8, 4, 0], [9, 6, 3, 0]])
        # Camper 2 gets the rest: firewood 5, water 4, food 3
        self.assertEqual(s["weights"]["p2"], [[0, 3, 6, 9], [0, 4, 8, 12], [0, 5, 10, 15]])
        self.assertEqual((s["outside"], s["max"]), ([5, 5], {"p1": 36, "p2": 36}))

    def test_instances_build_with_four_options_and_an_exact_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "casino.json"; path.write_text(json.dumps(self.specs))
            insts = bargain.instances(config_for(path))
        self.assertEqual(len(insts), 36)
        inst = insts[0]
        self.assertEqual((inst["k"], inst["options"], inst["m"]), (3, [4, 4, 4], 4))
        self.assertEqual(inst["opposed"], [True, True, True])
        self.assertEqual((inst["outside"]["p1"], inst["max"]["p1"]), (5.0, 36.0))
        self.assertEqual(bargain.thresholds(inst, "p1")["t60"], 21.6)
        reversed_ = next(i for i in insts if i["priorities"]["p1"] == ["Food", "Water", "Firewood"] and i["priorities"]["p2"] == ["Firewood", "Water", "Food"])
        self.assertEqual(reversed_["frontier"]["max_joint"], 15 + 12 + 15)  # each side takes all of its top item, water split 3-0 either way
        identical = next(i for i in insts if i["priorities"]["p1"] == i["priorities"]["p2"])
        self.assertEqual(identical["frontier"]["integrative_gain"], 0.0)
        self.assertIsNone(bargain.middle_share(inst, [1, 1, 1]))  # defined for three options only
        self.assertEqual(bargain.option_labels(inst, 0)[0], "A (3 to Camper 1, 0 to Camper 2)")
        self.assertEqual(bargain.option_labels(inst, 0)[3], "D (0 to Camper 1, 3 to Camper 2)")

    def test_prompt_and_parsing_with_four_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "casino.json"; path.write_text(json.dumps(self.specs))
            cfg = config_for(path); inst = bargain.instances(cfg)[1]
        text = bargain.system_prompt(cfg, inst, "p2", "neutral", "rules")
        self.assertIn("You are Camper 2, one of two campsite neighbours", text)
        self.assertIn("a split is worth the sum of your scores", text)
        self.assertIn("the other camper", text)
        move = bargain.parse_reply('{"message": "ok", "action": "propose", "proposal": {"Food": "D", "Water": "B (2 to Camper 1, 1 to Camper 2)", "Firewood": "a"}}', inst)
        self.assertEqual(move["design"], [3, 1, 0])
        with self.assertRaises(ValueError):
            bargain.parse_reply('{"message": "", "action": "propose", "proposal": {"Food": "E", "Water": "A", "Firewood": "A"}}', inst)


class DeliberationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.available = benchmarks.DELIBERATION_ROOT.is_dir()
        cls.specs = benchmarks.deliberation_instances() if cls.available else []

    def test_pairs_with_a_contested_decision(self):
        if not self.available:
            self.skipTest("external/LLM-Deliberation is not checked out")
        self.assertEqual(len(self.specs), 57)
        base = [s for s in self.specs if s["benchmark"] == "llm_deliberation/base"]
        self.assertEqual(len(base), 14)  # the environmental league and other cities share no contested decision
        s = next(x for x in self.specs if x["source"].endswith("SportCo_vs_enviroment"))
        self.assertEqual([len(w) for w in s["weights"]["p1"]], [3, 3, 4, 4, 5])
        self.assertEqual((s["max"], s["thresholds"], s["outside"]), ({"p1": 100, "p2": 100}, {"p1": 55, "p2": 55}, [30.0, 30.0]))
        self.assertEqual(s["skin"]["issues"][0]["options"], ["water-based", "water/land-based", "land-based"])
        self.assertEqual(s["skin"]["issues"][2]["options"][1], "Union quota of 2:1")

    def test_instances_and_prompt(self):
        if not self.available:
            self.skipTest("external/LLM-Deliberation is not checked out")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delib.json"; path.write_text(json.dumps(self.specs))
            cfg = config_for(path); insts = bargain.instances(cfg)
        inst = next(i for i in insts if i["seed"].endswith("SportCo_vs_enviroment"))
        self.assertEqual(inst["options"], [3, 3, 4, 4, 5])
        # the league is indifferent on C, D and E, so only A and B are contested
        self.assertEqual(inst["opposed"], [True, True, False, False, False])
        self.assertEqual(len(inst["frontier"]["pareto"]) > 1, True)
        text = bargain.system_prompt(cfg, inst, "p1", "neutral", "rules")
        self.assertIn('You are SportCo, one of two parties', text)
        self.assertIn('You are the "SportCo"; the other party is the "Environmental League"', text)
        self.assertIn("Compensation to other cities: A (", text)
        move = bargain.parse_reply('{"message": "", "action": "propose", "proposal": {"Infrastructure Mix": "A", "Ecological Impact": "C", "Employment Rules": "D", "Federal Loan": "A", "Compensation to other cities": "E"}}', inst)
        self.assertEqual(move["design"], [0, 2, 3, 0, 4])
        # a long option name shortened by the model still parses by its letter
        move = bargain.parse_reply('{"message": "", "action": "propose", "proposal": {"Infrastructure Mix": "A", "Ecological Impact": "C", "Employment Rules": "D (No Union pref.)", "Federal Loan": "a ($3 billion)", "Compensation to other cities": "E (SportCo pays nothing)"}}', inst)
        self.assertEqual(move["design"], [0, 2, 3, 0, 4])
        self.assertEqual(bargain.value(inst, "p1", move["design"]), 14 + 0 + 17 + 35 + 23)


class DealOrNoDealTest(unittest.TestCase):
    """Scenarios of the benchmark: (counts of books, hats, balls; values of side 1; values of side 2), each pool worth 10 to each side."""
    SCENARIOS = [((1, 2, 3), (4, 3, 0), (1, 0, 3)), ((2, 2, 1), (1, 3, 2), (3, 1, 2)), ((1, 1, 4), (2, 4, 1), (2, 0, 2)), ((1, 2, 3), (4, 3, 0), (1, 0, 3))]

    def test_reading_the_scenario_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selfplay.txt"
            path.write_text("1 4 2 3 3 0\n1 1 2 0 3 3\n2 1 2 3 1 2\n2 3 2 1 1 2\n")
            self.assertEqual(benchmarks.dond_scenarios(path), self.SCENARIOS[:2])
            path.write_text("1 4 2 3 3 0\n2 1 2 0 3 3\n")
            with self.assertRaises(ValueError):
                benchmarks.dond_scenarios(path)

    def test_a_scenario_as_an_instance(self):
        specs = benchmarks.dond_instances(self.SCENARIOS, n=3, seed=1)
        self.assertEqual(len(specs), 3)  # the repeated scenario counts once
        self.assertEqual(specs, benchmarks.dond_instances(self.SCENARIOS, n=3, seed=1))
        spec = benchmarks.dond_instance(*self.SCENARIOS[0])
        self.assertEqual(spec["weights"], {"p1": [[4, 0], [6, 3, 0], [0, 0, 0, 0]], "p2": [[0, 1], [0, 0, 0], [0, 3, 6, 9]]})
        self.assertEqual((spec["outside"], spec["max"], spec["source"]), ([0, 0], {"p1": 10, "p2": 10}, "dond/1-2-3_4-3-0_1-0-3"))
        self.assertIn("CC BY-NC 4.0", spec["licence"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dond.json"; path.write_text(json.dumps([spec, benchmarks.dond_instance(*self.SCENARIOS[1])]))
            cfg = config_for(path)
            insts = bargain.instances(cfg)
        inst = insts[0]
        self.assertEqual((inst["k"], inst["options"]), (3, [2, 3, 4]))
        self.assertEqual(inst["opposed"], [True, False, False])  # only the book is wanted by both; hats and balls have one taker each
        self.assertEqual(inst["frontier"]["max_joint"], 4 + 6 + 9)
        self.assertEqual(insts[1]["opposed"], [True, True, True])
        t = bargain.thresholds(inst, "p1")
        self.assertEqual((t["max"], t["t60"], t["t50"], t["outside"], t["outside10"]), (10.0, 6.0, 5.0, 0.0, 1.0))
        self.assertEqual(bargain.option_labels(inst, 1), ["A (2 to Person 1, 0 to Person 2)", "B (1 to Person 1, 1 to Person 2)", "C (0 to Person 1, 2 to Person 2)"])
        prompt = bargain.system_prompt(cfg, inst, "p1", "neutral", "rules")
        self.assertIn("You are Person 1, one of two people who must agree on how to split a pool of items with 3 decisions.", prompt)
        self.assertIn("The pool holds 1 book, 2 hats, 3 balls.", prompt)
        self.assertIn("A decision with two options has no middle option.", prompt)
        self.assertIn("yours is 0 points", prompt)
        self.assertIn("Books: A (1 to Person 1, 0 to Person 2) = 4, B (0 to Person 1, 1 to Person 2) = 0", prompt)
        move = bargain.parse_reply(json.dumps({"message": "", "action": "propose", "proposal": {"Books": "A", "Hats": "A (2 to Person 1, 0 to Person 2)", "Balls": "D"}}), inst)
        self.assertEqual(move["design"], [0, 0, 3])
        self.assertEqual((bargain.value(inst, "p1", move["design"]), bargain.value(inst, "p2", move["design"])), (10.0, 9.0))
        self.assertIsNone(bargain.middle_share(inst, move["design"]))


if __name__ == "__main__":
    unittest.main()
