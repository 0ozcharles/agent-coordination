"""E8, the rules played by scripts: evaluate configs/bargain_e8.json.

Reads the scripts' games (results/bargain_e8_rows.csv) and the model's games on the same 100 instances
(results/bargain_e1r_rows.csv); writes results/bargain_scripted.json. Per-instance paired differences as in
stats.py (the two seats of an instance averaged, differences matched by seat), two-sided Wilcoxon signed-rank
tests, Holm over the hypotheses of the config. Run from the repository root after the study has been reported:

    python3 scripts/analysis/scripted.py [spec] [script rows] [model rows] [output]
"""
import json
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import stats  # noqa: E402

RULES = ("competing", "collaborating", "compromising", "accommodating", "avoiding")
ARGS = sys.argv[1:] + [None] * 4
SPEC = json.loads(Path(ARGS[0] or "configs/bargain_e8.json").read_text())
SCRIPT_ROWS, MODEL_ROWS = ARGS[1] or "results/bargain_e8_rows.csv", ARGS[2] or "results/bargain_e1r_rows.csv"
OUT = ARGS[3] or "results/bargain_scripted.json"


def script(rule):
    return f"script_{rule}"


def own_matrix(R, names, ids):
    """Per instance, the row strategy's own payoff against the column strategy (both seats averaged): {i: 25 values}."""
    out = {}
    for i in ids:
        row = []
        for a in names:
            for b in names:
                vals = [R[(cell, i)][f"payoff_{seat}"] for cell, seat in stats.seat_cells(a, b) if (cell, i) in R]
                row.append(st.mean(vals) if vals else None)
        out[i] = row
    return out


def col_means(M, ids):
    return [st.mean(M[i][k] for i in ids) for k in range(len(next(iter(M.values()))))]


def pearson(x, y):
    mx, my = st.mean(x), st.mean(y)
    sx, sy = sum((a - mx) ** 2 for a in x) ** 0.5, sum((b - my) ** 2 for b in y) ** 0.5
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else None


