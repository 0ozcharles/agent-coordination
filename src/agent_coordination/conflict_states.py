"""Conflict-state library for coordination (class C) conflicts in CooperBench coop runs.

For every coop run with message logs, rebuild the message order from both agents' trajectories, find
the first conflict point (both agents claim the same file), record each agent's trajectory step there
(the state a branch would resume from) and whether it had written any file before it. A model labeler
then reads the messages, never the code or test results, and labels conflict types and each agent's
handling style with docs/C_CONFLICT_CODEBOOK.md. `execute` needs the OpenAI key; everything else uses
only the standard library.
"""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import random
import re
import sys

DATASET = "experiments/m0/CooperBench/dataset"
TYPES = ("resource", "responsibility", "interface", "requirement")
FACTS = ("agreement", "shared_code_owner", "interface_exact", "handoff", "unanswered_question")
CHOICES = {"agreement": {"explicit", "partial", "none"},
           "shared_code_owner": {"agent1", "agent2", "split", "neither", "unclear"}}
STYLE = {(2, 0): "competing", (2, 1): "compromising", (2, 2): "collaborating",
         (1, 0): "avoiding", (1, 1): "compromising", (1, 2): "accommodating",
         (0, 0): "avoiding", (0, 1): "accommodating", (0, 2): "accommodating"}

PATH = re.compile(r"[\w./-]*\w\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|rb|toml|cfg|yaml|yml|json)\b")
IMPLICIT = re.compile(r"\bsame (?:file|files|module|function|method|class)\b", re.I)
EXCLUSIVE = re.compile(r"please avoid|avoid (?:editing|touching|modifying|overlapping|those|that|these|this|it)\b"
                       r"|don.t (?:edit|touch|modify)|stay out of|keep out of", re.I)
WRITE = re.compile(r"apply_patch|cat\s*>[^&|>]|cat\s*<<'?\w+'?\s*>|python3? - <<|sed -i|perl -pi|\.write_text"
                   r"|open\([^)]*['\"][wa]['\"]|git (?:commit|apply)")
SEND_HEREDOC = re.compile(r"send_message(?:\s+--wait)?\s+(agent\d)\s+<<-?\s*['\"]?(\w+)['\"]?\n(.*?)(?:\n\2[ \t]*(?:\n|$)|$)", re.S)
SEND_QUOTED = re.compile(r"send_message(?:\s+--wait)?\s+(agent\d)\s+(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)')", re.S)
RECV = re.compile(r"^\[Message from (agent\d)[^\]]*\]:\s*(.*)", re.S)
REPLY = re.compile(r"\[Reply from (agent\d)\]:\s*(.*)", re.S)


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "sets", "conflict_point", "codebook", "labeler", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    return config


def files_in(text):
    return {p.rsplit("/", 1)[-1] for p in PATH.findall(text)}


def key(text):
    return re.sub(r"\\u[0-9a-fA-F]{4}|\W", "", text)[:50]


def sends_in(command):
    """(recipient, text) for every send_message in a shell command: heredoc (terminated or not) or quoted."""
    out = [(to, body) for to, _, body in SEND_HEREDOC.findall(command)]
    if not out:
        out = [(to, dq.replace('\\"', '"').replace("\\n", "\n") if dq else sq) for to, dq, sq in SEND_QUOTED.findall(command)]
    return out


def _tool_output(message):
    text = message.get("content") if isinstance(message.get("content"), str) else ""
    try:
        return str(json.loads(text).get("output", text))
    except (ValueError, AttributeError):
        return text


# ---------------------------------------------------------------- trajectories and order

