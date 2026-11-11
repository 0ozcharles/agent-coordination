"""Branching from coordination conflict states: restart both agents of a CooperBench coop run at its
first conflict point and let them continue, unchanged (an A/A check) or later with an intervention.

The cut is taken at the conflict message found by conflict_states (docs/C_CONFLICT_CODEBOOK.md). The
sender's history ends after the step that sent it; the receiver's ends just before the step that
receives it. Both agents have then stated their claims, and neither has reacted to the other's.
Messages sent before the cut but not yet received are put back in the receiver's inbox. Only runs whose
agents wrote no file before the cut are used, so a fresh container from the task image holds the same
code. `execute` imports CooperBench and runs under its venv; everything else uses only the standard
library.
"""
from collections import Counter, defaultdict
import copy
import csv
from datetime import datetime
import json
from pathlib import Path
import random
import re
import sys

from . import conflict_states as cs
from . import s1

PARTNER = {"agent1": "agent2", "agent2": "agent1"}
# The prompt names the host OS (platform.uname), which changes with macOS updates; commands run in a Linux
# container either way, and the restored history keeps the original text.
HOST = re.compile(r"<system_information>.*?</system_information>", re.S)


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "states", "sample", "arms", "continuations", "model", "agent_config",
                "remerge", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    return config


def tool_end(msgs, i):
    """Index just past the tool results that answer assistant message i."""
    j = i + 1
    while j < len(msgs) and msgs[j].get("role") == "tool":
        j += 1
    return j


def delivered_text(msgs, recv):
    """The message body exactly as the receiver got it in the original run."""
    if recv is None or recv.get("inline"):
        return None
    match = cs.RECV.match(msgs[recv["step"]].get("content") or "")
    return match.group(2) if match else None


def cut(d):
    """Where to branch one run: per-agent history length, and the messages left in each inbox.

    Returns {"reason": ...} when the run cannot be branched faithfully.
    """
    fa, fb = d.name[1:].split("_f")
    paths = {"agent1": d / f"agent{fa}_traj.json", "agent2": d / f"agent{fb}_traj.json"}
    if not all(p.is_file() for p in paths.values()):
        return {"reason": "trajectories missing"}
    msgs = {a: json.loads(p.read_text())["messages"] for a, p in paths.items()}
    lanes = {a: cs.trajectory(paths[a], a)[0] for a in paths}
    counts = Counter(m["from"] for m in json.loads((d / "conversation.json").read_text()))
    if not all(sum(e["kind"] == "send" and e["delivered"] for e in lanes[a]) >= counts.get(a, 0) for a in lanes):
        return {"reason": "compacted trajectory"}
    sends = [e for e in cs.order(lanes) if e["kind"] == "send"]
    n = cs.first_conflict_message(sends)
    if not n:
        return {"reason": "no conflict point"}
    k = sends[n - 1]
    sender, receiver = k["agent"], PARTNER[k["agent"]]
    recv = next((e for e in lanes[receiver] if e["kind"] == "recv" and e.get("src") is k), None)
    if recv is None:
        return {"reason": "conflict message never received"}
    cuts = {sender: tool_end(msgs[sender], k["step"]),
            receiver: tool_end(msgs[receiver], recv["step"]) if recv.get("inline") else recv["step"]}
    for a in lanes:
        for e in lanes[a]:
            if e["step"] >= cuts[a]:
                continue
            if e["kind"] == "recv" and (e.get("src") is None or e["src"]["step"] >= cuts[PARTNER[a]]):
                return {"reason": "inconsistent cut"}
            if e["kind"] == "write":
                return {"reason": "file written before the cut"}
            if e["kind"] in ("exit", "partner_exit"):
                return {"reason": "exit before the cut"}
    inbox = {"agent1": [], "agent2": []}
    for s in sends:
        to = PARTNER[s["agent"]]
        if s["step"] >= cuts[s["agent"]]:
            continue
        got = next((e for e in lanes[to] if e["kind"] == "recv" and e.get("src") is s), None)
        if got is not None and got["step"] < cuts[to]:
            continue
        if not s["delivered"]:
            return {"reason": "undelivered message before the cut"}
        inbox[to].append({"from": s["agent"], "content": delivered_text(msgs[to], got) or s["text"]})
    return {"conflict_message": n, "sender": sender, "cut": cuts, "inbox": inbox,
            "features": {"agent1": fa, "agent2": fb}}


