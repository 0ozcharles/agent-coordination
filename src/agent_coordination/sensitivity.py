"""Sensitivity of the merge-artifact conclusion on the CooperBench paper's runs.

Two checks. First, conflicts are resolved with git's union merge instead of gpt-6-luna, which gives
levels L0 (paper) to L3 (llm) of correction on every pair with a solo run. Second, on a random
sample of pairs, coop and solo are both scored in our harness, so the comparison does not mix the
paper's solo results with our coop results. `execute` needs the CooperBench venv and Docker (and the
OpenAI key for the few llm re-merges of sampled pairs); the analysis uses only the standard library.
"""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import random
import sys

from . import merge, merge_audit, paper_census, s1, upstream

DATASET = "experiments/m0/CooperBench/dataset"
SAMPLE = 150
SEED = 20261105
LEVELS = ("L0", "L1", "L2", "L3")


def load_config(path, root):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "from", "levels", "union", "same_harness", "decision_rule", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    _, census, base = merge_audit.load_config(root / config["from"], root)
    return config, census, base


def job_of(model, key, entry):
    return {"model": model, "repo": key[0], "task_id": key[1], "features": [key[2], key[3]], "recorded": entry}


def union_jobs(base, root):
    """Failed pairs whose real merge conflicts: the paper's own conflicts and the mislabeled clean pairs."""
    jobs = [job for job in paper_census.select(base, root) if job["recorded"]["conflict"]]
    return jobs + merge_audit.select(base, root)


def sample(base, root):
    """150 random pairs per model with both a coop and a solo run."""
    out = []
    for model in base["models"]:
        solo = upstream.results(base, root, "solo", model)
        coop = upstream.results(base, root, "coop", model)
        keys = sorted(k for k in coop if k in solo and upstream.usable(base, root, k))
        picked = random.Random(f"{SEED}-{model}").sample(keys, min(SAMPLE, len(keys)))
        out += [job_of(model, key, coop[key]) for key in sorted(picked)]
    return out


def solo_dir(config, root, job):
    f1, f2 = job["features"]
    return root / config["output"] / "solo" / job["model"] / job["repo"] / str(job["task_id"]) / f"f{f1}_f{f2}"


def remerge_union(base, root, job, target):
    source = merge.load_config(root / base["remerge"]["from"])
    record = merge.merge_run({**source, "methods": {"union": source["methods"]["union"]}},
                             {**job, "repeat": 1, "source": target}, target, root / DATASET)
    (target / "merge_union.json").write_text(json.dumps(record, indent=2))


def test_solo(base, root, config, job):
    from cooperbench.eval.sandbox import test_solo as run
    f1, f2 = job["features"]
    path = (f"{job['recorded']['upstream'].replace('coop/', 'solo/', 1)}/feature{f1}_feature{f2}/"
            f"patch_{job['model']}_k1_feature{f1}_feature{f2}.patch")
    patch = upstream.fetch(base, root, path)
    result = run(repo_name=job["repo"], task_id=job["task_id"], feature1_id=f1, feature2_id=f2, patch=patch,
                 backend="docker", dataset_dir=root / DATASET)
    target = solo_dir(config, root, job)
    target.mkdir(parents=True, exist_ok=True)
    (target / "solo.json").write_text(json.dumps(
        {"error": result.get("error"), "empty_patch": not patch.strip(),
         str(f1): bool((result.get("feature1") or {}).get("passed")),
         str(f2): bool((result.get("feature2") or {}).get("passed"))}, indent=2))


def tasks(config, base, root):
    """(kind, job) still to run: union re-merges first, then the sampled pairs."""
    out = []
    for job in union_jobs(base, root):
        if not (upstream.job_dir(base, root, job) / "merge_union.json").is_file():
            out.append(("union", job))
    for job in sample(base, root):
        target = upstream.job_dir(base, root, job)
        ours = s1.read_json(target / "calibration.json")
        pending = not ours or (ours.get("merge") == "conflicts" and not (
            (target / "merge_union.json").is_file() and (target / "merge.json").is_file()))
        if pending or not (solo_dir(config, root, job) / "solo.json").is_file():
            out.append(("sample", job))
    return out


