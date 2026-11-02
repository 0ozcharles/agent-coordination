"""S2 targeted interventions: S1-style runs in several arms, scored officially and after an llm re-merge.

Each arm is an S1 config (setting, agent config, git flag) with its own output directory.
Coop runs whose naive merge conflicts are re-merged right after scoring, with the frozen llm
method from the merge test. Labels follow S1 (docs/RESEARCH_PLAN.md) on the re-merged result.
`execute` needs the CooperBench venv; the analysis uses only the standard library.
"""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import random
import sys

from . import merge, s1

SEED = 20261102


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "decision_rule", "model", "arms", "baseline", "tasks_from", "repeats",
                "remerge", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    if config["baseline"] not in config["arms"]:
        raise ValueError("The baseline must be one of the arms.")
    if [a["setting"] for a in config["arms"].values()].count("solo") != 1:
        raise ValueError("S2 needs exactly one solo arm.")
    return config


def arm_config(config, root, name):
    """The S1 config for one arm."""
    arm = config["arms"][name]
    return {"study": f"{config['study']}_{name}", "question": config["question"], "model": config["model"],
            "agent": config.get("agent", "mini_swe_agent_v2"), "agent_config": arm["agent_config"],
            "settings": [arm["setting"]], "messaging": config.get("messaging", True), "git": arm.get("git", False),
            "repeats": config["repeats"], "tasks": s1.load_config(root / config["tasks_from"])["tasks"],
            "output": f"{config['output']}/{name}"}


def plan(config, root):
    """Every (arm, job), by repeat, shuffled within a repeat so arms progress together and
    no worker gets one arm only."""
    jobs = [(name, job) for name in config["arms"] for job in s1.plan(arm_config(config, root, name))]
    rng = random.Random(SEED)
    keys = {id(item): rng.random() for item in jobs}
    return sorted(jobs, key=lambda item: (item[1]["repeat"], keys[id(item)]))


def remerge_config(config, root):
    source = merge.load_config(root / config["remerge"]["from"])
    method = config["remerge"]["method"]
    return {**source, "methods": {method: source["methods"][method]}}


# --- execution (CooperBench venv only) -------------------------------------------------

def check_agent_configs(config, root):
    """CooperBench only logs a broken agent_config and falls back to defaults; fail loudly instead."""
    import yaml
    for name, arm in config["arms"].items():
        loaded = yaml.safe_load((root / arm["agent_config"]).read_text()) or {}
        if "config" not in loaded or "model" not in loaded["config"]:
            raise ValueError(f"{arm['agent_config']} for arm {name} has no config.model block.")


def remerge(config, root, job, job_dir):
    if job["setting"] != "coop" or (job_dir / "merge.json").is_file():
        return
    if (s1.read_json(job_dir / "eval.json").get("merge") or {}).get("status") != "conflicts":
        return
    record = merge.merge_run(remerge_config(config, root), {**job, "source": job_dir}, job_dir,
                             root / "experiments/m0/CooperBench/dataset")
    (job_dir / "merge.json").write_text(json.dumps(record, indent=2))


def execute(config, root, shard=(0, 1)):
    check_agent_configs(config, root)
    coop = any(a["setting"] == "coop" for a in config["arms"].values())
    s1.setup_runner(root, coop and config.get("messaging", True), config.get("redis", "redis://localhost:6379"))
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    if shard[0] == 0:
        (output / "config.json").write_text(json.dumps(
            {**config, "remerge_prompt": merge.PROMPT}, ensure_ascii=False, indent=2))
    for name, job in plan(config, root)[shard[0]::shard[1]]:
        arm = arm_config(config, root, name)
        s1.run_job(arm, root, job, root / arm["output"],
                   after=lambda job_dir, job=job: remerge(config, root, job, job_dir))
    if shard[1] == 1:
        report(config, root)


# --- analysis (standard library only) --------------------------------------------------

def job_of(row):
    return {"repeat": row["repeat"], "setting": row["setting"], "repo": row["repo"], "task_id": row["task_id"],
            "features": [int(f) for f in row["features"].split()]}


def with_remerge(row, job_dir, method="llm"):
    """The row scored on the re-merged patch where a re-merge ran; otherwise unchanged."""
    record = s1.read_json(job_dir / "merge.json")
    entry = (record.get("methods") or {}).get(method)
    if row["setting"] != "coop" or not entry:
        return {**row, "official_success": row["success"], "remerged": False}
    passed = [f for f in row["features"].split() if entry.get(f)]
    return {**row, "official_success": row["success"], "remerged": True,
            "merged_passed": " ".join(passed), "success": len(passed) == 2}


