"""Failure census of the CooperBench paper's coop runs: why does each failed pair fail?

Each feature that fails in the merged result gets one of S1's labels from three measurements: the
agent's own patch tested alone, the merged result, and the published solo runs. Clean-merge pairs
are tested here; conflicted pairs reuse the upstream check's own-patch tests. `execute` needs the
CooperBench venv and Docker; the analysis uses only the standard library.
"""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import sys

from . import s1, upstream

ORDER = ("integration", "context", "capability", "unknown")
REFINED_ORDER = ("integration", "handoff", "context", "capability", "unknown")
SOLO_THRESHOLD = 0.5
DATASET = "experiments/m0/CooperBench/dataset"


def load_config(path, root):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "from", "labels", "decision_rule", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    return config, upstream.load_config(root / config["from"])


def select(base, root):
    """Every usable coop pair that fails the paper's pipeline, as upstream jobs."""
    jobs = []
    for model in base["models"]:
        for key, entry in sorted(upstream.results(base, root, "coop", model).items()):
            if upstream.usable(base, root, key) and not entry["passed"]:
                jobs.append({"model": model, "repo": key[0], "task_id": key[1], "features": [key[2], key[3]],
                             "recorded": entry})
    return jobs


def done(base, root, job):
    target = upstream.job_dir(base, root, job)
    return (target / "agent_eval.json").is_file() and (
        job["recorded"]["conflict"] or (target / "calibration.json").is_file())


# --- execution (CooperBench venv only) -------------------------------------------------

def execute(base, root, shard=(0, 1)):
    s1.prepare_environment(root)
    todo = [job for job in select(base, root) if not done(base, root, job)]
    for job in todo[shard[0]::shard[1]]:
        target = upstream.job_dir(base, root, job)
        label = f"{job['model']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            print(f"census {label}", flush=True)
            upstream.prepare(base, root, job)
            if not job["recorded"]["conflict"] and not (target / "calibration.json").is_file():
                upstream.calibrate(root, job, target)  # merged re-test in our harness
            s1.score_agents(target, job, root / DATASET)
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


# --- labels (pure) ---------------------------------------------------------------------

def solo_rates(solo):
    """Share of a task's solo pairs that pass each feature, keyed (repo, task id, feature)."""
    counts = defaultdict(lambda: [0, 0])
    for (repo, task, f1, f2), entry in solo.items():
        for feature, passed in zip((f1, f2), entry.get("features") or ()):
            if passed is not None:
                counts[repo, task, feature][0] += bool(passed)
                counts[repo, task, feature][1] += 1
    return {key: passed / total for key, (passed, total) in counts.items()}


def merged_result(entry, retest, f1, f2):
    """Per-feature merged result and where it came from; (None, "missing") if there is none.

    Clean pairs use the paper's naive merge; conflicted pairs use the last stage the paper tested.
    A clean pair with no recorded result falls back to our re-test when our merge is clean too.
    """
    stages = entry["stages"]
    if entry["conflict"]:
        tested = [s for s in ("llm", "union") if any(v is not None for v in stages[s])]
        values, source = (stages[tested[0]], tested[0]) if tested else ([False, False], "none")
    elif None not in stages["naive"]:
        values, source = stages["naive"], "naive"
    elif retest and retest.get("merge") == "clean" and not retest.get("error"):
        values, source = [retest.get(str(f1)), retest.get(str(f2))], "retest"
    else:
        return None, "missing"
    return {str(f1): bool(values[0]), str(f2): bool(values[1])}, source


def feature_labels(features, merged, own, rates, repo, task):
    labels = {}
    for f in features:
        if merged[f]:
            continue
        if own[f]:
            labels[f] = "integration"
        else:
            rate = rates.get((repo, task, int(f)))
            labels[f] = "unknown" if rate is None else "context" if rate >= SOLO_THRESHOLD else "capability"
    return labels


def first(labels, order):
    return next((c for c in order if c in set(labels.values())), "")


def encode(labels):
    return " ".join(f"{f}:{c}" for f, c in labels.items())


# --- analysis (standard library only) --------------------------------------------------

