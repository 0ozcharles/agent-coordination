"""The grounded layer on claude-haiku-5-5 against luna's grounded run. Writes results/bargain_grounded_haiku_paired.json.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st
from pathlib import Path
STRATS = ["neutral", "competing", "collaborating", "compromising", "accommodating", "avoiding"]
CELLS = [f"{s}-{s}" for s in STRATS] + [f"{s}-neutral" for s in STRATS[1:]] + [f"neutral-{s}" for s in STRATS[1:]]
RUNS = {"luna": "grounded", "haiku": "grounded_claude_haiku_5_5"}

def load(run):
    out = {}
    for f in glob.glob(f"runs/bargain/{run}/games/*/*.json"):
        r = json.load(open(f))
        if r["cell"] in CELLS and not r.get("error"):
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
    return "  n/a  " if d is None else f"{d['mean']:+5.1f} ({d['wins']}/{d['losses']}, {d['p']:.3f}, n={d['n']})"

def other(a): return "p2" if a == "p1" else "p1"

runs = {m: load(r) for m, r in RUNS.items()}
ids = sorted({i for (c, i) in runs["haiku"]}) or sorted({i for (c, i) in runs["luna"]})
contested = sorted({i for (c, i), r in runs["luna"].items() if all(r["instance"]["opposed"])})
print("instances", len(ids), "all contested", len(contested))
out = {"cells": {}, "neutral_pair": {}, "symmetric": {}, "unilateral": {}, "vs_luna_twin": {}, "compliance": {}, "tokens": {}, "all_contested": {}}
print("=== per cell and model: joint | p1 | p2 | agree | frontier | gap | moves | tokens per agent | passes")
for c in CELLS:
    line = f"{c:26}"
    for m, g in runs.items():
        recs = [g[(c, i)] for i in ids if (c, i) in g]
        if not recs:
            continue
        e = {"n": len(recs), "joint": round(st.mean(r["joint"] for r in recs), 1), "p1": round(st.mean(r["payoff"]["p1"] for r in recs), 1),
             "p2": round(st.mean(r["payoff"]["p2"] for r in recs), 1), "agree": sum(bool(r["agreement"]) for r in recs), "frontier": sum(bool(r["on_frontier"]) for r in recs),
             "gap": round(st.mean(r["joint_gap"] for r in recs), 1), "moves": round(st.mean(len(r["moves"]) for r in recs), 1),
             "tokens": round(st.mean((r["usage"][a]["prompt"] + r["usage"][a]["completion"]) for r in recs for a in ("p1", "p2"))),
             "passes": sum(mv["action"] == "pass" for r in recs for mv in r["moves"]), "rejected": sum(len(r.get("rejected", [])) for r in recs), "cost": round(sum(r["cost_usd"] for r in recs), 3)}
        out["cells"].setdefault(c, {})[m] = e
        line += f" | {m}: {e['joint']:5.1f} {e['p1']:4.1f}/{e['p2']:4.1f} a{e['agree']:2d} f{e['frontier']:2d} g{e['gap']:4.1f} m{e['moves']:3.1f} t{e['tokens']:5d} p{e['passes']}"
    print(line)
print("=== symmetric pairs against the neutral pair (joint, paired by instance)")
for m, g in runs.items():
    line = f"{m:6}"
    for s in STRATS[1:]:
        d = paired([g[(f"{s}-{s}", i)]["joint"] - g[("neutral-neutral", i)]["joint"] for i in ids if (f"{s}-{s}", i) in g and ("neutral-neutral", i) in g])
        out["symmetric"].setdefault(m, {})[s] = d; line += f" | {s[:6]} {fmt(d)}"
    print(line)
print("=== adopted alone against neutral, seats pooled: own gain | partner's change | joint change")
for s in STRATS[1:]:
    line = f"{s:14}"
    for m, g in runs.items():
        own, part, joint = [], [], []
        for i in ids:
            nn = g.get(("neutral-neutral", i))
            if nn is None:
                continue
            for cell, a in ((f"{s}-neutral", "p1"), (f"neutral-{s}", "p2")):
                r = g.get((cell, i))
                if r is not None:
                    own.append(r["payoff"][a] - nn["payoff"][a]); part.append(r["payoff"][other(a)] - nn["payoff"][other(a)]); joint.append(r["joint"] - nn["joint"])
        e = {"own": paired(own), "partner": paired(part), "joint": paired(joint)}
        out["unilateral"].setdefault(s, {})[m] = e
        line += f" | {m}: own {fmt(e['own'])} partner {fmt(e['partner'])} joint {fmt(e['joint'])}"
    print(line)
print("=== haiku's cell paired with luna's grounded twin: joint difference, and the default pair's own payoffs")
for c in CELLS:
    d = paired([runs["haiku"][(c, i)]["joint"] - runs["luna"][(c, i)]["joint"] for i in ids if (c, i) in runs["haiku"] and (c, i) in runs["luna"]])
    out["vs_luna_twin"][c] = d
    print(f"{c:26} {fmt(d)}")
nnh = [runs["haiku"][("neutral-neutral", i)] for i in ids if ("neutral-neutral", i) in runs["haiku"]]
if nnh:
    out["neutral_pair"]["haiku"] = {"n": len(nnh), "joint": round(st.mean(r["joint"] for r in nnh), 1), "frontier": sum(bool(r["on_frontier"]) for r in nnh),
                                    "agree": sum(bool(r["agreement"]) for r in nnh), "gap": round(st.mean(r["joint_gap"] for r in nnh), 1), "moves": round(st.mean(len(r["moves"]) for r in nnh), 1),
                                    "asks": sum(bool(r["metrics"][a]["asks"]) for r in nnh for a in ("p1", "p2")), "discloses": sum(bool(r["metrics"][a]["discloses"]) for r in nnh for a in ("p1", "p2")),
                                    "max_joint": round(st.mean(r["max_joint"] for r in nnh), 1)}
    print("haiku default pair:", out["neutral_pair"]["haiku"])
print("=== the all-contested instances: symmetric pairs and unilateral collaborating/competing against neutral (haiku)")
g = runs["haiku"]
for s in STRATS[1:]:
    d = paired([g[(f"{s}-{s}", i)]["joint"] - g[("neutral-neutral", i)]["joint"] for i in contested if (f"{s}-{s}", i) in g and ("neutral-neutral", i) in g])
    own = []
    for i in contested:
        nn = g.get(("neutral-neutral", i))
        for cell, a in ((f"{s}-neutral", "p1"), (f"neutral-{s}", "p2")):
            r = g.get((cell, i))
            if r is not None and nn is not None:
                own.append(r["payoff"][a] - nn["payoff"][a])
    out["all_contested"][s] = {"symmetric": d, "alone": paired(own)}
    print(f"{s:14} pair {fmt(d)} alone {fmt(paired(own))}")
print("=== compliance of each rule (seat with the rule: its pair and both unilateral cells) and tokens per agent and game in symmetric play")
for m, g in runs.items():
    line = f"{m:6}"
    for s in STRATS[1:]:
        k = n = 0
        for (c, i), r in g.items():
            for a in ("p1", "p2"):
                if r["strategies"][a] == s and r["strategies"][other(a)] in ("neutral", s):
                    v = r["compliance"][a].get("overall"); n += v is not None; k += v is True
        out["compliance"].setdefault(m, {})[s] = [k, n]; line += f" | {s[:6]} {k}/{n}"
    print(line)
    line = f"{m:6} tokens"
    for s in STRATS:
        recs = [r for (c, i), r in g.items() if c == f"{s}-{s}"]
        if recs:
            t = round(st.mean(r["usage"][a]["prompt"] + r["usage"][a]["completion"] for r in recs for a in ("p1", "p2")))
            out["tokens"].setdefault(m, {})[s] = t; line += f" | {s[:6]} {t}"
    print(line)
out["games"] = {m: len(g) for m, g in runs.items()}; out["cost_usd"] = {m: round(sum(r["cost_usd"] for r in g.values()), 2) for m, g in runs.items()}
print("games", out["games"], "cost", out["cost_usd"])
Path("results/bargain_grounded_haiku_paired.json").write_text(json.dumps(out, indent=1)); print("wrote results/bargain_grounded_haiku_paired.json")
