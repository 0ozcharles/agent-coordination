import copy
import json
import unittest
from pathlib import Path

from agent_coordination import bargain, scripted

ROOT = Path(__file__).resolve().parents[1]
STYLES = bargain.STYLES


def config(n=12):
    c = json.loads((ROOT / "configs" / "bargain_e1r.json").read_text())
    c["instances"] = {"n": n, "seed": 20261121, "first_mover": "alternate"}  # development instances
    return c


def mv(by, action, design=None, message="", n=1):
    return {"n": n, "by": by, "action": action, "design": design, "message": message}


class ScriptedPoliciesTest(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.insts = bargain.instances(self.config)
        self.inst = next(i for i in self.insts if sum(i["opposed"]) == 3)
        self.T = self.config["environment"]["moves"]

    def game(self, a, b, inst=None):
        return bargain.play(self.config, inst or self.inst, {"name": f"{a}-{b}", "p1": a, "p2": b})

    def test_names(self):
        self.assertEqual(scripted.rule_of("script_competing"), ("competing", None))
        self.assertEqual(scripted.rule_of("script_avoiding_quiet"), ("avoiding", "quiet"))
        self.assertTrue(scripted.is_script("script_avoiding") and not scripted.is_script("avoiding"))
        for bad in ("script_neutral", "script_competing_quiet"):
            with self.assertRaises(ValueError):
                scripted.rule_of(bad)

    def test_every_pair_plays_without_a_model_and_follows_its_rule(self):
        names = [f"script_{s}" for s in STYLES] + [f"script_{s}_{v}" for s, vs in scripted.VARIANTS.items() for v in vs]
        for inst in self.insts:
            for a in names:
                for b in names:
                    r = self.game(a, b, inst)
                    self.assertEqual(r["cost_usd"], 0.0)
                    self.assertEqual(r["models"], {"p1": "script", "p2": "script"})
                    self.assertEqual(r["invalid_replies"], 0)
                    for seat, name in (("p1", a), ("p2", b)):
                        failed = {k for k, ok in r["compliance"][seat].items() if ok is False} - {"overall"}
                        # the checker's trade clause uses the other side's true top options, which a script learns only
                        # from their proposals: it is the one clause a script can fail
                        self.assertLessEqual(failed, {"proposes_trade"} if scripted.rule_of(name)[0] == "collaborating" else set(),
                                             (a, b, inst["id"], seat, r["compliance"][seat]))

    def test_a_script_reads_nothing_of_the_other_side(self):
        """The same history gives the same move whatever the other side's scores are."""
        for inst in self.insts[:6]:
            for a in STYLES:
                for b in STYLES:
                    r = self.game(f"script_{a}", f"script_{b}", inst)
                    blind = copy.deepcopy(inst)
                    for seat in ("p1", "p2"):
                        other = "p2" if seat == "p1" else "p1"
                        hidden = copy.deepcopy(blind)
                        hidden["weights"][other] = [[0.0] * len(w) for w in inst["weights"][other]]
                        hidden["opposed"] = [not x for x in inst["opposed"]]
                        hidden["outside"][other], hidden["frontier"], hidden["importance"] = 0, None, None
                        for k, m in enumerate(r["moves"]):
                            if m["by"] == seat:
                                again = scripted.move(r["strategies"][seat], hidden, seat, r["moves"][:k], m["n"], self.T)
                                self.assertEqual((again["action"], again["design"], again["message"]), (m["action"], m["design"], m["message"]))

    def test_competing(self):
        i, T = self.inst, self.T
        v = scripted.View(i, "p1", [], 1, T)
        self.assertEqual(scripted.move("script_competing", i, "p1", [], 1, T)["design"], v.top)
        theirs = bargain.tops(i, "p2")
        moves = [mv("p1", "propose", v.top), mv("p2", "propose", theirs, n=2)]
        step = scripted.move("script_competing", i, "p1", moves, 3, T)
        changed = [k for k in range(i["k"]) if step["design"][k] != v.top[k]]
        self.assertEqual(len(changed), 1)  # one decision, to its middle option, where the other side differs
        self.assertEqual(step["design"][changed[0]], v.mid[changed[0]])
        self.assertTrue(i["opposed"][changed[0]])
        self.assertGreaterEqual(bargain.value(i, "p1", step["design"]), 60)
        self.assertEqual(scripted.move("script_competing_firm", i, "p1", moves, 3, T)["design"], v.top)
        self.assertEqual(scripted.move("script_competing", i, "p1", moves, T - 1, T)["action"], "walk")  # its last move
        good = [mv("p2", "propose", v.top)]
        self.assertEqual(scripted.move("script_competing", i, "p1", good, 2, T)["action"], "accept")

    def test_compromising(self):
        i, T = self.inst, self.T
        v = scripted.View(i, "p1", [], 1, T)
        moves = [mv("p2", "propose", bargain.tops(i, "p2"))]
        two = scripted.move("script_compromising", i, "p1", moves, 2, T)["design"]
        one = scripted.move("script_compromising_one", i, "p1", moves, 2, T)["design"]
        self.assertEqual(sum(a != b for a, b in zip(two, v.top)), 2)
        self.assertEqual(sum(a != b for a, b in zip(one, v.top)), 1)
        self.assertTrue(all(two[k] in (v.top[k], v.mid[k]) for k in range(i["k"])))
        half = [v.mid[k] if i["opposed"][k] else v.top[k] for k in range(i["k"])]
        if bargain.value(i, "p1", half) >= 50:
            self.assertEqual(scripted.move("script_compromising", i, "p1", [mv("p2", "propose", half)], 2, T)["action"], "accept")

    def test_collaborating(self):
        i, T = self.inst, self.T
        v = scripted.View(i, "p1", [], 1, T)
        first = scripted.move("script_collaborating", i, "p1", [], 1, T)
        self.assertTrue(bargain.ASKS.search(first["message"]))
        self.assertEqual(first["design"], v.top)
        contested = [k for k in range(i["k"]) if i["opposed"][k]]
        said = contested[-1]
        names = bargain.issue_names(i)
        moves = [mv("p2", "propose", bargain.tops(i, "p2"), f"I propose this. {names[said]} matters most to me.")]
        offer = scripted.move("script_collaborating", i, "p1", moves, 2, T)["design"]
        self.assertEqual(offer[said], bargain.tops(i, "p2")[said])  # the other side gets the decision it named
        self.assertTrue(bargain.is_trade(i, offer))
        self.assertEqual(sum(offer[k] == v.mid[k] for k in contested), len(contested) - 2)
        keep = scripted.move("script_collaborating_keep", i, "p1", moves, 2, T)["design"]
        self.assertEqual(sum(keep[k] == v.top[k] for k in contested), len(contested) - 1)
        silent = [mv("p2", "propose", bargain.tops(i, "p2"))]
        cheap = min(contested, key=lambda k: (v.imp[k], k))
        self.assertEqual(scripted.move("script_collaborating", i, "p1", silent, 2, T)["design"][cheap], bargain.tops(i, "p2")[cheap])

    def test_what_the_other_side_says_matters(self):
        i = self.inst
        names = bargain.issue_names(i)
        v = scripted.View(i, "p1", [mv("p2", "propose", bargain.tops(i, "p2"), f"I propose {names[0]}: Option A. What matters most to me is {names[2]}, then {names[1]}.")], 2, self.T)
        self.assertEqual(scripted.stated(v, {0, 1, 2, 3}), 2)
        self.assertEqual(scripted.stated(v, {0, 1}), 1)
        self.assertIsNone(scripted.stated(scripted.View(i, "p1", [mv("p2", "propose", bargain.tops(i, "p2"), f"{names[0]}: Option A.")], 2, self.T), {0, 1, 2, 3}))
        self.assertEqual(scripted.named(i, f"{names[3]} and {names[1]}0 and {names[0]}"), [3, 0])

    def test_accommodating(self):
        i, T = self.inst, self.T
        v = scripted.View(i, "p1", [], 1, T)
        first = scripted.move("script_accommodating", i, "p1", [], 1, T)
        self.assertEqual((first["message"], first["design"]), (scripted.ASK_NEEDS, v.top))
        self.assertEqual(scripted.move("script_accommodating_middle", i, "p1", [], 1, T)["design"], v.mid)
        theirs = bargain.tops(i, "p2")
        reply = scripted.move("script_accommodating", i, "p1", [mv("p2", "propose", theirs)], 2, T)
        if bargain.value(i, "p1", theirs) >= 40:
            self.assertEqual(reply["action"], "accept")
        else:  # the other side's design with its own top option on as few decisions as reach 40 points
            self.assertGreaterEqual(bargain.value(i, "p1", reply["design"]), 40)
            back = [k for k in range(i["k"]) if reply["design"][k] != theirs[k]]
            self.assertTrue(all(reply["design"][k] == v.top[k] for k in back))
            for k in back:
                less = list(reply["design"]); less[k] = theirs[k]
                self.assertLess(bargain.value(i, "p1", less), 40)

    def test_avoiding(self):
        i, T = self.inst, self.T
        v = scripted.View(i, "p1", [], 1, T)
        theirs = bargain.tops(i, "p2")
        low = bargain.value(i, "p1", theirs) < 30
        self.assertEqual(scripted.move("script_avoiding", i, "p1", [mv("p2", "propose", theirs)], 2, T)["design"], v.top)
        quiet = scripted.move("script_avoiding_quiet", i, "p1", [mv("p2", "propose", theirs)], 2, T)
        self.assertEqual(quiet["action"], "propose" if low else "accept")
        second = [mv("p1", "propose", v.top), mv("p2", "propose", theirs, n=2)]
        self.assertEqual(scripted.move("script_avoiding", i, "p1", second, 3, T)["action"], "walk" if low else "accept")
        passed = [mv("p1", "propose", v.top), mv("p2", "pass", n=2)]
        self.assertEqual(scripted.move("script_avoiding", i, "p1", passed, 3, T)["action"], "walk")

    def test_a_script_against_a_model_seat(self):
        """The model seat is prompted as usual and reads the script's message and proposal; the script costs nothing."""
        seen = []

        def fake(config, messages, agent=None):
            seen.append((agent, messages[-1]["content"]))
            return json.dumps({"message": "Decision 1 matters most to me.", "action": "accept"}), 0.001, {"prompt": 10, "completion": 5, "seconds": 0.1}
        real, bargain.call = bargain.call, fake
        try:
            inst = next(i for i in self.insts if i["first_mover"] == "p1")
            r = self.game("script_collaborating", "neutral", inst)
        finally:
            bargain.call = real
        self.assertEqual([a for a, _ in seen], ["p2"])
        self.assertIn("Which decisions matter most to you?", seen[0][1])
        self.assertEqual(r["ended_by"], "p2_accepted")
        self.assertEqual(r["models"]["p1"], "script")
        self.assertEqual(r["usage"]["p1"]["calls"], 0)
        self.assertAlmostEqual(r["cost_usd"], 0.001)
        self.assertIsNotNone(r["compliance"]["p1"]["overall"])


if __name__ == "__main__":
    unittest.main()