def execute(config, base, root, shard=(0, 1)):
    s1.prepare_environment(root)
    for kind, job in tasks(config, base, root)[shard[0]::shard[1]]:
        target = upstream.job_dir(base, root, job)
        label = f"{kind} {job['model']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            print(label, flush=True)
            upstream.prepare(base, root, job)
            if kind == "union":
                remerge_union(base, root, job, target)
                continue
            if not (target / "calibration.json").is_file():
                upstream.calibrate(root, job, target)
            if (s1.read_json(target / "calibration.json") or {}).get("merge") == "conflicts":
                if not (target / "merge_union.json").is_file():
                    remerge_union(base, root, job, target)
                if not (target / "merge.json").is_file():
                    upstream.remerge(base, root, job, target)
            if not (solo_dir(config, root, job) / "solo.json").is_file():
                test_solo(base, root, config, job)
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


# --- analysis (standard library only) --------------------------------------------------

def method_pass(target, name, method, features):
    """True/False if the record exists and the method resolved; None otherwise."""
    record = s1.read_json(target / name)
    if not record or record.get("error"):
        return None
    entry = (record.get("methods") or {}).get(method) or {}
    if entry.get("status") != "resolved":
        return False
    return all(entry.get(str(f)) for f in features)


def closure(items, level, solo="solo", samples=5000):
    """Share of the paper's coop-solo gap (recorded solo minus L0) that a level closes, 95% CI over tasks.

    `level` and `solo` name the coop and solo results being compared; with our harness's solo the
    remaining gap is our solo minus our coop.
    """
    by_task = defaultdict(list)
    for it in items:
        gap = it["solo"] - it["L0"]
        by_task[it["repo"], it["task_id"]].append((gap - (it[solo] - it[level]), gap))
    tasks_ = list(by_task.values())

    def ratio(groups):
        num = sum(d for g in groups for d, _ in g)
        den = sum(s for g in groups for _, s in g)
        return num / den if den else None

    point = ratio(tasks_)
    if point is None:
        return None
    rng = random.Random(SEED)
    draws = sorted(r for r in (ratio([rng.choice(tasks_) for _ in tasks_]) for _ in range(samples)) if r is not None)
    return {"mean": point, "tasks": len(tasks_),
            "ci95": [draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]]}


def collect(config, base, root):
    census_rows = paper_census.collect(base, root)
    clean_fix = {(r["model"], r["repo"], r["task_id"], r["features"]) for r in census_rows
                 if not r["conflict"] and r["retest_merge"] == "clean"
                 and set((r["retest_passed"] or "").split()) == set(r["features"].split())}
    conflict_jobs = {(j["model"], j["repo"], j["task_id"], f"{j['features'][0]} {j['features'][1]}"): j
                     for j in union_jobs(base, root)}
    sampled = {(j["model"], j["repo"], j["task_id"], f"{j['features'][0]} {j['features'][1]}") for j in sample(base, root)}
    llm = base["remerge"]["method"]
    rows = []
    for model in base["models"]:
        solo = upstream.results(base, root, "solo", model)
        for key, entry in sorted(upstream.results(base, root, "coop", model).items()):
            if key not in solo or not upstream.usable(base, root, key):
                continue
            features = f"{key[2]} {key[3]}"
            name = (model, key[0], key[1], features)
            job = job_of(model, key, entry)
            target = upstream.job_dir(base, root, job)
            union = llm_pass = None
            if name in conflict_jobs:
                union = method_pass(target, "merge_union.json", "union", key[2:])
                llm_pass = method_pass(target, "merge.json", llm, key[2:])
            l1 = entry["passed"] or name in clean_fix
            row = {"model": model, "repo": key[0], "task_id": key[1], "features": features,
                   "in_sample": name in sampled, "solo": int(solo[key]["passed"]), "L0": int(entry["passed"]),
                   "L1": int(l1), "L2": int(l1 or bool(union)), "L3": int(l1 or bool(llm_pass)),
                   "union_tested": union is not None, "llm_tested": llm_pass is not None}
            if name in sampled:
                ours = s1.read_json(target / "calibration.json") or {}
                solo_ours = s1.read_json(solo_dir(config, root, job) / "solo.json")
                status = ours.get("merge")
                coop = {}
                if status in ("clean", "identical") and not ours.get("error"):
                    both = all(ours.get(str(f)) for f in key[2:])
                    coop = {"L1": both, "L2": both, "L3": both}
                elif status == "conflicts":
                    coop = {"L1": False, "L2": bool(method_pass(target, "merge_union.json", "union", key[2:])),
                            "L3": bool(method_pass(target, "merge.json", llm, key[2:]))}
                row.update({"sample_merge": status,
                            "solo_ours": None if not solo_ours or solo_ours.get("error") else
                            int(all(solo_ours.get(str(f)) for f in key[2:])),
                            **{f"ours_{lv}": (None if lv not in coop else int(coop[lv])) for lv in ("L1", "L2", "L3")}})
            rows.append(row)
    return rows