def prefix(d, state, agent):
    """The agent's restored history: its original messages up to the cut, as the API last saw them."""
    path = d / f"agent{state['features'][agent]}_traj.json"
    out = []
    for m in json.loads(path.read_text())["messages"][:state["cut"][agent]]:
        m = copy.deepcopy(m)
        if m.get("role") == "assistant" and m.get("tool_calls") and m.get("content") == "":
            m["content"] = None  # CooperBench saves None as "" when it writes the trajectory
        out.append(m)
    return out


def candidates(config, root):
    """Every library run that can be branched, with its source, row and cut; plus the skip reasons."""
    states_config = cs.load_config(root / config["states"]["config"])
    found, skipped = [], Counter()
    for set_name in config["states"]["sets"]:
        for source in states_config["sets"][set_name]:
            for d, row in cs.runs(root, source):
                c = cut(d)
                if "reason" in c:
                    skipped[c["reason"]] += 1
                else:
                    found.append({"source": source["name"], "run": str(d.relative_to(root)), "repo": d.parts[-3],
                                  "task_id": d.parts[-2], "features": [c["features"]["agent1"], c["features"]["agent2"]],
                                  "repeat": d.parts[-5][1:], "row": row, "state": c})
    return found, skipped


def select(config, root):
    """A seeded sample of branchable states, at most one per unit: a task, or a pair of features.

    With a number of states, the same number is taken from each source; with "all", every unit gets
    one state. `order: drawn` keeps the draw order, so a run stopped early still covers a random subset.
    """
    found, _ = candidates(config, root)
    sample = config["sample"]
    sources = sorted({c["source"] for c in found})
    rng = random.Random(sample["seed"])
    rng.shuffle(found)
    per_source = None if sample["states"] == "all" else sample["states"] // len(sources)
    seen, chosen = set(), []
    for c in found:
        unit = (c["repo"], c["task_id"]) + ((tuple(c["features"]),) if sample.get("per") == "pair" else ())
        if unit in seen or (per_source is not None and sum(x["source"] == c["source"] for x in chosen) >= per_source):
            continue
        seen.add(unit)
        chosen.append(c)
    if sample.get("order") == "drawn":
        return chosen
    return sorted(chosen, key=lambda c: (c["source"], c["repo"], c["task_id"], c["features"], c["repeat"]))


def name(state):
    return f"{state['source']}_r{state['repeat']}"


def plan(config, root):
    return [{**s, "arm": arm, "continuation": n}
            for s in select(config, root) for arm in config["arms"] for n in range(1, config["continuations"] + 1)]


def job_dir(output, job):
    return (Path(output) / "logs" / job["arm"] / f"{name(job)}_c{job['continuation']}" / "coop" / job["repo"]
            / str(job["task_id"]) / f"f{job['features'][0]}_f{job['features'][1]}")


def instruction(config, arm, agent):
    """The text an arm adds for one agent right before its first new model call, or None."""
    spec = config["arms"][arm]
    text = spec.get(f"instruction_{agent}", spec.get("instruction"))
    return config.get("instruction_prefix", "") + text if text else None


def claim_key(job):
    return "__".join([job["arm"], f"{name(job)}_c{job['continuation']}", job["repo"], str(job["task_id"]),
                      "_".join(job["features"])])


# --- execution (CooperBench venv only) -------------------------------------------------

RESTORE = {}  # agent id -> restored history and inbox for the job this process is running


