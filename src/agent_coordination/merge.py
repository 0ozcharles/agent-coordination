"""Offline merge test: re-merge the patch pairs of S1 coop runs whose merge conflicted.

CooperBench merges the two agents' patches with a plain `git merge` and scores a conflict
as a failure. This module redoes the merge with other methods and tests the result
against both features. No agent is re-run. `execute` needs the CooperBench venv and
Docker; the analysis uses only the standard library.
"""
from collections import Counter
import base64
import csv
import json
from pathlib import Path
import random
import re
import shlex
import sys

from . import s1

METHODS = ("union", "llm")
CONTEXT_LINES = 40
PROMPT = """Two developers changed the same file in parallel, each implementing a different feature, and git could not merge their changes. Each conflict block below shows developer B's version, the original code, and developer A's version.

Resolve every conflict so the file keeps both developers' changes. Combine them; do not drop either side's functionality and do not add anything new. Keep the surrounding indentation.

File: {path}

{excerpt}

There {count_text}. Reply with exactly {count} section(s), in order. Each section is a header line followed by one fenced code block containing only the code that replaces the whole conflict block (from the <<<<<<< line through the >>>>>>> line), with no markers left:

{template}"""


def prompt_for(path, parts):
    count = len(conflicts(parts))
    template = "".join(f"=== RESOLUTION {i} ===\n```\nresolved code for conflict {i}\n```\n"
                       for i in range(1, count + 1))
    count_text = "is 1 conflict" if count == 1 else f"are {count} conflicts"
    return PROMPT.format(path=path, excerpt=excerpt(parts), count=count, count_text=count_text,
                         template=template)


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "decision_rule", "methods", "model", "sets", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    if set(config["methods"]) - set(METHODS):
        raise ValueError(f"Merge methods are limited to {', '.join(METHODS)}.")
    return config


def select(config, root, name):
    """Coop runs of one set whose naive merge conflicted, as S1 jobs plus their log directory."""
    spec = config["sets"][name]
    source = s1.load_config(root / spec["source"])
    with open(root / source["output"] / "rows.csv") as handle:
        rows = [r for r in csv.DictReader(handle)
                if r["setting"] == "coop" and r["merge_status"] == "conflicts"
                and r["repo"] not in spec.get("exclude_repos", ())]
    if spec.get("sample"):
        rows = random.Random(spec.get("seed", 0)).sample(rows, min(spec["sample"], len(rows)))
    jobs = []
    for r in rows:
        job = {"repeat": int(r["repeat"]), "setting": "coop", "repo": r["repo"], "task_id": int(r["task_id"]),
               "features": [int(f) for f in r["features"].split()]}
        jobs.append({**job, "source": s1.log_dir(root / source["output"], job), "row": r})
    return sorted(jobs, key=lambda j: (j["repo"], j["task_id"], j["features"], j["repeat"]))


def target_dir(output, job):
    return s1.log_dir(output, job)


# --- conflict blocks (pure) -------------------------------------------------------------

def blocks(text):
    """Split a file with diff3 conflict markers into plain strings and conflict dicts."""
    parts, plain, block, side = [], [], None, None
    for line in text.splitlines(keepends=True):
        if block is None and line.startswith("<<<<<<< "):
            parts.append("".join(plain))
            plain, block, side = [], {"ours": [], "base": [], "theirs": []}, "ours"
        elif block is not None and line.startswith("||||||| ") and side == "ours":
            side = "base"
        elif block is not None and line.rstrip("\r\n") == "=======" and side in ("ours", "base"):
            side = "theirs"
        elif block is not None and line.startswith(">>>>>>> ") and side == "theirs":
            parts.append({k: "".join(v) for k, v in block.items()})
            block, side = None, None
        elif block is not None:
            block[side].append(line)
        else:
            plain.append(line)
    if block is not None:
        raise ValueError("Unterminated conflict block.")
    parts.append("".join(plain))
    return [p for p in parts if p != ""]


def conflicts(parts):
    return [p for p in parts if isinstance(p, dict)]