def ranks(x):
    order, out = sorted(range(len(x)), key=lambda i: x[i]), [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(x, y):
    return pearson(ranks(x), ranks(y))


def boot_r(A, B, ids, resamples=2000, seed=20261123):
    """Percentile interval for the correlation of the two matrices' cell means, resampling the instances."""
    rng, n, vals = random.Random(seed), len(ids), []
    for _ in range(resamples):
        pick = [ids[rng.randrange(n)] for _ in range(n)]
        r = pearson(col_means(A, pick), col_means(B, pick))
        if r is not None:
            vals.append(r)
    vals.sort()
    return [vals[int(0.025 * len(vals))], vals[min(len(vals) - 1, int(0.975 * len(vals)))]]


def grid(values, names):
    return {a: {b: values[k * len(names) + j] for j, b in enumerate(names)} for k, a in enumerate(names)}


def agreement_matrix(R, names, ids):
    return [st.mean(1.0 if R[(f"{a}-{b}", i)]["agreement"] else 0.0 for i in ids) for a in names for b in names]


def complies(R, strategy, partners):
    """(games the checker passes, games it judged) for a strategy over its cells against the given partners."""
    seen = []
    for p in partners:
        for cell, seat in stats.seat_cells(strategy, p):
            seen += [r[f"{seat}_complies"] for (c, _), r in R.items() if c == cell]
    known = [x for x in seen if x in ("True", "False")]
    return [sum(x == "True" for x in known), len(known)]


def unilateral(R, strategy):
    d = stats.alone(R, strategy)
    cells = [c for c, _ in stats.seat_cells(strategy, "neutral")]
    games = [r for (c, _), r in R.items() if c in cells]
    return {"own": stats.summary(d["own"]), "partner": stats.summary(d["partner"]), "joint": stats.summary(d["joint"]),
            "agreements": sum(r["agreement"] for r in games), "games": len(games)}


def differences(R, h, n):
    if h["kind"] == "alone":
        d = stats.alone(R, h["strategy"])[h["measure"]]
    elif h["kind"] == "contrast":
        d = stats.contrast(R, h["strategy"], h["other"])
    else:
        raise ValueError(h["kind"])
    return d if len(d) == n else None


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return round(o, 4) if isinstance(o, float) else o


def main():
    model, scripts = stats.load_rows(MODEL_ROWS), stats.load_rows(SCRIPT_ROWS)
    R = {**model, **scripts}
    ids = stats.instances_of(R)
    main_scripts, variants = [script(r) for r in RULES], SPEC["scripts"]["variants"]
    out = {"study": SPEC["study"], "test": SPEC["test"], "instances": len(ids), "hypotheses": []}

    for h in SPEC["hypotheses"]:
        d = differences(R, h, len(ids))
        out["hypotheses"].append({**{k: h[k] for k in ("id", "claim", "direction")}, **(stats.summary(d) if d else {"missing": True})})
    done = [e for e in out["hypotheses"] if not e.get("missing")]
    for e, p in zip(done, stats.holm([e["p_wilcoxon"] for e in done])):
        e["p_holm"] = p
        e["confirmed"] = bool(p < 0.05 and (1 if e["direction"] == "+" else -1) * e["mean"] > 0)
    out["evaluated"], out["confirmed"] = len(done), sum(e["confirmed"] for e in done)

    # arm C: each script, and the model given the same rule, alone against the default agent
    out["against_the_default_agent"] = {r: {"script": unilateral(R, script(r)), "model": unilateral(R, r),
                                            "script_minus_model": stats.summary(stats.contrast(R, script(r), r))} for r in RULES}

    # arm A: the 5 x 5 matrices
    A, B = own_matrix(R, main_scripts, ids), own_matrix(R, list(RULES), ids)
    a, b = col_means(A, ids), col_means(B, ids)
    ga, gb = agreement_matrix(R, main_scripts, ids), agreement_matrix(R, list(RULES), ids)
    pairs = [(x, y) for k, x in enumerate(RULES) for y in RULES[k:]]
    js = [st.mean(stats.joint_payoff(R, i, script(x), script(y)) for i in ids) for x, y in pairs]
    jm = [st.mean(stats.joint_payoff(R, i, x, y) for i in ids) for x, y in pairs]
    out["matrix"] = {"own_payoff": {"scripts": grid(a, RULES), "model": grid(b, RULES), "pearson": pearson(a, b), "pearson_ci95": boot_r(A, B, ids),
                                    "spearman": spearman(a, b), "mean_abs_difference": st.mean(abs(x - y) for x, y in zip(a, b))},
                     "agreement_rate": {"scripts": grid(ga, RULES), "model": grid(gb, RULES), "pearson": pearson(ga, gb),
                                        "mean_abs_difference": st.mean(abs(x - y) for x, y in zip(ga, gb))},
                     "joint_of_pairs": {"scripts": {f"{x}+{y}": v for (x, y), v in zip(pairs, js)}, "model": {f"{x}+{y}": v for (x, y), v in zip(pairs, jm)},
                                        "pearson": pearson(js, jm), "spearman": spearman(js, jm)}}
    sym_s = [st.mean(stats.joint_payoff(R, i, script(r), script(r)) for i in ids) for r in RULES]
    sym_m = [st.mean(stats.joint_payoff(R, i, r, r) for i in ids) for r in RULES]
    default_pair = st.mean(R[("neutral-neutral", i)]["joint"] for i in ids)
    out["symmetric_pairs"] = {"scripts": dict(zip(RULES, sym_s)), "model": dict(zip(RULES, sym_m)), "default_pair": default_pair,
                              "spearman": spearman(sym_s, sym_m),
                              "script_minus_model": {r: stats.summary([stats.joint_payoff(R, i, script(r), script(r)) - stats.joint_payoff(R, i, r, r) for i in ids])
                                                     for r in RULES}}
    out["checker"] = {"scripts": {r: {"against_scripts": complies(R, script(r), main_scripts), "against_the_default_agent": complies(R, script(r), ["neutral"])} for r in RULES},
                      "model": {r: {"against_rules": complies(R, r, list(RULES)), "against_the_default_agent": complies(R, r, ["neutral"])} for r in RULES}}

    # arm B: one variant per script
    out["variants"] = {}
    for v in variants:
        rule = v.split("_")[1]
        row = lambda s: {p.split("_", 1)[1]: st.mean(st.mean(R[(c, i)][f"payoff_{seat}"] for c, seat in stats.seat_cells(s, p) if (c, i) in R) for i in ids)
                         for p in main_scripts if any((c, ids[0]) in R for c, _ in stats.seat_cells(s, p))}
        out["variants"][v] = {"against_the_default_agent": unilateral(R, v), "variant_minus_script": stats.summary(stats.contrast(R, v, script(rule))),
                              "own_payoff_against_scripts": {"variant": row(v), "script": row(script(rule))},
                              "checker": {"against_scripts": complies(R, v, main_scripts), "against_the_default_agent": complies(R, v, ["neutral"])}}
    Path(OUT).write_text(json.dumps(clean(out), indent=1, ensure_ascii=False) + "\n")

    f = lambda s: f"{s['mean']:+6.1f} [{s['ci95'][0]:+5.1f}, {s['ci95'][1]:+5.1f}]"
    print(f"{len(ids)} instances; hypotheses confirmed {out['confirmed']} of {out['evaluated']}")
    for e in out["hypotheses"]:
        print(f"  {e['id']:3} {e['direction']} " + (f"{f(e)} p_holm {e['p_holm']:.4f} {'CONFIRMED' if e['confirmed'] else 'not confirmed':13}" if not e.get("missing") else "missing") + f"  {e['claim'][:80]}")
    print("against the default agent: own payoff (script | model | script minus model), partner (script | model), agreements")
    for r in RULES:
        e = out["against_the_default_agent"][r]
        print(f"  {r:14} {f(e['script']['own'])} | {f(e['model']['own'])} | {f(e['script_minus_model'])}   partner {e['script']['partner']['mean']:+6.1f} | {e['model']['partner']['mean']:+6.1f}"
              f"   agree {e['script']['agreements']}/{e['script']['games']} | {e['model']['agreements']}/{e['model']['games']}")
    m = out["matrix"]
    print(f"own-payoff matrices: r = {m['own_payoff']['pearson']:.2f} [{m['own_payoff']['pearson_ci95'][0]:.2f}, {m['own_payoff']['pearson_ci95'][1]:.2f}], "
          f"rho = {m['own_payoff']['spearman']:.2f}, mean |diff| = {m['own_payoff']['mean_abs_difference']:.1f}; agreement rates r = {m['agreement_rate']['pearson']:.2f}; "
          f"joint of 15 pairs r = {m['joint_of_pairs']['pearson']:.2f}")
    for name in ("scripts", "model"):
        print(f"  {name}: own payoff of row against column | agreement rate of the cell")
        for x in RULES:
            print(f"    {x:14}" + " ".join(f"{m['own_payoff'][name][x][y]:6.1f}" for y in RULES) + "   |  " + " ".join(f"{100 * m['agreement_rate'][name][x][y]:4.0f}" for y in RULES))
    s = out["symmetric_pairs"]
    print(f"symmetric pairs, joint (default pair {s['default_pair']:.1f}); rho = {s['spearman']:.2f}")
    for r in RULES:
        print(f"  {r:14} scripts {s['scripts'][r]:6.1f}   model {s['model'][r]:6.1f}   script minus model {f(s['script_minus_model'][r])}")
    print("checker passes (games passed, games judged): " + "; ".join(f"{r} {out['checker']['scripts'][r]['against_scripts']} {out['checker']['scripts'][r]['against_the_default_agent']}" for r in RULES))
    print("variants: own payoff against the default agent | variant minus script | checker")
    for v, e in out["variants"].items():
        print(f"  {v:28} {f(e['against_the_default_agent']['own'])} | {f(e['variant_minus_script'])} | {e['checker']}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
