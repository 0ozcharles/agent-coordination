"""Evaluate the confirmatory hypotheses of configs/bargain_replication.json.

Each hypothesis is a per-instance paired difference (see stats.py: the two seats of an instance are averaged,
differences are matched by seat). Primary test: two-sided Wilcoxon signed-rank on the confirmation sample
(E1-R, E2-R), Holm-corrected over the whole family. The same statistics are reported on the discovery sample
(E1, E2) and on both pooled. Reads results/bargain_*_rows.csv; writes results/bargain_replication.json.
Run from the repository root after the replication arms have been reported.
"""
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import stats  # noqa: E402

SPEC = json.loads(Path("configs/bargain_replication.json").read_text())


def discovery(arm):
    """The discovery-sample arm behind a confirmation arm: e1r -> e1, e2r_<condition> -> e2_<condition>."""
    return arm.replace("e1r", "e1", 1) if arm.startswith("e1r") else arm.replace("e2r_", "e2_", 1)


def scale_of(arm):
    return {"p1": 100.0, "p2": 30.0} if "low_stakes_p2" in arm else None


def differences(h, arm):
    """One difference per instance, keyed by instance id, for hypothesis h on one arm."""
    path = Path(f"results/bargain_{arm}_rows.csv")
    if not path.is_file():
        return None
    R, scale = stats.load_rows(path), scale_of(arm)
    ids = stats.instances_of(R)
    if h["kind"] == "seat":  # the strategy in one named seat against the neutral agent in that seat
        seat = h["seat"]
        cell = f"{h['strategy']}-neutral" if seat == "p1" else f"neutral-{h['strategy']}"
        k = 100.0 / scale[seat] if scale else 1.0
        return {i: (R[(cell, i)][f"payoff_{seat}"] - R[("neutral-neutral", i)][f"payoff_{seat}"]) * k for i in ids if (cell, i) in R}
    if h["kind"] == "alone":
        d = stats.alone(R, h["strategy"], scale)[h["measure"]]
    elif h["kind"] == "pair":
        d = stats.pair(R, h["strategy"])
    elif h["kind"] == "contrast":
        d = stats.contrast(R, h["strategy"], h["other"], h.get("partner", "neutral"), scale)
    elif h["kind"] == "reply":
        d = stats.reply(R, h["strategy"], h["to"], h.get("base", "neutral"), scale)
    else:
        raise ValueError(h["kind"])
    if len(d) != len(ids):  # every instance must contribute; a missing cell would misalign the ids
        return None
    return dict(zip(ids, d))


def sample(h, arms):
    """Per-instance differences averaged over the arms of a hypothesis (the arms share their instances)."""
    per_arm = [differences(h, a) for a in arms]
    if any(d is None for d in per_arm):
        return None
    ids = sorted(set.intersection(*(set(d) for d in per_arm)))
    return [st.mean(d[i] for d in per_arm) for i in ids]


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return round(o, 4) if isinstance(o, float) else o


def main():
    out = {"study": SPEC["study"], "test": SPEC["test"], "hypotheses": []}
    for h in SPEC["hypotheses"]:
        conf = sample(h, h["arms"])
        disc = sample(h, [discovery(a) for a in h["arms"]])
        entry = {"id": h["id"], "claim": h["claim"], "direction": h["direction"], "arms": h["arms"],
                 "confirmation": stats.summary(conf) if conf else None, "discovery": stats.summary(disc) if disc else None,
                 "pooled": stats.summary(conf + disc) if conf and disc else None}
        out["hypotheses"].append(entry)
    done = [e for e in out["hypotheses"] if e["confirmation"]]
    adjusted = stats.holm([e["confirmation"]["p_wilcoxon"] for e in done])
    for e, p in zip(done, adjusted):
        sign = 1 if e["direction"] == "+" else -1
        e["p_holm"] = p
        e["confirmed"] = bool(p < 0.05 and sign * e["confirmation"]["mean"] > 0)
    out["evaluated"] = len(done)
    out["confirmed"] = sum(e["confirmed"] for e in done)
    Path("results/bargain_replication.json").write_text(json.dumps(clean(out), indent=1, ensure_ascii=False) + "\n")
    print(f"{'id':4} {'dir':3} {'discovery':>24} | {'confirmation':>44} | holm   verdict")
    for e in out["hypotheses"]:
        d, c = e["discovery"], e["confirmation"]
        ds = f"{d['mean']:+6.1f} (n={d['n']}, p {d['p_wilcoxon']:.3f})" if d else "n/a"
        cs = f"{c['mean']:+6.1f} [{c['ci95'][0]:+5.1f}, {c['ci95'][1]:+5.1f}] n={c['n']} p {c['p_wilcoxon']:.4f}" if c else "not played yet"
        verdict = ("CONFIRMED" if e.get("confirmed") else "not confirmed") if c else ""
        print(f"{e['id']:4} {e['direction']:3} {ds:>24} | {cs:>44} | {e.get('p_holm', float('nan')):.4f} {verdict}  {e['claim'][:70]}")
    print(f"evaluated {out['evaluated']} of {len(out['hypotheses'])}; confirmed {out['confirmed']}; wrote results/bargain_replication.json")


if __name__ == "__main__":
    main()
