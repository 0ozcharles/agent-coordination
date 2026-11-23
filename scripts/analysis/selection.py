"""Evaluate the selection study (E6): the hypotheses and the secondary analyses of configs/bargain_selection.json.

An agent that chooses its own strategy, from the five rule sets and no rules ("choose") or from the five rule sets
alone ("pick"), is compared, instance by instance, with the games of the reference runs in which each rule was
assigned (E2-R for the ten conditions, E1-R where both agents choose, the grounded and benchmark runs). Each hypothesis is a per-instance paired difference (stats.py: the two seats of an instance are averaged and
matched by seat; conditions that share their instances are averaged); S1 is a permutation test on the choices.
Reads results/bargain_e6_<arm>_rows.csv and the reference rows; writes results/bargain_selection.json. Run from the
repository root after the arms have been reported. A different spec and output can be given as two arguments: the
informed-choice study (E7) is `selection.py configs/bargain_informed.json results/bargain_informed.json`. A spec may
name its own choosers (`menus`), the uninformed chooser each is compared with (`uninformed`), and arms on a subset
of the reference runs' instances; an arm whose config carries evidence also reports how often the option with the
highest figure was taken.
"""
import collections
import json
import math
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import stats  # noqa: E402

SPEC_PATH = sys.argv[1] if len(sys.argv) > 1 else "configs/bargain_selection.json"
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else "results/bargain_selection.json"
SPEC = json.loads(Path(SPEC_PATH).read_text())
MENU = ("neutral", *stats.STYLES)
MENUS = SPEC.get("menus", {"free": "choose", "forced": "pick"})   # menu -> the chooser that is reported for it
UNINFORMED = SPEC.get("uninformed", {})                            # chooser -> the same menu without evidence, in the reference rows
DISCOVERY = "results/bargain_e2_{}_rows.csv"   # the 20-instance runs behind the ten conditions, for the choice by condition


def load(arm):
    """The arm's games and its reference games as one table {(cell, instance): row}; None before the arm is reported."""
    spec = SPEC["arms"][arm]
    own = Path(json.loads(Path(spec["config"]).read_text())["results_prefix"] + "_rows.csv")
    if not own.is_file():
        return None
    R = {}
    for ref in spec["reference"]:
        R.update({k: v for k, v in stats.load_rows(ref["rows"]).items() if "cells" not in ref or k[0] in ref["cells"]})
    mine = stats.load_rows(own)
    ids = {i for _, i in mine}
    R = {k: v for k, v in R.items() if k[1] in ids}  # an arm may use a subset of the reference run's instances
    R.update(mine)
    return R


def picks(R, chooser="choose", partner="neutral"):
    """Every choice of `chooser` facing `partner`: instance, seat, the (first) pick, all picks and their menu letters."""
    out = []
    for cell, seat in dict.fromkeys(stats.seat_cells(chooser, partner)):
        for (c, i), r in sorted(R.items()):
            if c == cell and r.get(f"{seat}_choice"):
                out.append({"instance": i, "seat": seat, "choice": r[f"{seat}_choice"], "all": r[f"{seat}_choices"].split(">"),
                            "letters": r[f"{seat}_choice_letters"].split(">"), "invalid": int(float(r.get(f"{seat}_choice_invalid") or 0)),
                            "tokens": float(r.get(f"{seat}_choice_tokens") or 0), "complies": {"True": True, "False": False}.get(r.get(f"{seat}_complies"))})
    return out


def shares(values):
    n = len(values)
    count = collections.Counter(values)
    return {"n": n, **{s: round(count[s] / n, 4) if n else None for s in MENU}}


def assigned(R, s, i, seat, scale=None):
    """The seat's payoff in the reference game where rule s is assigned to it against the default agent."""
    cell = "neutral-neutral" if s == "neutral" else (f"{s}-neutral" if seat == "p1" else f"neutral-{s}")
    r = R.get((cell, i))
    return None if r is None else r[f"payoff_{seat}"] * (100.0 / scale[seat] if scale else 1.0)


def skill(R, scale=None, against="mix", chooser="pick"):
    """Per instance: the assigned-cell payoff of the option picked minus, on the same instance and seat, the average
    assigned-cell payoff under the arm's choice frequencies (against = "mix") or the default agent's (against = "neutral")."""
    P = picks(R, chooser)
    freq = shares([p["choice"] for p in P])
    by = collections.defaultdict(list)
    for p in P:
        table = {s: assigned(R, s, p["instance"], p["seat"], scale) for s in MENU}
        if any(v is None for v in table.values()):
            continue
        base = sum(freq[s] * table[s] for s in MENU) if against == "mix" else table["neutral"]
        by[p["instance"]].append(table[p["choice"]] - base)
    return {i: st.mean(v) for i, v in by.items()}


