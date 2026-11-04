"""Offline check on the runs behind the CooperBench paper (CooperBench/trajectories on Hugging Face).

The paper scored coop runs with git merge-file, then a union merge, then a small learned resolver,
and published each pair's agent patches and per-stage test results. We first check that our harness
reproduces the recorded results, then re-merge the conflicted pairs the paper's pipeline failed with
the merge test's frozen llm method. No agent is re-run. `execute` needs the CooperBench venv;
downloading and the analysis use only the standard library.
"""
from collections import defaultdict
import csv
import json
from pathlib import Path
import random
import sys
import urllib.error
import urllib.request

from . import merge, s1

FILE = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{path}"
TREE = "https://huggingface.co/api/datasets/{dataset}/tree/{revision}/{path}"
SETTINGS = ("coop", "solo")
SEED = 20261103


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "decision_rule", "source", "models", "sets", "remerge", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    return config


def dataset_repo(name):
    """The published runs call typst 'typst'; the dataset calls it 'typst_task'."""
    return name if name.endswith("_task") else f"{name}_task"


# --- published data (cached under <output>/published) -----------------------------------

def fetch(config, root, path):
    """One published file as text; a missing file is cached as empty."""
    target = root / config["output"] / "published" / path
    if not target.is_file():
        try:
            with urllib.request.urlopen(FILE.format(path=path, **config["source"])) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            data = b""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target.read_text()


def index(config, root):
    """Task directories per setting: {"coop": ["coop/<repo>/task<id>", ...], ...}."""
    target = root / config["output"] / "published" / "index.json"
    if not target.is_file():
        def tree(path):
            with urllib.request.urlopen(TREE.format(path=path, **config["source"])) as response:
                return [entry["path"] for entry in json.load(response) if entry["type"] == "directory"]
        found = {setting: [task for repo in tree(setting) for task in tree(repo)] for setting in SETTINGS}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(found, indent=2))
    return json.loads(target.read_text())


def both(entry, stage):
    return bool(entry.get(f"feature1_{stage}_test_passed") and entry.get(f"feature2_{stage}_test_passed"))


def results(config, root, setting, model):
    """Recorded results per pair, keyed (dataset repo, task id, f1, f2)."""
    out = {}
    for task_path in index(config, root)[setting]:
        text = fetch(config, root, f"{task_path}/aggregated_results_{model}_k1.json")
        if not text.strip():
            continue
        _, upstream_repo, task = task_path.split("/")
        for entry in json.loads(text).get("detailed_results", []):
            key = (dataset_repo(upstream_repo), int(task.removeprefix("task")), entry["feature1_id"], entry["feature2_id"])
            if setting == "solo":
                out[key] = {"passed": bool(entry.get("both_tests_passed")),
                            "features": [entry.get("feature1_test_passed"), entry.get("feature2_test_passed")]}
                continue
            out[key] = {"upstream": task_path, "conflict": bool(entry.get("has_naive_merge_conflict")),
                        "naive": [entry.get("feature1_naive_merge_test_passed"),
                                  entry.get("feature2_naive_merge_test_passed")],
                        "stages": {stage: [entry.get(f"feature{n}_{stage}_merge_test_passed") for n in (1, 2)]
                                   for stage in ("naive", "union", "llm")},
                        "passed": any(both(entry, stage) for stage in ("naive_merge", "union_merge", "llm_merge"))}
    return out


def usable(config, root, key):
    repo = key[0]
    return (repo not in config.get("exclude_repos", [])
            and (root / "experiments/m0/CooperBench/dataset" / repo / f"task{key[1]}").is_dir())


def select(config, root, name):
    """Jobs in a set, over every model, in a stable order."""
    jobs = []
    for model in config["models"]:
        coop = {k: v for k, v in results(config, root, "coop", model).items() if usable(config, root, k)}
        if name == "calibration":
            spec = config["sets"]["calibration"]
            keys = sorted(k for k, v in coop.items() if not v["conflict"] and None not in v["naive"])
            keys = sorted(random.Random(spec["seed"]).sample(keys, min(spec["sample"], len(keys))))
        elif name in ("remerge", "own"):
            keys = sorted(k for k, v in coop.items() if v["conflict"] and not v["passed"])
        else:
            raise ValueError(f"Unknown set '{name}'.")
        jobs += [{"model": model, "repo": k[0], "task_id": k[1], "features": [k[2], k[3]], "recorded": coop[k]}
                 for k in keys]
    return jobs


def job_dir(config, root, job):
    f1, f2 = job["features"]
    return root / config["output"] / job["model"] / job["repo"] / str(job["task_id"]) / f"f{f1}_f{f2}"