def summarize(config, rows):
    out = {}
    for model in sorted({r["model"] for r in rows}):
        mine = [r for r in rows if r["model"] == model]
        full = {lv: closure(mine, lv) for lv in ("L1", "L2", "L3")}
        counts = {lv: sum(r[lv] for r in mine) for lv in LEVELS}
        complete = [r for r in mine if r["in_sample"] and r.get("solo_ours") is not None
                    and r.get("ours_L1") is not None]
        agree = Counter((r["solo"], r["solo_ours"]) for r in complete)
        out[model] = {
            "pairs": len(mine), "solo": sum(r["solo"] for r in mine), "coop": counts,
            "union_tested": sum(r["union_tested"] for r in mine), "llm_tested": sum(r["llm_tested"] for r in mine),
            "closed": full,
            "sample": {"pairs": sum(r["in_sample"] for r in mine), "complete": len(complete),
                       "merge": dict(Counter(r.get("sample_merge") for r in mine if r["in_sample"])),
                       "solo_recorded": sum(r["solo"] for r in complete),
                       "solo_ours": sum(r["solo_ours"] for r in complete),
                       "solo_agreement": {f"recorded {a} / ours {b}": n for (a, b), n in sorted(agree.items())},
                       "coop_paper": sum(r["L0"] for r in complete),
                       "coop_ours": {lv: sum(r[f"ours_{lv}"] for r in complete) for lv in ("L1", "L2", "L3")},
                       "closed": {lv: closure(complete, f"ours_{lv}", "solo_ours") for lv in ("L1", "L2", "L3")},
                       "closed_paper_ruler": {lv: closure(complete, lv) for lv in ("L1", "L2", "L3")}},
        }
    decision = {}
    for model, s in out.items():
        l2 = s["closed"]["L2"]
        if l2 is None:
            decision[model] = None
            continue
        verdict = ("holds without llm conflict resolution" if l2["mean"] >= 1 / 3
                   else "depends on conflict resolution: report L1 to L3")
        same = s["sample"]["closed"]["L2"]
        if same is not None and abs(same["mean"] - l2["mean"]) > 0.15:
            verdict += "; same-harness figure is the main one"
        decision[model] = verdict
    return {"models": out, "decision": decision}


COLUMNS = ["model", "repo", "task_id", "features", "in_sample", "solo", "L0", "L1", "L2", "L3", "union_tested",
           "llm_tested", "sample_merge", "solo_ours", "ours_L1", "ours_L2", "ours_L3"]


def pct(stat):
    return "n/a" if stat is None else f"{stat['mean']:.0%} [{stat['ci95'][0]:.0%}, {stat['ci95'][1]:.0%}]"


def report(config, base, root):
    rows = collect(config, base, root)
    summary = summarize(config, rows)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "sensitivity_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (output / "sensitivity_summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], **summary}, indent=2))
    print(f"{config['study']}: {config['question']}")
    for model, s in summary["models"].items():
        c = s["coop"]
        print(f"\n{model}: {s['pairs']} pairs with solo; solo {s['solo']}; coop {c} "
              f"(union records {s['union_tested']}, llm records {s['llm_tested']})")
        print(f"  gap closed: L1 {pct(s['closed']['L1'])}, L2 {pct(s['closed']['L2'])}, L3 {pct(s['closed']['L3'])}")
        m = s["sample"]
        print(f"  sample: {m['complete']}/{m['pairs']} complete, our merge {m['merge']}")
        print(f"    solo recorded {m['solo_recorded']} vs ours {m['solo_ours']}: {m['solo_agreement']}")
        print(f"    coop paper {m['coop_paper']}, ours {m['coop_ours']}")
        print(f"    gap closed, same harness: L1 {pct(m['closed']['L1'])}, L2 {pct(m['closed']['L2'])}, "
              f"L3 {pct(m['closed']['L3'])}; paper ruler on the sample: L2 {pct(m['closed_paper_ruler']['L2'])}")
    print(f"\ndecision: {summary['decision']}")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config, _, base = load_config(argv[0], root)
    shard = (0, 1)
    if len(argv) == 3 and argv[1] == "--shard":
        shard = tuple(int(v) for v in argv[2].split("/"))
    execute(config, base, root, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