def install_restore():
    """Make DefaultAgent start from a restored history instead of the bare task, when one is set.

    `run()` empties the history and adds the system and task messages; that first add is replaced by
    the restored prefix, after checking that the prompt renders as in the original run (apart from the
    host OS line). The agent then puts its pending messages in its own inbox and waits for its partner,
    so neither acts before both inboxes are filled (each agent clears its own inbox when it connects).
    An arm's instruction is added as a user message just before the first new model call, after the
    messages the agent receives at that step.
    """
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    if getattr(DefaultAgent, "_branch_restore", False):
        return
    original_init, original_add, original_query = DefaultAgent.__init__, DefaultAgent.add_messages, DefaultAgent.query

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._restore = RESTORE.get(self.agent_id)

    def add_messages(self, *messages):
        restore = getattr(self, "_restore", None)
        if restore is None or self.messages:
            return original_add(self, *messages)
        self._restore = None
        history = restore["messages"]
        for new, old in zip(messages, history[:2]):
            a, b = (HOST.sub("", m.get("content") or "") for m in (new, old))
            if a != b:
                at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                raise RuntimeError(f"{self.agent_id}: the {old.get('role')} prompt differs from the original run's "
                                   f"at character {at}: now {a[at - 80:at + 120]!r}, then {b[at - 80:at + 120]!r}")
        self.messages.extend(copy.deepcopy(history))
        self.n_calls = sum(m.get("role") == "assistant" for m in history)
        self._branch_instruction = restore.get("instruction")
        for m in restore["inbox"]:
            self.comm._client.rpush(self.comm._inbox_key, json.dumps(
                {"from": m["from"], "to": self.agent_id, "content": m["content"],
                 "timestamp": datetime.now().isoformat()}))
        restore["barrier"].wait(timeout=900)
        return list(messages)

    def query(self):
        text = getattr(self, "_branch_instruction", None)
        if text:
            self._branch_instruction = None
            self.add_messages(self.model.format_message(role="user", content=text))
        return original_query(self)

    DefaultAgent.__init__ = init
    DefaultAgent.add_messages = add_messages
    DefaultAgent.query = query
    DefaultAgent._branch_restore = True


def run_job(config, root, job, output):
    import threading
    from cooperbench import run
    from . import s2
    target = job_dir(output, job)
    label = f"{job['arm']} {name(job)} c{job['continuation']} {job['repo']}/{job['task_id']} {job['features']}"
    dataset_dir = str(root / "experiments/m0/CooperBench/dataset")
    scoring_job = {"setting": "coop", "repeat": job["continuation"], "repo": job["repo"],
                   "task_id": int(job["task_id"]), "features": [int(f) for f in job["features"]]}
    try:
        if (target / "eval.json").is_file() and s1.agent_errors(target):
            attempt = 1
            while target.with_name(f"{target.name}.error{attempt}").exists():
                attempt += 1
            target.rename(target.with_name(f"{target.name}.error{attempt}"))
            print(f"retry {label} after agent error", flush=True)
        if not (target / "eval.json").is_file():
            print(f"run {label}", flush=True)
            source = root / job["run"]
            barrier = threading.Barrier(2)
            RESTORE.clear()
            for agent in ("agent1", "agent2"):
                RESTORE[agent] = {"messages": prefix(source, job["state"], agent),
                                  "inbox": job["state"]["inbox"][agent], "barrier": barrier,
                                  "instruction": instruction(config, job["arm"], agent)}
            target.mkdir(parents=True, exist_ok=True)
            (target / "branch.json").write_text(json.dumps(
                {k: job[k] for k in ("source", "run", "arm", "continuation", "state")}, indent=2))
            s1.CURRENT_JOB_DIR = target
            run(run_name=f"{job['arm']}/{name(job)}_c{job['continuation']}", repo=job["repo"],
                task_id=int(job["task_id"]), features=[int(f) for f in job["features"]],
                model_name=f"openai/{config['model']}", agent="mini_swe_agent_v2", setting="coop",
                backend="docker", concurrency=1, auto_eval=True, messaging_enabled=True, git_enabled=False,
                redis_url=config.get("redis", "redis://localhost:6379"),
                agent_config=str(root / config["agent_config"]), dataset_dir=dataset_dir,
                logs_dir=str(output / "logs"))
        RESTORE.clear()
        s1.CURRENT_JOB_DIR = None
        if (target / "eval.json").is_file() and not s1.agent_errors(target):
            if config.get("score_agents", True):
                s1.score_agents(target, scoring_job, dataset_dir)
                if not s1.read_json(target / "eval.json").get("both_passed"):
                    s1.score_local(target, scoring_job, dataset_dir)
            s2.remerge({"remerge": config["remerge"]}, root, scoring_job, target)
    except Exception as exc:  # one broken job must not stop the rest of the shard
        RESTORE.clear()
        s1.CURRENT_JOB_DIR = None
        print(f"job failed: {label}: {type(exc).__name__}: {exc}", flush=True)


