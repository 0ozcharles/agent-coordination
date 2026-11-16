"""Synthesis readouts on finished ParallACT coop runs: choose among teams, or re-merge knowing the requests.

Two offline experiments; no agent is re-run. Zhao et al. (NeurIPS 2026, "When Debate Helps") argue that
multi-agent systems beat simple voting only when the candidates contain a correct answer (supply) and the
final readout uses evidence of correctness instead of counting. The style study left both untouched: the
instructions changed how agents talk, not how their work is combined.

select: for each instance, a judge model reads both requests and the merged patch of every team (arm) that
ran it, in a seeded random order, and picks the one most likely to satisfy both. It sees no tests or scores.
It is compared with a consensus pick (the patch whose changed lines overlap most with the others, the analogue
of a majority vote), a random pick, and the oracle (some team passes). The judge's gain over consensus splits
into recovered headroom and damage, as in the paper.

remerge: for each run whose git merge conflicted, the conflicted files are resolved again with the model and
settings of the blind llm resolver used in scoring, in a 2 x 2 design. Information: blind, or `aware` of both
requests and the agents' messages. Format: rewrite each conflict block from a 40-line excerpt (as the blind
resolver did), or write the whole file from the whole conflicted file. `blind_repeat` repeats the original
blind prompt, to measure what a second sample alone changes. The merged patch is tested against the combined
tests.

`execute_*` import CooperBench (litellm, the test-file filter) and run under its venv; `report` uses only the
standard library.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import random
import re
import shutil
import sys

from . import merge, parallact

SELECT_PROMPT = """You are the manager of a software team. Two developers each implemented one of the two requests below in the same repository, and their changes were merged. Several teams did this independently. Below are their merged changes, as diffs against the same base commit.

Pick the candidate most likely to satisfy BOTH requests completely and correctly, with neither request's behaviour broken by the other's. Judge from the requests and the code only; you cannot run anything.

Request A:
{request_a}

Request B:
{request_b}

{candidates}

Reply with a short justification (at most five sentences), then a final line of the form `CHOICE: <number>`."""

AWARE_PROMPT = """Two developers changed the same file in parallel, each implementing a different request, and git could not merge their changes. Below are both requests, the messages the developers exchanged, and each conflict block, which shows developer A's version, the original code, and developer B's version.

Resolve every conflict so that the file satisfies both requests. Where the two changes are incompatible, write code that meets both requests, for example by accepting both forms of an argument. Do not drop either request's functionality and do not add anything unrelated. Keep the surrounding indentation.

Request A (developer A):
{request_a}

Request B (developer B):
{request_b}

Messages between the developers:
{messages}

File: {path}

{excerpt}

There {count_text}. Reply with exactly {count} section(s), in order. Each section is a header line followed by one fenced code block containing only the code that replaces the whole conflict block (from the <<<<<<< line through the >>>>>>> line), with no markers left:

{template}"""

FILE_PROMPT = """Two developers changed the same file in parallel, each implementing a different {kind}, and git could not merge their changes. {context_intro}Below is the whole file as git left it: the parts git could merge are already merged, and each conflict block shows developer A's version, the original code, and developer B's version.

Write the complete resolved file. {goal} Do not drop either side's functionality and do not add anything unrelated. Make sure the result is valid, complete code.
{context}
File: {path}

```
{text}
```

Reply with the complete resolved file in one fenced code block and nothing else."""

METHODS = ("aware", "blind_repeat", "aware_file", "blind_file")


def load_config(path):
    return parallact.load_config(path)


def source(config, root):
    return parallact.load_config(root / config["runs"])


def rows(config, root):
    return list(csv.DictReader(open(root / config["rows"])))


def run_dir(src, root, row):
    job = {"arm": row["arm"], "repeat": int(row["repeat"]), "setting": "coop", "repo": row["repo"],
           "task_id": int(row["task_id"])}
    return parallact.job_dir(root / src["output"], job)


def read(path):
    return Path(path).read_text() if Path(path).is_file() else ""


def flag(value):
    return value is True or value == "True"


# --- select ------------------------------------------------------------------------------

def changed_lines(patch):
    return {line for line in patch.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))}


def consensus(cands):
    """Index of the candidate whose changed lines overlap most with the others' (mean Jaccard); first on ties."""
    sets = [changed_lines(c["patch"]) for c in cands]

    def jaccard(a, b):
        return len(a & b) / len(a | b) if a | b else 1.0

    scores = [sum(jaccard(s, t) for j, t in enumerate(sets) if j != i) / max(1, len(sets) - 1)
              for i, s in enumerate(sets)]
    return max(range(len(cands)), key=lambda i: (scores[i], -i))