def prepare(config, root, job):
    """Copy the two agents' published patches into the job directory as agent<f>.patch."""
    target = job_dir(config, root, job)
    target.mkdir(parents=True, exist_ok=True)
    f1, f2 = job["features"]
    for fid in (f1, f2):
        path = f"{job['recorded']['upstream']}/feature{f1}_feature{f2}/patch_{job['model']}_k1_feature{fid}.patch"
        (target / f"agent{fid}.patch").write_text(fetch(config, root, path))
    return target


# --- execution (CooperBench venv only) -------------------------------------------------

def calibrate(root, job, target):
    from cooperbench.eval.sandbox import test_merged
    f1, f2 = job["features"]
    result = test_merged(job["repo"], job["task_id"], f1, f2, patch1=(target / f"agent{f1}.patch").read_text(),
                         patch2=(target / f"agent{f2}.patch").read_text(), backend="docker",
                         dataset_dir=root / "experiments/m0/CooperBench/dataset")
    (target / "calibration.json").write_text(json.dumps(
        {"merge": (result.get("merge") or {}).get("status"), "error": result.get("error"),
         str(f1): bool((result.get("feature1") or {}).get("passed")),
         str(f2): bool((result.get("feature2") or {}).get("passed"))}, indent=2))


def remerge(config, root, job, target):
    source = merge.load_config(root / config["remerge"]["from"])
    method = config["remerge"]["method"]
    record = merge.merge_run({**source, "methods": {method: source["methods"][method]}},
                             {**job, "repeat": 1, "source": target}, target,
                             root / "experiments/m0/CooperBench/dataset")
    (target / "merge.json").write_text(json.dumps(record, indent=2))


RECORDS = {"calibration": "calibration.json", "remerge": "merge.json", "own": "agent_eval.json"}


def execute(config, root, name, shard=(0, 1)):
    s1.prepare_environment(root)
    dataset_dir = root / "experiments/m0/CooperBench/dataset"
    steps = {"calibration": lambda job, target: calibrate(root, job, target),
             "remerge": lambda job, target: remerge(config, root, job, target),
             "own": lambda job, target: s1.score_agents(target, job, dataset_dir)}
    for job in select(config, root, name)[shard[0]::shard[1]]:
        target = job_dir(config, root, job)
        if (target / RECORDS[name]).is_file():
            continue
        label = f"{job['model']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            print(f"{name} {label}", flush=True)
            prepare(config, root, job)
            steps[name](job, target)
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


# --- analysis (standard library only) --------------------------------------------------

def clustered(pairs, value, samples=5000):
    """Mean of value(pair) over pairs, with a 95% bootstrap CI that resamples tasks."""
    by_task = defaultdict(list)
    for pair in pairs:
        by_task[pair["repo"], pair["task_id"]].append(value(pair))
    tasks = list(by_task.values())
    if not tasks:
        return None
    rng = random.Random(SEED)
    means = []
    for _ in range(samples):
        drawn = [rng.choice(tasks) for _ in tasks]
        means.append(sum(map(sum, drawn)) / sum(map(len, drawn)))
    means.sort()
    return {"mean": sum(map(sum, tasks)) / sum(map(len, tasks)), "tasks": len(tasks),
            "ci95": [means[int(0.025 * samples)], means[int(0.975 * samples) - 1]]}


def collect(config, root):
    """Per model: every usable coop pair with the paper's result, our re-merge result and solo's."""
    rows, calibration = [], []
    remerged = {(j["model"], j["repo"], j["task_id"], *j["features"]): j for j in select(config, root, "remerge")}
    for job in select(config, root, "calibration"):
        mine = s1.read_json(job_dir(config, root, job) / "calibration.json")
        if mine:
            calibration.append({"model": job["model"], "merge": mine.get("merge"), "error": mine.get("error"),
                                "agree": [mine.get(str(f)) == bool(r) for f, r in zip(job["features"], job["recorded"]["naive"])]})
    for model in config["models"]:
        solo = results(config, root, "solo", model)
        for key, entry in results(config, root, "coop", model).items():
            if not usable(config, root, key):
                continue
            row = {"model": model, "repo": key[0], "task_id": key[1], "features": f"{key[2]} {key[3]}",
                   "conflict": entry["conflict"], "paper_passed": entry["passed"],
                   "solo_passed": solo[key]["passed"] if key in solo else None,
                   "remerge_needed": (model, *key) in remerged, "remerge_status": None, "remerge_passed": None,
                   "own_both": None}
            if row["remerge_needed"]:
                scores = s1.read_json(job_dir(config, root, remerged[(model, *key)]) / "agent_eval.json")
                if scores:
                    # Integration case: each agent's own patch passes its own feature.
                    row["own_both"] = all((scores.get(str(f)) or {}).get(str(f)) for f in key[2:])
                record = s1.read_json(job_dir(config, root, remerged[(model, *key)]) / "merge.json")
                if record:
                    method = (record.get("methods") or {}).get(config["remerge"]["method"]) or {}
                    row["remerge_status"] = "error" if record.get("error") else method.get("status")
                    row["remerge_passed"] = bool(method.get(str(key[2])) and method.get(str(key[3])))
                    row["cost_usd"] = method.get("cost_usd")
            row["passed"] = row["paper_passed"] or bool(row["remerge_passed"])
            rows.append(row)
    return rows, calibration