def collect(config, root):
    """Rows per arm, labeled on the re-merged result; solo rows give the solo reference."""
    raw = {}
    for name in config["arms"]:
        arm = arm_config(config, root, name)
        output = root / arm["output"]
        raw[name] = [{**with_remerge(row, s1.log_dir(output, job_of(row)), config["remerge"]["method"]),
                      "arm": name} for row in s1.collect(arm, output)]
    solo = next(n for n, a in config["arms"].items() if a["setting"] == "solo")
    rows = []
    for name, arm_rows in raw.items():
        reference = [] if name == solo else [dict(r) for r in raw[solo]]
        labeled = s1.diagnose([dict(r) for r in arm_rows] + reference)
        rows += [r for r in labeled if r["arm"] == name]
    return rows


def task_key(row):
    return row["repo"], row["task_id"], row["features"]


def paired_difference(rows, arm, baseline, samples=5000):
    """Mean per-task pass-rate difference (arm - baseline) with a task-clustered bootstrap 95% CI."""
    rates = defaultdict(lambda: {arm: [], baseline: []})
    for row in rows:
        if row["arm"] in (arm, baseline) and row["label"] != "infra_error":
            rates[task_key(row)][row["arm"]].append(bool(row["success"]))
    diffs = [sum(v[arm]) / len(v[arm]) - sum(v[baseline]) / len(v[baseline])
             for v in rates.values() if v[arm] and v[baseline]]
    if not diffs:
        return None
    rng = random.Random(SEED)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(samples))
    return {"tasks": len(diffs), "difference": sum(diffs) / len(diffs),
            "ci95": [means[int(0.025 * samples)], means[int(0.975 * samples) - 1]]}


def summarize(config, rows):
    out = {}
    for name, arm in config["arms"].items():
        subset = [r for r in rows if r["arm"] == name]
        valid = [r for r in subset if r["label"] != "infra_error"]
        entry = {"setting": arm["setting"], "runs": len(subset), "infra_errors": len(subset) - len(valid),
                 "official_passed": sum(bool(r["official_success"]) for r in valid),
                 "passed": sum(bool(r["success"]) for r in valid),
                 "remerged_runs": sum(r["remerged"] for r in valid),
                 "cost_usd": round(sum(r["cost_usd"] or 0 for r in subset), 6)}
        if arm["setting"] == "coop":
            entry["labels"] = dict(Counter(r["label"] for r in valid))
            entry["refined_labels"] = dict(Counter(r["refined_label"] for r in valid))
            if name != config["baseline"]:
                entry["vs_baseline"] = paired_difference(rows, name, config["baseline"])
        else:
            entry["passed_counting_unpublished"] = entry["passed"] + sum(r["label"] == "submission" for r in valid)
        out[name] = entry
    return out


ROW_COLUMNS = ["arm", "official_success", "remerged", *s1.ROW_COLUMNS]


def report(config, root):
    rows = collect(config, root)
    summary = summarize(config, rows)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (output / "summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], "arms": summary}, indent=2))
    print(f"{config['study']}: {config['question']}")
    for name, s in summary.items():
        line = f"\n{name} ({s['setting']}): passed {s['passed']}/{s['runs'] - s['infra_errors']}"
        if s["setting"] == "coop":
            line += f" after re-merge (official {s['official_passed']}; {s['remerged_runs']} runs re-merged)"
        else:
            line += f" (counting unpublished work: {s['passed_counting_unpublished']})"
        print(line + (f", {s['infra_errors']} infra errors" if s["infra_errors"] else "") + f", ${s['cost_usd']:.2f}")
        if s["setting"] == "coop":
            failures = {k: v for k, v in s["refined_labels"].items() if k != "passed"}
            print(f"  failures by cause (with handoff): {failures}")
            if s.get("vs_baseline"):
                d = s["vs_baseline"]
                print(f"  vs {config['baseline']}: {d['difference']:+.3f} per task "
                      f"[{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}] over {d['tasks']} tasks")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    shard = (0, 1)
    if len(argv) == 3 and argv[1] == "--shard":
        shard = tuple(int(v) for v in argv[2].split("/"))
    execute(config, root, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