def candidates(config, root, src, all_rows, instance_id):
    """Each team's merged patch for one instance, in arm order, with its scores (never shown to the judge)."""
    out = []
    for arm in src["arms"]:
        row = next((r for r in all_rows if r["arm"] == arm and r["instance"] == instance_id), None)
        if row is None or row["finished"] != "True":
            continue
        out.append({"arm": arm, "patch": read(run_dir(src, root, row) / "merged.patch"),
                    "both": flag(row["both"]), "A": flag(row["merged_A"]), "B": flag(row["merged_B"])})
    return out


def select_prompt(instance, shown, max_lines):
    blocks = []
    for n, c in enumerate(shown, 1):
        lines = c["patch"].splitlines()
        body = "\n".join(lines[:max_lines]) + (f"\n... ({len(lines) - max_lines} more lines)" if len(lines) > max_lines else "")
        blocks.append(f"Candidate {n}:\n```diff\n{body or '(no changes)'}\n```")
    return SELECT_PROMPT.format(request_a=instance["agentA_problem_statement"].strip(),
                                request_b=instance["agentB_problem_statement"].strip(),
                                candidates="\n\n".join(blocks))


def parse_choice(reply, count):
    found = re.findall(r"CHOICE:\s*\**\s*(\d+)", reply or "")
    if not found or not 1 <= int(found[-1]) <= count:
        raise ValueError(f"no valid CHOICE line for {count} candidates")
    return int(found[-1])


def select_one(config, root, src, all_rows, instances, instance_id):
    out = root / config["output"] / "select" / f"{instance_id}.json"
    if out.is_file():
        return
    spec = config["select"]
    cands = candidates(config, root, src, all_rows, instance_id)
    order = list(range(len(cands)))
    random.Random(f"{spec['seed']}-{instance_id}").shuffle(order)
    shown = [cands[i] for i in order]
    prompt = select_prompt(instances[instance_id], shown, spec.get("max_patch_lines", 400))
    tries, choice = [], None
    for _ in range(spec.get("attempts", 2)):
        reply, cost = merge.ask_model({"model": spec["model"], "model_kwargs": spec.get("model_kwargs", {})}, prompt)
        try:
            choice, error = parse_choice(reply, len(shown)), None
        except ValueError as exc:
            error = str(exc)
        tries.append({"reply": reply, "error": error, "cost_usd": cost})
        if choice:
            break
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"instance": instance_id, "order": [c["arm"] for c in shown],
                               "choice_arm": shown[choice - 1]["arm"] if choice else None,
                               "consensus_arm": cands[consensus(cands)]["arm"], "tries": tries,
                               "prompt_chars": len(prompt)}, indent=2))


def execute_select(config, root, workers=4, limit=None):
    from .s1 import load_env_file
    load_env_file(root)
    src, all_rows = source(config, root), rows(config, root)
    instances = parallact.all_instances(src, root)
    ids = sorted({r["instance"] for r in all_rows})[:limit] if limit else sorted({r["instance"] for r in all_rows})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(select_one, config, root, src, all_rows, instances, i) for i in ids]:
            f.result()
    return len(ids)


# --- remerge -----------------------------------------------------------------------------

def render_aware(part):
    """Conflict block with developer A (request A, the merge's own side) first."""
    return ("<<<<<<< developer A\n" + part["ours"] + "||||||| original\n" + part["base"]
            + "=======\n" + part["theirs"] + ">>>>>>> developer B\n")


def excerpt_with(parts, render, context=merge.CONTEXT_LINES):
    original = merge.render
    merge.render = render
    try:
        return merge.excerpt(parts, context)
    finally:
        merge.render = original


def messages_text(conversation, limit):
    lines = [f"Agent {m['from'][-1]} (request {'A' if m['from'] == 'agent1' else 'B'}): {m['message'].strip()}"
             for m in conversation]
    text = "\n\n".join(lines) or "(none)"
    return text if len(text) <= limit else text[:limit] + "\n... (later messages cut)"


