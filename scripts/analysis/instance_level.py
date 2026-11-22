"""Every paired comparison of the strategy payoff study with the instance as the unit of analysis.

For each study arm: the symmetric pairs against the neutral pair (joint payoff), each strategy adopted alone
against neutral (own payoff, partner's payoff, joint payoff; the two seats of an instance averaged), and each
reply to a competing partner against the neutral reply, with the mean difference, a bootstrap 95% interval,
d_z, the exact sign test and the Wilcoxon signed-rank test. Own and partner payoffs are in percent of the
seat's maximum (points where the maximum is 100). Reads results/bargain_*_rows.csv, writes
results/bargain_instance_level.json. Run from the repository root.
"""
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import stats  # noqa: E402

E2 = ["base", "urgent", "time_cost", "tight_budget", "distributive", "integrative", "all_contested", "low_stakes_p2", "power_p1", "open_info"]
ARMS = (["e1"] + [f"e2_{c}" for c in E2] + [f"e4_{k}" for k in ("software_design", "team_plan", "release_plan")]
        + ["grounded", "grounded_abstract", "grounded_claude_haiku_5_5"]
        + [f"e3_{m}" for m in ("gpt_6_luna_t1", "claude_haiku_5_5", "claude_sonnet_5_5", "gpt_6_1_sol")]
        + [f"e3_{c}_{m}" for c in ("open_info", "low_stakes_p2") for m in ("claude_sonnet_5_5", "gpt_6_1_sol")]
        + ["casino", "deliberation"])
EXTRA = sorted(p.name[len("bargain_"):-len("_rows.csv")] for p in Path("results").glob("bargain_e[12]r*_rows.csv"))


def scale_of(arm):
    if "low_stakes_p2" in arm:
        return {"p1": 100.0, "p2": 30.0}
    if arm == "casino":
        return {"p1": 36.0, "p2": 36.0}
    return None


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return round(o, 4) if isinstance(o, float) else o


def arm_stats(arm, resamples=4000):
    R = stats.load_rows(f"results/bargain_{arm}_rows.csv")
    ids, scale = stats.instances_of(R), scale_of(arm)
    nn = [R[("neutral-neutral", i)] for i in ids]
    out = {"instances": len(ids), "default_pair": {"joint": st.mean(r["joint"] for r in nn), "frontier": sum(r["on_frontier"] for r in nn),
                                                   "gap_pct": 100 * st.mean(r["joint_gap"] for r in nn) / st.mean(r["max_joint"] for r in nn)},
           "pairs": {}, "alone": {}, "reply_to_competing": {}}
    for s in stats.STYLES:
        p = stats.summary(stats.pair(R, s), resamples)
        if p:
            out["pairs"][s] = p
        a = stats.alone(R, s, scale)
        if a["own"]:
            out["alone"][s] = {k: stats.summary(v, resamples) for k, v in a.items()}
        r = stats.reply(R, s, scale=scale)
        if r:
            out["reply_to_competing"][s] = stats.summary(r, resamples)
    return out


def cross_stats(adopter, resamples=4000):
    """A luna agent and a sonnet agent: per instance, the adopter's seat-wise differences in the two arms averaged."""
    seats = {"e3_cross_luna_sonnet": {"luna": "p1", "sonnet": "p2"}, "e3_cross_sonnet_luna": {"sonnet": "p1", "luna": "p2"}}
    R = {arm: stats.load_rows(f"results/bargain_{arm}_rows.csv") for arm in seats}
    ids = stats.instances_of(R["e3_cross_luna_sonnet"])
    out = {"instances": len(ids), "alone": {}}
    for s in stats.STYLES:
        acc = {"own": [], "partner": [], "joint": []}
        for i in ids:
            d = {"own": [], "partner": [], "joint": []}
            for arm, seat_of in seats.items():
                a = seat_of[adopter]
                cell = f"{s}-neutral" if a == "p1" else f"neutral-{s}"
                r, nn = R[arm].get((cell, i)), R[arm].get(("neutral-neutral", i))
                if r is None or nn is None:
                    continue
                d["own"].append(r[f"payoff_{a}"] - nn[f"payoff_{a}"])
                d["partner"].append(r[f"payoff_{stats.other(a)}"] - nn[f"payoff_{stats.other(a)}"])
                d["joint"].append(r["joint"] - nn["joint"])
            for k in acc:
                if d[k]:
                    acc[k].append(st.mean(d[k]))
        if acc["own"]:
            out["alone"][s] = {k: stats.summary(v, resamples) for k, v in acc.items()}
    return out


def fmt(s):
    if s is None:
        return "n/a"
    return f"{s['mean']:+5.1f} [{s['ci95'][0]:+5.1f}, {s['ci95'][1]:+5.1f}] sign {s['p_sign']:.3f} wilcoxon {s['p_wilcoxon']:.3f} (n={s['n']})"


def main():
    out = {arm: arm_stats(arm) for arm in ARMS + EXTRA if Path(f"results/bargain_{arm}_rows.csv").is_file()}
    out["cross_luna_adopts"], out["cross_sonnet_adopts"] = cross_stats("luna"), cross_stats("sonnet")
    Path("results/bargain_instance_level.json").write_text(json.dumps(clean(out), indent=1) + "\n")
    for arm in ("e1", "e2_base", "e2_open_info", "e3_claude_sonnet_5_5", "e3_gpt_6_1_sol", "cross_luna_adopts", "cross_sonnet_adopts", "casino", "deliberation"):
        e = out[arm]
        print(f"== {arm} ({e['instances']} instances)")
        for s in stats.STYLES:
            line = f"  {s:14}"
            if s in e.get("pairs", {}):
                line += f" pair {fmt(e['pairs'][s])} |"
            if s in e["alone"]:
                line += f" alone own {fmt(e['alone'][s]['own'])} | partner {e['alone'][s]['partner']['mean']:+5.1f} p {e['alone'][s]['partner']['p_wilcoxon']:.3f}"
            print(line)
    print(f"wrote results/bargain_instance_level.json: {len(out)} arms")


if __name__ == "__main__":
    main()
