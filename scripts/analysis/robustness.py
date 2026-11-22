"""E5: does the map depend on how the rules are written? Paraphrases, moved thresholds and the collaborating
rule taken apart, each against the default agent and against its original rule in E1-R on the same instances.

Reads results/bargain_e1r_rows.csv and results/bargain_e5_rules_rows.csv; writes
results/bargain_e5_rules_robustness.json. Run from the repository root.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import stats  # noqa: E402

CONFIG = json.loads(Path("configs/bargain_e5_rules.json").read_text())


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return round(o, 4) if isinstance(o, float) else o


def effects(R, s):
    a = stats.alone(R, s)
    return {"own": stats.summary(a["own"]), "partner": stats.summary(a["partner"]), "joint_alone": stats.summary(a["joint"]),
            "pair": stats.summary(stats.pair(R, s)),
            "agreements": {"alone": sum(R[(c, i)]["agreement"] for c in (f"{s}-neutral", f"neutral-{s}") for i in stats.instances_of(R) if (c, i) in R),
                           "pair": sum(R[(f"{s}-{s}", i)]["agreement"] for i in stats.instances_of(R) if (f"{s}-{s}", i) in R)}}


def fmt(x):
    return "n/a" if x is None else f"{x['mean']:+5.1f} [{x['ci95'][0]:+5.1f},{x['ci95'][1]:+5.1f}] p {x['p_wilcoxon']:.3f}"


def main():
    R = stats.load_rows("results/bargain_e1r_rows.csv")
    R.update(stats.load_rows("results/bargain_e5_rules_rows.csv"))
    out = {"instances": len(stats.instances_of(R)), "original": {s: effects(R, s) for s in stats.STYLES}, "variants": {}}
    for v, meta in CONFIG["variants"].items():
        b = meta["base"]
        if (f"{v}-{v}", 0) not in R:
            continue
        m = stats.matched(R, v, "neutral", b)
        out["variants"][v] = {**meta, **effects(R, v),
                              "vs_original": {"own": stats.summary(m["own"]), "partner": stats.summary(m["partner"]), "pair": stats.summary(stats.pair(R, v, base=b))}}
    # the paraphrase criterion: same sign where the original is significant; paired difference under 5 points
    checks = []
    for v, e in out["variants"].items():
        if e["kind"] != "paraphrase":
            continue
        o = out["original"][e["base"]]
        for k in ("own", "partner", "pair"):
            same = (e[k]["mean"] > 0) == (o[k]["mean"] > 0)
            checks.append({"variant": v, "effect": k, "original": o[k]["mean"], "paraphrase": e[k]["mean"], "original_significant": o[k]["p_wilcoxon"] < 0.05,
                           "same_sign": same, "difference": e["vs_original"][k]["mean"], "within_5": abs(e["vs_original"][k]["mean"]) < 5})
    out["paraphrase_checks"] = {"effects": checks, "same_sign_where_significant": [sum(c["same_sign"] for c in checks if c["original_significant"]), sum(c["original_significant"] for c in checks)],
                                "within_5_points": [sum(c["within_5"] for c in checks), len(checks)]}
    Path("results/bargain_e5_rules_robustness.json").write_text(json.dumps(clean(out), indent=1, ensure_ascii=False) + "\n")
    print(f"{'rule':22} own alone                      | partner                        | pair against default pair      | agreements alone/pair | variant - original: own, pair")
    for s in stats.STYLES:
        o = out["original"][s]
        print(f"{s:22} {fmt(o['own'])} | {fmt(o['partner'])} | {fmt(o['pair'])} | {o['agreements']['alone']:3d}/200 {o['agreements']['pair']:3d}/100")
        for v, e in out["variants"].items():
            if e["base"] == s:
                print(f"  {v:20} {fmt(e['own'])} | {fmt(e['partner'])} | {fmt(e['pair'])} | {e['agreements']['alone']:3d}/200 {e['agreements']['pair']:3d}/100 | {e['vs_original']['own']['mean']:+5.1f} (p {e['vs_original']['own']['p_wilcoxon']:.3f}), {e['vs_original']['pair']['mean']:+5.1f} (p {e['vs_original']['pair']['p_wilcoxon']:.3f})")
    pc = out["paraphrase_checks"]
    print(f"paraphrases: same sign where the original is significant {pc['same_sign_where_significant'][0]}/{pc['same_sign_where_significant'][1]}; within 5 points {pc['within_5_points'][0]}/{pc['within_5_points'][1]}")
    print("wrote results/bargain_e5_rules_robustness.json")


if __name__ == "__main__":
    main()
