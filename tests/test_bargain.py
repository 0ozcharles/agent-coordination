import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_coordination import bargain


ENV = {"issues": 4, "options": 3, "moves": 10, "outside": [20, 20], "opposition": 0.75, "asymmetry": 0.5}


def config(**over):
    base = {"study": "t", "question": "q", "model": "m", "environment": dict(ENV), "control": "neutral",
            "instances": {"n": 4, "seed": 7, "first_mover": "alternate"}, "output": "runs/t", "instruction_prefix": "[G] ",
            "strategies": {"shared": {"neutral": "Do as you like."}, "prompted": {"competing": "Hold out."},
                           "rules": {"competing": "Open at 95."}},
            "cells": [{"name": "neutral", "p1": "neutral", "p2": "neutral", "op": "prompted"},
                      {"name": "competing", "p1": "competing", "p2": "neutral", "op": "rules", "p2_op": "prompted"}]}
    base.update(over)
    return base


def move(n, by, action, design=None, message="hi", inst=None):
    m = {"n": n, "by": by, "action": action, "design": design, "message": message, "raw": ""}
    if design is not None:
        m["values"] = {a: bargain.value(inst, a, design) for a in bargain.AGENTS}
    return m


class BargainTest(unittest.TestCase):
    def setUp(self):
        self.inst = bargain.make_instance(ENV, seed=3, index=0, first_mover="p1")

    def test_instance_structure(self):
        inst = self.inst
        self.assertEqual(sum(inst["importance"]["p1"]), 100)
        self.assertEqual(sum(inst["importance"]["p2"]), 100)
        self.assertEqual(sum(inst["opposed"]), 3)
        for agent in bargain.AGENTS:
            best = [max(range(3), key=lambda o: inst["weights"][agent][i][o]) for i in range(4)]
            self.assertEqual(bargain.value(inst, agent, best), 100.0)
        for i, opposed in enumerate(inst["opposed"]):
            top1 = max(range(3), key=lambda o: inst["weights"]["p1"][i][o])
            top2 = max(range(3), key=lambda o: inst["weights"]["p2"][i][o])
            self.assertEqual(top1 == top2, not opposed)

    def test_asymmetry_extremes(self):
        same = bargain.make_instance({**ENV, "asymmetry": 0.0}, 5, 0, "p1")
        self.assertEqual(same["importance"]["p1"], same["importance"]["p2"])
        reverse = bargain.make_instance({**ENV, "asymmetry": 1.0}, 5, 0, "p1")
        self.assertEqual(sorted(reverse["importance"]["p1"]), sorted(reverse["importance"]["p2"]))
        order1 = sorted(range(4), key=lambda i: reverse["importance"]["p1"][i])
        order2 = sorted(range(4), key=lambda i: reverse["importance"]["p2"][i])
        self.assertEqual(order1, order2[::-1])

    def test_frontier(self):
        f = self.inst["frontier"]
        self.assertTrue(all(u1 + u2 <= f["max_joint"] for u1, u2 in f["pareto"]))
        self.assertIn(f["max_joint"], [u1 + u2 for u1, u2 in f["pareto"]])
        self.assertTrue(f["nash"]["p1"] >= 20 and f["nash"]["p2"] >= 20)
        self.assertEqual(len(bargain.instances(config())), 4)
        self.assertEqual([i["first_mover"] for i in bargain.instances(config())], ["p1", "p2", "p1", "p2"])

    def test_stakes_move_cost_and_budget(self):
        env = {**ENV, "stakes": [1.0, 0.3], "outside": [30, 30], "move_cost": 2, "output_budget": 400}
        inst = bargain.make_instance(env, 3, 0, "p1")
        self.assertEqual((inst["max"]["p1"], inst["max"]["p2"], inst["outside"]["p2"]), (100, 30, 9))
        best2 = [max(range(3), key=lambda o: inst["weights"]["p2"][i][o]) for i in range(4)]
        self.assertAlmostEqual(bargain.value(inst, "p2", best2), 30.0, places=0)
        self.assertEqual(bargain.thresholds(inst, "p2"), {"max": 30, "t60": 18, "t50": 15, "outside": 9, "outside10": 12,
                                                         "t40": 12, "t70": 21, "outside20": 15})
        cfg = config(strategies={"shared": {"neutral": "Do."}, "prompted": {}, "rules": {"competing": "Accept {t60}+, not {outside}."}})
        self.assertEqual(bargain.guidance(cfg, "competing", "rules", inst, "p2"), "[G] Accept 18+, not 9.")
        text = bargain.system_prompt(cfg, inst, "p2", "neutral", "prompted")
        self.assertIn("at most 30 points", text)
        self.assertIn("costs each of you 2 points", text)
        self.assertIn("at most 400 tokens", text)
        self.assertIn("reply budget is used up", bargain.describe(inst, None, "p1", 1, 10, 0))
        moves = [move(1, "p1", "propose", best2, "hi", inst), move(2, "p2", "accept")]
        out = bargain.outcome(inst, moves, {"by": "p1", "design": best2, "values": moves[0]["values"]})
        self.assertEqual(out["move_cost_paid"], 4)
        self.assertAlmostEqual(out["payoff"]["p2"], 30.0 - 4, places=0)
        self.assertEqual(bargain.outcome(inst, moves[:1] + [move(2, "p2", "walk")], None)["payoff"], {"p1": 26, "p2": 5})

    def test_prompts_and_guidance(self):
        cfg = config()
        self.assertEqual(bargain.guidance(cfg, "competing", "rules"), "[G] Open at 95.")
        self.assertEqual(bargain.guidance(cfg, "neutral", "rules"), "[G] Do as you like.")
        self.assertIsNone(bargain.guidance(cfg, "default", "rules"))
        text = bargain.system_prompt(cfg, self.inst, "p1", "competing", "rules")
        self.assertIn("Decision 1:", text)
        self.assertIn("[G] Open at 95.", text)
        self.assertNotIn("The other developer's scores", text)
        visible = bargain.system_prompt(cfg, {**self.inst, "visible_weights": True}, "p1", "neutral", "prompted")
        self.assertIn("The other developer's scores are:", visible)

    def test_parse_reply(self):
        reply = 'Sure. {"message": "Deal?", "action": "propose", "proposal": {"Decision 1": "Option A", "Decision 2": "b", "Decision 3": "Option C", "Decision 4": "A"}}'
        parsed = bargain.parse_reply(reply, self.inst)
        self.assertEqual((parsed["action"], parsed["design"]), ("propose", [0, 1, 2, 0]))
        self.assertEqual(bargain.parse_reply('{"action": "accept", "message": ""}', self.inst)["action"], "accept")
        for bad in ('{"action": "propose", "proposal": {"Decision 1": "Option A"}}', '{"action": "fold"}', "no json",
                    '{"action": "propose", "proposal": {"Decision 9": "Option A"}}'):
            with self.assertRaises(ValueError):
                bargain.parse_reply(bad, self.inst)

    def test_outcome_metrics_and_compliance(self):
        inst = self.inst
        best1 = [max(range(3), key=lambda o: inst["weights"]["p1"][i][o]) for i in range(4)]
        best2 = [max(range(3), key=lambda o: inst["weights"]["p2"][i][o]) for i in range(4)]
        middle = [bargain.middle_option(inst, i) for i in range(4)]  # the value-middle option, not index 1
        moves = [move(1, "p1", "propose", best1, "I need Decision 1, it matters most to me. What matters to you?", inst),
                 move(2, "p2", "propose", best2, "Mine.", inst),
                 move(3, "p1", "propose", middle, "Middle then.", inst),
                 move(4, "p2", "accept", message="ok")]
        agreement = {"by": "p1", "design": middle, "values": moves[2]["values"]}
        out = bargain.outcome(inst, moves, agreement)
        self.assertEqual(out["payoff"]["p1"], bargain.value(inst, "p1", middle))
        self.assertEqual(out["joint_gap"], round(inst["frontier"]["max_joint"] - out["joint"], 1))
        m = bargain.agent_metrics(inst, moves, agreement, "p1")
        self.assertEqual((m["first"], m["last"]), (100.0, bargain.value(inst, "p1", middle)))
        self.assertTrue(m["asks"])
        self.assertEqual(m["proposals"], [100.0, bargain.value(inst, "p1", middle)])
        c = bargain.compliance("competing", inst, moves, agreement, "p1")
        self.assertTrue(c["opens_high"])
        self.assertFalse(c["one_decision_per_step"])  # best -> all-middle changes several decisions
        self.assertFalse(c["overall"])
        self.assertTrue(bargain.is_trade(inst, [t1 if i % 2 else t2 for i, (t1, t2) in enumerate(zip(best1, best2))]))
        self.assertFalse(bargain.is_trade(inst, best1))
        self.assertEqual(bargain.middle_share(inst, middle), 1.0)
        comp = bargain.compliance("compromising", inst, moves, agreement, "p1")
        self.assertFalse(comp["steps_of_two_decisions"])
        self.assertTrue(comp["monotone"])
        self.assertTrue(bargain.compliance("avoiding", inst, moves[:2] + [move(3, "p1", "accept")], None, "p1")["one_proposal"])
        no_deal = bargain.outcome(inst, moves[:2] + [move(3, "p1", "walk")], None)
        self.assertEqual(no_deal["payoff"], {"p1": 20, "p2": 20})

    def test_label_parsing_and_transcript(self):
        reply = ('{"agent1": {"assertiveness": 2, "cooperativeness": 2, "asks_needs": true, "discloses_priorities": true}, '
                 '"agent2": {"assertiveness": 1, "cooperativeness": 1, "asks_needs": false, "discloses_priorities": false}, '
                 '"first_concession": "agent2"}')
        label = bargain.parse_label(reply)
        self.assertEqual((label["agent1"]["style"], label["agent2"]["style"]), ("collaborating", "compromising"))
        with self.assertRaises(ValueError):
            bargain.parse_label(reply.replace('"discloses_priorities": true', '"discloses_priorities": "yes"'))
        record = {"instance": self.inst, "moves": [move(1, "p1", "propose", [0, 0, 0, 0], "Take it.", self.inst), move(2, "p2", "walk", message="")]}
        text = bargain.transcript(record)
        self.assertIn("#1 Agent 1 proposes Decision 1: Option A", text)
        self.assertIn("#2 Agent 2 WALKS AWAY: (no message)", text)

    def test_seats_can_play_different_models(self):
        from unittest import mock
        cfg = config(model="gpt-6-luna", max_tokens=700, model_kwargs={"extra_body": {"reasoning_effort": "none"}},
                     seats={"p2": {"model": "ai2s/claude-sonnet-5-5", "model_kwargs": {}, "max_tokens": 1500}})
        self.assertEqual(bargain.seat_settings(cfg, "p1"), {"model": "gpt-6-luna", "model_kwargs": {"extra_body": {"reasoning_effort": "none"}}, "max_tokens": 700})
        self.assertEqual(bargain.seat_settings(cfg, "p2"), {"model": "ai2s/claude-sonnet-5-5", "model_kwargs": {}, "max_tokens": 1500})
        self.assertEqual(bargain.seat_settings(config(), "p1")["max_tokens"], 700)  # no seats: the config's settings
        inst = bargain.instances(cfg)[0]
        seen = []

        def fake(cfg_, messages, agent=None):
            seen.append((agent, bargain.seat_settings(cfg_, agent)["model"]))
            if len(seen) == 1:  # the first mover proposes, the other accepts
                reply = json.dumps({"message": "hi", "action": "propose", "proposal": {f"decision {i + 1}": "A" for i in range(inst["k"])}})
            else:
                reply = json.dumps({"message": "ok", "action": "accept"})
            return reply, 0.002 if agent == "p2" else 0.001, {"prompt": 10, "completion": 5, "seconds": 0.1}

        with mock.patch.object(bargain, "call", fake):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual(record["models"], {"p1": "gpt-6-luna", "p2": "ai2s/claude-sonnet-5-5"})
        self.assertEqual(seen, [(inst["first_mover"], "gpt-6-luna" if inst["first_mover"] == "p1" else "ai2s/claude-sonnet-5-5"),
                                ("p2" if inst["first_mover"] == "p1" else "p1", "ai2s/claude-sonnet-5-5" if inst["first_mover"] == "p1" else "gpt-6-luna")])
        self.assertTrue(record["agreement"])
        self.assertEqual(round(record["usage"]["p1"]["cost_usd"] + record["usage"]["p2"]["cost_usd"], 3), round(record["cost_usd"], 3))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = bargain.game_path(cfg, root, cfg["cells"][0], inst)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
            row = bargain.rows(cfg, root)[0]
        self.assertEqual((row["p1_model"], row["p2_model"]), ("gpt-6-luna", "ai2s/claude-sonnet-5-5"))
        # a rule variant is checked with its base rule's checker; an unmapped variant has no compliance verdict
        variant = config(model="m", strategies={"shared": {"neutral": "Do as you like."}, "prompted": {}, "rules": {"competing_v2": "Start with your best design; accept at {t70} or more."}},
                         cells=[{"name": "competing_v2-neutral", "p1": "competing_v2", "p2": "neutral", "op": "rules"}])
        self.assertIn("accept at 70 or more", bargain.guidance(variant, "competing_v2", "rules", inst, "p1"))
        seen.clear()
        with mock.patch.object(bargain, "call", fake):
            unmapped = bargain.play(variant, inst, variant["cells"][0])
        seen.clear()
        with mock.patch.object(bargain, "call", fake):
            mapped = bargain.play(dict(variant, strategy_base={"competing_v2": "competing"}), inst, variant["cells"][0])
        self.assertIsNone(unmapped["compliance"]["p1"]["overall"])
        self.assertIn("opens_high", mapped["compliance"]["p1"])

    def test_rows_summary_matrix_and_replicator(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for cell in cfg["cells"]:
                for inst in bargain.instances(cfg)[:2]:
                    middle = [1, 1, 1, 1]
                    moves = [move(1, "p1", "propose", middle, "hi", inst), move(2, "p2", "accept")]
                    agreement = {"by": "p1", "design": middle, "values": moves[0]["values"]}
                    record = {"cell": cell["name"], "instance": inst, "strategies": {"p1": cell["p1"], "p2": cell["p2"]},
                              "moves": moves, "agreement": agreement, "ended_by": "p2_accepted", "invalid_replies": 0,
                              "cost_usd": 0.01, "error": None, **bargain.outcome(inst, moves, agreement),
                              "metrics": {a: bargain.agent_metrics(inst, moves, agreement, a) for a in bargain.AGENTS},
                              "compliance": {a: bargain.compliance(cell[a], inst, moves, agreement, a) for a in bargain.AGENTS}}
                    path = bargain.game_path(cfg, root, cell, inst)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(record))
            table = bargain.rows(cfg, root)
            self.assertEqual(len(table), 4)
            summary = bargain.summarize(cfg, table)
            self.assertEqual(summary["competing"]["games"], 2)
            self.assertEqual(summary["competing"]["vs_control"]["pairs"], 2)
            self.assertIn("p1_in_target_style", summary["competing"])
            M = bargain.matrix(table)
            self.assertIsNotNone(M["competing"]["neutral"])
            self.assertIsNotNone(M["neutral"]["competing"])
            self.assertEqual(bargain.best_responses({"a": {"a": 1, "b": 3}, "b": {"a": 2, "b": 0}}), {"a": "b", "b": "a"})
            pop = bargain.replicator({"a": {"a": 3, "b": 0}, "b": {"a": 5, "b": 1}})
            self.assertGreater(pop["b"], 0.9)
            svg = bargain.chart(cfg, summary)
            self.assertTrue(svg.startswith("<svg") and "competing" in svg)

    def test_views_and_net_table(self):
        def row(p1, p2, pay1, pay2, tok1, tok2):
            return {"error": False, "p1": p1, "p2": p2, "payoff_p1": pay1, "payoff_p2": pay2, "p1_tokens": tok1, "p2_tokens": tok2}
        table = [row("competing", "neutral", 70, 40, 3000, 2000), row("neutral", "competing", 45, 65, 2000, 3000),
                 row("avoiding", "neutral", 40, 60, 500, 1500), row("neutral", "neutral", 55, 55, 2000, 2000)]
        v = bargain.views(table)
        self.assertEqual(len(v["competing"]["vs_neutral"]["both"]), 2)
        self.assertEqual(len(v["neutral"]["vs_competing"]["both"]), 2)
        self.assertEqual(len(v["neutral"]["symmetric"]["both"]), 2)
        net = bargain.net_table({s: v[s]["vs_neutral"]["both"] for s in v})
        self.assertEqual(net["best"]["0"], "competing")
        self.assertEqual(net["best"]["10"], "competing")  # 67.5 - 30 = 37.5 > 40 - 5 = 35
        self.assertEqual(net["best"]["20"], "avoiding")  # 67.5 - 60 = 7.5 < 40 - 10 = 30
        self.assertEqual(net["switch_lambda"], "20")
        self.assertEqual(net["strategies"]["competing"]["regret"]["0"], 0.0)
        # a prompted style cell is not pooled with the rule cell of the same strategy (E1's prompted diagonal)
        mixed = [dict(row("competing", "competing", 50, 52, 4000, 4000), op="rules"), dict(row("competing", "competing", 60, 58, 3000, 3000), op="prompted"),
                 dict(row("neutral", "neutral", 55, 57, 2000, 2000), op="prompted")]
        self.assertEqual(bargain.matrix(mixed)["competing"]["competing"], 51.0)
        self.assertEqual(bargain.matrix(mixed)["neutral"]["neutral"], 56.0)
        self.assertEqual(len(bargain.views(mixed)["competing"]["symmetric"]["both"]), 2)
        out = {"study": "t", "question": "q", "lambdas": list(bargain.LAMBDAS),
               "conditions": {"c": {"games": 4, "errors": 0, "model_condition": "", "prediction": "",
                                    "readings": {r: {"both": net} for r in bargain.READINGS}}}}
        self.assertIn("avoiding", bargain.map_chart(out))