def trajectory(path, me):
    """Events in one agent's trajectory by step: send, recv (also inline --wait replies), write,
    partner_exit and exit; plus the trajectory length."""
    msgs = json.loads(Path(path).read_text())["messages"]
    out = []
    for i, m in enumerate(msgs):
        content = m.get("content") if isinstance(m.get("content"), str) else ""
        if m["role"] == "assistant":
            for call in m.get("tool_calls") or []:
                try:
                    cmd = json.loads(call["function"]["arguments"]).get("command", "")
                except (ValueError, KeyError, TypeError):
                    continue
                sends = sends_in(cmd)
                if not sends and WRITE.search(cmd):
                    out.append({"agent": me, "step": i, "kind": "write"})
                for _, body in sends:
                    result = next((_tool_output(x) for x in msgs[i + 1:i + 3] if x["role"] == "tool"), "")
                    out.append({"agent": me, "step": i, "kind": "send", "text": body.strip(),
                                "delivered": "NOT delivered" not in result})
                    reply = REPLY.search(result)
                    if reply:
                        out.append({"agent": me, "step": i, "kind": "recv", "text": reply.group(2).strip(), "inline": True})
        elif m["role"] == "user":
            r = RECV.match(content)
            if r:
                out.append({"agent": me, "step": i, "kind": "recv", "text": r.group(2).strip()})
            elif "has completed their work and exited" in content:
                out.append({"agent": me, "step": i, "kind": "partner_exit"})
        elif m["role"] == "exit":
            out.append({"agent": me, "step": i, "kind": "exit"})
    return out, len(msgs)


def order(lanes):
    """All events in a causal order: each lane keeps its step order, a receive comes after the send
    it carries, and an undelivered send comes after the partner's first exit."""
    sends = [e for lane in lanes.values() for e in lane if e["kind"] == "send"]
    for name, lane in lanes.items():
        partner = next(l for n, l in lanes.items() if n != name)
        exits = [x for x in partner if x["kind"] == "exit"]
        for e in lane:
            if e["kind"] == "recv":
                e["src"] = next((s for s in sends if s["agent"] != name and key(s["text"]) == key(e["text"])), None)
            elif e["kind"] == "send" and not e["delivered"] and exits:
                e["src"] = exits[0]
    y = {}
    for _ in range(4 * sum(len(l) for l in lanes.values()) + 4):
        changed = False
        for lane in lanes.values():
            prev = -1
            for e in lane:
                v = prev + 1
                src = e.get("src")
                if src is not None and id(src) in y:
                    v = max(v, y[id(src)] + 1)
                if y.get(id(e)) != v:
                    y[id(e)], changed = v, True
                prev = y[id(e)]
        if not changed:
            break
    events = [e for lane in lanes.values() for e in lane]
    return sorted(events, key=lambda e: (y[id(e)], e["agent"]))


def agent_conflict(events):
    """Step at which this agent has stated a claim and received a partner claim on the same file
    (or a reference to "the same file"), with the shared files; (None, set()) if never."""
    own, partner = set(), set()
    own_ref = partner_ref = False
    for e in events:
        if e["kind"] == "send":
            own |= files_in(e["text"])
            own_ref |= bool(IMPLICIT.search(e["text"]))
        elif e["kind"] == "recv":
            partner |= files_in(e["text"])
            partner_ref |= bool(IMPLICIT.search(e["text"]))
        else:
            continue
        shared = own & partner
        if (own or own_ref) and (partner or partner_ref) and (shared or own_ref or partner_ref):
            return e["step"], shared
    return None, set()


def first_conflict_message(sends):
    """1-based index, in the ordered list of sent messages, of the message that completes the first
    conflict point; None if there is none."""
    claims = {"agent1": set(), "agent2": set()}
    refs = {"agent1": False, "agent2": False}
    for n, e in enumerate(sends, 1):
        claims[e["agent"]] |= files_in(e["text"])
        refs[e["agent"]] |= bool(IMPLICIT.search(e["text"]))
        a, b = claims["agent1"], claims["agent2"]
        if (a or refs["agent1"]) and (b or refs["agent2"]) and (a & b or refs["agent1"] or refs["agent2"]):
            return n
    return None


# ---------------------------------------------------------------- runs and states

def gold(root, repo, task, feature):
    path = root / DATASET / repo / f"task{task}" / f"feature{feature}" / "feature.patch"
    files, ranges, cur = set(), defaultdict(list), None
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            files.add(cur)
        m = re.match(r"@@ -(\d+)(?:,(\d+))? ", line)
        if m and cur:
            a, n = int(m.group(1)), int(m.group(2) or 1)
            ranges[cur].append((a, a + max(n, 1)))
    return files, ranges


