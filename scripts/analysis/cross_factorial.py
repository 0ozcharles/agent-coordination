"""Cross-model pairs as a 2 x 2: does a rule's own gain depend on the adopter's model or on the partner's?

For competing and collaborating adopted alone, the adopter's own gain over the default agent in the same seat is
computed per instance in the four cells (adopter luna or sonnet, partner luna or sonnet; same-model cells from
E3's luna control and sonnet arms, mixed cells from the two cross arms, all on E1's 40 instances). The partner
effect (partner luna minus partner sonnet, adopters averaged) and the adopter effect are paired by instance.
Writes results/bargain_e3_cross_factorial.json. Run from the repository root.
"""
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import stats  # noqa: E402

SAME = {"luna": "e3_gpt_6_luna_t1", "sonnet": "e3_claude_sonnet_5_5"}
CROSS = {"e3_cross_luna_sonnet": {"luna": "p1", "sonnet": "p2"}, "e3_cross_sonnet_luna": {"sonnet": "p1", "luna": "p2"}}


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return round(o, 4) if isinstance(o, float) else o


def main():
    same = {m: stats.load_rows(f"results/bargain_{arm}_rows.csv") for m, arm in SAME.items()}
    cross = {arm: stats.load_rows(f"results/bargain_{arm}_rows.csv") for arm in CROSS}
    ids = stats.instances_of(same["luna"])

    def gain(adopter, partner, s, i, who):
        d = []
        if adopter == partner:  # the same-model arms played the rule in seat 1 only
            games = [(same[adopter], f"{s}-neutral", "p1")]
        else:
            games = [(cross[arm], f"{s}-neutral" if seats[adopter] == "p1" else f"neutral-{s}", seats[adopter]) for arm, seats in CROSS.items()]
        for R, cell, seat in games:
            r, nn = R.get((cell, i)), R.get(("neutral-neutral", i))
            if r is None or nn is None:
                continue
            k = seat if who == "own" else stats.other(seat)
            d.append(r[f"payoff_{k}"] - nn[f"payoff_{k}"])
        return st.mean(d) if d else None

    out = {"instances": len(ids)}
    for s in ("collaborating", "competing"):
        e = {}
        for who in ("own", "partner"):
            c = {(a, p): [gain(a, p, s, i, who) for i in ids] for a in SAME for p in SAME}
            block = {f"adopter {a}, partner {p}": stats.summary(v) for (a, p), v in c.items()}
            block["partner effect (partner luna - partner sonnet), adopters averaged"] = stats.summary(
                [((w - x) + (y - z)) / 2 for w, x, y, z in zip(c[("luna", "luna")], c[("luna", "sonnet")], c[("sonnet", "luna")], c[("sonnet", "sonnet")])])
            block["adopter effect (adopter luna - adopter sonnet), partners averaged"] = stats.summary(
                [((w - y) + (x - z)) / 2 for w, x, y, z in zip(c[("luna", "luna")], c[("luna", "sonnet")], c[("sonnet", "luna")], c[("sonnet", "sonnet")])])
            e["adopter's own gain" if who == "own" else "partner's change"] = block
        out[s] = e
        print(f"== {s}")
        for who, block in e.items():
            for label, x in block.items():
                print(f"  {who:18} {label:68} {x['mean']:+5.1f} [{x['ci95'][0]:+5.1f}, {x['ci95'][1]:+5.1f}] wilcoxon {x['p_wilcoxon']:.3f} n={x['n']}")
    Path("results/bargain_e3_cross_factorial.json").write_text(json.dumps(clean(out), indent=1) + "\n")
    print("wrote results/bargain_e3_cross_factorial.json")


if __name__ == "__main__":
    main()