def summarize(config, rows, calibration):
    out = {}
    for model, name in config["models"].items():
        subset = [r for r in rows if r["model"] == model]
        both = [r for r in subset if r["solo_passed"] is not None]
        agree = [a for c in calibration if c["model"] == model for a in c["agree"]]
        todo = [r for r in subset if r["remerge_needed"]]
        entry = {"name": name, "pairs": len(subset), "tasks": len({(r["repo"], r["task_id"]) for r in subset}),
                 "calibration": {"pairs": len(agree) // 2, "features_agree": sum(agree), "features": len(agree)},
                 "remerge": {"needed": len(todo), "done": sum(r["remerge_status"] is not None for r in todo),
                             "errors": sum(r["remerge_status"] == "error" for r in todo),
                             "recovered": sum(bool(r["remerge_passed"]) for r in todo),
                             "cost_usd": round(sum(r.get("cost_usd") or 0 for r in todo), 4),
                             "own_tested": sum(r["own_both"] is not None for r in todo),
                             "integration": sum(bool(r["own_both"]) for r in todo),
                             "integration_recovered": sum(bool(r["own_both"] and r["remerge_passed"]) for r in todo)},
                 "paper": clustered(subset, lambda r: r["paper_passed"]),
                 "with_remerge": clustered(subset, lambda r: r["passed"]),
                 "gain": clustered(subset, lambda r: r["passed"] - r["paper_passed"])}
        if both:
            solo = sum(r["solo_passed"] for r in both) / len(both)
            paper = sum(r["paper_passed"] for r in both) / len(both)
            mine = sum(r["passed"] for r in both) / len(both)
            entry["with_solo"] = {"pairs": len(both), "solo": solo, "paper": paper, "with_remerge": mine,
                                  "gap_closed": (mine - paper) / (solo - paper) if solo > paper else None}
            closed = entry["with_solo"]["gap_closed"]
            entry["claim_holds"] = bool(closed is not None and closed >= 1 / 3 and entry["gain"]["ci95"][0] > 0)
        out[model] = entry
    return out


ROW_COLUMNS = ["model", "repo", "task_id", "features", "conflict", "paper_passed", "remerge_needed",
               "remerge_status", "remerge_passed", "own_both", "passed", "solo_passed"]


def report(config, root):
    rows, calibration = collect(config, root)
    summary = summarize(config, rows, calibration)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (output / "summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], "models": summary}, indent=2))
    print(f"{config['study']}: {config['question']}")
    for s in summary.values():
        c, m = s["calibration"], s["remerge"]
        print(f"\n{s['name']}: {s['pairs']} coop pairs over {s['tasks']} tasks")
        if c["features"]:
            print(f"  calibration: {c['features_agree']}/{c['features']} features match the recorded result")
        print(f"  re-merge: {m['done']}/{m['needed']} done, {m['errors']} errors, {m['recovered']} recovered, ${m['cost_usd']:.2f}")
        if m["own_tested"]:
            print(f"  own patches tested on {m['own_tested']}: {m['integration']} are integration cases "
                  f"(both own patches pass), {m['integration_recovered']} of them recovered")
        print(f"  coop success: paper {s['paper']['mean']:.1%} -> with re-merge {s['with_remerge']['mean']:.1%}; "
              f"gain {s['gain']['mean']:+.1%} [{s['gain']['ci95'][0]:+.1%}, {s['gain']['ci95'][1]:+.1%}]")
        if "with_solo" in s:
            w = s["with_solo"]
            closed = f"{w['gap_closed']:.0%}" if w["gap_closed"] is not None else "n/a"
            print(f"  on {w['pairs']} pairs with solo: solo {w['solo']:.1%}, coop {w['paper']:.1%} -> "
                  f"{w['with_remerge']:.1%}; gap closed {closed}; claim holds: {s['claim_holds']}")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    shard = (0, 1)
    if len(argv) == 4 and argv[2] == "--shard":
        shard = tuple(int(v) for v in argv[3].split("/"))
    execute(config, root, argv[1], shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