def title(root, repo, task, feature):
    path = root / DATASET / repo / f"task{task}" / f"feature{feature}" / "feature.md"
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"\**Title\**:\s*(.+)", line.strip())
        if m:
            return m.group(1).strip()
    return f"feature {feature}"


def runs(root, source):
    """(run directory, scored row) for every scored coop run of one source in the config."""
    rows = {}
    for r in csv.DictReader(open(root / source["rows"])):
        if r["setting"] == "coop" and (not source.get("arm") or r.get("arm") == source["arm"]):
            rows[(r["repeat"], r["repo"], r["task_id"], r["features"])] = r
    for conv in sorted((root / source["logs"]).glob("r*/coop/*/*/*/conversation.json")):
        d = conv.parent
        fa, fb = d.name[1:].split("_f")
        row = rows.get((d.parts[-5][1:], d.parts[-3], d.parts[-2], f"{fa} {fb}"))
        if row is not None:
            yield d, row


def state(root, d, row):
    """One run's conflict state, the ordered transcript, and its outcome (kept apart from labels)."""
    repo, task = d.parts[-3], d.parts[-2]
    fa, fb = d.name[1:].split("_f")
    t1, t2 = d / f"agent{fa}_traj.json", d / f"agent{fb}_traj.json"
    conv = json.loads((d / "conversation.json").read_text())
    rec = {"run": str(d.relative_to(root)), "repo": repo, "task": task, "features": f"{fa} {fb}",
           "repeat": d.parts[-5][1:], "titles": [title(root, repo, task, fa), title(root, repo, task, fb)]}
    lanes, lengths = {}, {}
    if t1.is_file() and t2.is_file():
        lanes["agent1"], lengths["agent1"] = trajectory(t1, "agent1")
        lanes["agent2"], lengths["agent2"] = trajectory(t2, "agent2")
    counts = Counter(m["from"] for m in conv)
    complete = bool(lanes) and all(sum(e["kind"] == "send" and e["delivered"] for e in lanes[a]) >= counts.get(a, 0)
                                   for a in ("agent1", "agent2"))
    if complete:
        events = order(lanes)
        sends = [e for e in events if e["kind"] == "send"]
        exits = [e for e in events if e["kind"] == "exit"]
        transcript = [{"from": e["agent"], "text": e["text"], "delivered": e["delivered"]} if e["kind"] == "send"
                      else {"from": e["agent"], "exit": True}
                      for e in events if e["kind"] == "send" or (e["kind"] == "exit" and e is next(x for x in reversed(exits) if x["agent"] == e["agent"]))]
        k = {a: agent_conflict(lanes[a]) for a in lanes}
        rec.update(source="trajectories", step1=k["agent1"][0], step2=k["agent2"][0],
                   length1=lengths["agent1"], length2=lengths["agent2"],
                   writes_before1=any(e["kind"] == "write" and k["agent1"][0] is not None and e["step"] < k["agent1"][0] for e in lanes["agent1"]),
                   writes_before2=any(e["kind"] == "write" and k["agent2"][0] is not None and e["step"] < k["agent2"][0] for e in lanes["agent2"]),
                   undelivered=sum(not e["delivered"] for e in sends))
    else:  # trajectories missing or incomplete: conversation order, grouped by sender
        sends = [{"agent": m["from"], "text": m["message"], "delivered": True} for m in conv]
        transcript = [{"from": m["from"], "text": m["message"], "delivered": True} for m in conv]
        rec.update(source="conversation", step1=None, step2=None, length1=None, length2=None,
                   writes_before1=None, writes_before2=None, undelivered=None)
    n = first_conflict_message(sends)
    claims = {a: set().union(*[files_in(e["text"]) for e in sends if e["agent"] == a] or [set()]) for a in ("agent1", "agent2")}
    upto = sends[:n] if n else sends
    ga, ra = gold(root, repo, task, fa)
    gb, rb = gold(root, repo, task, fb)
    shared_gold = ga & gb
    merged = set(row.get("merged_passed", "").split())
    own = set(row.get("own_passed", "").split())
    rec.update(conflict_point=n is not None, conflict_message=n, messages=len(sends),
               both_spoke=all(any(e["agent"] == a for e in sends) for a in ("agent1", "agent2")),
               shared_files=sorted(claims["agent1"] & claims["agent2"]),
               exclusive=any(EXCLUSIVE.search(e["text"]) for e in upto),
               gold_shared_file=bool(shared_gold),
               gold_shared_lines=any(x0 < y1 and y0 < x1 for f in shared_gold for x0, x1 in ra[f] for y0, y1 in rb[f]),
               merge=row.get("merge_status", ""), both_pass=int(merged >= {fa, fb}), own_both=int(own >= {fa, fb}),
               label=row.get("refined_label", ""), transcript=transcript)
    return rec


