"""Decompose the CooperBench paper's coop-solo gap, scoring coop and solo in one harness.

For every pair with a published coop and solo run: merge the two agents' patches with the released
harness; if they conflict, also re-merge with git's union merge and with the frozen gpt-6-luna
method; test the published solo patch; and, when coop still fails after the llm re-merge, test each
agent's own patch alone. The paper's gap then splits into evaluation artifacts, conflicts a
resolver can fix, and the failures that remain (division of labor or integration). `execute` needs
the CooperBench venv, Docker and the OpenAI key; the analysis uses only the standard library.
"""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import random
import sys

from . import s1, sensitivity, upstream

DATASET = "experiments/m0/CooperBench/dataset"
SEED = 20261107
COMPONENTS = ("artifact", "resolvable", "division_of_labor", "integration", "coop_beats_solo")


def load_config(path, root):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "from", "models", "per_pair", "decomposition", "decision_rule", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    _, _, base = sensitivity.load_config(root / config["from"], root)
    return config, {**base, "models": config["models"]}


def pairs(base, root):
    """Every usable pair with a coop and a solo run, GPT-5 and Claude first, as jobs."""
    jobs = []
    for model in base["models"]:
        solo = upstream.results(base, root, "solo", model)
        for key, entry in sorted(upstream.results(base, root, "coop", model).items()):
            if key in solo and upstream.usable(base, root, key):
                jobs.append({"model": model, "repo": key[0], "task_id": key[1], "features": [key[2], key[3]],
                             "recorded": entry, "solo_recorded": solo[key]["passed"]})
    return jobs


def merged_l3(target, features):
    """Per-feature result after the llm level, or None if it cannot be known yet."""
    ours = s1.read_json(target / "calibration.json")
    if not ours or ours.get("error"):
        return None
    if ours.get("merge") in ("clean", "identical"):
        return {str(f): bool(ours.get(str(f))) for f in features}
    if ours.get("merge") == "conflicts":
        record = s1.read_json(target / "merge.json")
        if not record:
            return None
        entry = (record.get("methods") or {}).get("llm") or {}
        if record.get("error") or entry.get("status") != "resolved":
            return {str(f): False for f in features}
        return {str(f): bool(entry.get(str(f))) for f in features}
    return None  # missing input


def pending(config, base, root, job):
    target = upstream.job_dir(base, root, job)
    ours = s1.read_json(target / "calibration.json")
    if not ours:
        return True
    if ours.get("merge") == "missing_input":
        return False
    if ours.get("merge") == "conflicts" and not ((target / "merge_union.json").is_file() and (target / "merge.json").is_file()):
        return True
    if not (sensitivity.solo_dir(config, root, job) / "solo.json").is_file():
        return True
    l3 = merged_l3(target, job["features"])
    return l3 is not None and not all(l3.values()) and not (target / "agent_eval.json").is_file()


def execute(config, base, root, shard=(0, 1)):
    s1.prepare_environment(root)
    todo = [job for job in pairs(base, root) if pending(config, base, root, job)]
    for job in todo[shard[0]::shard[1]]:
        target = upstream.job_dir(base, root, job)
        label = f"{job['model']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            print(label, flush=True)
            upstream.prepare(base, root, job)
            if not (target / "calibration.json").is_file():
                upstream.calibrate(root, job, target)
            merge = (s1.read_json(target / "calibration.json") or {}).get("merge")
            if merge == "missing_input":
                continue
            if merge == "conflicts":
                if not (target / "merge_union.json").is_file():
                    sensitivity.remerge_union(base, root, job, target)
                if not (target / "merge.json").is_file():
                    upstream.remerge(base, root, job, target)
            if not (sensitivity.solo_dir(config, root, job) / "solo.json").is_file():
                sensitivity.test_solo(base, root, config, job)
            l3 = merged_l3(target, job["features"])
            if l3 is not None and not all(l3.values()):
                s1.score_agents(target, job, root / DATASET)
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


# --- analysis (standard library only) --------------------------------------------------

def classify(job, target, solo_dir):
    """One row per pair; complete is False when a record is still missing."""
    f1, f2 = job["features"]
    features = (str(f1), str(f2))
    row = {"model": job["model"], "repo": job["repo"], "task_id": job["task_id"], "features": f"{f1} {f2}",
           "solo_recorded": int(job["solo_recorded"]), "coop_recorded": int(job["recorded"]["passed"]),
           "merge": None, "solo": None, "L1": None, "L2": None, "L3": None, "remaining": "", "complete": False}
    ours = s1.read_json(target / "calibration.json")
    solo = s1.read_json(solo_dir / "solo.json")
    if not ours or not solo or solo.get("error"):
        return row
    row["merge"] = ours.get("merge")
    if row["merge"] == "missing_input" or ours.get("error"):
        return row
    row["solo"] = int(all(solo.get(f) for f in features))
    clean = row["merge"] in ("clean", "identical")
    l1 = clean and all(ours.get(f) for f in features)
    union = sensitivity.method_pass(target, "merge_union.json", "union", features)
    l3_features = merged_l3(target, (f1, f2))
    if l3_features is None or (not clean and union is None):
        return row
    row.update(L1=int(l1), L2=int(l1 or bool(union)), L3=int(all(l3_features.values())))
    if row["solo"] and not row["L3"]:
        scores = s1.read_json(target / "agent_eval.json")
        if not scores or any((scores.get(f) or {}).get("error") for f in features):
            return row
        failed = [f for f in features if not l3_features[f]]
        row["remaining"] = "integration" if any((scores.get(f) or {}).get(f) for f in failed) else "division_of_labor"
    row["complete"] = True
    return row