def splice(parts, resolutions):
    out, i = [], 0
    for part in parts:
        if isinstance(part, dict):
            text = resolutions[i]
            out.append(text if not text or text.endswith("\n") else text + "\n")
            i += 1
        else:
            out.append(part)
    return "".join(out)


def union(parts):
    return splice(parts, [c["ours"] + c["theirs"] for c in conflicts(parts)])


def render(part):
    return ("<<<<<<< developer B\n" + part["ours"] + "||||||| original\n" + part["base"]
            + "=======\n" + part["theirs"] + ">>>>>>> developer A\n")


def excerpt(parts, context=CONTEXT_LINES):
    """The conflict blocks with `context` lines of surrounding code; distant code becomes '...'."""
    lines, keep, number = [], [], 0
    for part in parts:
        if isinstance(part, dict):
            number += 1
            start = len(lines)
            lines += [f"[CONFLICT {number}]\n", *render(part).splitlines(keepends=True)]
            keep.append((start, len(lines)))
        else:
            lines += part.splitlines(keepends=True)
    show = set()
    for start, end in keep:
        show.update(range(max(0, start - context), min(len(lines), end + context)))
    out, skipped = [], False
    for i, line in enumerate(lines):
        if i in show:
            out.append(line if line.endswith("\n") else line + "\n")
            skipped = False
        elif not skipped:
            out.append("...\n")
            skipped = True
    return "".join(out)


def parse_resolutions(reply, count):
    found = re.split(r"^=== RESOLUTION (\d+) ===[ \t]*\n?", reply, flags=re.M)
    sections = {int(found[i]): found[i + 1] for i in range(1, len(found) - 1, 2)}
    sections = {k: v for k, v in sections.items() if k <= count or v.strip()}  # ignore empty extras
    if sorted(sections) != list(range(1, count + 1)):
        raise ValueError(f"Expected {count} resolutions, got {sorted(sections)}.")
    out = []
    for i in range(1, count + 1):
        text = sections[i]
        fenced = re.search(r"```[^\n]*\n(.*?)\n?```[ \t]*$(?![\s\S]*```)", text, flags=re.S | re.M)  # to the last fence
        body = fenced.group(1) if fenced else text.strip("\n")
        out.append(body + "\n" if body.strip() else "")
    return out


# --- execution (CooperBench venv only) -------------------------------------------------

def shell(sb, script):
    result = sb.exec("bash", "-c", script)
    return result.returncode, result.stdout_read() + result.stderr_read()


def write_file(sb, path, text, chunk=60000):
    """Write a repo file in pieces: one exec argument is capped at 128 KB on Linux."""
    encoded = base64.b64encode(text.encode()).decode()
    shell(sb, "rm -f /tmp/resolved.b64")
    for i in range(0, len(encoded), chunk):
        shell(sb, f"echo -n '{encoded[i:i + chunk]}' >> /tmp/resolved.b64")
    shell(sb, f"cd /workspace/repo && base64 -d /tmp/resolved.b64 > {shlex.quote(path)}")


MERGE = """
cd /workspace/repo
git merge --abort >/dev/null 2>&1
git checkout -f -B work agent2 >/dev/null 2>&1
git clean -fdq
git -c merge.conflictStyle=diff3 merge agent1 --no-commit --no-ff >/dev/null 2>&1
git diff --name-only --diff-filter=U
"""


def ask_model(config, prompt):
    import litellm
    kwargs = config.get("model_kwargs", {})
    last = None
    for _ in range(3):
        try:
            response = litellm.completion(model=f"openai/{config['model']}", drop_params=True,
                                          messages=[{"role": "user", "content": prompt}], **kwargs)
            try:
                cost = litellm.completion_cost(completion_response=response)
            except Exception:
                cost = None
            return response.choices[0].message.content or "", cost
        except Exception as exc:  # transient API errors
            last = exc
    raise RuntimeError(f"model call failed: {last}")


