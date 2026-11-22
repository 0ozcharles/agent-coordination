"""E3 conditions, the eight cells against competing: each reply to a competing partner against the neutral reply. Adds the vs_competing sections to results/bargain_e3_conditions_paired.json.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st
from pathlib import Path
STRATS = ["neutral", "competing", "collaborating", "compromising", "accommodating", "avoiding"]
ARMS = {"open_info": [("luna_E2", "e2/open_info"), ("sonnet", "e3_open_info_claude_sonnet_5_5"), ("sol", "e3_open_info_gpt_6_1_sol")],
        "low_stakes_p2": [("luna_E2", "e2/low_stakes_p2"), ("sonnet", "e3_low_stakes_p2_claude_sonnet_5_5"), ("sol", "e3_low_stakes_p2_gpt_6_1_sol")]}
N = 20

def load(run):
    out = {}
    for f in glob.glob(f"runs/bargain/{run}/games/*/*.json"):
        r = json.load(open(f))
        if not r.get("error"):
            out[(r["cell"], r["instance"]["id"])] = r
    return out

def sign_p(h, l):
    n = h + l
    if n == 0:
        return 1.0
    k = min(h, l)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)

def paired(d):
    if not d:
        return None
    h, l = sum(v > 0 for v in d), sum(v < 0 for v in d)
    return {"mean": round(st.mean(d), 1), "wins": h, "losses": l, "n": len(d), "p": round(sign_p(h, l), 3)}

def fmt(d):
    return "  n/a  " if d is None else f"{d['mean']:+5.1f} ({d['wins']}/{d['losses']}, {d['p']:.3f})"

def facing(g, s):
    """(record, seat of s) for every game where s faces competing."""
    if s == "competing":
        return [(g[("competing-competing", i)], a) for i in range(N) for a in ("p1", "p2") if ("competing-competing", i) in g]
    return [(g[(f"{s}-competing", i)], "p1") for i in range(N) if (f"{s}-competing", i) in g] + [(g[(f"competing-{s}", i)], "p2") for i in range(N) if (f"competing-{s}", i) in g]

def other(a): return "p2" if a == "p1" else "p1"

path = Path("results/bargain_e3_conditions_paired.json")
out = json.loads(path.read_text()) if path.is_file() else {}
for cond, arms in ARMS.items():
    runs = {nm: load(run) for nm, run in arms}
    o = out.setdefault(cond, {})["vs_competing"] = {"reply": {}, "vs_neutral_reply": {}, "joint_vs_competing_pair": {}, "seats": {}, "compliance": {}, "vs_e2_twin": {}, "cells": {}}
    print(f"\n################ {cond}")
    print("=== cells against competing per arm: joint | p1 | p2 | agree/20 | frontier | gap | moves | passes")
    for s in STRATS[2:]:
        for c in (f"{s}-competing", f"competing-{s}"):
            line = f"{c:26}"
            for nm, g in runs.items():
                recs = [g[(c, i)] for i in range(N) if (c, i) in g]
                if not recs:
                    continue
                e = {"n": len(recs), "joint": round(st.mean(r["joint"] for r in recs), 1), "p1": round(st.mean(r["payoff"]["p1"] for r in recs), 1),
                     "p2": round(st.mean(r["payoff"]["p2"] for r in recs), 1), "agree": sum(bool(r["agreement"]) for r in recs),
                     "frontier": sum(bool(r["on_frontier"]) for r in recs), "gap": round(st.mean(r["joint_gap"] for r in recs), 1),
                     "moves": round(st.mean(len(r["moves"]) for r in recs), 1), "passes": sum(m["action"] == "pass" for r in recs for m in r["moves"]),
                     "rejected": sum(len(r.get("rejected", [])) for r in recs), "cost": round(sum(r["cost_usd"] for r in recs), 3)}
                o["cells"].setdefault(c, {})[nm] = e
                line += f" | {nm[:7]}: {e['joint']:5.1f} {e['p1']:4.1f}/{e['p2']:4.1f} a{e['agree']:2d} f{e['frontier']:2d} g{e['gap']:4.1f} m{e['moves']:3.1f} p{e['passes']}"
            print(line)
    print("=== the reply to competing, seats pooled (40 games): own payoff of the replying strategy | the competing side's payoff | joint | agreements")
    for s in STRATS:
        line = f"{s:14}"
        for nm, g in runs.items():
            f_ = facing(g, s)
            if not f_:
                continue
            e = {"n": len(f_), "own": round(st.mean(r["payoff"][a] for r, a in f_), 1), "competing_side": round(st.mean(r["payoff"][other(a)] for r, a in f_), 1),
                 "joint": round(st.mean(r["joint"] for r, a in f_), 1), "agree": sum(bool(r["agreement"]) for r, a in f_), "moves": round(st.mean(len(r["moves"]) for r, a in f_), 1),
                 "tokens": round(st.mean(r["usage"][a]["prompt"] + r["usage"][a]["completion"] for r, a in f_))}
            o["reply"].setdefault(s, {})[nm] = e
            line += f" | {nm[:7]}: own {e['own']:5.1f} comp {e['competing_side']:5.1f} joint {e['joint']:5.1f} a{e['agree']:2d}/{e['n']}"
        print(line)
    print("=== reply X against the neutral reply, paired by instance and seat (40 pairs): own payoff change | competing side's change | joint change")
    for s in STRATS[1:]:
        line = f"{s:14}"
        for nm, g in runs.items():
            own, comp, joint = [], [], []
            for r, a in facing(g, s):
                base = g.get((f"neutral-competing", r["instance"]["id"])) if a == "p1" else g.get((f"competing-neutral", r["instance"]["id"]))
                if base is None:
                    continue
                own.append(r["payoff"][a] - base["payoff"][a]); comp.append(r["payoff"][other(a)] - base["payoff"][other(a)]); joint.append(r["joint"] - base["joint"])
            if not own:
                continue
            e = {"own": paired(own), "competing_side": paired(comp), "joint": paired(joint)}
            o["vs_neutral_reply"].setdefault(s, {})[nm] = e
            line += f" | {nm[:7]}: own {fmt(e['own'])} comp {fmt(e['competing_side'])} joint {fmt(e['joint'])}"
        print(line)
    print("=== joint payoff of competing against X minus the competing pair's, paired by instance (seats pooled, 40 pairs)")
    for s in STRATS[2:]:
        line = f"{s:14}"
        for nm, g in runs.items():
            d = [r["joint"] - g[("competing-competing", r["instance"]["id"])]["joint"] for r, a in facing(g, s) if ("competing-competing", r["instance"]["id"]) in g]
            if d:
                o["joint_vs_competing_pair"].setdefault(s, {})[nm] = paired(d); line += f" | {nm[:7]}: {fmt(paired(d))}"
        print(line)
    if cond == "low_stakes_p2":
        print("=== seats facing competing: the high-stakes side's own payoff (p1, of 100, in X-competing) and the low-stakes side's (p2, of 30, in competing-X), by its own strategy")
        for nm, g in runs.items():
            line = f"{nm:8}"
            for s in STRATS:
                hi_cell = "neutral-competing" if s == "neutral" else ("competing-competing" if s == "competing" else f"{s}-competing")
                lo_cell = "competing-neutral" if s == "neutral" else ("competing-competing" if s == "competing" else f"competing-{s}")
                hi = [g[(hi_cell, i)]["payoff"]["p1"] for i in range(N) if (hi_cell, i) in g]; lo = [g[(lo_cell, i)]["payoff"]["p2"] for i in range(N) if (lo_cell, i) in g]
                if hi and lo:
                    o["seats"].setdefault(nm, {})[s] = {"high_side": round(st.mean(hi), 1), "low_side": round(st.mean(lo), 1)}
                    line += f" | {s[:6]} hi {st.mean(hi):5.1f} lo {st.mean(lo):5.1f}"
            print(line)
    print("=== compliance of the replying rule when facing competing (of 40) and of the competing rule facing each reply (of 40)")
    for nm, g in runs.items():
        line = f"{nm:8}"
        for s in STRATS[2:]:
            f_ = facing(g, s)
            k = sum(r["compliance"][a].get("overall") is True for r, a in f_); n = sum(r["compliance"][a].get("overall") is not None for r, a in f_)
            kc = sum(r["compliance"][other(a)].get("overall") is True for r, a in f_); nc = sum(r["compliance"][other(a)].get("overall") is not None for r, a in f_)
            o["compliance"].setdefault(nm, {})[s] = {"reply": [k, n], "competing": [kc, nc]}; line += f" | {s[:6]} {k}/{n} (comp {kc}/{nc})"
        print(line)
    print("=== each cell against competing paired with its E2 twin (luna): joint difference")
    for s in STRATS[2:]:
        for c in (f"{s}-competing", f"competing-{s}"):
            line = f"{c:26}"
            for nm, g in runs.items():
                if nm == "luna_E2":
                    continue
                d = [g[(c, i)]["joint"] - runs["luna_E2"][(c, i)]["joint"] for i in range(N) if (c, i) in g and (c, i) in runs["luna_E2"]]
                if d:
                    o["vs_e2_twin"].setdefault(c, {})[nm] = paired(d); line += f" | {nm}: {fmt(paired(d))}"
            print(line)
path.write_text(json.dumps(out, indent=1)); print(f"\nwrote {path}")
