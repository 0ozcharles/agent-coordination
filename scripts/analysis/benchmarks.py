"""CaSiNo, LLM-Deliberation and Deal or No Deal: symmetric pairs and unilateral deviations against the default pair, by priority structure and number of contested decisions. Writes results/bargain_benchmarks_paired.json.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st, collections
from pathlib import Path
STRATS = ["neutral", "competing", "collaborating", "compromising", "accommodating", "avoiding"]
CELLS = [f"{s}-{s}" for s in STRATS] + [f"{s}-neutral" for s in STRATS[1:]] + [f"neutral-{s}" for s in STRATS[1:]]
def sign_p(h, l):
    n = h + l
    if n == 0: return 1.0
    k = min(h, l); return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
def paired(d):
    h, l = sum(v > 0 for v in d), sum(v < 0 for v in d); return round(st.mean(d), 1), h, l, round(sign_p(h, l), 3)
def tokens(r): return sum(r["usage"][a]["prompt"] + r["usage"][a]["completion"] for a in ("p1", "p2"))
out = {}
for run in ("casino", "deliberation", "dond"):
    recs = {}
    for f in glob.glob(f"runs/bargain/{run}/games/*/*.json"):
        r = json.load(open(f)); recs[(r["cell"], r["instance"]["id"])] = r
    ids = sorted({i for _, i in recs}); n = len(ids)
    if not ids: continue  # a run that has not been played
    errs = sum(1 for r in recs.values() if r.get("error")); cost = sum(r.get("cost_usd") or 0 for r in recs.values())
    ok = [r for r in recs.values() if not r.get("error")]
    calls = sum(r["usage"][a]["calls"] for r in ok for a in ("p1", "p2"))
    print(f"\n################ {run}: {len(recs)} games, {n} instances, errors {errs}, cost ${cost:.2f}, rejected {sum(r['invalid_replies'] for r in ok)}/{calls}, passes {sum(m['action']=='pass' for r in ok for m in r['moves'])}")
    o = out[run] = {"n": n, "cost": round(cost, 2), "cells": {}, "symmetric_vs_neutral": {}, "unilateral": {}, "compliance": {}, "groups": {}}
    # groups of instances
    inst0 = {i: recs[("neutral-neutral", i)]["instance"] for i in ids}
    if run == "casino":
        groups = {"identical": [i for i in ids if inst0[i]["priorities"]["p1"] == inst0[i]["priorities"]["p2"]],
                  "reversed": [i for i in ids if inst0[i]["priorities"]["p1"] == inst0[i]["priorities"]["p2"][::-1]]}
        groups["mixed"] = [i for i in ids if i not in groups["identical"] and i not in groups["reversed"]]
    else:
        groups = collections.defaultdict(list)
        for i in ids: groups[f"contested={sum(inst0[i]['opposed'])}"].append(i)
        groups = dict(sorted(groups.items()))
    print("groups:", {g: len(v) for g, v in groups.items()})
    print("=== per cell: joint | p1/p2 | agree | frontier | gap | moves | tokens | p1 compliance")
    for c in CELLS:
        rs = [recs[(c, i)] for i in ids]
        s = {"joint": round(st.mean(r["joint"] for r in rs), 1), "p1": round(st.mean(r["payoff"]["p1"] for r in rs), 1), "p2": round(st.mean(r["payoff"]["p2"] for r in rs), 1),
             "agree": sum(bool(r["agreement"]) for r in rs), "frontier": sum(bool(r["on_frontier"]) for r in rs), "gap": round(st.mean(r["joint_gap"] for r in rs), 1),
             "moves": round(st.mean(len(r["moves"]) for r in rs), 2), "tokens": round(st.mean(tokens(r) for r in rs)),
             "p1_comply": sum(r["compliance"]["p1"].get("overall") is True for r in rs), "p1_known": sum(r["compliance"]["p1"].get("overall") is not None for r in rs)}
        o["cells"][c] = s
        print(f"  {c:26} {s['joint']:6.1f} | {s['p1']:5.1f}/{s['p2']:5.1f} | {s['agree']:2d}/{n} | {s['frontier']:2d} | {s['gap']:5.1f} | {s['moves']:4.2f} | {s['tokens']:5d} | {s['p1_comply']}/{s['p1_known']}")
    base = {i: recs[("neutral-neutral", i)] for i in ids}
    maxj = st.mean(base[i]["max_joint"] for i in ids)
    print(f"neutral pair: joint {o['cells']['neutral-neutral']['joint']} of a maximal {maxj:.1f}; outside options {base[ids[0]]['instance']['outside']}")
    print("=== symmetric pairs vs the neutral pair (joint, paired by instance), all instances then by group")
    for g, members in [("all", ids)] + list(groups.items()):
        line = f"{g:14}"
        for s_ in STRATS[1:]:
            d = paired([recs[(f"{s_}-{s_}", i)]["joint"] - base[i]["joint"] for i in members]); o["symmetric_vs_neutral"].setdefault(g, {})[s_] = d
            line += f" | {s_[:6]} {d[0]:+5.1f} ({d[1]}/{d[2]}, {d[3]:.3f})"
        print(line)
    print("=== adopted alone against neutral: own gain pooled over seats (2n pairs), seat 1, seat 2, partner's change; then by group (pooled)")
    for s_ in STRATS[1:]:
        s1 = [recs[(f"{s_}-neutral", i)]["payoff"]["p1"] - base[i]["payoff"]["p1"] for i in ids]
        s2 = [recs[(f"neutral-{s_}", i)]["payoff"]["p2"] - base[i]["payoff"]["p2"] for i in ids]
        partner = [recs[(f"{s_}-neutral", i)]["payoff"]["p2"] - base[i]["payoff"]["p2"] for i in ids] + [recs[(f"neutral-{s_}", i)]["payoff"]["p1"] - base[i]["payoff"]["p1"] for i in ids]
        pooled, d1, d2, pp = paired(s1 + s2), paired(s1), paired(s2), paired(partner)
        o["unilateral"][s_] = {"pooled": pooled, "seat1": d1, "seat2": d2, "partner": pp, "groups": {}}
        line = f"{s_:14} {pooled[0]:+5.1f} ({pooled[1]}/{pooled[2]}, {pooled[3]:.3f}) s1 {d1[0]:+5.1f} s2 {d2[0]:+5.1f} partner {pp[0]:+5.1f} |"
        for g, members in groups.items():
            gd = paired([recs[(f"{s_}-neutral", i)]["payoff"]["p1"] - base[i]["payoff"]["p1"] for i in members] + [recs[(f"neutral-{s_}", i)]["payoff"]["p2"] - base[i]["payoff"]["p2"] for i in members])
            o["unilateral"][s_]["groups"][g] = gd; line += f" {g[:10]} {gd[0]:+5.1f} ({gd[3]:.2f})"
        print(line)
    print("=== compliance of each rule: seat 1 in its diagonal and against neutral, seat 2 against neutral (of 3n)")
    line = ""
    for s_ in STRATS[1:]:
        rs = [(recs[(f"{s_}-{s_}", i)], "p1") for i in ids] + [(recs[(f"{s_}-neutral", i)], "p1") for i in ids] + [(recs[(f"neutral-{s_}", i)], "p2") for i in ids]
        k = sum(r["compliance"][a].get("overall") is True for r, a in rs); kn = sum(r["compliance"][a].get("overall") is not None for r, a in rs)
        o["compliance"][s_] = (k, kn); line += f" | {s_[:6]} {k}/{kn}"
    print(line)
    nn = [base[i] for i in ids]
    print("=== neutral pair process: discloses", sum(r["metrics"][a]["discloses"] for r in nn for a in ("p1","p2")), f"/{2*n}; asks", sum(r["metrics"][a]["asks"] for r in nn for a in ("p1","p2")), f"/{2*n}; first-proposal value to self {st.mean(r['metrics'][a]['first'] for r in nn for a in ('p1','p2') if r['metrics'][a]['first'] is not None):.1f}; walks {sum(r['ended_by'].endswith('walked') for r in nn)}")
    if run == "deliberation":
        # the original thresholds: which agreements would both parties have accepted under the six-party rule?
        passes = collections.Counter(); totals = collections.Counter()
        for c in CELLS:
            for i in ids:
                r = recs[(c, i)]
                if r["agreement"]:
                    totals[c] += 1; thr = r["instance"]["thresholds"]
                    passes[c] += (r["payoff"]["p1"] >= thr["p1"] and r["payoff"]["p2"] >= thr["p2"])
        o["thresholds"] = {c: (passes[c], totals[c]) for c in CELLS}
        print("=== agreements clearing both original thresholds:", {c: f"{passes[c]}/{totals[c]}" for c in ("neutral-neutral", "collaborating-collaborating", "competing-competing", "accommodating-accommodating")}, "| all cells:", sum(passes.values()), "/", sum(totals.values()))
        # frontier by group
        print("=== neutral pair frontier rate by contested count:", {g: f"{sum(bool(base[i]['on_frontier']) for i in m)}/{len(m)}" for g, m in groups.items()})
    else:
        print("=== neutral pair by group: joint / frontier:", {g: f"{st.mean(base[i]['joint'] for i in m):.1f} / {sum(bool(base[i]['on_frontier']) for i in m)}/{len(m)}" for g, m in groups.items()})
        lows = [min(r["payoff"].values()) for r in nn]; print("    lowest side's payoff in the neutral pair: mean", round(st.mean(lows), 1), "| agreements worth under 10 to one side:", sum(1 for r in nn if r["agreement"] and min(r["agreement"]["values"].values()) < 10))
Path("results/bargain_benchmarks_paired.json").write_text(json.dumps(out, indent=1)); print("\nwrote results/bargain_benchmarks_paired.json")
