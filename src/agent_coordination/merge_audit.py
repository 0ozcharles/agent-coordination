"""Audit the CooperBench paper's clean merges and re-merge the ones that are real conflicts.

In many pairs the paper recorded as clean, the merged patch it tested holds only agent 2's code: the
diff its merge produced is empty, so agent 1's work never entered the merge. Those pairs often
conflict when actually merged. The audit counts, offline, which agents' code each tested patch holds;
the failed ones whose merge conflicts in our harness are re-merged with the upstream check's frozen
llm method and tested. `execute` needs the CooperBench venv, Docker and the OpenAI key; the audit and
the analysis use only the standard library.
"""
from collections import Counter
import csv
import json
from pathlib import Path
import sys

from . import merge_check, paper_census, s1, upstream


def load_config(path, root):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "from", "audit", "remerge", "decision_rule", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    census, base = paper_census.load_config(root / config["from"], root)
    return config, census, base


def classify(agent1, agent2, merged):
    """Which agents' code the tested merged patch holds."""
    if not merge_check.added(agent1):
        return "agent 1 empty"
    if not merged.strip():
        return "no published patch"
    lines = set(merge_check.added(merged))
    one, two = merge_check.present(agent1, lines), merge_check.present(agent2, lines)
    return {(True, True): "both", (False, True): "agent 2 only", (True, False): "agent 1 only"}.get(
        (bool(one), bool(two)), "neither")


def audit(base, root):
    rows = []
    for model in base["models"]:
        for key, entry in sorted(upstream.results(base, root, "coop", model).items()):
            if not upstream.usable(base, root, key) or entry["conflict"] or None in entry["stages"]["naive"]:
                continue
            folder = merge_check.pair_folder(model, key, entry)
            _, repo, task = entry["upstream"].split("/")
            diff = upstream.fetch(base, root, f"{folder}/{model}_{repo}_{task}_merge_diff_f{key[2]}_into_f{key[3]}_k1.diff")
            merged, agents = merge_check.patches(base, root, model, key, entry)
            job = {"model": model, "repo": key[0], "task_id": key[1], "features": [key[2], key[3]]}
            ours = s1.read_json(upstream.job_dir(base, root, job) / "calibration.json")
            rows.append({"model": model, "repo": key[0], "task_id": key[1], "features": f"{key[2]} {key[3]}",
                         "paper_passed": all(entry["stages"]["naive"]),
                         "patch": classify(agents[key[2]], agents[key[3]], merged),
                         "merge_diff_empty": not diff.strip(), "our_merge": (ours or {}).get("merge")})
    return rows


def select(base, root):
    """Failed pairs the paper recorded as clean whose merge conflicts in our harness."""
    jobs = []
    for job in paper_census.select(base, root):
        if job["recorded"]["conflict"]:
            continue
        ours = s1.read_json(upstream.job_dir(base, root, job) / "calibration.json")
        if ours and ours.get("merge") == "conflicts":
            jobs.append(job)
    return jobs


def done(base, root, job):
    return (upstream.job_dir(base, root, job) / "merge.json").is_file()


def execute(base, root, shard=(0, 1)):
    s1.prepare_environment(root)
    todo = [job for job in select(base, root) if not done(base, root, job)]
    for job in todo[shard[0]::shard[1]]:
        target = upstream.job_dir(base, root, job)
        label = f"{job['model']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            print(f"re-merge {label}", flush=True)
            upstream.prepare(base, root, job)
            upstream.remerge(base, root, job, target)
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


# --- analysis (standard library only) --------------------------------------------------

def remerged(base, root, job):
    """(status, passed both, cost) of a pair's llm re-merge, or None if it has none."""
    record = s1.read_json(upstream.job_dir(base, root, job) / "merge.json")
    if not record:
        return None
    method = (record.get("methods") or {}).get(base["remerge"]["method"]) or {}
    f1, f2 = job["features"]
    return ("error" if record.get("error") else method.get("status"),
            bool(method.get(str(f1)) and method.get(str(f2))), method.get("cost_usd") or 0)