def aware_prompt(path, parts, instance, conversation, limit):
    count = len(merge.conflicts(parts))
    template = "".join(f"=== RESOLUTION {i} ===\n```\nresolved code for conflict {i}\n```\n" for i in range(1, count + 1))
    return AWARE_PROMPT.format(request_a=instance["agentA_problem_statement"].strip(),
                               request_b=instance["agentB_problem_statement"].strip(),
                               messages=messages_text(conversation, limit), path=path,
                               excerpt=excerpt_with(parts, render_aware), count=count,
                               count_text="is 1 conflict" if count == 1 else f"are {count} conflicts",
                               template=template)


def file_prompt(path, parts, instance, conversation, limit, aware):
    text = "".join(render_aware(p) if isinstance(p, dict) else p for p in parts)
    if aware:
        return FILE_PROMPT.format(
            kind="request", path=path, text=text.rstrip("\n"),
            context_intro="Below are both requests and the messages the developers exchanged. ",
            goal="It must satisfy both requests; where the two changes are incompatible, write code that meets both, for example by accepting both forms of an argument.",
            context=(f"\nRequest A (developer A):\n{instance['agentA_problem_statement'].strip()}\n\n"
                     f"Request B (developer B):\n{instance['agentB_problem_statement'].strip()}\n\n"
                     f"Messages between the developers:\n{messages_text(conversation, limit)}\n"))
    return FILE_PROMPT.format(kind="feature", path=path, text=text.rstrip("\n"), context_intro="",
                              goal="Keep both developers' changes and combine them.", context="")


def parse_file(reply):
    """The body of the reply's outermost fenced block; raises if it is missing or still has conflict markers."""
    lines = (reply or "").splitlines()
    fences = [i for i, line in enumerate(lines) if line.strip().startswith("```")]
    if len(fences) < 2:
        raise ValueError("no fenced code block in the reply")
    body = lines[fences[0] + 1:fences[-1]]
    if not any(line.strip() for line in body):
        raise ValueError("empty file")
    if any(line.startswith(("<<<<<<< ", ">>>>>>> ")) or line.rstrip() == "=======" for line in body):
        raise ValueError("conflict markers left in the file")
    return "\n".join(body) + "\n"


def resolve(method, files, instance, conversation, merge_config, limit, calls):
    out = {}
    for path, text in files.items():
        parts = merge.blocks(text)
        if not merge.conflicts(parts):
            out[path] = text
            continue
        if method.endswith("_file"):
            prompt = file_prompt(path, parts, instance, conversation, limit, method == "aware_file")
        elif method == "aware":
            prompt = aware_prompt(path, parts, instance, conversation, limit)
        else:
            prompt = merge.prompt_for(path, parts)
        reply, cost = merge.ask_model(merge_config, prompt)
        calls.append({"path": path, "prompt": prompt, "reply": reply, "cost_usd": cost})
        if method.endswith("_file"):
            out[path] = parse_file(reply)
        else:
            out[path] = merge.splice(parts, merge.parse_resolutions(reply, len(merge.conflicts(parts))))
    return out


def remerge_path(config, root, row, method):
    return root / config["output"] / "remerge" / row["arm"] / str(row["task_id"]) / f"{method}.json"