if __name__ == "__main__":
    unittest.main()


RULES = {"competing": "Follow these rules: open high; accept only {t60} or more.", "collaborating": "Follow these rules: ask first; trade.",
         "compromising": "Follow these rules: meet in the middle; accept {t50}.", "accommodating": "Follow these rules: accept the first offer over {outside10}.",
         "avoiding": "Follow these rules: make one proposal, then accept or walk."}
CHOICE = {"once": "{prefix}Before the negotiation starts, pick one.\n\n{menu}\n\nReply with {\"choice\": \"<letter>\"}",
          "again": "{prefix}Before you move, pick one.\n\n{menu}\n\nReply with {\"choice\": \"<letter>\"}",
          "once_forced": "{prefix}Before the negotiation starts, pick one of the five.\n\n{menu}\n\nReply with {\"choice\": \"<letter>\"}",
          "again_forced": "{prefix}Before you move, pick one of the five.\n\n{menu}\n\nReply with {\"choice\": \"<letter>\"}",
          "kept": "You chose {letter}. From now on: {rule}"}


def chooser_config(*cells):
    return config(strategies={"shared": {"neutral": "Do as you like."}, "prompted": {}, "rules": RULES}, choice=CHOICE,
                  cells=[{"name": f"{a}-{b}", "p1": a, "p2": b, "op": "rules"} for a, b in cells])


