"""E4 framings: the three skins against their abstract twins in E1, the diagonal and the two unilateral deviations, and the agreed options by position. Writes results/bargain_e4_paired.json.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st
from pathlib import Path
from collections import defaultdict

SKINS = ["software_design", "team_plan", "release_plan"]
CELLS = ["neutral-neutral", "competing-competing", "collaborating-collaborating", "compromising-compromising",
         "accommodating-accommodating", "avoiding-avoiding", "competing-neutral", "collaborating-neutral"]

def load(run):
    out = {}
    for f in glob.glob(f"runs/bargain/{run}/games/*/*.json"):
        r = json.load(open(f))
        if r["cell"] in CELLS and not r.get("error"):
            out[(r["cell"], r["instance"]["id"])] = r
    return out

def sign_p(h, l):
    n = h + l
    if n == 0: return 1.0
    k = min(h, l)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)

def tokens(r):
    return sum(r["usage"][a]["prompt"] + r["usage"][a]["completion"] for a in ("p1", "p2"))

def stats(recs):
    n = len(recs)
    return {"n": n, "joint": st.mean(r["joint"] for r in recs), "p1": st.mean(r["payoff"]["p1"] for r in recs),
            "p2": st.mean(r["payoff"]["p2"] for r in recs), "agree": sum(1 for r in recs if r["agreement"]),
            "frontier": sum(1 for r in recs if r["on_frontier"]), "gap": st.mean(r["joint_gap"] for r in recs),
            "moves": st.mean(len(r["moves"]) for r in recs), "tokens": st.mean(tokens(r) for r in recs),
            "p1_comply": sum(1 for r in recs if r["compliance"]["p1"].get("overall") is True),
            "p1_known": sum(1 for r in recs if r["compliance"]["p1"].get("overall") is not None),
            "disclose": sum(r["metrics"][a]["discloses"] for r in recs for a in ("p1", "p2")),
            "asks": sum(r["metrics"][a]["asks"] for r in recs for a in ("p1", "p2"))}

def paired(a, b, key):
    d = [key(x) - key(y) for x, y in zip(a, b)]
    h, l = sum(1 for v in d if v > 0), sum(1 for v in d if v < 0)
    return st.mean(d), h, l, sign_p(h, l)

runs = {"abstract": load("e1")}
for s in SKINS: runs[s] = load(f"e4_{s}")
names = ["abstract"] + SKINS
out = {"cells": {}, "vs_abstract": {}, "diagonal_vs_neutral": {}, "unilateral": {}, "options": {}}

print("=== Per cell and rendering: joint | p1 | p2 | agree/40 | frontier | gap | moves | tokens | p1 compliance | discloses/80 | asks/80")
for c in CELLS:
    print(c)
    for nm in names:
        recs = [runs[nm][(c, i)] for i in range(40) if (c, i) in runs[nm]]
        s = stats(recs); out["cells"].setdefault(c, {})[nm] = s
        print(f"  {nm:16} {s['joint']:6.1f} | {s['p1']:5.1f} | {s['p2']:5.1f} | {s['agree']:2d} | {s['frontier']:2d} | {s['gap']:5.1f} | {s['moves']:4.2f} | {s['tokens']:6.0f} | {s['p1_comply']}/{s['p1_known']} | {s['disclose']:2d} | {s['asks']:2d}")

print("\n=== Each skin's cell paired by instance with its abstract twin (E1): mean difference, wins/losses, sign p; joint then p1 own")
for c in CELLS:
    line = f"{c:28}"
    for s in SKINS:
        a = [runs[s][(c, i)] for i in range(40)]; b = [runs["abstract"][(c, i)] for i in range(40)]
        dj = paired(a, b, lambda r: r["joint"]); d1 = paired(a, b, lambda r: r["payoff"]["p1"]); dt = paired(a, b, tokens)
        out["vs_abstract"].setdefault(c, {})[s] = {"joint": dj, "p1": d1, "tokens": dt}
        line += f" | {s[:8]}: joint {dj[0]:+5.1f} ({dj[1]}/{dj[2]}, p={dj[3]:.2f}) p1 {d1[0]:+5.1f} (p={d1[3]:.2f})"
    print(line)

print("\n=== Diagonal pairs against neutral-neutral within each rendering: joint difference paired by instance (sign p)")
diag = CELLS[1:6]
for nm in names:
    base = [runs[nm][("neutral-neutral", i)] for i in range(40)]
    line = f"{nm:16}"
    order = []
    for c in diag:
        a = [runs[nm][(c, i)] for i in range(40)]
        dj = paired(a, base, lambda r: r["joint"]); out["diagonal_vs_neutral"].setdefault(nm, {})[c] = dj
        line += f" | {c.split('-')[0][:6]} {dj[0]:+5.1f} (p={dj[3]:.3f})"
    ranking = sorted(CELLS[:6], key=lambda c: -out["cells"][c][nm]["joint"])
    print(line, "\n   order by joint:", " > ".join(c.split('-')[0] for c in ranking))

print("\n=== Unilateral deviation against a neutral partner: p1's own payoff minus p1's in neutral-neutral, paired by instance")
for c in ("competing-neutral", "collaborating-neutral"):
    line = f"{c:22}"
    for nm in names:
        a = [runs[nm][(c, i)] for i in range(40)]; base = [runs[nm][("neutral-neutral", i)] for i in range(40)]
        d1 = paired(a, base, lambda r: r["payoff"]["p1"]); d2 = paired(a, base, lambda r: r["payoff"]["p2"]); dj = paired(a, base, lambda r: r["joint"])
        out["unilateral"].setdefault(c, {})[nm] = {"own": d1, "partner": d2, "joint": dj}
        line += f" | {nm[:8]}: own {d1[0]:+5.1f} (p={d1[3]:.3f}) partner {d2[0]:+5.1f} joint {dj[0]:+5.1f}"
    print(line)

print("\n=== Semantic pull: share of agreements choosing option A/B/C on each issue, pooled over the 8 cells (abstract twin in brackets); same-design share")
skins = json.load(open("configs/bargain_skins.json"))
for s in SKINS:
    same = tot = 0
    counts = defaultdict(lambda: [0, 0, 0]); base_counts = defaultdict(lambda: [0, 0, 0])
    for key, r in runs[s].items():
        b = runs["abstract"].get(key)
        if r["agreement"] and b and b["agreement"]:
            tot += 1; same += r["agreement"]["design"] == b["agreement"]["design"]
        if r["agreement"]:
            for i, o in enumerate(r["agreement"]["design"]): counts[i][o] += 1
        if b and b["agreement"]:
            for i, o in enumerate(b["agreement"]["design"]): base_counts[i][o] += 1
    print(f"{s}: same design as the abstract twin in {same}/{tot} agreements")
    for i in range(4):
        n = sum(counts[i]); nb = sum(base_counts[i])
        names_i = skins[s]["issues"][i]["options"]
        print("   " + skins[s]["issues"][i]["name"] + ": " + ", ".join(f"{chr(65+j)} {counts[i][j]/n:.2f} [{base_counts[i][j]/nb:.2f}] ({names_i[j]})" for j in range(3)))
    out["options"][s] = {"same": same, "total": tot, "counts": dict(counts), "abstract": dict(base_counts)}

final = {"study": "bargain_e4", "renderings": ["abstract"] + SKINS, "cells": out["cells"], "vs_abstract_twin": out["vs_abstract"],
         "diagonal_vs_neutral": out["diagonal_vs_neutral"], "unilateral_vs_neutral": out["unilateral"], "agreed_options": out["options"],
         "note": "paired by instance: mean difference, wins, losses, exact two-sided sign p; tokens are prompt plus completion of both agents per game"}
Path("results/bargain_e4_paired.json").write_text(json.dumps(final, indent=1, default=str))
print("wrote results/bargain_e4_paired.json")
