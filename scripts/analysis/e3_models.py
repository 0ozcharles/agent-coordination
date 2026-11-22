"""E3 models: E1's eight reading cells on four arms, each paired with its own neutral pair and with its E1 twin. Writes results/bargain_e3_paired.json and results/bargain_e3_models.svg.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st
from pathlib import Path
ARMS = [("luna_0.7_E1", "e1"), ("luna_t1", "e3_gpt_6_luna_t1"), ("haiku_5_5", "e3_claude_haiku_5_5"), ("sonnet_5_5", "e3_claude_sonnet_5_5"), ("sol_6_1", "e3_gpt_6_1_sol")]
CELLS = ["neutral-neutral", "competing-competing", "collaborating-collaborating", "compromising-compromising",
         "accommodating-accommodating", "avoiding-avoiding", "competing-neutral", "collaborating-neutral"]
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
def tokens(r): return sum(r["usage"][a]["prompt"] + r["usage"][a]["completion"] for a in ("p1", "p2"))
def paired(a, b, key):
    d = [key(x) - key(y) for x, y in zip(a, b)]; h, l = sum(v > 0 for v in d), sum(v < 0 for v in d)
    return round(st.mean(d), 1), h, l, round(sign_p(h, l), 3)
runs = {name: load(run) for name, run in ARMS}
names = [n for n, _ in ARMS]
out = {"arms": names, "cells": {}, "diagonal_vs_neutral": {}, "unilateral": {}, "vs_e1_twin": {}, "compliance": {}, "process": {}}
print("=== per cell and arm: joint | p1 | p2 | agree/40 | frontier | gap | moves | tokens/game | p1 compliance | passes")
for c in CELLS:
    print(c)
    for nm in names:
        recs = [runs[nm][(c, i)] for i in range(40) if (c, i) in runs[nm]]
        s = {"n": len(recs), "joint": round(st.mean(r["joint"] for r in recs), 1), "p1": round(st.mean(r["payoff"]["p1"] for r in recs), 1),
             "p2": round(st.mean(r["payoff"]["p2"] for r in recs), 1), "agree": sum(bool(r["agreement"]) for r in recs),
             "frontier": sum(bool(r["on_frontier"]) for r in recs), "gap": round(st.mean(r["joint_gap"] for r in recs), 1),
             "moves": round(st.mean(len(r["moves"]) for r in recs), 2), "tokens": round(st.mean(tokens(r) for r in recs)),
             "p1_comply": sum(r["compliance"]["p1"].get("overall") is True for r in recs), "p1_known": sum(r["compliance"]["p1"].get("overall") is not None for r in recs),
             "passes": sum(m["action"] == "pass" for r in recs for m in r["moves"]), "walks": sum(r["ended_by"].endswith("walked") for r in recs)}
        out["cells"].setdefault(c, {})[nm] = s
        print(f"  {nm:12} {s['joint']:6.1f} | {s['p1']:5.1f} | {s['p2']:5.1f} | {s['agree']:2d} | {s['frontier']:2d} | {s['gap']:5.1f} | {s['moves']:4.2f} | {s['tokens']:5d} | {s['p1_comply']}/{s['p1_known']} | {s['passes']}")
print("\n=== diagonal pairs against the neutral pair within each arm: joint difference (wins/losses, sign p); order by joint")
for nm in names:
    base = [runs[nm][("neutral-neutral", i)] for i in range(40)]; line = f"{nm:12}"
    for c in CELLS[1:6]:
        d = paired([runs[nm][(c, i)] for i in range(40)], base, lambda r: r["joint"]); out["diagonal_vs_neutral"].setdefault(nm, {})[c] = d
        line += f" | {c.split('-')[0][:6]} {d[0]:+5.1f} ({d[1]}/{d[2]}, p={d[3]:.3f})"
    order = sorted(CELLS[:6], key=lambda c: -out["cells"][c][nm]["joint"])
    print(line, "\n    order:", " > ".join(c.split("-")[0] for c in order))
print("\n=== unilateral deviation against neutral: p1's own payoff minus p1's in the neutral pair (sign p); partner; joint")
for c in ("competing-neutral", "collaborating-neutral"):
    line = f"{c:22}"
    for nm in names:
        a = [runs[nm][(c, i)] for i in range(40)]; base = [runs[nm][("neutral-neutral", i)] for i in range(40)]
        own = paired(a, base, lambda r: r["payoff"]["p1"]); partner = paired(a, base, lambda r: r["payoff"]["p2"]); joint = paired(a, base, lambda r: r["joint"])
        out["unilateral"].setdefault(c, {})[nm] = {"own": own, "partner": partner, "joint": joint}
        line += f" | {nm[:10]}: own {own[0]:+5.1f} (p={own[3]:.3f}) partner {partner[0]:+5.1f} joint {joint[0]:+5.1f}"
    print(line)
print("\n=== each arm's cell paired by instance with its E1 twin (luna 0.7): joint diff (sign p), p1 own diff, tokens diff")
for c in CELLS:
    line = f"{c:28}"
    for nm in names[1:]:
        a = [runs[nm][(c, i)] for i in range(40)]; b = [runs["luna_0.7_E1"][(c, i)] for i in range(40)]
        dj = paired(a, b, lambda r: r["joint"]); d1 = paired(a, b, lambda r: r["payoff"]["p1"]); dt = paired(a, b, tokens)
        out["vs_e1_twin"].setdefault(c, {})[nm] = {"joint": dj, "p1": d1, "tokens": dt}
        line += f" | {nm[:10]}: {dj[0]:+5.1f} (p={dj[3]:.2f}) own {d1[0]:+5.1f} tok {dt[0]:+6.0f}"
    print(line)
print("\n=== compliance of each rule (seat 1 in its diagonal cell and, for competing and collaborating, against neutral too), out of 40 / 80")
for nm in names:
    line = f"{nm:12}"
    for s in ["competing", "collaborating", "compromising", "accommodating", "avoiding"]:
        cells = [f"{s}-{s}"] + ([f"{s}-neutral"] if f"{s}-neutral" in CELLS else [])
        recs = [runs[nm][(c, i)] for c in cells for i in range(40)]
        k = sum(r["compliance"]["p1"].get("overall") is True for r in recs); n = sum(r["compliance"]["p1"].get("overall") is not None for r in recs)
        out["compliance"].setdefault(nm, {})[s] = (k, n); line += f" | {s[:6]} {k}/{n}"
    print(line)
print("\n=== process in the neutral pair: discloses (of 80 agents), asks, mean first-proposal value to self, mean messages chars; rejected replies per arm")
for nm in names:
    recs = [runs[nm][("neutral-neutral", i)] for i in range(40)]
    disc = sum(r["metrics"][a]["discloses"] for r in recs for a in ("p1", "p2")); asks = sum(r["metrics"][a]["asks"] for r in recs for a in ("p1", "p2"))
    first = st.mean(r["metrics"][a]["first"] for r in recs for a in ("p1", "p2") if r["metrics"][a]["first"] is not None)
    chars = st.mean(r["metrics"][a]["chars"] for r in recs for a in ("p1", "p2"))
    allr = list(runs[nm].values()); rej = sum(r.get("invalid_replies", 0) for r in allr); calls = sum(r["usage"][a]["calls"] for r in allr for a in ("p1", "p2"))
    out["process"][nm] = {"discloses": disc, "asks": asks, "first": round(first, 1), "chars": round(chars), "rejected": rej, "calls": calls}
    print(f"{nm:12} discloses {disc:2d}/80 asks {asks:2d}/80 first {first:5.1f} chars {chars:4.0f} | rejected {rej}/{calls}")
Path("results/bargain_e3_paired.json").write_text(json.dumps(out, indent=1))
# chart: joint payoff by cell under the five arms
pal = {"luna_0.7_E1": "#7f8c8d", "luna_t1": "#bdc3c7", "haiku_5_5": "#e67e22", "sonnet_5_5": "#c0392b", "sol_6_1": "#2980b9"}
x0, y0, gw, bw, H0 = 170, 50, 130, 22, 260; W = x0 + gw * len(CELLS) + 20; H = y0 + H0 + 90; lo, hi = 60, 150
def sy(v): return y0 + H0 - (v - lo) / (hi - lo) * H0
s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Helvetica, Arial, sans-serif" font-size="11">', f'<rect width="{W}" height="{H}" fill="white"/>',
     f'<text x="{x0}" y="22" font-size="14" font-weight="bold">Joint payoff by cell on five model settings (E1: gpt-6-luna at 0.7; E3: four arms at the default temperature), same 40 instances</text>']
for v in range(lo, hi + 1, 10):
    s.append(f'<line x1="{x0}" y1="{sy(v):.1f}" x2="{W - 20}" y2="{sy(v):.1f}" stroke="#eee"/><text x="{x0 - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v}</text>')
for j, c in enumerate(CELLS):
    gx = x0 + j * gw + 6
    for k, nm in enumerate(names):
        v = out["cells"][c][nm]["joint"]; x = gx + k * bw
        s.append(f'<rect x="{x}" y="{sy(v):.1f}" width="{bw - 3}" height="{y0 + H0 - sy(v):.1f}" fill="{pal[nm]}"/><text x="{x + (bw - 3) / 2:.1f}" y="{sy(v) - 3:.1f}" text-anchor="middle" font-size="8">{v:.0f}</text>')
    a1, a2 = c.split("-"); s.append(f'<text x="{gx + 2.5 * bw:.0f}" y="{y0 + H0 + 16}" text-anchor="middle">{a1[:6]}</text><text x="{gx + 2.5 * bw:.0f}" y="{y0 + H0 + 28}" text-anchor="middle">vs {a2[:6]}</text>')
s.append(f'<line x1="{x0}" y1="{y0 + H0}" x2="{W - 20}" y2="{y0 + H0}" stroke="#333"/>')
lx = x0
for nm in names:
    s.append(f'<rect x="{lx}" y="{H - 28}" width="12" height="12" fill="{pal[nm]}"/><text x="{lx + 16}" y="{H - 18}">{nm}</text>'); lx += 30 + 7 * len(nm)
s.append(f'<text x="{x0}" y="{H - 40}" font-size="10" fill="#555">Bars start at 60. Outside options sum to 60; the mean maximum joint value is about 141.</text></svg>')
Path("results/bargain_e3_models.svg").write_text("\n".join(s)); print("\nwrote results/bargain_e3_paired.json and results/bargain_e3_models.svg")
