"""Cross-model pairs (luna against sonnet): own gain of a rule by the adopter's model and the partner's model. Writes results/bargain_e3_cross_paired.json.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, math, statistics as st
from pathlib import Path
STRATS = ["neutral", "competing", "collaborating", "compromising", "accommodating", "avoiding"]
CELLS = [f"{s}-{s}" for s in STRATS] + [f"{s}-neutral" for s in STRATS[1:]] + [f"neutral-{s}" for s in STRATS[1:]]
ARMS = {"luna_sonnet": {"p1": "luna", "p2": "sonnet"}, "sonnet_luna": {"p1": "sonnet", "p2": "luna"}}
TWINS = {"luna": "e3_gpt_6_luna_t1", "sonnet": "e3_claude_sonnet_5_5"}
N = 40

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
    return "   n/a   " if d is None else f"{d['mean']:+5.1f} ({d['wins']}/{d['losses']}, {d['p']:.3f}, n={d['n']})"

def tok(r, a): return r["usage"][a]["prompt"] + r["usage"][a]["completion"]
def other(a): return "p2" if a == "p1" else "p1"

runs = {arm: load(f"e3_cross_{arm}") for arm in ARMS}
runs = {arm: g for arm, g in runs.items() if g}
twins = {m: load(run) for m, run in TWINS.items()}
seat_of = {arm: {m: a for a, m in seats.items()} for arm, seats in ARMS.items()}
out = {"arms": list(runs), "cells": {}, "neutral_pair": {}, "unilateral": {}, "same_model": {}, "symmetric": {}, "compliance": {}, "tokens": {}}

print("=== per cell and arm: joint | p1 | p2 | agree | frontier | gap | moves | out tokens p1/p2 | cost p1/p2 | passes")
for c in CELLS:
    line = f"{c:26}"
    for arm, g in runs.items():
        recs = [g[(c, i)] for i in range(N) if (c, i) in g]
        if not recs:
            continue
        s = {"n": len(recs), "joint": round(st.mean(r["joint"] for r in recs), 1), "p1": round(st.mean(r["payoff"]["p1"] for r in recs), 1),
             "p2": round(st.mean(r["payoff"]["p2"] for r in recs), 1), "agree": sum(bool(r["agreement"]) for r in recs),
             "frontier": sum(bool(r["on_frontier"]) for r in recs), "gap": round(st.mean(r["joint_gap"] for r in recs), 1),
             "moves": round(st.mean(len(r["moves"]) for r in recs), 1),
             "out_tokens": {a: round(st.mean(r["usage"][a]["completion"] for r in recs)) for a in ("p1", "p2")},
             "cost": {a: round(sum(r["usage"][a]["cost_usd"] for r in recs), 3) for a in ("p1", "p2")},
             "passes": sum(m["action"] == "pass" for r in recs for m in r["moves"]), "rejected": sum(len(r.get("rejected", [])) for r in recs)}
        out["cells"].setdefault(c, {})[arm] = s
        line += (f" | {arm}: {s['joint']:5.1f} {s['p1']:4.1f}/{s['p2']:4.1f} a{s['agree']:2d} f{s['frontier']:2d} g{s['gap']:4.1f} m{s['moves']:3.1f} "
                 f"o{s['out_tokens']['p1']}/{s['out_tokens']['p2']} ${s['cost']['p1']:.2f}/{s['cost']['p2']:.2f} p{s['passes']}")
    print(line)

print("\n=== the mixed default pair: luna's and sonnet's own payoff, the difference (sonnet - luna, paired by instance), and the joint against the same-model default pairs on the same instances")
for arm, g in runs.items():
    nn = {i: g[("neutral-neutral", i)] for i in range(N) if ("neutral-neutral", i) in g}
    L, Sn = seat_of[arm]["luna"], seat_of[arm]["sonnet"]
    e = {"n": len(nn), "luna": round(st.mean(r["payoff"][L] for r in nn.values()), 1), "sonnet": round(st.mean(r["payoff"][Sn] for r in nn.values()), 1),
         "sonnet_minus_luna": paired([r["payoff"][Sn] - r["payoff"][L] for r in nn.values()]),
         "joint": round(st.mean(r["joint"] for r in nn.values()), 1), "frontier": sum(bool(r["on_frontier"]) for r in nn.values()),
         "gap": round(st.mean(r["joint_gap"] for r in nn.values()), 1), "moves": round(st.mean(len(r["moves"]) for r in nn.values()), 1),
         "first_mover_luna_payoff": round(st.mean(r["payoff"][L] for r in nn.values() if r["instance"]["first_mover"] == L), 1),
         "second_mover_luna_payoff": round(st.mean(r["payoff"][L] for r in nn.values() if r["instance"]["first_mover"] != L), 1)}
    for m, tw in twins.items():
        e[f"joint_vs_{m}_pair"] = paired([nn[i]["joint"] - tw[("neutral-neutral", i)]["joint"] for i in nn if ("neutral-neutral", i) in tw])
    out["neutral_pair"][arm] = e
    print(f"{arm}: n={e['n']} luna {e['luna']} sonnet {e['sonnet']} diff {fmt(e['sonnet_minus_luna'])} joint {e['joint']} frontier {e['frontier']} gap {e['gap']} moves {e['moves']} "
          f"| luna first {e['first_mover_luna_payoff']} second {e['second_mover_luna_payoff']} | vs luna pair {fmt(e['joint_vs_luna_pair'])} vs sonnet pair {fmt(e['joint_vs_sonnet_pair'])}")

print("\n=== adopted alone: own gain of the adopter over its payoff in the mixed default pair (same arm and instance), partner's change, joint change; pooled over arms, then per arm")
for s_ in STRATS[1:]:
    for adopter in ("luna", "sonnet"):
        partner = "sonnet" if adopter == "luna" else "luna"
        own, part, joint, per_arm = [], [], [], {}
        for arm, g in runs.items():
            a = seat_of[arm][adopter]; cell = f"{s_}-neutral" if a == "p1" else f"neutral-{s_}"
            o_, p_, j_ = [], [], []
            for i in range(N):
                if (cell, i) in g and ("neutral-neutral", i) in g:
                    r, nn = g[(cell, i)], g[("neutral-neutral", i)]
                    o_.append(r["payoff"][a] - nn["payoff"][a]); p_.append(r["payoff"][other(a)] - nn["payoff"][other(a)]); j_.append(r["joint"] - nn["joint"])
            own += o_; part += p_; joint += j_
            per_arm[arm] = {"own": paired(o_), "partner": paired(p_), "joint": paired(j_),
                            "agree": sum(bool(g[(cell, i)]["agreement"]) for i in range(N) if (cell, i) in g),
                            "own_payoff": round(st.mean(g[(cell, i)]["payoff"][a] for i in range(N) if (cell, i) in g), 1) if o_ else None}
        e = {"own": paired(own), "partner": paired(part), "joint": paired(joint), "per_arm": per_arm}
        out["unilateral"].setdefault(s_, {})[f"{adopter}_vs_{partner}"] = e
        print(f"{s_:14} {adopter:6} vs {partner:6}: own {fmt(e['own'])} partner {fmt(e['partner'])} joint {fmt(e['joint'])}  "
              + " ".join(f"| {arm}: own {fmt(v['own'])} agree {v['agree']}" for arm, v in per_arm.items()))

print("\n=== the 2 x 2 for competing and collaborating: own gain by adopter model (rows) and partner model (columns); same-model cells from E3's twins (seat 1 only, 40 games)")
for s_ in ("competing", "collaborating"):
    for adopter in ("luna", "sonnet"):
        tw = twins[adopter]
        same = paired([tw[(f"{s_}-neutral", i)]["payoff"]["p1"] - tw[("neutral-neutral", i)]["payoff"]["p1"] for i in range(N) if (f"{s_}-neutral", i) in tw and ("neutral-neutral", i) in tw])
        same_partner = paired([tw[(f"{s_}-neutral", i)]["payoff"]["p2"] - tw[("neutral-neutral", i)]["payoff"]["p2"] for i in range(N) if (f"{s_}-neutral", i) in tw and ("neutral-neutral", i) in tw])
        cross = out["unilateral"][s_][f"{adopter}_vs_{'sonnet' if adopter == 'luna' else 'luna'}"]
        out["same_model"].setdefault(s_, {})[adopter] = {"own": same, "partner": same_partner}
        print(f"{s_:14} adopter {adopter:6}: partner luna  {fmt(same if adopter == 'luna' else cross['own'])}   partner sonnet {fmt(cross['own'] if adopter == 'luna' else same)}"
              f"   [partner's change: same-model {fmt(same_partner)}, cross {fmt(cross['partner'])}]")

print("\n=== symmetric mixed pairs: joint against the mixed default pair (paired), each model's payoff, and the joint against the same-model pairs on the same instances")
for s_ in STRATS[1:]:
    c = f"{s_}-{s_}"; line = f"{c:28}"; e = {}
    for arm, g in runs.items():
        d = [g[(c, i)]["joint"] - g[("neutral-neutral", i)]["joint"] for i in range(N) if (c, i) in g and ("neutral-neutral", i) in g]
        if not d:
            continue
        L, Sn = seat_of[arm]["luna"], seat_of[arm]["sonnet"]
        recs = [g[(c, i)] for i in range(N) if (c, i) in g]
        e[arm] = {"vs_neutral_pair": paired(d), "luna": round(st.mean(r["payoff"][L] for r in recs), 1), "sonnet": round(st.mean(r["payoff"][Sn] for r in recs), 1),
                  "joint": round(st.mean(r["joint"] for r in recs), 1), "agree": sum(bool(r["agreement"]) for r in recs)}
        for m, tw in twins.items():
            e[arm][f"vs_{m}_pair"] = paired([g[(c, i)]["joint"] - tw[(c, i)]["joint"] for i in range(N) if (c, i) in g and (c, i) in tw])
        line += f" | {arm}: {fmt(e[arm]['vs_neutral_pair'])} luna {e[arm]['luna']} sonnet {e[arm]['sonnet']} vs luna pair {fmt(e[arm]['vs_luna_pair'])} vs sonnet pair {fmt(e[arm]['vs_sonnet_pair'])}"
    out["symmetric"][s_] = e
    print(line)

print("\n=== compliance of each rule by the model that follows it (its diagonal and its unilateral cell, both arms)")
for m in ("luna", "sonnet"):
    line = f"{m:7}"
    for s_ in STRATS[1:]:
        k = n = 0
        for arm, g in runs.items():
            a = seat_of[arm][m]
            for c in (f"{s_}-{s_}", f"{s_}-neutral" if a == "p1" else f"neutral-{s_}"):
                for i in range(N):
                    if (c, i) in g:
                        v = g[(c, i)]["compliance"][a].get("overall")
                        n += v is not None; k += v is True
        out["compliance"].setdefault(m, {})[s_] = [k, n]; line += f" | {s_[:6]} {k}/{n}"
    print(line)

print("\n=== completion tokens and cost per game by model and own strategy (all cells, both arms)")
for m in ("luna", "sonnet"):
    line = f"{m:7}"
    for s_ in STRATS:
        toks, cost = [], []
        for arm, g in runs.items():
            a = seat_of[arm][m]
            for r in g.values():
                if r["strategies"][a] == s_:
                    toks.append(r["usage"][a]["completion"]); cost.append(r["usage"][a]["cost_usd"])
        if toks:
            out["tokens"].setdefault(m, {})[s_] = {"out_tokens": round(st.mean(toks)), "cost_per_game": round(st.mean(cost), 4), "games": len(toks)}
            line += f" | {s_[:6]} {round(st.mean(toks))} tok ${st.mean(cost):.4f}"
    print(line)

total = {arm: round(sum(r["cost_usd"] for r in g.values()), 2) for arm, g in runs.items()}
out["cost_usd"] = total; out["games"] = {arm: len(g) for arm, g in runs.items()}
print("\ngames", out["games"], "cost", total)
Path("results/bargain_e3_cross_paired.json").write_text(json.dumps(out, indent=1)); print("wrote results/bargain_e3_cross_paired.json")