def collect(base, root, jobs=None):
    """Census rows; `jobs` replaces the recorded failed pairs (the re-scoring passes corrected ones)."""
    rows = []
    rates = {model: solo_rates(upstream.results(base, root, "solo", model)) for model in base["models"]}
    solo = {model: upstream.results(base, root, "solo", model) for model in base["models"]}
    for job in select(base, root) if jobs is None else jobs:
        target = upstream.job_dir(base, root, job)
        f1, f2 = (str(f) for f in job["features"])
        key = (job["repo"], job["task_id"], int(f1), int(f2))
        entry = job["recorded"]
        retest = s1.read_json(target / "calibration.json")
        agents = s1.read_json(target / "agent_eval.json")
        merged, source = merged_result(entry, retest, f1, f2)
        pair_solo = solo[job["model"]].get(key)
        row = {"model": job["model"], "repo": job["repo"], "task_id": job["task_id"], "features": f"{f1} {f2}",
               "conflict": entry["conflict"], "merged_source": source,
               "merged_passed": " ".join(f for f in (f1, f2) if merged and merged[f]),
               "solo_pair_passed": pair_solo["passed"] if pair_solo else None,
               "solo_rates": " ".join(f"{f}:{rates[job['model']][key[:2] + (int(f),)]:.2f}" for f in (f1, f2)
                                      if key[:2] + (int(f),) in rates[job["model"]]),
               "retest_merge": retest.get("merge") if retest else None,
               "retest_passed": " ".join(f for f in (f1, f2) if retest.get(f)) if retest else None,
               "tested": bool(agents), "own_passed": "", "cross_passed": "", "coordination": None,
               "feature_labels": "", "label": "", "refined_feature_labels": "", "refined_label": "",
               "robust_feature_labels": "", "robust_label": ""}
        if agents and merged is not None and not any((agents.get(f) or {}).get("error") for f in (f1, f2)):
            own = {f: bool(agents[f][f]) for f in (f1, f2)}
            row["own_passed"] = " ".join(f for f in (f1, f2) if own[f])
            row["cross_passed"] = " ".join(f for f, g in ((f1, f2), (f2, f1)) if agents[f][g])
            labels = feature_labels((f1, f2), merged, own, rates[job["model"]], job["repo"], job["task_id"])
            texts = {f: (target / f"agent{f}.patch").read_text() for f in (f1, f2)}
            handed = set(s1.handoffs(row, job, texts, root / DATASET).split())
            refined = {f: "handoff" if c in ("context", "capability") and f in handed else c
                       for f, c in labels.items()}
            row["coordination"] = bool(row["cross_passed"]) or (bool(texts[f1].strip()) and texts[f1] == texts[f2])
            # Robustness: drop failures of clean pairs that our merged re-test does not reproduce.
            if entry["conflict"]:
                robust = dict(labels)
            elif retest.get("merge") == "clean" and not retest.get("error"):
                robust = {f: c for f, c in labels.items() if not retest.get(f)}
            else:
                robust = {}
            row.update(feature_labels=encode(labels), label=first(labels, ORDER) or "no_failed_feature",
                       refined_feature_labels=encode(refined), refined_label=first(refined, REFINED_ORDER) or "no_failed_feature",
                       robust_feature_labels=encode(robust), robust_label=first(robust, ORDER))
        rows.append(row)
    return rows


def counts(rows, field):
    return dict(Counter(r[field] for r in rows if r[field]).most_common())


def share(rows, field, value):
    pairs = [r for r in rows if r[field]]
    return upstream.clustered(pairs, lambda r: r[field] == value)


