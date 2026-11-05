"""Re-score the CooperBench paper's published coop test logs with a correct pass check.

CooperBench counts a test run as failed whenever its output contains 'failed' anywhere, in any case
(evaluation/test_runner.py), so a warning or an install message can fail a run whose tests all pass.
Each published log is scored twice: with that check re-implemented, and with a check that reads the
test framework's own summary. The paper census is then re-labeled with the corrected merged results.
Standard library only; nothing runs in Docker.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import re
import sys

from . import paper_census, s1, upstream

STAGES = ("naive", "union", "llm")

# CooperBench evaluation/test_runner.py, commit 95cd5a2
PAPER_RAN = re.compile(r"(passed|PASSED|Test execution completed|\bok\b)")
PAPER_OK = re.compile(r"(\bok\b|passed|PASSED)")
PAPER_FAIL = re.compile(r"(FAIL:|FAILED|failed.*tests?.*failed)", re.IGNORECASE)

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PYTEST_SUMMARY = re.compile(r"^=+ (.+?) in [\d.]+s(?: \([^)]*\))? =+$")
COUNT = re.compile(r"(\d+) (\w+)")
GO_DETECT = re.compile(r"^(=== RUN|--- (PASS|FAIL):|(ok|FAIL)\s+\S+\s+(\(cached\)|[\d.]+s)|\?\s+\S+\s+\[no test files\])"
                       r"|\[build failed\]")
GO_OK = re.compile(r"^ok\s+\S+")
GO_FAIL = re.compile(r"^(FAIL\b|--- FAIL:|panic:)|\[build failed\]|\[setup failed\]")
CARGO = re.compile(r"^test result: (\w+)\.")
JEST = re.compile(r"^\s*Tests:?\s+(.*\d+ (?:passed|failed).*)$")
SUITE_FAILED = re.compile(r"^\s*Test (?:Suites|Files):?\s+.*\b\d+ failed")


def paper_check(output):
    """The paper's verdict on one log."""
    if not PAPER_RAN.search(output):
        return False
    return bool(PAPER_OK.search(output)) and not PAPER_FAIL.search(output)


def trigger(output):
    """The line that made the paper check fail, without colour codes, trimmed."""
    match = PAPER_FAIL.search(output)
    if not match:
        return ""
    start = output.rfind("\n", 0, match.start()) + 1
    end = output.find("\n", match.end())
    return ANSI.sub("", output[start:end if end != -1 else None]).strip()[:160]


def _pytest_ok(summary):
    counts = Counter()
    for number, word in COUNT.findall(summary):
        counts[word[:-1] if word in ("errors", "warnings") else word] += int(number)
    return counts["passed"] > 0 and not counts["failed"] and not counts["error"]


def corrected_check(output):
    """(verdict, framework) from the test framework's own summary; (None, "unparsed") if none is found."""
    lines = [ANSI.sub("", line).rstrip() for line in output.splitlines()]
    summaries = [m.group(1) for m in map(PYTEST_SUMMARY.match, lines) if m]
    if summaries:
        return all(_pytest_ok(s) for s in summaries), "pytest"
    if any(GO_DETECT.search(line) for line in lines):
        return any(GO_OK.match(l) for l in lines) and not any(GO_FAIL.search(l) for l in lines), "go"
    results = [m.group(1) for m in map(CARGO.match, lines) if m]
    if results:
        return all(r == "ok" for r in results), "cargo"
    if any("could not compile" in line for line in lines):
        return False, "cargo"
    jest = [m.group(1) for m in map(JEST.match, lines) if m]
    if jest:
        ok = all(re.search(r"\d+ passed", s) and not re.search(r"\d+ failed", s) for s in jest)
        return ok and not any(SUITE_FAILED.match(l) for l in lines), "jest"
    return None, "unparsed"


def load_config(path, root):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "from", "paper_check", "corrected_check", "decision_rule", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    census, base = paper_census.load_config(root / config["from"], root)
    return config, census, base


def report_path(model, key, entry):
    _, repo, task = entry["upstream"].split("/")
    f1, f2 = key[2], key[3]
    return f"{entry['upstream']}/feature{f1}_feature{f2}/{model}_{repo}_{task}_merge_report_f{f1}_into_f{f2}_k1.json"


def coop_pairs(base, root):
    """Every usable coop pair: {(model, key): recorded entry}."""
    return {(model, key): entry for model in base["models"]
            for key, entry in upstream.results(base, root, "coop", model).items() if upstream.usable(base, root, key)}