class ChooserTest(unittest.TestCase):
    """A seat that picks its own strategy: once, once with the pick kept in view, or before every move."""

    def player(self, inst, want, seen, bad=0):
        """A stand-in for the model: picks the strategies in `want` in turn when shown a menu (after `bad` unreadable
        replies), proposes on the first move of the game and accepts on the second."""
        state = {"picks": 0, "bad": bad, "moves": 0}

        def fake(cfg_, messages, agent=None):
            seen.append((agent, [dict(m) for m in messages]))
            last = messages[-1]["content"]
            if '"choice"' in last:
                if state["bad"]:
                    state["bad"] -= 1
                    return "I would rather not say", 0.0005, {"prompt": 100, "completion": 9, "seconds": 0.1}
                target = want[min(state["picks"], len(want) - 1)]
                state["picks"] += 1
                text = "Do as you like." if target == "neutral" else RULES[target].split(";")[0].split("{")[0]
                menu = next(m["content"] for m in reversed(messages) if m["role"] == "user" and "\nA. " in m["content"])
                letter = next(line[0] for line in menu.split("\n") if line[1:3] == ". " and text in line)
                return json.dumps({"choice": letter, "reason": "because"}), 0.0005, {"prompt": 100, "completion": 9, "seconds": 0.1}
            state["moves"] += 1
            if state["moves"] == 1:
                return (json.dumps({"message": "hi", "action": "propose", "proposal": {f"decision {i + 1}": "A" for i in range(inst["k"])}}),
                        0.001, {"prompt": 10, "completion": 5, "seconds": 0.1})
            return json.dumps({"message": "ok", "action": "accept"}), 0.001, {"prompt": 10, "completion": 5, "seconds": 0.1}
        return fake

    def test_menu(self):
        cfg = chooser_config(("choose", "neutral"))
        inst = bargain.instances(cfg)[0]
        order = bargain.menu_order(cfg, inst, "p1")
        self.assertEqual(sorted(order), sorted(bargain.MENU))
        self.assertEqual(order, bargain.menu_order(cfg, inst, "p1"))  # fixed by seed, instance, seat and move
        self.assertEqual(len({tuple(bargain.menu_order(cfg, i, a, n)) for i in bargain.instances(cfg) for a in bargain.AGENTS for n in range(3)}) > 6, True)
        text = bargain.menu_text(cfg, inst, "p1", order, "once")
        self.assertTrue(text.startswith("[G] Before the negotiation starts, pick one.\n\nA. "))
        for letter, s in zip("ABCDEF", order):  # every option word for word as an assigned agent reads it, thresholds filled in
            self.assertIn(f"{letter}. " + bargain.guidance(dict(cfg, instruction_prefix=""), s, "rules", inst, "p1"), text)
        self.assertIn("accept only 60 or more", text)
        self.assertNotIn("{", text.replace('{"choice": "<letter>"}', ""))
        self.assertEqual(bargain.parse_choice('{"choice": "b", "reason": "x"}', order), {"letter": "B", "strategy": order[1], "reason": "x"})
        self.assertEqual(bargain.parse_choice('Sure: {"choice": "Option F."}', order)["strategy"], order[5])
        for bad in ("no json", '{"choice": "G"}', '{"choice": "competing"}', "[1]", '{"choice": ""}'):
            with self.assertRaises(ValueError):
                bargain.parse_choice(bad, order)

    def test_a_single_choice_is_played_as_if_assigned(self):
        from unittest import mock
        cfg = chooser_config(("choose", "neutral"), ("competing", "neutral"))
        inst = bargain.instances(cfg)[0]
        seen = []
        with mock.patch.object(bargain, "call", self.player(inst, ["competing"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual((record["assigned"], record["strategies"]), ({"p1": "choose", "p2": "neutral"}, {"p1": "competing", "p2": "neutral"}))
        choice = record["choice"]["p1"][0]
        self.assertEqual((choice["strategy"], choice["invalid"], choice["usage"]["calls"], choice["when"]), ("competing", 0, 1, "once"))
        # the choice is asked without guidance, in a conversation of its own
        agent, asked = seen[0]
        self.assertEqual((agent, len(asked)), ("p1", 2))
        self.assertEqual(asked[0]["content"], bargain.system_prompt(cfg, inst, "p1", "default", "rules"))
        self.assertNotIn("[G]", asked[0]["content"])
        # and the game is prompted exactly as in the cell where the same rule is assigned
        seen_assigned = []
        with mock.patch.object(bargain, "call", self.player(inst, [], seen_assigned)):
            assigned = bargain.play(cfg, inst, cfg["cells"][1])
        self.assertEqual([m for a, m in seen[1:]], [m for a, m in seen_assigned])
        self.assertNotIn("choice", assigned)
        self.assertIn("opens_high", record["compliance"]["p1"])  # compliance is checked against the rule picked
        # the cost of the choice is in the game's cost; its tokens are not in the seat's game tokens
        self.assertAlmostEqual(record["cost_usd"], 0.0005 + 0.002)
        self.assertEqual(record["usage"]["p1"]["completion"] + record["usage"]["p2"]["completion"], 10)
        self.assertAlmostEqual(record["usage"]["p1"]["cost_usd"] + record["usage"]["p2"]["cost_usd"], record["cost_usd"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = bargain.game_path(cfg, root, cfg["cells"][0], inst)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
            row = bargain.rows(cfg, root)[0]
            self.assertEqual((row["p1"], row["p1_choice"], row["p1_choices"], row.get("p2_choice")), ("choose", "competing", "competing", None))
            self.assertEqual((row["p1_choice_letters"], row["p1_choice_invalid"], row["p1_choice_tokens"]), (choice["letter"], 0, 109))
            cfg2 = dict(cfg, cells=cfg["cells"][:1], control="choose-neutral")
            with contextlib.redirect_stdout(io.StringIO()):
                bargain.report(cfg2, root)
            header = (root / "results" / "t_rows.csv").read_text().split("\n")[0].split(",")
            self.assertEqual(header[-len(bargain.CHOICE_COLUMNS):], list(bargain.CHOICE_COLUMNS))
            self.assertEqual(header[:len(bargain.ROW_COLUMNS)], list(bargain.ROW_COLUMNS))

    def test_a_forced_choice_offers_the_five_rule_sets_only(self):
        from unittest import mock
        cfg = chooser_config(("pick", "neutral"), ("neutral", "pick_kept"), ("repick", "competing"))
        inst = bargain.instances(cfg)[0]
        order = bargain.menu_order(cfg, inst, "p1", options=bargain.STYLES)
        self.assertEqual(sorted(order), sorted(bargain.STYLES))
        text = bargain.menu_text(cfg, inst, "p1", order, "once")
        self.assertTrue(text.startswith("[G] Before the negotiation starts, pick one of the five.\n\nA. "))
        self.assertNotIn("Do as you like.", text)
        self.assertNotIn("\nF. ", text)
        with self.assertRaises(ValueError):
            bargain.parse_choice('{"choice": "F"}', order)
        seen = []
        with mock.patch.object(bargain, "call", self.player(inst, ["avoiding"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual((record["strategies"]["p1"], record["choice"]["p1"][0]["order"]), ("avoiding", order))
        self.assertEqual(seen[1][1][0]["content"], bargain.system_prompt(cfg, inst, "p1", "avoiding", "rules"))
        seen.clear()
        with mock.patch.object(bargain, "call", self.player(inst, ["compromising"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][1])
        self.assertEqual(record["strategies"]["p2"], "compromising")
        self.assertIn("pick one of the five", record["choice"]["p2"][0]["question"])
        seen.clear()
        with mock.patch.object(bargain, "call", self.player(inst, ["collaborating"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][2])
        self.assertEqual((record["strategies"]["p1"], record["moves"][0]["rule"]), ("repick", "collaborating"))
        self.assertIn("Before you move, pick one of the five.", seen[0][1][-1]["content"])
        seen.clear()  # a forced choice that cannot be read: the agent plays without rules, and the record says so
        with mock.patch.object(bargain, "call", self.player(inst, ["avoiding"], seen, bad=2)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual((record["strategies"]["p1"], record["choice"]["p1"][0]["invalid"], record["choice"]["p1"][0]["letter"]), ("neutral", 2, "-"))

    def test_unreadable_choice_falls_back_to_no_rules(self):
        from unittest import mock
        cfg = chooser_config(("neutral", "choose"))
        inst = bargain.instances(cfg)[0]
        seen = []
        with mock.patch.object(bargain, "call", self.player(inst, ["avoiding"], seen, bad=2)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        choice = record["choice"]["p2"][0]
        self.assertEqual((choice["strategy"], choice["invalid"], choice["usage"]["calls"]), ("neutral", 2, 2))
        self.assertEqual(record["strategies"]["p2"], "neutral")
        seen.clear()
        with mock.patch.object(bargain, "call", self.player(inst, ["avoiding"], seen, bad=1)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual((record["choice"]["p2"][0]["strategy"], record["choice"]["p2"][0]["invalid"]), ("avoiding", 1))
        self.assertIn("could not be used", seen[1][1][-1]["content"])

    def test_a_kept_choice_stays_in_the_conversation(self):
        from unittest import mock
        cfg = chooser_config(("choose_kept", "choose_kept"))
        inst = bargain.instances(cfg)[0]
        seen = []
        with mock.patch.object(bargain, "call", self.player(inst, ["collaborating", "neutral"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual(record["strategies"], {"p1": "collaborating", "p2": "neutral"})
        first, second = inst["first_mover"], "p2" if inst["first_mover"] == "p1" else "p1"
        for agent, rule in ((first, RULES[record["strategies"][first]] if record["strategies"][first] != "neutral" else "Do as you like."),
                            (second, RULES[record["strategies"][second]] if record["strategies"][second] != "neutral" else "Do as you like.")):
            messages = next(m for a, m in seen[2:] if a == agent)  # the seat's first move of the game
            c = record["choice"][agent][0]
            self.assertEqual(messages[0]["content"], bargain.system_prompt(cfg, inst, agent, "default", "rules"))
            self.assertEqual((messages[1]["content"], messages[2]["content"]), (c["question"], c["raw"]))
            self.assertTrue(messages[3]["content"].startswith(f"You chose {c['letter']}. From now on: {rule}\n\n"))
            self.assertIn("Move", messages[3]["content"])

    def test_choosing_again_before_every_move(self):
        from unittest import mock
        cfg = chooser_config(("rechoose", "neutral"), ("neutral", "rechoose"))
        inst = bargain.instances(cfg)[0]  # p1 moves first
        self.assertEqual(inst["first_mover"], "p1")
        seen = []
        with mock.patch.object(bargain, "call", self.player(inst, ["competing"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][0])
        self.assertEqual(record["strategies"]["p1"], "rechoose")
        self.assertIsNone(record["compliance"]["p1"]["overall"])
        self.assertEqual([c["n"] for c in record["choice"]["p1"]], [1])
        self.assertEqual(record["moves"][0]["rule"], "competing")
        self.assertNotIn("rule", record["moves"][1])
        self.assertTrue(seen[0][1][-1]["content"].startswith("Move 1 of 10. You move first.\n\n[G] Before you move, pick one."))
        self.assertEqual(seen[1][1][0]["content"], bargain.system_prompt(cfg, inst, "p1", "competing", "rules"))
        self.assertEqual(seen[1][1][1]["content"], "Move 1 of 10. You move first.")
        # in the other seat the chooser sees the first proposal before it picks, and its history carries no menu
        seen.clear()
        with mock.patch.object(bargain, "call", self.player(inst, ["accommodating"], seen)):
            record = bargain.play(cfg, inst, cfg["cells"][1])
        self.assertEqual([c["n"] for c in record["choice"]["p2"]], [2])
        asked = seen[1][1]
        self.assertEqual(asked[0]["content"], bargain.system_prompt(cfg, inst, "p2", "default", "rules"))
        self.assertIn("proposed:", asked[-1]["content"])
        self.assertIn("Before you move, pick one.", asked[-1]["content"])
        self.assertEqual(record["moves"][1]["rule"], "accommodating")
        self.assertEqual(seen[2][1][0]["content"], bargain.system_prompt(cfg, inst, "p2", "accommodating", "rules"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = bargain.game_path(cfg, root, cfg["cells"][1], inst)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
            self.assertEqual(bargain.rows(cfg, root)[0]["p2_choices"], "accommodating")


class SkinAndGroundTest(unittest.TestCase):
    def test_abstract_rendering_is_the_default(self):
        inst = bargain.make_instance(ENV, seed=3, index=0, first_mover="p1")
        text = bargain.system_prompt(config(), inst, "p1", "neutral", "prompted")
        self.assertIn("You are Developer 1, one of two developers who must agree on a shared design with 4 decisions. For each", text)
        self.assertIn("Decision 1: Option A = ", text)
        self.assertIn("The other developer has their own private scores", text)
        self.assertEqual(bargain.issue_names(inst), ["Decision 1", "Decision 2", "Decision 3", "Decision 4"])
        self.assertEqual(bargain.option_labels(inst, 0), ["Option A", "Option B", "Option C"])

    def test_named_skin_rendering_and_parsing(self):
        inst = bargain.make_instance({**ENV, "skin": "team_plan"}, seed=3, index=0, first_mover="p1")
        plain = bargain.make_instance(ENV, seed=3, index=0, first_mover="p1")
        self.assertEqual(inst["weights"], plain["weights"])  # a skin changes the rendering, not the game
        text = bargain.system_prompt(config(), inst, "p2", "competing", "prompted")
        self.assertIn("You are Team lead 2, one of two team leads who must agree on a quarterly plan for both teams with 4 decisions.", text)
        self.assertIn("Hiring: A (two senior engineers) = ", text)
        self.assertIn("a plan is worth the sum of your scores", text)
        self.assertIn("The other team lead has their own private scores", text)
        self.assertNotIn("developer", text)
        self.assertNotIn("design", text)
        told = bargain.describe(inst, move(1, "p1", "propose", [0, 1, 2, 0], inst=inst), "p2", 2, 10)
        self.assertTrue(told.startswith('The other team lead said: "hi" and proposed: Hiring: A (two senior engineers), Office days: B (Tuesday to Thursday)'))
        reply = ('{"action": "propose", "message": "m", "proposal": {"Hiring": "B", "Office days": "Option C", '
                 '"Tooling budget": "training", "Review cadence": "A (weekly)"}}')
        self.assertEqual(bargain.parse_reply(reply, inst)["design"], [1, 2, 2, 0])
        with self.assertRaises(ValueError):
            bargain.parse_reply(reply.replace('"training"', '"nothing"'), inst)
        self.assertIn("Two team leads negotiate a quarterly plan for both teams", bargain.label_prompt("CB", {"instance": inst, "moves": []}))

    def test_instances_from_a_file(self):
        spec = {"source": "x", "importance": {"p1": [60.0, 40.0], "p2": [25.0, 75.0]},
                "rankings": {"p1": [[2, 0, 1], [1, 0, 2]], "p2": [[1, 0, 2], [1, 0, 2]]}, "opposed": [True, False],
                "skin": {"parties": ["Developer A", "Developer B"], "roles": ["the batch API", "the fast path"],
                         "artifact": "the design of src/meta/index.js", "intro": "Context here. You are working on {role}; the other developer on {other_role}.",
                         "issues": [{"name": "Input shape", "options": ["one", "two", "three"]}, {"name": "Lookups", "options": ["x", "y", "z"]}]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "specs.json"
            path.write_text(json.dumps([spec, spec]))
            cfg = config(instances={"path": str(path), "first_mover": "alternate"}, environment={"moves": 8, "outside": [30, 30]})
            insts = bargain.instances(cfg)
        self.assertEqual([i["first_mover"] for i in insts], ["p1", "p2"])
        inst = insts[0]
        self.assertEqual((inst["k"], inst["m"], inst["opposed"], inst["opposition"]), (2, 3, [True, False], 0.5))
        self.assertEqual(inst["weights"]["p1"][0], [30.0, 0.0, 60.0])  # option 2 is p1's top on issue 0, option 1 its bottom
        self.assertEqual(inst["weights"]["p2"][0], [12.5, 25.0, 0.0])
        self.assertEqual(inst["outside"], {"p1": 30, "p2": 30})
        self.assertIn("integrative_gain", inst["frontier"])
        text_a = bargain.system_prompt(cfg, inst, "p1", "neutral", "prompted")
        text_b = bargain.system_prompt(cfg, inst, "p2", "neutral", "prompted")
        self.assertIn("You are Developer A, one of two developers who must agree on the design of src/meta/index.js with 2 decisions. Context here. "
                      "You are working on the batch API; the other developer on the fast path.", text_a)
        self.assertIn("You are working on the fast path; the other developer on the batch API.", text_b)
        self.assertIn("Input shape: A (one) = 30, B (two) = 0, C (three) = 60", text_a)
        self.assertIn("8 moves in total", text_a)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "specs.json"
            path.write_text(json.dumps([spec]))
            cfg = config(instances={"path": str(path), "skin": "abstract"}, environment={"moves": 8, "outside": [30, 30]})
            plain = bargain.instances(cfg)[0]
        self.assertEqual(plain["weights"], inst["weights"])  # the override changes the rendering only
        text = bargain.system_prompt(cfg, plain, "p1", "neutral", "prompted")
        self.assertIn("You are Developer 1, one of two developers who must agree on a shared design with 2 decisions. For each", text)
        self.assertIn("Decision 1: Option A = 30, Option B = 0, Option C = 60", text)
        self.assertNotIn("Context here", text)

    def test_ground_validation_and_spec(self):
        from agent_coordination import ground
        out = {"context": "The shared code validates slugs across three namespaces and both tickets rewrite that flow.",
               "roles": ["Add batch slug checks", "Short-circuit lookups"],
               "decisions": [{"name": "Input shape", "options": ["Scalar only", "Scalar and array", "Array only"],
                              "A_needs": 2, "B_needs": 1, "middle": 3, "importance": {"A": 5, "B": 2}},
                             {"name": "Invalid input", "options": ["Accept falsy", "Reject missing", "Reject missing or falsy"],
                              "A_needs": 3, "B_needs": 3, "middle": 2, "importance": {"A": 3, "B": 3}}]}
        ground.validate(out)
        data = {"instance_id": "inst-1", "repo": "r", "shared_artifact": "src/meta/index.js — Meta.slugTaken flow"}
        spec = ground.to_spec(data, out)
        self.assertEqual(spec["opposed"], [True, False])
        self.assertEqual(spec["importance"], {"p1": [62.5, 37.5], "p2": [40.0, 60.0]})
        self.assertEqual(spec["skin"]["artifact"], "the design of src/meta/index.js")
        issues = spec["skin"]["issues"]
        self.assertEqual(sorted(issues[0]["options"]), ["Array only", "Scalar and array", "Scalar only"])
        top = lambda agent, i: issues[i]["options"][spec["rankings"][agent][i][0]]
        self.assertEqual((top("p1", 0), top("p2", 0)), ("Scalar and array", "Scalar only"))
        self.assertEqual(issues[0]["options"][spec["rankings"]["p1"][0][1]], "Array only")  # the middle ground is second for both
        self.assertEqual(spec["rankings"]["p1"][0][1], spec["rankings"]["p2"][0][1])
        self.assertEqual((top("p1", 1), top("p2", 1)), ("Reject missing or falsy", "Reject missing or falsy"))
        inst = bargain.assemble({"moves": 10, "outside": [30, 30]}, 0, "p1", spec["importance"], spec["rankings"], spec["opposed"], skin=spec["skin"])
        self.assertEqual(inst["max"], {"p1": 100, "p2": 100})
        self.assertEqual(bargain.value(inst, "p1", [spec["rankings"]["p1"][0][0], spec["rankings"]["p1"][1][0]]), 100)
        for broken in ({**out, "decisions": out["decisions"][:1] + [{**out["decisions"][1], "A_needs": 1, "B_needs": 1, "middle": 1}]},
                       {**out, "decisions": [out["decisions"][1]] * 2},
                       {**out, "decisions": [{**out["decisions"][0], "A_needs": 1, "B_needs": 1}, out["decisions"][1]]},
                       {**out, "roles": ["only one"]}):
            with self.assertRaises(ValueError):
                ground.validate(broken)
        self.assertEqual(ground.short_artifact("src/a.js: the thing"), "src/a.js")
        self.assertEqual(ground.short_artifact("src/a.js"), "src/a.js")


class CollaboratingRuleTest(unittest.TestCase):
    def spec_instance(self, opposed):
        k = len(opposed)
        importance = {"p1": [round(100 / k, 1)] * k, "p2": [round(100 / k, 1)] * k}
        importance["p1"][-1] = importance["p2"][-1] = round(100 - sum(importance["p1"][:-1]), 1)
        rankings = {"p1": [[0, 1, 2]] * k, "p2": [[2, 1, 0] if o else [0, 1, 2] for o in opposed]}
        return bargain.assemble({"moves": 10, "outside": [30, 30]}, 0, "p1", importance, rankings, opposed)

    def test_collaborative_proposal_by_number_of_contested_decisions(self):
        two = self.spec_instance([True, True, False])
        self.assertTrue(bargain.collaborative_proposal(two, [0, 2, 0], "p1"))   # a trade: p1's top on 0, p2's top on 1
        self.assertFalse(bargain.collaborative_proposal(two, [0, 0, 0], "p1"))  # p1 keeps both contested tops
        self.assertFalse(bargain.collaborative_proposal(two, [1, 1, 0], "p1"))  # all middle: nobody's top
        one = self.spec_instance([True, False, False])
        self.assertFalse(bargain.collaborative_proposal(one, [0, 0, 0], "p1"))  # insists on its top
        self.assertTrue(bargain.collaborative_proposal(one, [1, 0, 0], "p1"))   # the middle option
        self.assertTrue(bargain.collaborative_proposal(one, [2, 0, 0], "p1"))   # the partner's top
        self.assertTrue(bargain.collaborative_proposal(one, [0, 0, 0], "p2"))   # for p2 option 0 is the concession
        self.assertFalse(bargain.is_trade(one, [1, 0, 0]))                       # the old test could never pass here
        self.assertIsNone(bargain.collaborative_proposal(self.spec_instance([False, False]), [0, 0], "p1"))

    def test_compliance_uses_the_applicable_clause(self):
        one = self.spec_instance([True, False, False])
        asks = "Which decisions matter most to you? Decision 1 matters most to me."
        moves = [move(1, "p1", "propose", [1, 0, 0], message=asks, inst=one), move(2, "p2", "accept")]
        agreement = {"by": "p1", "design": [1, 0, 0], "values": moves[0]["values"]}
        c = bargain.compliance("collaborating", one, moves, agreement, "p1")
        self.assertTrue(c["proposes_trade"])
        self.assertTrue(c["overall"])
        moves = [move(1, "p1", "propose", [0, 0, 0], message=asks, inst=one), move(2, "p2", "accept")]
        agreement = {"by": "p1", "design": [0, 0, 0], "values": moves[0]["values"]}
        self.assertFalse(bargain.compliance("collaborating", one, moves, agreement, "p1")["proposes_trade"])
        # accepting the partner's proposal that takes the contested decision away from the agent counts as the concession
        moves = [move(1, "p1", "propose", [0, 0, 0], message=asks, inst=one), move(2, "p2", "propose", [2, 0, 0], inst=one), move(3, "p1", "accept")]
        agreement = {"by": "p2", "design": [2, 0, 0], "values": moves[1]["values"]}
        c = bargain.compliance("collaborating", one, moves, agreement, "p1")
        self.assertTrue(c["proposes_trade"])
        moves = [move(1, "p1", "propose", [0, 0, 0], message=asks, inst=one), move(2, "p2", "propose", [0, 1, 0], inst=one), move(3, "p1", "accept")]
        agreement = {"by": "p2", "design": [0, 1, 0], "values": moves[1]["values"]}
        self.assertFalse(bargain.compliance("collaborating", one, moves, agreement, "p1")["proposes_trade"])  # kept its top
        none = self.spec_instance([False, False])
        moves = [move(1, "p1", "propose", [0, 0], message=asks, inst=none), move(2, "p2", "accept")]
        agreement = {"by": "p1", "design": [0, 0], "values": moves[0]["values"]}
        self.assertIsNone(bargain.compliance("collaborating", none, moves, agreement, "p1")["proposes_trade"])