def summarize(config, base, root, audit_rows):
    census_rows = paper_census.collect(base, root)
    clean_fix = {(r["model"], r["repo"], r["task_id"], r["features"]) for r in census_rows
                 if not r["conflict"] and r["retest_merge"] == "clean"
                 and set((r["retest_passed"] or "").split()) == set(r["features"].split())}
    jobs = select(base, root)
    outcome = {(j["model"], j["repo"], j["task_id"], f"{j['features'][0]} {j['features'][1]}"): remerged(base, root, j)
               for j in jobs}
    upstream_rows, _ = upstream.collect(base, root)
    out = {}
    for model, name in base["models"].items():
        mine = [r for r in audit_rows if r["model"] == model]
        failed = [r for r in mine if not r["paper_passed"]]
        known = [r for r in failed if r["patch"] not in ("agent 1 empty", "no published patch")]
        lost = [r for r in known if r["patch"] in ("agent 2 only", "neither")]
        census_failed = [r for r in census_rows if r["model"] == model]
        merged_known = [r for r in census_failed if r["conflict"] or r["retest_merge"] in ("clean", "conflicts")]
        real_conflicts = [r for r in merged_known if r["conflict"] or r["retest_merge"] == "conflicts"]
        results = [v for k, v in outcome.items() if k[0] == model]
        tested = [v for v in results if v is not None]
        with_solo = [r for r in upstream_rows if r["model"] == model and r["solo_passed"] is not None]

        def coop(r, level):
            key = (model, r["repo"], r["task_id"], r["features"])
            fixed = [r["paper_passed"], r["passed"], key in clean_fix,
                     bool(outcome.get(key)) and outcome[key][1]]
            return any(fixed[:level + 1])

        solo = sum(bool(r["solo_passed"]) for r in with_solo)
        steps = [sum(coop(r, level) for r in with_solo) for level in range(4)]
        gap = solo - steps[0]
        out[model] = {
            "name": name,
            "audit": dict(Counter(f"{'pass' if r['paper_passed'] else 'fail'} | {r['patch']} | "
                                  f"{'diff empty' if r['merge_diff_empty'] else 'diff'}" for r in mine).most_common()),
            "lost_side": [len(lost), len(known)],
            "lost_side_our_merge": dict(Counter(r["our_merge"] for r in lost)),
            "conflicts": [len(real_conflicts), len(merged_known)],
            "paper_conflicts": sum(r["conflict"] for r in census_failed),
            "remerge": {"pairs": len(results), "tested": len(tested),
                        "status": dict(Counter(v[0] for v in tested)), "passed": sum(v[1] for v in tested),
                        "cost_usd": round(sum(v[2] for v in tested), 4)},
            "gap": {"pairs": len(with_solo), "solo": solo, "coop": steps,
                    "closed": [round((s - steps[0]) / gap, 3) if gap else None for s in steps]},
        }
    decided = {m: v["gap"]["closed"][-1] for m, v in out.items()}
    return {"models": out,
            "decision": {m: (None if c is None else "merge artifact: gap closes by a third or more" if c >= 1 / 3
                             else "gap mostly stands") for m, c in decided.items()},
            "withdraw_clean_claim": {m: v["lost_side"][1] > 0 and v["lost_side"][0] / v["lost_side"][1] >= 0.25
                                     for m, v in out.items()}}


COLUMNS = ["model", "repo", "task_id", "features", "paper_passed", "patch", "merge_diff_empty", "our_merge"]


def report(config, base, root):
    rows = audit(base, root)
    summary = summarize(config, base, root, rows)
    output = root / config["output"] / "merge_audit"
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "merge_audit_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (output / "merge_audit_summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], **summary}, indent=2))
    print(f"{config['study']}: {config['question']}")
    for s in summary["models"].values():
        g = s["gap"]
        print(f"\n{s['name']}:")
        for line, n in s["audit"].items():
            print(f"  {n:4} {line}")
        print(f"  failed clean pairs whose tested patch lost agent 1's code: {s['lost_side'][0]}/{s['lost_side'][1]} "
              f"(our merge: {s['lost_side_our_merge']})")
        print(f"  failed pairs that conflict when merged: {s['conflicts'][0]}/{s['conflicts'][1]} "
              f"(paper recorded {s['paper_conflicts']} conflicts)")
        r = s["remerge"]
        print(f"  re-merged {r['tested']}/{r['pairs']}: {r['status']}, {r['passed']} pass both, ${r['cost_usd']}")
        print(f"  on {g['pairs']} pairs with solo: solo {g['solo']}, coop {g['coop']} "
              f"(paper, +conflict re-merge, +clean fixes, +re-merged here); gap closed {g['closed']}")
    print(f"\ndecision: {summary['decision']}; withdraw the clean-merge claim: {summary['withdraw_clean_claim']}")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config, _, base = load_config(argv[0], root)
    shard = (0, 1)
    if len(argv) == 3 and argv[1] == "--shard":
        shard = tuple(int(v) for v in argv[2].split("/"))
    execute(base, root, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