def resolve(config, method, files, log):
    """Resolved contents for every conflicted file, or raise if the method cannot produce them."""
    out = {}
    for path, text in files.items():
        parts = blocks(text)
        if not conflicts(parts):
            out[path] = text  # e.g. modify/delete: keep the surviving version
        elif method == "union":
            out[path] = union(parts)
        else:
            prompt = prompt_for(path, parts)
            reply, cost = ask_model(config, prompt)
            log.append({"path": path, "conflicts": len(conflicts(parts)), "prompt": prompt, "reply": reply,
                        "cost_usd": cost})
            out[path] = splice(parts, parse_resolutions(reply, len(conflicts(parts))))
    return out


def merge_run(config, job, target, dataset_dir):
    from cooperbench.eval.backends import get_backend
    from cooperbench.eval.sandbox import _filter_test_files, _load_patch, _run_tests, _setup_branches, _write_patch
    from cooperbench.utils import get_image_name
    f1, f2 = job["features"]
    task = Path(dataset_dir) / job["repo"] / f"task{job['task_id']}"
    sb = get_backend("docker").create_sandbox(get_image_name(job["repo"], job["task_id"]), 1800)
    record = {"job": {k: job[k] for k in ("repeat", "repo", "task_id", "features")}, "methods": {}}
    try:
        for n, fid in ((1, f1), (2, f2)):
            patch = job["source"] / f"agent{fid}.patch"
            _write_patch(sb, f"patch{n}.patch", _filter_test_files(_load_patch(patch.read_text()) or "")
                         if patch.is_file() else "")
            _write_patch(sb, f"tests{n}.patch", (task / f"feature{fid}" / "tests.patch").read_text())
        setup = _setup_branches(sb)
        if setup.get("error") or "failed" in setup["apply_status"].values():
            record["error"] = setup.get("error") or f"apply failed: {setup['apply_status']}"
            return record
        base = setup["base_sha"]
        _, listing = shell(sb, MERGE)
        paths = [p for p in listing.split() if p]
        files = {}
        for path in paths:
            _, encoded = shell(sb, f"cd /workspace/repo && base64 -w0 {shlex.quote(path)}")
            files[path] = base64.b64decode(encoded).decode("utf-8", errors="replace")
        conflict_dir = target / "conflicts"
        conflict_dir.mkdir(parents=True, exist_ok=True)
        for path, text in files.items():
            (conflict_dir / path.replace("/", "__")).write_text(text)
        record["conflicted_files"] = paths
        record["conflict_blocks"] = sum(len(conflicts(blocks(t))) for t in files.values())
        for method in config["methods"]:
            entry, calls = {"status": "resolved"}, []
            try:
                resolved = resolve(config, method, files, calls)
            except Exception as exc:
                entry.update(status="unresolved", error=f"{type(exc).__name__}: {exc}")
                resolved = None
            if calls:
                (target / f"{method}_calls.json").write_text(json.dumps(calls, indent=2))
                entry["cost_usd"] = sum(c["cost_usd"] or 0 for c in calls)
            if resolved is not None:
                shell(sb, MERGE)
                for path, text in resolved.items():
                    write_file(sb, path, text)
                code, out = shell(sb, f"cd /workspace/repo && git add -A && git commit -qm merged --no-verify "
                                      f"&& git diff {base} HEAD > /patches/merged_{method}.patch "
                                      f"&& cat /patches/merged_{method}.patch")
                if code != 0:
                    entry.update(status="unresolved", error=out[-2000:])
                else:
                    (target / f"merged_{method}.patch").write_text(out)
            record["methods"][method] = entry
        for method, entry in record["methods"].items():
            if entry["status"] != "resolved":
                continue
            for n, fid in ((1, f1), (2, f2)):
                result = _run_tests(sb, f"tests{n}.patch", f"merged_{method}.patch", base)
                entry[str(fid)] = bool(result["passed"])
                entry[f"output_{fid}"] = result["output"][-4000:]
        return record
    finally:
        sb.terminate()