def score_logs(base, root, pairs):
    """One row per published test log, and the number of pairs without a report."""
    paths = {pair: report_path(pair[0], pair[1], entry) for pair, entry in pairs.items()}
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = dict(zip(paths, pool.map(lambda p: upstream.fetch(base, root, p), paths.values())))
    rows, missing = [], 0
    for (model, key), entry in sorted(pairs.items()):
        if not texts[model, key].strip():
            missing += 1
            continue
        features = json.loads(texts[model, key]).get("features", {})
        for slot, feature in ((1, key[2]), (2, key[3])):
            record = features.get(f"feature{slot}") or {}
            for stage in STAGES:
                output = record.get(f"{stage}_merge_test_output")
                if not isinstance(output, str):
                    continue
                verdict, framework = corrected_check(output)
                rows.append({"model": model, "repo": key[0], "task_id": key[1], "features": f"{key[2]} {key[3]}",
                             "conflict": entry["conflict"], "feature": feature, "slot": slot, "stage": stage,
                             "recorded": entry["stages"][stage][slot - 1], "paper": paper_check(output),
                             "corrected": verdict, "framework": framework,
                             "trigger": "" if paper_check(output) else trigger(output)})
    return rows, missing


def corrected_entries(pairs, rows):
    """Recorded entries with each scored log's result replaced by the corrected one."""
    out = {}
    for pair, entry in pairs.items():
        out[pair] = dict(entry, stages={s: list(v) for s, v in entry["stages"].items()})
    for row in rows:
        if row["corrected"] is not None:
            key = (row["repo"], row["task_id"], *(int(f) for f in row["features"].split()))
            out[row["model"], key]["stages"][row["stage"]][row["slot"] - 1] = row["corrected"]
    for entry in out.values():
        entry["passed"] = any(all(entry["stages"][s]) and None not in entry["stages"][s] for s in STAGES)
    return out


def relabel(base, root, corrected):
    """Census rows for the failed pairs that still fail after correction, and how many now pass."""
    jobs, now_pass = [], Counter()
    for job in paper_census.select(base, root):
        key = (job["repo"], job["task_id"], *job["features"])
        entry = corrected[job["model"], key]
        if entry["passed"]:
            now_pass[job["model"]] += 1
        else:
            jobs.append(dict(job, recorded=entry))
    return paper_census.collect(base, root, jobs), dict(now_pass)


def unreproduced(census_rows):
    """Features of the census's clean pairs that the paper failed and our clean re-merge passes."""
    out = []
    for r in census_rows:
        if r["conflict"] or r["merged_source"] != "naive" or r["retest_merge"] != "clean":
            continue
        for f in r["features"].split():
            if f not in r["merged_passed"].split() and f in (r["retest_passed"] or "").split():
                out.append({"model": r["model"], "repo": r["repo"], "task_id": r["task_id"],
                            "features": r["features"], "feature": f})
    return out


def share(items, value):
    return upstream.clustered(items, value) if items else None