def differences(h, arm, R):
    """One difference per instance, keyed by instance id, for hypothesis h on one arm."""
    scale = SPEC["arms"][arm].get("scale")
    ids = stats.instances_of(R)
    if h["kind"] in ("skill", "gain"):  # gain: the same from the assigned cells, against the default agent's game
        return skill(R, scale, "mix" if h["kind"] == "skill" else "neutral", h["chooser"])
    if h["kind"] == "alone":
        d = stats.alone(R, h["strategy"], scale)[h["measure"]]
    elif h["kind"] == "contrast":
        d = stats.contrast(R, h["strategy"], h["other"], h.get("partner", "neutral"), scale)
    elif h["kind"] == "pair":
        d = stats.pair(R, h["strategy"])
    elif h["kind"] == "reply":
        d = stats.reply(R, h["strategy"], h["to"], h.get("base", "neutral"), scale)
    else:
        raise ValueError(h["kind"])
    return dict(zip(ids, d)) if len(d) == len(ids) else None  # every instance must contribute, or the ids would not line up


def sample(h, tables):
    """Per-instance differences averaged over the arms of a hypothesis (the arms share their instances)."""
    per_arm = [differences(h, a, tables[a]) if tables.get(a) else None for a in h["arms"]]
    if any(d is None for d in per_arm):
        return None
    ids = sorted(set.intersection(*(set(d) for d in per_arm)))
    return [st.mean(d[i] for d in per_arm) for i in ids]


def chi_square(table, columns):
    """Pearson's statistic of a list of count rows."""
    n = sum(map(sum, table))
    col = [sum(row[j] for row in table) for j in range(columns)]
    return sum((row[j] - sum(row) * col[j] / n) ** 2 / (sum(row) * col[j] / n) for row in table for j in range(columns) if col[j] and sum(row))


def dependence(tables, arms, chooser, permutations=10000, seed=20261123):
    """Whether the choice depends on the condition: the ten picks of an instance and seat are shuffled over the conditions."""
    by = collections.defaultdict(dict)
    for k, arm in enumerate(arms):
        if not tables.get(arm):
            return None
        for p in picks(tables[arm], chooser):
            by[(p["instance"], p["seat"])][k] = MENU.index(p["choice"])
    units = [[u[k] for k in range(len(arms))] for u in by.values() if len(u) == len(arms)]
    if not units:
        return None

    def statistic(us):
        table = [[0] * len(MENU) for _ in arms]
        for u in us:
            for k, c in enumerate(u):
                table[k][c] += 1
        return chi_square(table, len(MENU))

    observed, rng, beyond = statistic(units), random.Random(seed), 0
    for _ in range(permutations):
        shuffled = [rng.sample(u, len(u)) for u in units]
        beyond += statistic(shuffled) >= observed - 1e-9
    n = len(units) * len(arms)
    return {"units": len(units), "chi_square": observed, "cramers_v": math.sqrt(observed / (n * (min(len(arms), len(MENU)) - 1))),
            "p": (1 + beyond) / (1 + permutations), "permutations": permutations}


def effect(d):
    s = stats.summary(d)
    return None if s is None else {k: s[k] for k in ("n", "mean", "ci95", "p_wilcoxon", "wins", "losses", "dz")}


def by_condition_from_assigned_cells(arms):
    """What a choice of rule by condition could earn, from the assigned cells of E2 (discovery) and E2-R alone."""
    conf, disc = {}, {}
    for arm in arms:
        scale = SPEC["arms"][arm].get("scale")
        for store, path in ((conf, SPEC["arms"][arm]["reference"][0]["rows"]), (disc, DISCOVERY.format(arm))):
            if not Path(path).is_file():
                return None
            R = stats.load_rows(path)
            store[arm] = {s: dict(zip(stats.instances_of(R), stats.alone(R, s, scale)["own"])) for s in stats.STYLES}
    ids = sorted(set.intersection(*(set(conf[a][s]) for a in arms for s in stats.STYLES)))
    best = {a: max(stats.STYLES, key=lambda s: st.mean(disc[a][s].values())) for a in arms}
    over = lambda value: [st.mean(value(a, i) for a in arms) for i in ids]  # noqa: E731
    out = {f"always {s}": effect(over(lambda a, i, s=s: conf[a][s][i])) for s in stats.STYLES}
    out["by condition, the rule with the largest mean in the discovery sample"] = effect(over(lambda a, i: conf[a][best[a]][i]))
    out["the same, minus always collaborating"] = effect(over(lambda a, i: conf[a][best[a]][i] - conf[a]["collaborating"][i]))
    out["the best of the five in each game, in hindsight (an upper bound inflated by noise)"] = effect(over(lambda a, i: max(conf[a][s][i] for s in stats.STYLES)))
    out["picked in the discovery sample"] = best
    return out