def execute(config, root, shard=(0, 1), limit=None):
    """Run missing branches in plan order. Workers share one queue: each claims a branch by creating a
    directory under claims/ (the CLI clears it before starting workers), so a long branch does not hold
    up a fixed share of the others."""
    import os
    s1.setup_runner(root, True, config.get("redis", "redis://localhost:6379"))
    install_restore()
    output = root / config["output"]
    claims = output / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    jobs = plan(config, root)[:limit] if limit else plan(config, root)
    if shard[0] == 0:
        (output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    for job in jobs:
        try:
            os.mkdir(claims / claim_key(job))
        except FileExistsError:
            continue
        run_job(config, root, job, output)


def label_branches(config, root, workers=4):
    """Label every finished branch with the frozen conflict codebook (cents)."""
    from concurrent.futures import ThreadPoolExecutor
    s1.load_env_file(root)
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env or export it. No request was made.")
    labeler = labeler_config(config, root)
    codebook = (root / labeler["codebook"]).read_text()
    recs = [rec for _, rec in branch_states(config, root)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(cs.label_one, labeler, root, codebook, r) for r in recs]:
            f.result()
    return len(recs)


# --- analysis (standard library only) --------------------------------------------------

def labeler_config(config, root):
    """The conflict-state labeler, writing its labels under this study's output."""
    return {**cs.load_config(root / config["states"]["config"]), "output": config["output"]}


def outcome(target, features, method_name="llm"):
    """Official and re-merged both-pass for one finished coop run directory, or None if not scored."""
    evaluation = s1.read_json(target / "eval.json")
    if not evaluation or s1.agent_errors(target):
        return None
    f1, f2 = (str(f) for f in features)
    official = {f for f, key in ((f1, "feature1"), (f2, "feature2")) if (evaluation.get(key) or {}).get("passed")}
    remerged = official
    if (evaluation.get("merge") or {}).get("status") == "conflicts":
        entry = (s1.read_json(target / "merge.json").get("methods") or {}).get(method_name)
        remerged = {f for f in (f1, f2) if entry.get(f)} if entry else None
    agents = s1.read_json(target / "agent_eval.json")
    result = s1.read_json(target / "result.json")
    return {"merge": (evaluation.get("merge") or {}).get("status", ""), "official": int(official >= {f1, f2}),
            "remerged": None if remerged is None else int(remerged >= {f1, f2}),
            "merged_passed": " ".join(sorted(remerged if remerged is not None else official)),
            "own_passed": " ".join(f for f in (f1, f2) if agents.get(f, {}).get(f)),
            "cost_usd": result.get("total_cost") or 0.0, "messages": result.get("messages_sent", 0),
            "steps": result.get("total_steps", 0), "minutes": round((result.get("duration_seconds") or 0) / 60, 1)}


def original_outcome(root, state):
    """The original run's outcome on the same two scores (S1 main is re-merged by the merge test)."""
    row = state["row"]
    if "official_success" in row:
        return {"official": int(row["official_success"] == "True"), "remerged": int(row["success"] == "True")}
    official = int(row["success"] == "True")
    if row["merge_status"] != "conflicts":
        return {"official": official, "remerged": official}
    for r in csv.DictReader(open(root / "results/merge_test_rows.csv")):
        if (r["repeat"], r["repo"], r["task_id"], r["features"]) == (row["repeat"], row["repo"], row["task_id"],
                                                                     row["features"]):
            return {"official": official, "remerged": int(set(r["llm_passed"].split()) >= set(row["features"].split()))}
    return {"official": official, "remerged": None}


def branch_states(config, root):
    """(job, conflict-state record) for every finished branch, built from its own trajectories."""
    output = root / config["output"]
    for job in plan(config, root):
        target = job_dir(output, job)
        result = outcome(target, job["features"])
        if result is None:
            continue
        row = {"merged_passed": result["merged_passed"], "own_passed": result["own_passed"],
               "merge_status": result["merge"], "refined_label": ""}
        yield job, dict(cs.state(root, target, row), set=f"branch_{job['arm']}")


def first_reply_ok(config, target, job):
    """Whether each agent's first new step is fed the messages left in its inbox at the cut, followed by
    the arm's instruction if there is one."""
    ok = True
    for agent in ("agent1", "agent2"):
        expected = job["state"]["inbox"][agent]
        text = instruction(config, job["arm"], agent)
        if not expected and not text:
            continue
        path = target / f"agent{job['state']['features'][agent]}_traj.json"
        full = target / f"{agent}_full_traj.json"  # written when the context was compacted; starts uncompacted
        if full.is_file():
            msgs = json.loads(full.read_text())["segments"][0]["messages"]
        else:
            msgs = json.loads(path.read_text())["messages"] if path.is_file() else []
        start = job["state"]["cut"][agent]
        got = [cs.RECV.match(m.get("content") or "") for m in msgs[start:start + len(expected)]]
        ok &= all(g and g.group(2) == e["content"] for g, e in zip(got, expected)) and len(got) == len(expected)
        if text:
            after = msgs[start + len(expected)] if len(msgs) > start + len(expected) else {}
            ok &= after.get("role") == "user" and after.get("content") == text
    return ok


def report(config, root):
    output = root / config["output"]
    states_config = labeler_config(config, root)
    original_labels = cs.load_config(root / config["states"]["config"])
    rows = []
    for job in plan(config, root):
        target = job_dir(output, job)
        result = outcome(target, job["features"])
        base = original_outcome(root, job)
        rec = None
        if result is not None:
            row = {"merged_passed": result["merged_passed"], "own_passed": result["own_passed"],
                   "merge_status": result["merge"], "refined_label": ""}
            rec = dict(cs.state(root, target, row), set=f"branch_{job['arm']}")
        label = cs.load_label(states_config, root, rec) if rec else None
        source_rec = {"run": job["run"], "set": job["source"]}
        original = cs.load_label(original_labels, root, source_rec)
        rows.append({"state": f"{name(job)} {job['repo']}/{job['task_id']} {' '.join(job['features'])}",
                     "source": job["source"], "run": job["run"], "arm": job["arm"], "continuation": job["continuation"],
                     "cut1": job["state"]["cut"]["agent1"], "cut2": job["state"]["cut"]["agent2"],
                     "finished": result is not None, "order": rec["source"] if rec else "",
                     "inbox_delivered": first_reply_ok(config, target, job) if result is not None else "",
                     "conflict_point": rec["conflict_point"] if rec else "",
                     "messages": rec["messages"] if rec else "",
                     "original_official": base["official"], "original_remerged": base["remerged"],
                     **{k: (result or {}).get(k, "") for k in ("merge", "official", "remerged", "own_passed",
                                                               "cost_usd", "steps", "minutes")},
                     "target": config["arms"][job["arm"]].get("target", ""),
                     "original_style": " + ".join(original[a]["style"] for a in ("agent1", "agent2")) if original else "",
                     "style": " + ".join(label[a]["style"] for a in ("agent1", "agent2")) if label else "",
                     "handoff": label["handoff"] if label else "", "original_handoff": original["handoff"] if original else ""})
    output.mkdir(parents=True, exist_ok=True)
    with open(output / "branch_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(config, root, rows)
    (output / "branch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def exact_p(k, n):
    """Two-sided exact binomial test of k successes in n trials against one half (paired sign test)."""
    from math import comb
    if n == 0:
        return None
    tail = sum(comb(n, i) for i in range(min(k, n - k) + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def agents(r, key):
    return r[key].split(" + ") if r[key] else ["", ""]


def agree(pairs):
    pairs = [(a, b) for a, b in pairs if a not in ("", None) and b not in ("", None)]
    return {"agree": sum(a == b for a, b in pairs), "of": len(pairs)}


def arm_stats(config, done):
    """Per arm: labeled styles (the manipulation check), outcomes, and the paired comparison with the
    control arm on the re-merged score."""
    control = config.get("control")
    by_arm = defaultdict(list)
    for r in done:
        by_arm[r["arm"]].append(r)
    base = {r["state"]: r for r in by_arm.get(control, [])}
    out = {}
    for arm in config["arms"]:
        rs = by_arm.get(arm, [])
        styles = [x for r in rs for x in agents(r, "style") if x]
        target = config["arms"][arm].get("target")
        scored = [r for r in rs if r["remerged"] not in ("", None)]
        stats = {"branches": len(rs), "labeled_agents": len(styles), "agent_styles": dict(Counter(styles)),
                 "target": target, "target_share": round(styles.count(target) / len(styles), 3) if target and styles else None,
                 "handoff_runs": sum(r["handoff"] is True for r in rs),
                 "remerged_pass": sum(r["remerged"] == 1 for r in scored), "scored": len(scored),
                 "official_pass": sum(r["official"] == 1 for r in rs),
                 "merge_conflicts": sum(r["merge"] == "conflicts" for r in rs),
                 "mean_messages": round(sum(r["messages"] or 0 for r in rs) / len(rs), 1) if rs else None,
                 "mean_minutes": round(sum(float(r["minutes"] or 0) for r in rs) / len(rs), 1) if rs else None,
                 "cost_usd": round(sum(float(r["cost_usd"] or 0) for r in rs), 4)}
        if control and arm != control:
            pairs = [(r["remerged"], base[r["state"]]["remerged"]) for r in scored
                     if r["state"] in base and base[r["state"]]["remerged"] not in ("", None)]
            only_arm = sum(a == 1 and b == 0 for a, b in pairs)
            only_control = sum(a == 0 and b == 1 for a, b in pairs)
            stats["vs_control"] = {"pairs": len(pairs), "both_pass": sum(a == b == 1 for a, b in pairs),
                                   "only_arm": only_arm, "only_control": only_control,
                                   "sign_test_p": exact_p(only_arm, only_arm + only_control)}
        out[arm] = stats
    return out


def summarize(config, root, rows):
    found, skipped = candidates(config, root)
    done = [r for r in rows if r["finished"]]
    # A branch whose context was compacted has its messages grouped by sender, as in the library; its
    # labels are left out of the comparisons.
    done = [r if r["order"] == "trajectories" else {**r, "style": "", "handoff": ""} for r in done]
    originals = {r["state"]: r for r in rows}.values()
    summary = {"study": config["study"], "branchable_runs": len(found), "not_branchable": dict(skipped),
               "states": len(originals), "jobs": len(rows), "finished": len(done),
               "inbox_delivered": sum(r["inbox_delivered"] is True for r in done),
               "compacted": sum(r["order"] == "conversation" for r in done),
               "conflict_point_again": sum(r["conflict_point"] is True for r in done),
               "both_pass": {"original_remerged": sum(r["original_remerged"] or 0 for r in originals),
                             "original_official": sum(r["original_official"] for r in originals),
                             "branches_remerged": sum(r["remerged"] or 0 for r in done),
                             "branches_official": sum(r["official"] or 0 for r in done),
                             "states": len(originals), "branches": len(done)},
               "cost_usd": round(sum(float(r["cost_usd"] or 0) for r in done), 4)}
    if len(config["arms"]) > 1:
        summary["arms"] = arm_stats(config, done)
    if config["continuations"] >= 2:
        by_state = defaultdict(list)
        for r in done:
            by_state[r["state"]].append(r)
        pairs = [(rs[0], rs[1]) for rs in by_state.values() if len(rs) == config["continuations"] * len(config["arms"])]
        summary.update({
            "remerged_c1_vs_c2": agree([(a["remerged"], b["remerged"]) for a, b in pairs]),
            "remerged_branch_vs_original": agree([(r["remerged"], r["original_remerged"]) for r in done]),
            "style_pair_c1_vs_c2": agree([(a["style"], b["style"]) for a, b in pairs]),
            "style_pair_branch_vs_original": agree([(r["style"], r["original_style"]) for r in done]),
            "agent_style_c1_vs_c2": agree([(x, y) for a, b in pairs
                                           for x, y in zip(agents(a, "style"), agents(b, "style"))]),
            "agent_style_branch_vs_original": agree([(x, y) for r in done
                                                     for x, y in zip(agents(r, "style"), agents(r, "original_style"))]),
            "handoff_c1_vs_c2": agree([(a["handoff"], b["handoff"]) for a, b in pairs]),
            "handoff_branch_vs_original": agree([(r["handoff"], r["original_handoff"]) for r in done])})
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    if len(argv) > 1 and argv[1] == "--label":
        print(f"labeled {label_branches(config, root)} branches")
        return 0
    shard, limit = (0, 1), None
    if "--shard" in argv:
        shard = tuple(int(v) for v in argv[argv.index("--shard") + 1].split("/"))
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    execute(config, root, shard, limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
