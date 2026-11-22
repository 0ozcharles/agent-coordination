"""Build the human annotation pack under annotation/ from the E1 game records and the grounded instances.

Task A: 120 E1 games, the public transcript only, for the style scores the judge model gave (assertiveness and
cooperativeness per agent). Task B: 100 E1 games, 20 per rule, with the rule text and the rule agent's own value
of every proposal, for the programmatic compliance check. Task C: 15 grounded instances with the extracted
decisions, options and importances, to be compared with the two ParallACT tickets they came from. The keys
(which game an item is, the judge's labels, the checker's verdicts) go to annotation/keys/ and are not to be
opened before the answers are in. Run from the repository root; needs runs/bargain/e1 (not in the repository).
"""
import csv
import glob
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import bargain  # noqa: E402

OUT = Path("annotation")
SEED = 20261122


def write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(header)
        w.writerows(rows)


def main():
    rng = random.Random(SEED)
    config = bargain.load_config("configs/bargain_e1.json")
    records = {}
    for f in sorted(glob.glob("runs/bargain/e1/games/*/*.json")):
        r = json.loads(Path(f).read_text())
        if not r.get("error"):
            records.setdefault(r["cell"], []).append(r)
    # ---- Task A: style labels, blind
    items, key = [], {}
    for cell in sorted(records):
        k = 3 if not cell.startswith(("prompted", "default")) else 2
        for r in rng.sample(records[cell], k):
            items.append(r)
    rng.shuffle(items)
    rows = []
    for n, r in enumerate(items, 1):
        item = f"A{n:03d}"
        rows.append([item, bargain.transcript(r)])
        lab = (r.get("label") or {}).get("label") or {}
        key[item] = {"cell": r["cell"], "instance": r["instance"]["id"], "judge": {a: {k: lab.get(a, {}).get(k) for k in ("assertiveness", "cooperativeness", "style")} for a in ("agent1", "agent2")}}
    write(OUT / "task_a_style_items.csv", ["item", "transcript"], rows)
    write(OUT / "task_a_style_answers_TEMPLATE.csv", ["item", "agent1_assertiveness", "agent1_cooperativeness", "agent2_assertiveness", "agent2_cooperativeness", "note"], [[r[0], "", "", "", "", ""] for r in rows])
    (OUT / "keys").mkdir(parents=True, exist_ok=True)
    (OUT / "keys" / "task_a_key.json").write_text(json.dumps(key, indent=1))
    # ---- Task B: rule compliance, the rule agent is Agent 1
    rows, key, n = [], {}, 0
    for s in bargain.STYLES:
        pool = [r for cell, rs in records.items() for r in rs if r["strategies"]["p1"] == s and r["ops"]["p1"] == "rules" and r["compliance"]["p1"].get("overall") is not None]
        bad = [r for r in pool if r["compliance"]["p1"]["overall"] is False]
        good = [r for r in pool if r["compliance"]["p1"]["overall"] is True]
        chosen = rng.sample(bad, min(10, len(bad)))
        chosen += rng.sample(good, 20 - len(chosen))
        rng.shuffle(chosen)
        for r in chosen:
            n += 1
            item, inst = f"B{n:03d}", r["instance"]
            lines = []
            for m in r["moves"]:
                who = f"Agent {1 if m['by'] == 'p1' else 2}"
                if m["action"] == "propose":
                    did = f"proposes {bargain.design_text(inst, m['design'])} [worth {m['values']['p1']:g} points to Agent 1]"
                else:
                    did = {"accept": "ACCEPTS the last proposal", "walk": "WALKS AWAY", "pass": "makes no valid move"}[m["action"]]
                lines.append(f"#{m['n']} {who} {did}: {m['message'] or '(no message)'}")
            rule = bargain.guidance(config, s, "rules", inst, "p1").replace(config.get("instruction_prefix", ""), "")
            t = bargain.thresholds(inst, "p1")
            facts = (f"Agent 1's ideal design is worth {t['max']:g} points to it and its outside option {t['outside']:g}. The game has {inst.get('moves', 10)} moves; "
                     f"Agent {1 if inst['first_mover'] == 'p1' else 2} moves first. Contested decisions: {sum(inst['opposed'])} of {inst['k']}.")
            rows.append([item, rule, facts, "\n".join(lines)])
            key[item] = {"strategy": s, "cell": r["cell"], "instance": inst["id"], "checker": r["compliance"]["p1"]}
    order = list(range(len(rows)))
    rng.shuffle(order)
    rows = [rows[i] for i in order]
    write(OUT / "task_b_compliance_items.csv", ["item", "rule given to Agent 1", "facts", "transcript"], rows)
    write(OUT / "task_b_compliance_answers_TEMPLATE.csv", ["item", "agent1_followed_the_rule (yes/no)", "which clause was broken, if any", "note"], [[r[0], "", "", ""] for r in rows])
    (OUT / "keys" / "task_b_key.json").write_text(json.dumps(key, indent=1))
    # ---- Task C: grounded extractions against the tickets
    grounded = json.loads(Path("data/bargain_grounded_instances.json").read_text())
    rows, key = [], {}
    for n, i in enumerate(sorted(rng.sample(range(len(grounded)), 15)), 1):
        g, item = grounded[i], f"C{n:02d}"
        skin = g["skin"]
        parts = [f"Context told to both: {skin['intro'].replace('{role}', skin['roles'][0]).replace('{other_role}', skin['roles'][1])}",
                 f"Developer A's ticket: {skin['roles'][0]}", f"Developer B's ticket: {skin['roles'][1]}"]
        for j, issue in enumerate(skin["issues"]):
            top_a, top_b = g["rankings"]["p1"][j][0], g["rankings"]["p2"][j][0]
            parts.append(f"Decision {j + 1}: {issue['name']} ({'contested' if g['opposed'][j] else 'shared'}; importance A {g['raw_importance']['A'][j]}, B {g['raw_importance']['B'][j]} on 1-5)\n"
                         + "\n".join(f"   {chr(65 + o)}. {text}" + (" <- A needs this" if o == top_a else "") + (" <- B needs this" if o == top_b else "") for o, text in enumerate(issue["options"])))
        rows.append([item, g["repo"], f"external/ParallACT/dataset/v1/generated/contradictory/{g['source']}.json", g["artifact"], "\n".join(parts)])
        key[item] = {"index": i, "source": g["source"]}
    write(OUT / "task_c_extraction_items.csv", ["item", "repository", "ticket pair (local file, not redistributed)", "colliding code", "extracted structure"], rows)
    write(OUT / "task_c_extraction_answers_TEMPLATE.csv", ["item", "decisions are real design choices behind the collision (yes/partly/no)", "each side's needed option is right (yes/partly/no)",
                                                           "the middle option is a real compromise (yes/partly/no)", "importances are plausible (yes/partly/no)", "note"], [[r[0], "", "", "", "", ""] for r in rows])
    (OUT / "keys" / "task_c_key.json").write_text(json.dumps(key, indent=1))
    print(f"wrote annotation/: task A {len(items)} items, task B {len(json.loads((OUT / 'keys' / 'task_b_key.json').read_text()))} items, task C {len(rows)} items")


if __name__ == "__main__":
    main()
