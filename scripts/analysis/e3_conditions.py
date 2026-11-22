"""E3 conditions: open information and unequal stakes on sonnet and sol against E2's luna runs, the 16 cells without a competing opponent. Writes results/bargain_e3_conditions_paired.json (run before e3_vs_competing.py).

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st
from pathlib import Path
STRATS = ["neutral", "competing", "collaborating", "compromising", "accommodating", "avoiding"]
CELLS = [f"{s}-{s}" for s in STRATS] + [f"{s}-neutral" for s in STRATS[1:]] + [f"neutral-{s}" for s in STRATS[1:]]
ARMS = {"open_info": [("luna_E2", "e2/open_info"), ("sonnet", "e3_open_info_claude_sonnet_5_5"), ("sol", "e3_open_info_gpt_6_1_sol")],
        "low_stakes_p2": [("luna_E2", "e2/low_stakes_p2"), ("sonnet", "e3_low_stakes_p2_claude_sonnet_5_5"), ("sol", "e3_low_stakes_p2_gpt_6_1_sol")]}
def load(run):
    out = {}
    for f in glob.glob(f"runs/bargain/{run}/games/*/*.json"):
        r = json.load(open(f))
        if r["cell"] in CELLS and not r.get("error"): out[(r["cell"], r["instance"]["id"])] = r
    return out
def sign_p(h, l):
    n = h + l
    if n == 0: return 1.0
    k = min(h, l); return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
def paired(d):
    h, l = sum(v > 0 for v in d), sum(v < 0 for v in d); return round(st.mean(d), 1), h, l, round(sign_p(h, l), 3)
def tokens(r): return sum(r["usage"][a]["prompt"] + r["usage"][a]["completion"] for a in ("p1", "p2"))
out = {}
for cond, arms in ARMS.items():
    runs = {nm: load(run) for nm, run in arms}; names = [nm for nm, _ in arms]
    o = out[cond] = {"cells": {}, "symmetric_vs_neutral": {}, "unilateral": {}, "vs_e2_twin": {}, "compliance": {}, "seats": {}}
    print(f"\n################ {cond}")
    print("=== per cell and arm: joint | p1 | p2 | agree/20 | frontier | gap | moves | tokens | passes")
    for c in CELLS:
        line = f"{c:26}"
        for nm in names:
            recs = [runs[nm][(c, i)] for i in range(20) if (c, i) in runs[nm]]
            s = {"n": len(recs), "joint": round(st.mean(r["joint"] for r in recs), 1), "p1": round(st.mean(r["payoff"]["p1"] for r in recs), 1), "p2": round(st.mean(r["payoff"]["p2"] for r in recs), 1),
                 "agree": sum(bool(r["agreement"]) for r in recs), "frontier": sum(bool(r["on_frontier"]) for r in recs), "gap": round(st.mean(r["joint_gap"] for r in recs), 1),
                 "moves": round(st.mean(len(r["moves"]) for r in recs), 1), "tokens": round(st.mean(tokens(r) for r in recs)), "passes": sum(m["action"] == "pass" for r in recs for m in r["moves"])}
            o["cells"].setdefault(c, {})[nm] = s
            line += f" | {nm[:7]}: {s['joint']:5.1f} {s['p1']:4.1f}/{s['p2']:4.1f} a{s['agree']:2d} f{s['frontier']:2d} g{s['gap']:4.1f} m{s['moves']:3.1f} t{s['tokens']:5d} p{s['passes']}"
        print(line)
    print("=== symmetric pairs against the neutral pair (joint, paired by instance): mean (wins/losses, p)")
    for nm in names:
        base = [runs[nm][("neutral-neutral", i)] for i in range(20)]; line = f"{nm:8}"
        for s_ in STRATS[1:]:
            d = paired([runs[nm][(f"{s_}-{s_}", i)]["joint"] - base[i]["joint"] for i in range(20)]); o["symmetric_vs_neutral"].setdefault(nm, {})[s_] = d
            line += f" | {s_[:6]} {d[0]:+5.1f} ({d[1]}/{d[2]}, {d[3]:.3f})"
        print(line)
    print("=== adopted alone against neutral: own gain over neutral's payoff in the neutral pair, seats pooled (40 pairs), then seat 1 (X-neutral) and seat 2 (neutral-X); partner's change pooled")
    for s_ in STRATS[1:]:
        line = f"{s_:14}"
        for nm in names:
            nn = [runs[nm][("neutral-neutral", i)] for i in range(20)]
            s1 = [runs[nm][(f"{s_}-neutral", i)]["payoff"]["p1"] - nn[i]["payoff"]["p1"] for i in range(20)]
            s2 = [runs[nm][(f"neutral-{s_}", i)]["payoff"]["p2"] - nn[i]["payoff"]["p2"] for i in range(20)]
            partner = [runs[nm][(f"{s_}-neutral", i)]["payoff"]["p2"] - nn[i]["payoff"]["p2"] for i in range(20)] + [runs[nm][(f"neutral-{s_}", i)]["payoff"]["p1"] - nn[i]["payoff"]["p1"] for i in range(20)]
            pooled = paired(s1 + s2); d1 = paired(s1); d2 = paired(s2); pp = paired(partner)
            o["unilateral"].setdefault(s_, {})[nm] = {"pooled": pooled, "seat1": d1, "seat2": d2, "partner": pp}
            line += f" | {nm[:7]}: {pooled[0]:+5.1f} ({pooled[1]}/{pooled[2]}, {pooled[3]:.3f}) s1 {d1[0]:+5.1f} s2 {d2[0]:+5.1f} partner {pp[0]:+5.1f}"
        print(line)
    if cond == "low_stakes_p2":
        print("=== seats: high-stakes side (p1, of 100) and low-stakes side (p2, of 30) own payoff by own strategy against neutral, and in the neutral pair")
        for nm in names:
            nn = [runs[nm][("neutral-neutral", i)] for i in range(20)]
            line = f"{nm:8} neutral pair p1 {st.mean(r['payoff']['p1'] for r in nn):5.1f} p2 {st.mean(r['payoff']['p2'] for r in nn):5.1f}"
            for s_ in STRATS[1:]:
                hi = st.mean(runs[nm][(f"{s_}-neutral", i)]["payoff"]["p1"] for i in range(20)); lo = st.mean(runs[nm][(f"neutral-{s_}", i)]["payoff"]["p2"] for i in range(20))
                o["seats"].setdefault(nm, {})[s_] = {"high_side": round(hi, 1), "low_side": round(lo, 1)}
                line += f" | {s_[:6]} hi {hi:5.1f} lo {lo:5.1f}"
            print(line)
    print("=== each arm's cell paired with its E2 twin (luna): joint difference (p)")
    for c in CELLS:
        line = f"{c:26}"
        for nm in names[1:]:
            d = paired([runs[nm][(c, i)]["joint"] - runs["luna_E2"][(c, i)]["joint"] for i in range(20)]); o["vs_e2_twin"].setdefault(c, {})[nm] = d
            line += f" | {nm}: {d[0]:+5.1f} ({d[3]:.2f})"
        print(line)
    print("=== compliance of each rule, seat 1 in its diagonal and against neutral (of 40) plus seat 2 against neutral (of 20): total of 60")
    for nm in names:
        line = f"{nm:8}"
        for s_ in STRATS[1:]:
            recs = [(runs[nm][(f"{s_}-{s_}", i)], "p1") for i in range(20)] + [(runs[nm][(f"{s_}-neutral", i)], "p1") for i in range(20)] + [(runs[nm][(f"neutral-{s_}", i)], "p2") for i in range(20)]
            k = sum(r["compliance"][a].get("overall") is True for r, a in recs); n = sum(r["compliance"][a].get("overall") is not None for r, a in recs)
            o["compliance"].setdefault(nm, {})[s_] = (k, n); line += f" | {s_[:6]} {k}/{n}"
        print(line)
Path("results/bargain_e3_conditions_paired.json").write_text(json.dumps(out, indent=1)); print("\nwrote results/bargain_e3_conditions_paired.json")