def states(config, root, set_name):
    out = []
    for source in config["sets"][set_name]:
        for d, row in runs(root, source):
            out.append(dict(state(root, d, row), set=source["name"]))
    return out


# ---------------------------------------------------------------- labeling

def render(rec):
    names = {"agent1": "Agent 1", "agent2": "Agent 2"}
    lines, n = [], 0
    for m in rec["transcript"]:
        if m.get("exit"):
            lines.append(f"-- {names[m['from']]} submitted its work and exited.")
            continue
        n += 1
        other = "agent2" if m["from"] == "agent1" else "agent1"
        lost = "" if m["delivered"] else " [NOT DELIVERED: the partner had already exited]"
        lines.append(f"#{n} {names[m['from']]} -> {names[other]}{lost}: {m['text']}")
    return "\n".join(lines)


def prompt(codebook, rec):
    where = (f"The detector marks message #{rec['conflict_message']} as the first conflict point "
             f"(files claimed by both: {', '.join(rec['shared_files']) or 'referred to as the same file'})."
             if rec["conflict_point"] else "The detector found no conflict point; confirm or reject.")
    order_note = "" if rec["source"] == "trajectories" else (
        "\nNote: message order across the two agents is not known for this run; messages are grouped by sender.")
    return (f"{codebook}\n\n---\n\nAgent 1's feature: {rec['titles'][0]}\nAgent 2's feature: {rec['titles'][1]}\n"
            f"{where}{order_note}\n\nMessages in the order they happened:\n{render(rec)}\n\nReturn the JSON object only.")


def style(a, c):
    return STYLE[(a, c)]


def parse_label(reply):
    """The labeler's JSON reply, validated, with each agent's style added; raises ValueError."""
    text = reply.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data.get("claims_incompatible"), bool):
        raise ValueError("claims_incompatible must be true or false")
    types = data.get("types") or {}
    if set(types) != set(TYPES) or not all(isinstance(v, bool) for v in types.values()):
        raise ValueError("types must give true/false for each conflict type")
    primary = data.get("primary_type")
    if primary not in (*TYPES, None):
        raise ValueError("primary_type must be a conflict type or null")
    for agent in ("agent1", "agent2"):
        a = data.get(agent) or {}
        if a.get("assertiveness") not in (0, 1, 2) or a.get("cooperativeness") not in (0, 1, 2):
            raise ValueError(f"{agent} scores must be 0, 1 or 2")
        a["style"] = style(a["assertiveness"], a["cooperativeness"])
    for fact, allowed in CHOICES.items():
        if data.get(fact) not in allowed:
            raise ValueError(f"{fact} must be one of {sorted(allowed)}")
    for fact in ("interface_exact", "handoff", "unanswered_question"):
        if not isinstance(data.get(fact), bool):
            raise ValueError(f"{fact} must be true or false")
    return data


def label_path(config, root, rec, repeat=0):
    name = rec["run"].replace("/", "__") + (f"__repeat{repeat}" if repeat else "") + ".json"
    return root / config["output"] / "labels" / rec["set"] / name