def arm_report(arm, R, chooser):
    """Shares and payoffs of a chooser adopted alone in one arm."""
    scale, P = SPEC["arms"][arm].get("scale"), picks(R, chooser)
    if not P:
        return None
    out = {"shares": shares([p["choice"] for p in P]), "shares_by_seat": {seat: shares([p["choice"] for p in P if p["seat"] == seat]) for seat in ("p1", "p2")},
           "letters": dict(sorted(collections.Counter(p["letters"][0] for p in P).items())), "invalid_choices": sum(p["invalid"] for p in P),
           "choice_tokens": round(st.mean(p["tokens"] for p in P), 1),
           "own": effect(stats.alone(R, chooser, scale)["own"]), "partner": effect(stats.alone(R, chooser, scale)["partner"]),
           "joint": effect(stats.alone(R, chooser, scale)["joint"]),
           "own_minus_assigned_collaborating": effect(stats.contrast(R, chooser, "collaborating", "neutral", scale)),
           "own_minus_assigned_competing": effect(stats.contrast(R, chooser, "competing", "neutral", scale)),
           "skill": effect(list(skill(R, scale, "mix", chooser).values())),
           "gain_from_assigned_cells": effect(list(skill(R, scale, "neutral", chooser).values()))}
    comply = {}
    for s in stats.STYLES:
        mine = [p["complies"] for p in P if p["choice"] == s and p["complies"] is not None]
        ref = [{"True": True, "False": False}.get(R[(cell, i)].get(f"{seat}_complies")) for cell, seat in stats.seat_cells(s, "neutral") for i in stats.instances_of(R) if (cell, i) in R]
        ref = [v for v in ref if v is not None]
        comply[s] = {"picked": [sum(mine), len(mine)], "assigned": [sum(ref), len(ref)]}
    out["compliance"] = comply
    return out


def evidence_report(arm, R, chooser):
    """Where the menu showed what each option earned before: how often the option with the highest figure was taken,
    the mean rank of the figure of the option taken (1 = highest), and what taking the highest figure every time would
    have earned, from the assigned cells, against the default agent."""
    figures = json.loads(Path(SPEC["arms"][arm]["config"]).read_text()).get("choice", {}).get("evidence")
    P = picks(R, chooser)
    if not figures or not P:
        return None
    options = MENU if chooser == MENUS.get("free") else stats.STYLES
    scale, best, ranks, gain = SPEC["arms"][arm].get("scale"), 0, [], collections.defaultdict(list)
    for p in P:
        f = figures[p["seat"]]
        order = sorted(options, key=lambda s: -f[s])
        best += p["choice"] == order[0]
        ranks.append(order.index(p["choice"]) + 1 if p["choice"] in order else len(order) + 1)
        top, base = assigned(R, order[0], p["instance"], p["seat"], scale), assigned(R, "neutral", p["instance"], p["seat"], scale)
        if top is not None and base is not None:
            gain[p["instance"]].append(top - base)
    return {"n": len(P), "took_the_highest_figure": round(best / len(P), 4), "mean_rank_of_the_figure_taken": round(st.mean(ranks), 2),
            "highest_figure_by_seat": {seat: max(options, key=lambda s: figures[seat][s]) for seat in ("p1", "p2")},
            "the_highest_figure_every_time_from_assigned_cells": effect([st.mean(v) for v in gain.values()])}


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return round(o, 4) if isinstance(o, float) else o