def remerge_one(config, root, src, row, instance, method, runner):
    out = remerge_path(config, root, row, method)
    if out.is_file():
        return
    spec = config["remerge"]
    work = run_dir(src, root, row) / "merge_work"
    paths = [p for p in read(work / "conflicts.txt").split("\n") if p]
    files = {p: read(work / "conflicted" / p) for p in paths if (work / "conflicted" / p).is_file()}
    conversation = json.loads(read(run_dir(src, root, row) / "conversation.json") or "[]")
    calls, record = [], {"arm": row["arm"], "instance": row["instance"], "method": method, "conflicted_files": paths}
    try:
        resolved = resolve(method, files, instance, conversation, merge.load_config(root / spec["merge_config"]),
                           spec.get("max_message_chars", 6000), calls)
    except Exception as exc:
        resolved, record["error"] = None, f"{type(exc).__name__}: {exc}"
    record["cost_usd"] = sum(c["cost_usd"] or 0 for c in calls)
    status = "unresolved"
    if resolved is not None and set(resolved) == set(paths):
        scratch = root / config["output"] / "scratch" / f"{row['arm']}_{row['task_id']}_{method}"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        for name in ("a.patch", "b.patch"):
            shutil.copy(work / name, scratch / name)
        for path, text in resolved.items():
            target = scratch / "resolved" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        parallact.docker_run(instance["runtime"]["image_uri"], parallact.MERGE, scratch, [instance["base_commit"]])
        status = read(scratch / "status").strip() or "error"
        if status == "resolved":
            merged = read(scratch / "merged.patch")
            result = parallact.test_state(runner, instance, merged, parallact.test_sets(instance)["combined"],
                                          root / config["output"] / "scratch", config.get("test_timeout", 2400))
            record["merged"] = result | {"A": parallact.side_passed(result, instance["agentA_fail_to_pass"]),
                                         "B": parallact.side_passed(result, instance["agentB_fail_to_pass"])}
            out.parent.mkdir(parents=True, exist_ok=True)
            (out.parent / f"{method}.patch").write_text(merged)
        shutil.rmtree(scratch, ignore_errors=True)
    record["status"] = "resolved" if status == "resolved" else "unresolved"
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / f"{method}_calls.json").write_text(json.dumps(calls, indent=2))
    out.write_text(json.dumps(record, indent=2))


def remerge_jobs(config, root):
    conflicted = [r for r in rows(config, root) if r["merge_status"] in ("resolved", "unresolved")]
    return [(r, m) for r in conflicted for m in config["remerge"]["methods"]]