def codebook_id(codebook):
    import hashlib
    return hashlib.sha256(codebook.encode()).hexdigest()[:12]


def label_one(config, root, codebook, rec, repeat=0):
    """Label one run; an existing label made with this exact codebook text is kept."""
    from .merge import ask_model
    target = label_path(config, root, rec, repeat)
    if target.is_file() and json.loads(target.read_text()).get("codebook_id") == codebook_id(codebook):
        return
    lab = config["labeler"]
    text = prompt(codebook, rec)
    tries, cost = [], 0.0
    for _ in range(lab.get("attempts", 2)):
        reply, c = ask_model({"model": lab["model"], "model_kwargs": lab.get("model_kwargs", {})}, text)
        cost += c or 0.0
        try:
            label, error = parse_label(reply), None
        except (ValueError, TypeError) as exc:
            label, error = None, str(exc)
        tries.append({"reply": reply, "error": error})
        if label:
            break
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"run": rec["run"], "codebook": config["codebook"], "codebook_id": codebook_id(codebook),
                                  "model": lab["model"],
                                  "label": label, "tries": tries, "cost_usd": cost}, indent=2))


def execute(config, root, set_name, workers=4, limit=None, repeat_sample=False):
    from .s1 import load_env_file
    import os
    load_env_file(root)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env or export it. No request was made.")
    codebook = (root / config["codebook"]).read_text()
    recs = states(config, root, set_name)
    jobs = [(r, 0) for r in recs]
    if repeat_sample:
        rng = random.Random(config["labeler"]["seed"])
        jobs = [(r, 1) for r in rng.sample(recs, min(config["labeler"]["repeat_sample"], len(recs)))]
    if limit:
        jobs = jobs[:limit]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(label_one, config, root, codebook, r, k) for r, k in jobs]:
            f.result()
    return len(jobs)


# ---------------------------------------------------------------- analysis (standard library only)

def kappa(a, b):
    """Cohen's kappa for two lists of nominal labels."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return None
    n = len(pairs)
    po = sum(x == y for x, y in pairs) / n
    ca, cb = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    pe = sum(ca[k] * cb[k] for k in ca) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def load_label(config, root, rec, repeat=0):
    """The run's label if it was made with the current codebook text, else None."""
    path = label_path(config, root, rec, repeat)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    if data.get("codebook_id") != codebook_id((root / config["codebook"]).read_text()):
        return None
    return data.get("label")


def flat(rec, label):
    row = {k: rec[k] for k in ("set", "run", "repo", "task", "features", "repeat", "source", "conflict_point",
                               "conflict_message", "messages", "both_spoke", "exclusive", "step1", "step2", "length1",
                               "length2", "writes_before1", "writes_before2", "undelivered", "gold_shared_file",
                               "gold_shared_lines", "merge", "both_pass", "own_both", "label")}
    row["shared_files"] = " ".join(rec["shared_files"])
    if label:
        row.update(incompatible=label["claims_incompatible"], primary_type=label["primary_type"],
                   **{f"type_{t}": label["types"][t] for t in TYPES},
                   **{f"{a}_{k}": label[a][k] for a in ("agent1", "agent2") for k in ("assertiveness", "cooperativeness", "style")},
                   **{f: label[f] for f in FACTS},
                   style_pair=" + ".join(sorted((label["agent1"]["style"], label["agent2"]["style"]))))
    return row


