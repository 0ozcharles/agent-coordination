"""Test the CooperBench paper's published naive-merge patches in our harness.

For pairs where the paper recorded a merged failure that our own merge of the same two patches does
not reproduce, this tests the merged patch the paper published, alone, against both features. Control
sets check that our harness agrees with the paper on pairs it recorded as passing. `execute` needs the
CooperBench venv and Docker; selection and the analysis use only the standard library.
"""
from collections import Counter
import csv
import json
from pathlib import Path
import random
import re
import sys

from . import paper_census, rescore, s1, upstream

DATASET = "experiments/m0/CooperBench/dataset"
SETS = ("unreproduced", "pass_one_side", "pass_both_sides", "reproduced")
SIZES = {"pass_one_side": 20, "pass_both_sides": 15, "reproduced": 15}


def load_config(path, root):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "from", "sets", "decision_rule", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    _, census, base = rescore.load_config(root / config["from"], root)
    return config, census, base


def added(text):
    return [line[1:].strip() for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++") and line[1:].strip()]


def present(agent_text, merged_lines):
    """True if at least half of the agent's added lines are among the merged patch's; None if it adds none."""
    lines = added(agent_text)
    return None if not lines else sum(line in merged_lines for line in lines) / len(lines) >= 0.5


def pair_folder(model, key, entry):
    return f"{entry['upstream']}/feature{key[2]}_feature{key[3]}"


def patches(base, root, model, key, entry):
    folder = pair_folder(model, key, entry)
    merged = upstream.fetch(base, root, f"{folder}/{model}_naive_merge_k1.patch")
    agents = {f: upstream.fetch(base, root, f"{folder}/patch_{model}_k1_feature{f}.patch") for f in key[2:]}
    return merged, agents


def sides(base, root, model, key, entry):
    """Per feature, whether its agent's side is in the published merged patch; None if the patch is empty."""
    merged, agents = patches(base, root, model, key, entry)
    if not merged.strip():
        return None
    lines = set(added(merged))
    return {f: present(text, lines) for f, text in agents.items()}


def select(config, base, root):
    pairs = rescore.coop_pairs(base, root)
    census_rows = paper_census.collect(base, root)
    as_key = lambda r: (r["model"], (r["repo"], r["task_id"], *(int(f) for f in r["features"].split())))
    chosen = {"unreproduced": sorted({as_key(i) for i in rescore.unreproduced(census_rows)})}
    one, both = [], []
    for (model, key), entry in sorted(pairs.items()):
        if entry["conflict"] or None in entry["stages"]["naive"] or not all(entry["stages"]["naive"]):
            continue
        found = sides(base, root, model, key, entry)
        if found and all(v is not None for v in found.values()):
            (both if all(found.values()) else one).append((model, key))
    reproduced = set()
    for r in census_rows:
        if r["conflict"] or r["merged_source"] != "naive" or r["retest_merge"] != "clean":
            continue
        failed = [f for f in r["features"].split() if f not in r["merged_passed"].split()]
        if any(f not in (r["retest_passed"] or "").split() for f in failed):
            model, key = as_key(r)
            if sides(base, root, model, key, pairs[model, key]):
                reproduced.add((model, key))
    rng = random.Random(config["sets"]["seed"])
    for name, pool in (("pass_one_side", one), ("pass_both_sides", both), ("reproduced", sorted(reproduced))):
        chosen[name] = sorted(rng.sample(pool, min(SIZES[name], len(pool))))
    return [{"set": name, "model": model, "repo": key[0], "task_id": key[1], "features": [key[2], key[3]],
             "recorded": pairs[model, key]} for name in SETS for model, key in chosen[name]]


def job_dir(config, root, job):
    f1, f2 = job["features"]
    return (root / config["output"] / job["set"] / job["model"] / job["repo"] / str(job["task_id"])
            / f"f{f1}_f{f2}")


def done(config, root, job):
    return (job_dir(config, root, job) / "result.json").is_file()


def execute(config, base, root, shard=(0, 1)):
    from cooperbench.eval.sandbox import test_solo
    s1.prepare_environment(root)
    todo = [job for job in select(config, base, root) if not done(config, root, job)]
    for job in todo[shard[0]::shard[1]]:
        target = job_dir(config, root, job)
        target.mkdir(parents=True, exist_ok=True)
        key = (job["repo"], job["task_id"], *job["features"])
        label = f"{job['set']} {job['model']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            print(f"merge check {label}", flush=True)
            merged, _ = patches(base, root, job["model"], key, job["recorded"])
            (target / "paper_merged.patch").write_text(merged)
            f1, f2 = job["features"]
            result = test_solo(repo_name=job["repo"], task_id=job["task_id"], feature1_id=f1, feature2_id=f2,
                               patch=merged, backend="docker", dataset_dir=root / DATASET)
            record = {"error": result.get("error")}
            for slot, f in (("feature1", f1), ("feature2", f2)):
                record[str(f)] = bool((result.get(slot) or {}).get("passed"))
                (target / f"output_f{f}.txt").write_text(((result.get(slot) or {}).get("test_output") or "")[-40000:])
            (target / "result.json").write_text(json.dumps(record, indent=2))
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


# --- analysis (standard library only) --------------------------------------------------

def first_error(output):
    """The first line that names an error in a test log, without colour codes and spacing."""
    for line in output.splitlines():
        line = rescore.ANSI.sub("", line).strip()
        if re.match(r"^E\s+\S", line) or re.search(r"\b\w*(Error|Exception):", line):
            return re.sub(r"\s+", " ", line.lstrip("E").strip())
    return ""


def collect(config, base, root):
    rows = []
    lost = {}
    census_rows = paper_census.collect(base, root)
    for item in rescore.unreproduced(census_rows):
        lost.setdefault((item["model"], item["repo"], item["task_id"], item["features"]), set()).add(item["feature"])
    for job in select(config, base, root):
        target = job_dir(config, root, job)
        result = s1.read_json(target / "result.json")
        key = (job["repo"], job["task_id"], *job["features"])
        found = sides(base, root, job["model"], key, job["recorded"])
        features = " ".join(str(f) for f in job["features"])
        report = upstream.fetch(base, root, rescore.report_path(job["model"], key, job["recorded"]))
        logs = json.loads(report).get("features", {}) if report.strip() else {}
        for slot, f in enumerate(job["features"], start=1):
            paper_output = (logs.get(f"feature{slot}") or {}).get("naive_merge_test_output") or ""
            ours = result.get(str(f)) if result else None
            own_output = (target / f"output_f{f}.txt").read_text() if (target / f"output_f{f}.txt").is_file() else ""
            paper_error = first_error(paper_output)
            rows.append({
                "set": job["set"], "model": job["model"], "repo": job["repo"], "task_id": job["task_id"],
                "features": features, "feature": f,
                "unreproduced": str(f) in lost.get((job["model"], job["repo"], job["task_id"], features), ()),
                "recorded": job["recorded"]["stages"]["naive"][slot - 1],
                "merged_empty": found is None, "side_present": None if found is None else found.get(f),
                "tested": result is not None, "error": (result or {}).get("error"), "published_passed": ours,
                "paper_error": paper_error,
                "same_error": (paper_error in re.sub(r"\s+", " ", rescore.ANSI.sub("", own_output))
                               if paper_error and ours is False else None)})
    return rows


def share(items, value):
    return upstream.clustered(items, value) if items else None


def summarize(config, rows):
    tested = [r for r in rows if r["tested"] and not r["error"]]
    primary_items = [r for r in tested if r["set"] == "unreproduced" and r["unreproduced"] and not r["merged_empty"]]
    primary = share(primary_items, lambda r: not r["published_passed"])
    both = share([r for r in tested if r["set"] == "pass_both_sides"], lambda r: bool(r["published_passed"]))
    one_items = [r for r in tested if r["set"] == "pass_one_side"]
    sets = {}
    for name in SETS:
        mine = [r for r in rows if r["set"] == name]
        sets[name] = {"features": len(mine), "tested": sum(r["tested"] for r in mine),
                      "errors": sum(bool(r["error"]) for r in mine),
                      "published_passed": sum(bool(r["published_passed"]) for r in mine if r["tested"]),
                      "by_side": dict(Counter(f"{'empty' if r['merged_empty'] else 'side present' if r['side_present'] else 'side missing'}"
                                              f" -> {'pass' if r['published_passed'] else 'fail'}"
                                              for r in mine if r["tested"] and not r["error"]))}
    failing = [r for r in primary_items if r["published_passed"] is False and r["same_error"] is not None]
    if primary is None or both is None:
        decision = None
    elif both["mean"] < 0.9:
        decision = "control failed: our harness disagrees with the paper on passing pairs"
    elif primary["mean"] >= 0.8:
        decision = "the paper's merged code explains the non-reproduction"
    elif primary["mean"] < 0.5:
        decision = "environment or flaky tests: repeat the tests"
    else:
        decision = "mixed"
    return {"sets": sets, "primary": primary, "primary_features": len(primary_items),
            "control_both_sides_pass": both,
            "control_one_side_fail": share(one_items, lambda r: not r["published_passed"]),
            "control_one_side_missing_fail": share([r for r in one_items if r["side_present"] is False],
                                                   lambda r: not r["published_passed"]),
            "same_error": [sum(bool(r["same_error"]) for r in failing), len(failing)],
            "decision": decision}


COLUMNS = ["set", "model", "repo", "task_id", "features", "feature", "unreproduced", "recorded", "merged_empty",
           "side_present", "tested", "error", "published_passed", "same_error", "paper_error"]


def report(config, base, root):
    rows = collect(config, base, root)
    summary = summarize(config, rows)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "merge_check_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (output / "merge_check_summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], **summary}, indent=2))
    pct = paper_census.pct
    print(f"{config['study']}: {config['question']}\n")
    for name, s in summary["sets"].items():
        print(f"{name}: {s['tested']}/{s['features']} features tested, {s['errors']} errors, "
              f"{s['published_passed']} pass with the published patch; {s['by_side']}")
    print(f"\nprimary: {pct(summary['primary'])} of {summary['primary_features']} unreproduced features fail "
          f"with the published patch too")
    print(f"control, both sides present: {pct(summary['control_both_sides_pass'])} pass")
    print(f"control, one side missing: {pct(summary['control_one_side_fail'])} fail "
          f"({pct(summary['control_one_side_missing_fail'])} of the missing side's features)")
    print(f"same first error as the paper: {summary['same_error'][0]}/{summary['same_error'][1]}")
    print(f"decision: {summary['decision']}")
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