def main():
    tables = {arm: load(arm) for arm in SPEC["arms"]}
    missing = [a for a, R in tables.items() if R is None]
    out = {"study": SPEC["study"], "test": SPEC["test"], "arms_missing": missing, "hypotheses": []}
    for h in SPEC["hypotheses"]:
        entry = {k: h[k] for k in ("id", "claim", "kind", "arms") if k in h}
        entry["direction"] = h.get("direction")
        if h["kind"] == "dependence":
            res = dependence(tables, h["arms"], h["chooser"])
            entry.update(res or {})
            entry["p_test"] = res["p"] if res else None
        else:
            d = sample(h, tables)
            s = stats.summary(d) if d else None
            entry.update(s or {})
            entry["p_test"] = s["p_wilcoxon"] if s else None
        out["hypotheses"].append(entry)
    tested = [e for e in out["hypotheses"] if e["p_test"] is not None]
    for e, p in zip(tested, stats.holm([e["p_test"] for e in tested])):
        e["p_holm"] = p
        right_way = e["kind"] == "dependence" or (e["mean"] > 0) == (e["direction"] == "+")
        e["confirmed"] = bool(p < 0.05 and right_way)
    out["complete"] = not missing and len(tested) == len(SPEC["hypotheses"])

    ten = SPEC["hypotheses"][0]["arms"]
    menus = MENUS
    out["arms"] = {arm: {menu: arm_report(arm, R, chooser) for menu, chooser in menus.items()} for arm, R in tables.items() if R and arm != "core"}
    for arm, R in tables.items():
        for menu, chooser in menus.items():
            a = out["arms"].get(arm, {}).get(menu)
            if a and chooser in UNINFORMED:
                scale = SPEC["arms"][arm].get("scale")
                a["own_minus_uninformed"] = effect(stats.contrast(R, chooser, UNINFORMED[chooser], "neutral", scale))
                a["uninformed_shares"] = shares([p["choice"] for p in picks(R, UNINFORMED[chooser])])
                a["evidence"] = evidence_report(arm, R, chooser)
    if all(tables.get(a) for a in ten):
        out["ten_conditions"] = {}
        for menu, chooser in menus.items():
            pooled = [p for a in ten for p in picks(tables[a], chooser)]
            out["ten_conditions"][menu] = {
                "shares": shares([p["choice"] for p in pooled]), "letters": dict(sorted(collections.Counter(p["letters"][0] for p in pooled).items())),
                "shares_by_letter": {L: shares([p["choice"] for p in pooled if p["letters"][0] == L]) for L in "ABCDEF" if any(p["letters"][0] == L for p in pooled)},
                "invalid_choices": sum(p["invalid"] for p in pooled), "choice_tokens": round(st.mean(p["tokens"] for p in pooled), 1),
                "dependence": dependence(tables, ten, chooser, permutations=2000) if not any(h["kind"] == "dependence" and h.get("chooser") == chooser for h in SPEC["hypotheses"]) else "see S1",
                "gain_from_assigned_cells": effect(sample({"kind": "gain", "chooser": chooser, "arms": ten}, tables)),
                "own_minus_assigned_competing": effect(sample({"kind": "contrast", "strategy": chooser, "other": "competing", "arms": ten}, tables)),
                "joint": effect(sample({"kind": "alone", "strategy": chooser, "measure": "joint", "arms": ten}, tables)),
                "own_minus_assigned_collaborating": effect(sample({"kind": "contrast", "strategy": chooser, "other": "collaborating", "arms": ten}, tables)),
                "partner": effect(sample({"kind": "alone", "strategy": chooser, "measure": "partner", "arms": ten}, tables)),
                "skill": effect(sample({"kind": "skill", "chooser": chooser, "arms": ten}, tables))}
            if chooser in UNINFORMED:
                took = [a["evidence"] for a in (out["arms"][x][menu] for x in ten) if a and a.get("evidence")]
                out["ten_conditions"][menu].update({
                    "own_minus_uninformed": effect(sample({"kind": "contrast", "strategy": chooser, "other": UNINFORMED[chooser], "arms": ten}, tables)),
                    "uninformed_shares": shares([p["choice"] for a in ten for p in picks(tables[a], UNINFORMED[chooser])]),
                    "took_the_highest_figure": round(sum(e["took_the_highest_figure"] * e["n"] for e in took) / sum(e["n"] for e in took), 4) if took else None,
                    "mean_rank_of_the_figure_taken": round(sum(e["mean_rank_of_the_figure_taken"] * e["n"] for e in took) / sum(e["n"] for e in took), 2) if took else None})
    out["from_assigned_cells"] = by_condition_from_assigned_cells(ten)
    R = tables.get("base")
    if R:
        kept = picks(R, "pick_kept")
        if kept:
            out["kept"] = {"shares": shares([p["choice"] for p in kept]), "own": effect(stats.alone(R, "pick_kept")["own"]), "partner": effect(stats.alone(R, "pick_kept")["partner"]),
                           "own_minus_not_kept": effect(stats.contrast(R, "pick_kept", "pick")),
                           "compliance": {s: [sum(p["complies"] for p in kept if p["choice"] == s and p["complies"] is not None),
                                              sum(1 for p in kept if p["choice"] == s and p["complies"] is not None)] for s in stats.STYLES}}
        out["repick"] = {}
        for partner in ("neutral", "competing"):
            P = picks(R, "repick", partner)
            if not P:
                continue
            own = stats.alone(R, "repick")["own"] if partner == "neutral" else stats.reply(R, "repick", "competing")
            other = stats.alone(R, "repick")["partner"] if partner == "neutral" else stats.matched(R, "repick", "competing", "neutral")["partner"]
            out["repick"][partner] = {"first": shares([p["all"][0] for p in P]), "last": shares([p["all"][-1] for p in P]),
                                      "first_by_seat": {seat: shares([p["all"][0] for p in P if p["seat"] == seat]) for seat in ("p1", "p2")},
                                      "every_pick": shares([c for p in P for c in p["all"]]), "picks_per_game": round(st.mean(len(p["all"]) for p in P), 2),
                                      "games_with_a_change": round(sum(len(set(p["all"])) > 1 for p in P) / len(P), 4), "own": effect(own), "partner": effect(other)}
    R = tables.get("core")
    if R:
        out["both_choose"] = {}
        for menu, chooser in menus.items():
            cell = f"{chooser}-{chooser}"
            P = {(p["instance"], p["seat"]): p["choice"] for p in picks(R, chooser, chooser)}
            ids = sorted({i for i, _ in P})
            if not ids:
                continue
            pairs = collections.Counter(" + ".join(sorted((P[(i, "p1")], P[(i, "p2")]))) for i in ids)
            from_cells = [R[(f"{P[(i, 'p1')]}-{P[(i, 'p2')]}", i)]["joint"] - R[("neutral-neutral", i)]["joint"] for i in ids if (f"{P[(i, 'p1')]}-{P[(i, 'p2')]}", i) in R]
            own = [st.mean([R[(cell, i)]["payoff_p1"] - R[("neutral-neutral", i)]["payoff_p1"], R[(cell, i)]["payoff_p2"] - R[("neutral-neutral", i)]["payoff_p2"]]) for i in ids]
            out["both_choose"][menu] = {"shares": shares(list(P.values())), "pairs": dict(pairs.most_common()), "joint": effect(stats.pair(R, chooser)), "own": effect(own),
                                        "joint_from_assigned_cells": effect(from_cells), "agreements": sum(R[(cell, i)]["agreement"] for i in ids),
                                        "default_pair_agreements": sum(R[("neutral-neutral", i)]["agreement"] for i in ids), "games": len(ids)}
    Path(OUT_PATH).write_text(json.dumps(clean(out), indent=1) + "\n")

    for e in out["hypotheses"]:
        if e["p_test"] is None:
            print(f"{e['id']}  (games missing)  {e['claim']}")
        elif e["kind"] == "dependence":
            print(f"{e['id']}  chi2 {e['chi_square']:.1f}, V {e['cramers_v']:.3f}, {e['units']} units  Holm p {e['p_holm']:.4f}  {'CONFIRMED' if e['confirmed'] else 'not confirmed'}  {e['claim']}")
        else:
            print(f"{e['id']}  {e['mean']:+6.2f} [{e['ci95'][0]:+.2f}, {e['ci95'][1]:+.2f}] n={e['n']}  Holm p {e['p_holm']:.4f}  {'CONFIRMED' if e['confirmed'] else 'not confirmed'}  {e['claim']}")
    fmt = lambda e: f"{e['mean']:+8.1f}" if e else "       -"  # noqa: E731
    for menu in menus:
        print(f"\n{menu + ' menu':15} {'n':>4} " + " ".join(f"{s[:7]:>8}" for s in MENU) + "      own  partner  vs collab    skill")
        for arm, by_menu in out["arms"].items():
            a = by_menu[menu]
            if a:
                print(f"{arm:15} {a['shares']['n']:>4} " + " ".join(f"{a['shares'][s]:>8.2f}" for s in MENU)
                      + f" {fmt(a['own'])} {fmt(a['partner'])}  {fmt(a['own_minus_assigned_collaborating'])} {fmt(a['skill'])}")
    if missing:
        print("\nnot yet reported:", ", ".join(missing))
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