def summarize(rows):
    out = {}
    for name in sorted({r["set"] for r in rows}):
        R = [r for r in rows if r["set"] == name]
        L = [r for r in R if "incompatible" in r]
        C = [r for r in L if r["conflict_point"]]
        s = {"runs": len(R), "detector_conflict": sum(r["conflict_point"] for r in R),
             "exclusive_claim": sum(r["conflict_point"] and r["exclusive"] for r in R),
             "gold_shared_lines": sum(r["gold_shared_lines"] for r in R), "labeled": len(L),
             "labeled_conflicts": len(C),
             "labeler_incompatible": sum(r["incompatible"] for r in C),
             "labeler_incompatible_when_exclusive": [sum(r["incompatible"] for r in C if r["exclusive"]), sum(r["exclusive"] for r in C)]}
        steps = sorted(v for r in R for v in (r["step1"], r["step2"]) if v is not None)
        if steps:
            s["conflict_step_median"] = steps[len(steps) // 2]
            wb = [v for r in R for v in (r["writes_before1"], r["writes_before2"]) if v is not None]
            s["writes_before_conflict"] = [sum(wb), len(wb)]
        if C:
            s["types_present"] = {t: sum(r[f"type_{t}"] for r in C) for t in TYPES}
            s["primary_type"] = dict(Counter(r["primary_type"] for r in C))
            s["agent_styles"] = dict(Counter(r[f"{a}_style"] for r in C for a in ("agent1", "agent2")))
            s["style_pairs"] = dict(Counter(r["style_pair"] for r in C).most_common())
            s["facts"] = {f: dict(Counter(str(r[f]) for r in C)) for f in FACTS}

            def rate(sel):
                return [sum(r["both_pass"] for r in sel), len(sel)]
            s["both_pass_by"] = {
                "primary_type": {t: rate([r for r in C if r["primary_type"] == t]) for t in TYPES},
                "style_pair": {p: rate([r for r in C if r["style_pair"] == p]) for p, _ in Counter(r["style_pair"] for r in C).most_common()},
                "handoff": {str(v): rate([r for r in C if r["handoff"] == v]) for v in (True, False)},
                "interface_exact": {str(v): rate([r for r in C if r["interface_exact"] == v]) for v in (True, False)},
                "unanswered_question": {str(v): rate([r for r in C if r["unanswered_question"] == v]) for v in (True, False)},
                "agreement": {v: rate([r for r in C if r["agreement"] == v]) for v in sorted(CHOICES["agreement"])}}
        out[name] = s
    return out


def consistency(config, root, recs):
    pairs = [(load_label(config, root, r), load_label(config, root, r, 1)) for r in recs]
    pairs = [(a, b) for a, b in pairs if a and b]
    if not pairs:
        return None
    get = {"incompatible": lambda l: l["claims_incompatible"], "primary_type": lambda l: l["primary_type"],
           **{f"type_{t}": (lambda t: lambda l: l["types"][t])(t) for t in TYPES},
           **{f"{a}_{k}": (lambda a, k: lambda l: l[a][k])(a, k) for a in ("agent1", "agent2") for k in ("assertiveness", "cooperativeness", "style")},
           **{f: (lambda f: lambda l: l[f])(f) for f in FACTS}}
    return {"pairs": len(pairs),
            **{name: {"agree": sum(g(a) == g(b) for a, b in pairs), "kappa": kappa([g(a) for a, _ in pairs], [g(b) for _, b in pairs])}
               for name, g in get.items()}}


COLUMNS_EXCLUDE = ("transcript", "titles")


def report(config, root, set_names):
    output = root / config["output"]
    output.mkdir(parents=True, exist_ok=True)
    rows, recs = [], []
    for name in set_names:
        for rec in states(config, root, name):
            recs.append(rec)
            rows.append(flat(rec, load_label(config, root, rec)))
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with open(output / "conflict_states.csv", "w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    summary = {"study": config["study"], "question": config["question"], "sets": summarize(rows),
               "consistency": consistency(config, root, recs),
               "label_cost_usd": round(sum(json.loads(p.read_text()).get("cost_usd", 0) for p in (output / "labels").rglob("*.json")), 4)
               if (output / "labels").is_dir() else 0}
    (output / "conflict_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def main(argv):
    """argv: config, set, [threads], [--repeat] (second labels for the consistency sample), [--limit N]."""
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    rest = argv[2:]
    limit = int(rest[rest.index("--limit") + 1]) if "--limit" in rest else None
    threads = int(rest[0]) if rest and rest[0].isdigit() else 4
    n = execute(config, root, argv[1], threads, limit=limit, repeat_sample="--repeat" in rest)
    print(n, "labels requested", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