def summarize(config, base, rows):
    out = {}
    for model, name in base["models"].items():
        subset = [r for r in rows if r["model"] == model]
        labeled = [r for r in subset if r["label"]]
        clean = [r for r in labeled if not r["conflict"]]
        gap = [r for r in labeled if r["solo_pair_passed"]]
        features = Counter(c for r in labeled for c in (x.split(":")[1] for x in r["feature_labels"].split()))
        semantic = sum(r["label"] == "integration" and not r["conflict"] for r in labeled)
        retested = [r for r in subset if not r["conflict"] and r["retest_merge"]]
        failed_clean = [(r, f) for r in retested if r["retest_merge"] == "clean"
                        for f in r["features"].split() if r["merged_source"] == "naive" and f not in r["merged_passed"].split()]
        out[model] = {
            "name": name, "failed_pairs": len(subset), "clean": sum(not r["conflict"] for r in subset),
            "tested": sum(r["tested"] for r in subset), "labeled": len(labeled),
            "missing_merged_result": sum(r["merged_source"] == "missing" for r in subset),
            "labels": counts(labeled, "label"), "refined_labels": counts(labeled, "refined_label"),
            "clean_labels": counts(clean, "label"), "clean_refined_labels": counts(clean, "refined_label"),
            "feature_labels": dict(features.most_common()),
            "integration": {"semantic": semantic, "conflict": sum(r["label"] == "integration" and r["conflict"] for r in labeled)},
            "coordination_flag": sum(bool(r["coordination"]) for r in labeled),
            "gap": {"pairs": len(gap), "labels": counts(gap, "label"), "refined_labels": counts(gap, "refined_label"),
                    "integration": share(gap, "label", "integration"),
                    "robust_pairs": sum(bool(r["robust_label"]) for r in gap),
                    "robust_integration": share(gap, "robust_label", "integration")},
            "retest": {"clean_pairs": len(retested), "merge_clean": sum(r["retest_merge"] == "clean" for r in retested),
                       "failed_features": len(failed_clean),
                       "reproduced": sum(f not in (r["retest_passed"] or "").split() for r, f in failed_clean)},
        }
    decided = [out[m]["gap"]["integration"] for m in out]
    if all(d is not None for d in decided):
        if all(d["mean"] >= 0.5 for d in decided):
            verdict = "integration: work on the merged code"
        elif all(d["mean"] < 0.5 for d in decided):
            verdict = "own feature: look at how the work is split"
        else:
            verdict = "split by model"
    else:
        verdict = None
    return {"models": out, "verdict": verdict}


ROW_COLUMNS = ["model", "repo", "task_id", "features", "conflict", "merged_source", "merged_passed", "own_passed",
               "cross_passed", "solo_pair_passed", "solo_rates", "retest_merge", "retest_passed", "coordination",
               "feature_labels", "label", "refined_feature_labels", "refined_label", "robust_feature_labels",
               "robust_label"]


def pct(stat):
    return "n/a" if stat is None else f"{stat['mean']:.0%} [{stat['ci95'][0]:.0%}, {stat['ci95'][1]:.0%}]"


def report(config, base, root):
    rows = collect(base, root)
    summary = summarize(config, base, rows)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "census_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (output / "census_summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], **summary}, indent=2))
    print(f"{config['study']}: {config['question']}")
    for s in summary["models"].values():
        print(f"\n{s['name']}: {s['failed_pairs']} failed pairs ({s['clean']} clean merges), "
              f"{s['tested']} tested, {s['labeled']} labeled, {s['missing_merged_result']} without a merged result")
        print(f"  all failed pairs:  {s['labels']}")
        print(f"    with handoff:    {s['refined_labels']}")
        print(f"  clean merges:      {s['clean_labels']}")
        print(f"    with handoff:    {s['clean_refined_labels']}")
        print(f"  integration: {s['integration']['semantic']} semantic, {s['integration']['conflict']} conflict; "
              f"coordination flag on {s['coordination_flag']}")
        g = s["gap"]
        print(f"  gap pairs (solo passes, coop fails): {g['pairs']}: {g['labels']}")
        print(f"    integration share {pct(g['integration'])}; robust ({g['robust_pairs']} pairs) {pct(g['robust_integration'])}")
        t = s["retest"]
        print(f"  merged re-test: {t['merge_clean']}/{t['clean_pairs']} clean in our harness; "
              f"{t['reproduced']}/{t['failed_features']} recorded failures reproduced")
    print(f"\nverdict: {summary['verdict']}")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config, base = load_config(argv[0], root)
    shard = (0, 1)
    if len(argv) == 3 and argv[1] == "--shard":
        shard = tuple(int(v) for v in argv[2].split("/"))
    execute(base, root, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