def summarize(config, census, base, root, pairs, rows, missing):
    scored = {(r["model"], r["repo"], r["task_id"], r["features"], str(r["feature"]), r["stage"]): r for r in rows}
    corrected = corrected_entries(pairs, rows)
    old_rows = paper_census.collect(base, root)
    new_rows, now_pass = relabel(base, root, corrected)
    lost = unreproduced(old_rows)
    for item in lost:
        row = scored.get((item["model"], item["repo"], item["task_id"], item["features"], item["feature"], "naive"))
        item["corrected"] = None if row is None else row["corrected"]
    new = paper_census.summarize(census, base, new_rows)
    out = {}
    for model, name in base["models"].items():
        logs = [r for r in rows if r["model"] == model]
        judged = [r for r in logs if r["recorded"] is not None]
        flips = Counter((r["stage"], "fail->pass" if r["corrected"] else "pass->fail") for r in judged
                        if r["corrected"] is not None and bool(r["corrected"]) != bool(r["recorded"]))
        model_pairs = {k: e for (m, k), e in pairs.items() if m == model}
        solo = upstream.results(base, root, "solo", model)
        with_solo = [k for k in model_pairs if k in solo]
        agree = Counter()
        for (m, key), entry in pairs.items():
            if m != model or entry["conflict"]:
                continue
            retest = s1.read_json(upstream.job_dir(base, root, {"model": m, "repo": key[0], "task_id": key[1],
                                                                 "features": [key[2], key[3]]}) / "calibration.json")
            if not retest or retest.get("merge") != "clean" or retest.get("error"):
                continue
            for slot, f in ((1, key[2]), (2, key[3])):
                recorded, ours = entry["stages"]["naive"][slot - 1], retest.get(str(f))
                if recorded is None or ours is None:
                    continue
                agree["features"] += 1
                agree["recorded"] += bool(recorded) == bool(ours)
                agree["corrected"] += bool(corrected[m, key]["stages"]["naive"][slot - 1]) == bool(ours)
        triggers = Counter(re.sub(r"\d+", "N", r["trigger"])[:100] for r in judged
                           if r["corrected"] and not r["recorded"] and r["trigger"])
        mine = [i for i in lost if i["model"] == model]
        out[model] = {
            "name": name, "pairs": len(model_pairs), "logs": len(logs),
            "frameworks": dict(Counter(r["framework"] for r in logs).most_common()),
            "check_agreement": [sum(bool(r["paper"]) == bool(r["recorded"]) for r in judged), len(judged)],
            "flips": {f"{s} {d}": n for (s, d), n in sorted(flips.items())},
            "triggers": triggers.most_common(8),
            "coop": {"recorded": sum(e["passed"] for e in model_pairs.values()),
                     "corrected": sum(corrected[model, k]["passed"] for k in model_pairs),
                     "of": len(model_pairs),
                     "with_solo": {"pairs": len(with_solo),
                                   "coop_recorded": sum(model_pairs[k]["passed"] for k in with_solo),
                                   "coop_corrected": sum(corrected[model, k]["passed"] for k in with_solo),
                                   "solo_recorded": sum(solo[k]["passed"] for k in with_solo)}},
            "retest_agreement": dict(agree),
            "unreproduced": {"features": len(mine), "corrected_pass": sum(bool(i["corrected"]) for i in mine),
                             "no_log": sum(i["corrected"] is None for i in mine),
                             "share": share(mine, lambda i: bool(i["corrected"]))},
            "census_now_passing": now_pass.get(model, 0),
            "census": new["models"][model],
        }
    primary = share(lost, lambda i: bool(i["corrected"]))
    rule = None
    if primary is not None:
        rule = ("pass check explains it: census re-labeled" if primary["mean"] >= 0.5
                else "cause lies elsewhere: re-run the tests in Docker")
    agreement = [sum(v["check_agreement"][0] for v in out.values()), sum(v["check_agreement"][1] for v in out.values())]
    return {"models": out, "missing_reports": missing, "check_agreement": agreement,
            "check_passed": bool(agreement[1]) and agreement[0] / agreement[1] >= 0.95,
            "primary": primary, "decision": rule, "census_verdict": new["verdict"]}, new_rows


LOG_COLUMNS = ["model", "repo", "task_id", "features", "conflict", "feature", "slot", "stage", "recorded", "paper",
               "corrected", "framework", "trigger"]


def report(config, census, base, root):
    pairs = coop_pairs(base, root)
    rows, missing = score_logs(base, root, pairs)
    summary, census_rows = summarize(config, census, base, root, pairs, rows, missing)
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "rescore_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with open(output / "rescore_census_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=paper_census.ROW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(census_rows)
    (output / "rescore_summary.json").write_text(json.dumps(
        {"study": config["study"], "question": config["question"], **summary}, indent=2))
    pct = paper_census.pct
    print(f"{config['study']}: {config['question']}\n")
    a = summary["check_agreement"]
    print(f"paper check matches the recorded result on {a[0]}/{a[1]} logs; {summary['missing_reports']} pairs have no report")
    for s in summary["models"].values():
        c, w = s["coop"], s["coop"]["with_solo"]
        print(f"\n{s['name']}: {s['logs']} logs, frameworks {s['frameworks']}")
        print(f"  flips: {s['flips']}")
        print(f"  coop success: {c['recorded']}/{c['of']} recorded, {c['corrected']}/{c['of']} corrected; "
              f"on {w['pairs']} pairs with solo: coop {w['coop_recorded']} -> {w['coop_corrected']}, "
              f"solo {w['solo_recorded']} (recorded, uncorrected)")
        t = s["retest_agreement"]
        if t:
            print(f"  agreement with our re-test: recorded {t['recorded']}/{t['features']}, corrected {t['corrected']}/{t['features']}")
        u = s["unreproduced"]
        print(f"  unreproduced failures: {u['corrected_pass']}/{u['features']} pass when re-scored ({u['no_log']} without a log)")
        g = s["census"]["gap"]
        print(f"  census after correction: {s['census_now_passing']} failed pairs now pass; {s['census']['labeled']} labeled")
        print(f"    all failed pairs: {s['census']['labels']}")
        print(f"    gap pairs {g['pairs']}: {g['labels']}")
        print(f"    integration share {pct(g['integration'])}; robust ({g['robust_pairs']} pairs) {pct(g['robust_integration'])}")
        for line, n in s["triggers"][:5]:
            print(f"    trigger x{n}: {line}")
    print(f"\nprimary: {pct(summary['primary'])} of unreproduced failures pass when re-scored -> {summary['decision']}")
    print(f"check passed: {summary['check_passed']}; census verdict after correction: {summary['census_verdict']}")
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config, census, base = load_config(argv[0], root)
    report(config, census, base, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
