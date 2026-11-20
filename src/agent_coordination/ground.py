"""Ground bargaining instances in real conflicts: extract the decisions behind each ParallACT contradictory pair.

A contradictory pair gives two developers' tickets that rewrite the same code in incompatible ways and a
reconciliation that satisfies both. The extractor asks a model to name the two to four design decisions the
developers would have to settle, three options per decision ordered from A's need through a middle ground to
B's need, and how much each decision matters to each side. The structures are then built into bargaining
instances by `bargain.assemble`, with the ticket context as an inline scenario skin, so that the same strategy
cells, measures and frontier analysis run on grounded instances as on generated ones. ParallACT's own data is
not copied: the output holds only the extracted structure and the context paragraph the model writes.
"""
import argparse
import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import bargain

PROMPT = """Two developers, A and B, each have a ticket on the same code base. Their changes rewrite the same code in \
incompatible ways, so before either can merge they must agree on one design of the shared code that serves both tickets.

Shared code: {artifact}

How the two changes collide: {explanation}

Ticket A:
{a}

Ticket B:
{b}

A design that reconciles both (for your reference only; do not reveal it as the answer): {reconciliation}

Extract the design decisions the two developers would have to negotiate. Return one JSON object and nothing else:
{{
  "context": "<two or three neutral sentences a developer could read to understand what the shared code does and why both tickets change it; no names, no code>",
  "roles": ["<what developer A is working on, in at most eight words>", "<the same for developer B>"],
  "decisions": [
    {{
      "name": "<the decision, at most five words>",
      "options": ["<option 1>", "<option 2>", "<option 3>"],
      "A_needs": <1, 2 or 3: the option A's ticket requires>,
      "B_needs": <1, 2 or 3: the option B's ticket requires; the same number as A_needs when both tickets want the same thing>,
      "middle": <1, 2 or 3: when A_needs and B_needs differ, the option that serves each ticket partly; when they are the same, the second-best option for both>,
      "importance": {{"A": <1-5>, "B": <1-5>}}
    }}
  ]
}}
Rules: two to four decisions, each a design choice about the shared code that the two tickets settle differently or \
both depend on; three concrete, distinct, short noun-phrase options per decision, each a design the code could \
actually have; at least one decision where A_needs and B_needs differ; importance 5 means the ticket cannot be done \
without that option, 1 means the developer barely cares; distinct decision names."""


def source_files(root):
    return sorted((root / "external" / "ParallACT" / "dataset").glob("**/contradictory/instance_*.json"))


def extract_prompt(data):
    return PROMPT.format(artifact=data["shared_artifact"], explanation=data["natural_language_explanation"],
                         a=data["agentA_problem_statement"].strip(), b=data["agentB_problem_statement"].strip(),
                         reconciliation=data["reconciliation_explanation"])