def collect(config, base, root):
    return [classify(job, upstream.job_dir(base, root, job), sensitivity.solo_dir(config, root, job))
            for job in pairs(base, root)]


def contributions(row):
    """Each pair's contribution to G0 and to each component (they sum to the pair's G0)."""
    g0 = row["solo_recorded"] - row["coop_recorded"]
    out = {"G0": g0, "artifact": g0 - (row["solo"] - row["L1"]), "resolvable": row["L3"] - row["L1"],
           "resolvable_union": row["L2"] - row["L1"], "division_of_labor": 0, "integration": 0, "coop_beats_solo": 0}
    if row["remaining"]:
        out[row["remaining"]] = 1
    if not row["solo"] and row["L3"]:
        out["coop_beats_solo"] = -1
    return out


def shares(rows, samples=5000):
    """Each component's share of G0, with a 95% bootstrap CI that resamples tasks."""
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["repo"], r["task_id"]].append(contributions(r))
    tasks = list(by_task.values())

    def ratio(groups, key):
        g0 = sum(c["G0"] for g in groups for c in g)
        return sum(c[key] for g in groups for c in g) / g0 if g0 else None

    rng = random.Random(SEED)
    draws = [[rng.choice(tasks) for _ in tasks] for _ in range(samples)]
    out = {}
    for key in COMPONENTS + ("resolvable_union",):
        point = ratio(tasks, key)
        values = sorted(v for v in (ratio(d, key) for d in draws) if v is not None)
        out[key] = None if point is None else {
            "mean": point, "ci95": [values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]]}
    return out


def summarize(config, rows):
    out = {}
    for model, name in config["models"].items():
        mine = [r for r in rows if r["model"] == model]
        done = [r for r in mine if r["complete"]]
        if not done:
            out[model] = {"name": name, "pairs": len(mine), "complete": 0}
            continue
        totals = Counter()
        for r in done:
            totals.update(contributions(r))
        remaining = Counter(r["remaining"] for r in done if r["remaining"])
        positive = sum(remaining.values())
        dol = remaining["division_of_labor"] / positive if positive else None
        verdict = (None if dol is None else "division of labor" if dol >= 0.5
                   else "integration" if remaining["integration"] / positive >= 0.5 else "mixed")
        out[model] = {
            "name": name, "pairs": len(mine), "complete": len(done),
            "missing_input": sum(r["merge"] == "missing_input" for r in mine),
            "solo_recorded": sum(r["solo_recorded"] for r in done), "coop_recorded": sum(r["coop_recorded"] for r in done),
            "solo": sum(r["solo"] for r in done), "coop": {lv: sum(r[lv] for r in done) for lv in ("L1", "L2", "L3")},
            "totals": dict(totals), "remaining": dict(remaining), "division_of_labor_share": dol,
            "shares": shares(done), "verdict": verdict}
    return {"models": out}


COLUMNS = ["model", "repo", "task_id", "features", "solo_recorded", "coop_recorded", "merge", "solo", "L1", "L2", "L3",
           "remaining", "complete"]


def pct(stat):
    return "n/a" if stat is None else f"{stat['mean']:.0%} [{stat['ci95'][0]:.0%}, {stat['ci95'][1]:.0%}]"


def report(config, base, root):
    rows = collect(config, base, root)
    summary = summarize(config, rows)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "decomposition_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (output / "decomposition_summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], **summary}, indent=2))
    print(f"{config['study']}: {config['question']}")
    for s in summary["models"].values():
        print(f"\n{s['name']}: {s['complete']}/{s['pairs']} pairs complete")
        if not s["complete"]:
            continue
        t, sh = s["totals"], s["shares"]
        print(f"  solo recorded {s['solo_recorded']}, ours {s['solo']}; coop recorded {s['coop_recorded']}, "
              f"ours {s['coop']}; missing input {s['missing_input']}")
        print(f"  paper gap G0 = {t['G0']} pairs")
        for key in COMPONENTS + ("resolvable_union",):
            print(f"    {key:18} {t.get(key, 0):5}  share {pct(sh[key])}")
        print(f"  remaining (solo passes, coop fails at L3): {s['remaining']}; "
              f"division of labor share {s['division_of_labor_share']}; verdict: {s['verdict']}")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config, base = load_config(argv[0], root)
    shard = (0, 1)
    if len(argv) == 3 and argv[1] == "--shard":
        shard = tuple(int(v) for v in argv[2].split("/"))
    execute(config, base, root, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