def execute_remerge(config, root, workers=3, limit=None):
    from . import s1
    s1.prepare_environment(root)
    parallact.check_source(source(config, root), root)
    src = source(config, root)
    instances, runner = parallact.all_instances(src, root), parallact.load_runner(src, root)
    jobs = remerge_jobs(config, root)[:limit] if limit else remerge_jobs(config, root)

    def one(row, method):
        try:
            remerge_one(config, root, src, row, instances[row["instance"]], method, runner)
            print(f"remerged {method} {row['arm']} {row['instance'][9:45]}", flush=True)
        except Exception as exc:
            print(f"remerge failed: {method} {row['arm']} {row['instance']}: {type(exc).__name__}: {exc}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(one, r, m) for r, m in jobs]:
            f.result()
    return len(jobs)


# --- analysis (standard library) -----------------------------------------------------------

def select_summary(config, root):
    from .branch import exact_p
    src, all_rows = source(config, root), rows(config, root)
    per, out = [], {}
    for path in sorted((root / config["output"] / "select").glob("*.json")):
        rec = json.loads(path.read_text())
        cands = {c["arm"]: c for c in candidates(config, root, src, all_rows, rec["instance"])}
        if not cands:
            continue
        judge = cands.get(rec["choice_arm"], {}).get("both", False)
        vote = cands[rec["consensus_arm"]]["both"]
        hit = any(c["both"] for c in cands.values())
        per.append({"instance": rec["instance"], "judge": judge, "consensus": vote, "oracle": hit,
                    "random": sum(c["both"] for c in cands.values()) / len(cands),
                    "judge_arm": rec["choice_arm"], "consensus_arm": rec["consensus_arm"],
                    "parsed": rec["choice_arm"] is not None,
                    "cost_usd": sum(t.get("cost_usd") or 0 for t in rec["tries"])})
    if not per:
        return None
    up = sum(p["judge"] and not p["consensus"] for p in per)
    down = sum(p["consensus"] and not p["judge"] for p in per)
    headroom = [p for p in per if p["oracle"] and not p["consensus"]]
    out = {"instances": len(per), "parsed": sum(p["parsed"] for p in per),
           "both_pass": {"judge": sum(p["judge"] for p in per), "consensus": sum(p["consensus"] for p in per),
                         "random_expected": round(sum(p["random"] for p in per), 1),
                         "oracle": sum(p["oracle"] for p in per)},
           "judge_vs_consensus": {"judge_better": up, "consensus_better": down, "p": exact_p(up, up + down)},
           "decomposition": {"headroom": len(headroom), "recovered": sum(p["judge"] for p in headroom),
                             "consensus_correct": sum(p["consensus"] for p in per),
                             "damaged": sum(p["consensus"] and not p["judge"] for p in per)},
           "judge_arms": dict(Counter(p["judge_arm"] for p in per).most_common()),
           "consensus_arms": dict(Counter(p["consensus_arm"] for p in per).most_common()),
           "cost_usd": round(sum(p["cost_usd"] for p in per), 3)}
    return out, per


def remerge_summary(config, root):
    from .branch import exact_p
    by_run = {(r["arm"], r["task_id"]): r for r in rows(config, root) if r["merge_status"] in ("resolved", "unresolved")}
    results, per = {}, []
    for method in config["remerge"]["methods"]:
        for (arm, task), row in by_run.items():
            rec = json.loads(read(remerge_path(config, root, row, method)) or "{}")
            if not rec:
                continue
            merged = rec.get("merged", {})
            per.append({"arm": arm, "instance": row["instance"], "method": method, "status": rec["status"],
                        "A": merged.get("A", False), "B": merged.get("B", False),
                        "both": bool(merged.get("A") and merged.get("B")),
                        "blind_A": flag(row["merged_A"]), "blind_B": flag(row["merged_B"]), "blind_both": flag(row["both"]),
                        "own_both": flag(row["own_both"]), "cost_usd": rec.get("cost_usd", 0)})
    if not per:
        return None
    for method in config["remerge"]["methods"]:
        P = [p for p in per if p["method"] == method]
        up = sum(p["both"] and not p["blind_both"] for p in P)
        down = sum(p["blind_both"] and not p["both"] for p in P)
        req = lambda p, pre="": int(p[pre + "A"]) + int(p[pre + "B"])
        rup = sum(req(p) > req(p, "blind_") for p in P)
        rdown = sum(req(p) < req(p, "blind_") for p in P)
        results[method] = {"runs": len(P), "resolved": sum(p["status"] == "resolved" for p in P),
                           "both_pass": sum(p["both"] for p in P), "blind_both_pass": sum(p["blind_both"] for p in P),
                           "requests_passed": sum(req(p) for p in P), "blind_requests_passed": sum(req(p, "blind_") for p in P),
                           "vs_blind": {"better": up, "worse": down, "p": exact_p(up, up + down),
                                        "requests_better": rup, "requests_worse": rdown, "requests_p": exact_p(rup, rup + rdown)},
                           "own_both_runs": sum(p["own_both"] for p in P),
                           "both_pass_when_own_both": [sum(p["both"] for p in P if p["own_both"]), sum(p["own_both"] for p in P)],
                           "blind_both_pass_when_own_both": [sum(p["blind_both"] for p in P if p["own_both"]), sum(p["own_both"] for p in P)],
                           "cost_usd": round(sum(p["cost_usd"] for p in P), 3)}
    pairs = {}
    for p in per:
        pairs.setdefault((p["arm"], p["instance"]), {})[p["method"]] = p["both"]
    for a, b in (("aware", "blind_repeat"), ("blind_file", "blind_repeat"), ("aware_file", "blind_file"),
                 ("aware_file", "aware"), ("aware_file", "blind_repeat")):
        both = [v for v in pairs.values() if a in v and b in v]
        if both:
            up = sum(v[a] and not v[b] for v in both)
            down = sum(v[b] and not v[a] for v in both)
            results[f"{a}_vs_{b}"] = {"pairs": len(both), f"{a}_better": up, f"{b}_better": down,
                                      "p": exact_p(up, up + down)}
    return results, per


def report(config, root):
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    summary = {"study": config["study"], "question": config["question"]}
    sel, rem = select_summary(config, root), remerge_summary(config, root)
    if sel:
        summary["select"] = sel[0]
        write_csv(output / "select_rows.csv", root / "results" / f"{config['study']}_select_rows.csv", sel[1])
    if rem:
        summary["remerge"] = rem[0]
        write_csv(output / "remerge_rows.csv", root / "results" / f"{config['study']}_remerge_rows.csv", rem[1])
    for path in (output / "summary.json", root / "results" / f"{config['study']}_summary.json"):
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def write_csv(*paths_and_rows):
    *paths, data = paths_and_rows
    for path in paths:
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    workers = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else 4
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    if "--select" in argv:
        print(execute_select(config, root, workers, limit), "selections requested", flush=True)
    if "--remerge" in argv:
        print(execute_remerge(config, root, workers, limit), "re-merges requested", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