def loads(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def validate(out):
    """Raises ValueError unless `out` is a usable extraction."""
    if not isinstance(out, dict):
        raise ValueError("not an object")
    if not isinstance(out.get("context"), str) or len(out["context"].split()) < 10:
        raise ValueError("context missing or too short")
    roles = out.get("roles")
    if not (isinstance(roles, list) and len(roles) == 2 and all(isinstance(r, str) and r.strip() for r in roles)):
        raise ValueError("roles must be two non-empty strings")
    decisions = out.get("decisions")
    if not (isinstance(decisions, list) and 2 <= len(decisions) <= 4):
        raise ValueError("two to four decisions are needed")
    names = set()
    for d in decisions:
        name = str(d.get("name", "")).strip()
        if not name or len(name.split()) > 8:
            raise ValueError(f"bad decision name {name!r}")
        if name.lower() in names:
            raise ValueError(f"duplicate decision {name!r}")
        names.add(name.lower())
        opts = d.get("options")
        if not (isinstance(opts, list) and len(opts) == 3 and all(isinstance(o, str) and o.strip() for o in opts)):
            raise ValueError(f"{name}: three non-empty options are needed")
        if len({o.strip().lower() for o in opts}) != 3:
            raise ValueError(f"{name}: options must differ")
        picks = {key: d.get(key) for key in ("A_needs", "B_needs", "middle")}
        if not all(isinstance(v, int) and 1 <= v <= 3 for v in picks.values()):
            raise ValueError(f"{name}: A_needs, B_needs and middle must be 1, 2 or 3")
        if picks["middle"] in (picks["A_needs"], picks["B_needs"]):
            raise ValueError(f"{name}: middle must differ from the options the tickets need")
        imp = d.get("importance")
        if not (isinstance(imp, dict) and all(isinstance(imp.get(s), int) and 1 <= imp[s] <= 5 for s in ("A", "B"))):
            raise ValueError(f"{name}: importance must be integers 1-5 for A and B")
    if not any(contested(d) for d in decisions):
        raise ValueError("at least one decision must be contested")


def contested(d):
    return d["A_needs"] != d["B_needs"]


def canonical(d):
    """A decision's options in the order A's need, the middle ground, the third (B's need when contested)."""
    first, mid = d["A_needs"] - 1, d["middle"] - 1
    third = ({0, 1, 2} - {first, mid}).pop()
    return [d["options"][i].strip() for i in (first, mid, third)]


def normalized(values):
    total = sum(values)
    out = [round(100 * v / total, 1) for v in values]
    out[-1] = round(100 - sum(out[:-1]), 1)
    return out


def short_artifact(text):
    """'src/meta/index.js — Meta.slugTaken validation ...' -> 'src/meta/index.js'."""
    return re.split(r"\s+[—-]\s+|:\s", text.strip(), maxsplit=1)[0].strip() or text.strip()


def to_spec(data, out):
    """The structure `bargain.assemble` needs, plus an inline skin with the ticket context."""
    decisions = out["decisions"]
    # The options are shown in a shuffled order so that no letter is always one side's need; the rankings list
    # displayed option indices from each side's top to bottom: A's need, the middle ground, B's need for A.
    rng = random.Random(data["instance_id"])
    perms = [rng.sample(range(3), 3) for _ in decisions]  # displayed position j shows canonical option perms[i][j]
    pos = [[perm.index(o) for o in range(3)] for perm in perms]  # displayed index of canonical option o
    rankings = {"p1": [list(p) for p in pos], "p2": [list(reversed(p)) if contested(d) else list(p) for p, d in zip(pos, decisions)]}
    importance = {"p1": normalized([d["importance"]["A"] for d in decisions]),
                  "p2": normalized([d["importance"]["B"] for d in decisions])}
    skin = {"name": "grounded", "parties": ["Developer A", "Developer B"], "party_kind": "developers",
            "other": "the other developer", "artifact": f"the design of {short_artifact(data['shared_artifact'])}",
            "thing": "design", "intro": out["context"].strip() + " You are working on {role}; the other developer on {other_role}.",
            "roles": [r.strip() for r in out["roles"]],
            "issues": [{"name": d["name"].strip(), "options": [canonical(d)[o] for o in perm]} for d, perm in zip(decisions, perms)]}
    return {"source": data["instance_id"], "repo": data["repo"], "artifact": data["shared_artifact"],
            "importance": importance, "rankings": rankings, "opposed": [contested(d) for d in decisions],
            "raw_importance": {"A": [d["importance"]["A"] for d in decisions], "B": [d["importance"]["B"] for d in decisions]},
            "skin": skin}


def extract(config, data):
    """One extraction with a corrective retry: (spec or None, cost, error)."""
    messages = [{"role": "user", "content": extract_prompt(data)}]
    # Extraction gets a little reasoning (the API refuses temperature together with it) and room for the JSON.
    config = {**config, "max_tokens": 4000, "model_kwargs": {"extra_body": {"reasoning_effort": "low"}}}
    cost, error = 0.0, None
    for attempt in range(2):
        text, c, _ = bargain.call(config, messages)
        cost += c
        try:
            out = loads(text)
            validate(out)
            return to_spec(data, out), cost, None
        except (ValueError, json.JSONDecodeError) as exc:
            error = str(exc)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That could not be used ({exc}). Return the corrected JSON object only."}]
    return None, cost, error


def run(config, root, out_path, limit=None, workers=4):
    files = source_files(root)[:limit]
    existing = {s["source"]: s for s in json.loads(out_path.read_text())} if out_path.exists() else {}
    todo = [json.loads(f.read_text()) for f in files]
    todo = [d for d in todo if d["instance_id"] not in existing]
    print(f"{len(files)} contradictory instances; {len(existing)} extracted already; {len(todo)} to do", flush=True)
    total, failed = 0.0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for data, (spec, cost, error) in zip(todo, pool.map(lambda d: extract(config, d), todo)):
            total += cost
            if spec is None:
                failed.append((data["instance_id"], error))
                print(f"FAILED {data['instance_id']}: {error}", flush=True)
            else:
                existing[data["instance_id"]] = spec
                print(f"{data['instance_id']}: {len(spec['opposed'])} decisions, {sum(spec['opposed'])} contested", flush=True)
    order = [json.loads(f.read_text())["instance_id"] for f in files]
    specs = [existing[i] for i in order if i in existing] + [s for i, s in existing.items() if i not in order]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(specs, indent=1, ensure_ascii=False) + "\n")
    print(f"done; {len(specs)} specs in {out_path}; ${total:.3f} spent; {len(failed)} failed", flush=True)
    return specs


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="A bargain config: its model settings and instances.path are used")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    config = bargain.load_config(args.config)
    run(config, Path.cwd(), Path(config["instances"]["path"]), args.limit, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