def execute(config, root, name, shard=(0, 1)):
    s1.prepare_environment(root)
    output = root / config["output"] / name
    dataset_dir = root / "experiments/m0/CooperBench/dataset"
    output.mkdir(parents=True, exist_ok=True)
    if shard[0] == 0:
        (output / "config.json").write_text(json.dumps({**config, "set": name, "prompt": PROMPT}, indent=2))
    for job in select(config, root, name)[shard[0]::shard[1]]:
        target = target_dir(output, job)
        label = f"r{job['repeat']} {job['repo']}/{job['task_id']} {job['features']}"
        if (target / "merge.json").is_file():
            continue
        print(f"merge {label}", flush=True)
        try:
            target.mkdir(parents=True, exist_ok=True)
            record = merge_run(config, job, target, dataset_dir)
            (target / "merge.json").write_text(json.dumps(record, indent=2))
        except Exception as exc:  # one broken run must not stop the shard
            print(f"merge failed: {label}: {type(exc).__name__}: {exc}", flush=True)
    if shard[1] == 1:
        report(config, root, name)


# --- analysis (standard library only) --------------------------------------------------

def collect(config, root, name):
    output = root / config["output"] / name
    rows = []
    for job in select(config, root, name):
        path = target_dir(output, job) / "merge.json"
        if path.is_file():
            rows.append({"job": job, "record": json.loads(path.read_text())})
    return rows


def summarize(config, rows):
    """Per method: features and runs recovered, regressions, failures to produce a merge."""
    out = {}
    for method in config["methods"]:
        tally = Counter()
        for item in rows:
            row, entry = item["job"]["row"], item["record"].get("methods", {}).get(method)
            features = row["features"].split()
            labels = dict(x.split(":") for x in row["feature_labels"].split())
            official = set(row["merged_passed"].split())
            passed = {f for f in features if entry and entry.get(f)}
            tally["runs"] += 1
            tally["unresolved"] += not entry or entry.get("status") != "resolved"
            tally["integration_features"] += sum(c == "integration" for c in labels.values())
            tally["integration_recovered"] += sum(labels.get(f) == "integration" and f in passed for f in features)
            tally["features_passed"] += len(passed)
            tally["runs_passed"] += len(passed) == 2
            tally["runs_passed_officially"] += row["label"] == "passed"
            tally["regressions"] += len(official - passed)
            tally["cost_usd"] += entry.get("cost_usd", 0) if entry else 0
        out[method] = dict(tally)
    return out


def report(config, root, name):
    rows = collect(config, root, name)
    errors = [r for r in rows if r["record"].get("error")]
    print(f"{config['study']} [{name}]: {len(rows)} conflicted runs merged, {len(errors)} could not be set up")
    blocks_seen = [r["record"].get("conflict_blocks", 0) for r in rows if not r["record"].get("error")]
    if blocks_seen:
        print(f"conflict blocks per run: median {sorted(blocks_seen)[len(blocks_seen) // 2]}, max {max(blocks_seen)}")
    summary = summarize(config, rows)
    for method, t in summary.items():
        print(f"\n{method}: {config['methods'][method]}")
        print(f"  integration features recovered: {t['integration_recovered']}/{t['integration_features']}")
        print(f"  runs passing both features: {t['runs_passed']}/{t['runs']} "
              f"(officially {t['runs_passed_officially']})")
        print(f"  regressions: {t['regressions']}   no merge produced: {t['unresolved']}   "
              f"model cost: ${t['cost_usd']:.4f}")
    output = root / config["output"] / name
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "rows.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["repeat", "repo", "task_id", "features", "s1_label", "feature_labels", "merged_passed",
                         "conflict_blocks", *[f"{m}_passed" for m in config["methods"]]])
        for item in rows:
            row, record = item["job"]["row"], item["record"]
            writer.writerow([row["repeat"], row["repo"], row["task_id"], row["features"], row["label"],
                             row["feature_labels"], row["merged_passed"], record.get("conflict_blocks", ""),
                             *[" ".join(f for f in row["features"].split() if record.get("methods", {}).get(m, {}).get(f))
                               for m in config["methods"]]])
    (output / "summary.json").write_text(json.dumps(
        {"study": config["study"], "set": name, "question": config["question"], "runs": len(rows),
         "setup_errors": len(errors), "methods": summary}, indent=2))
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    name = argv[1]
    shard = (0, 1)
    if len(argv) == 4 and argv[2] == "--shard":
        shard = tuple(int(v) for v in argv[3].split("/"))
    execute(config, root, name, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
