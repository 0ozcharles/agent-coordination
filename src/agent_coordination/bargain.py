"""Parametric multi-issue bargaining between two LLM agents.

Two agents must agree on one shared design with K decisions, each with M options. Each agent
has private importance weights over the decisions and a ranking of the options within each,
so every design is worth an exact number of points to each agent (100 is its ideal). The
generator sets the structure of the conflict (opposition: on how many decisions the rankings
are reversed; asymmetry: how differently the two weigh the decisions), the outside option,
whether weights are visible and the number of moves. A cell assigns each agent a strategy in
one of two operationalizations: a prompted style sentence, or behavioural rules stated on the
agent's own point scale, whose compliance is checked from the moves. `execute` and `label` need
the OpenAI key; `report` needs neither. Design: docs/RESEARCH_PLAN.md, "Strategy payoff study".
"""
import argparse
import csv
import itertools
import json
import random
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .conflict_states import STYLE, codebook_id
from .negotiation import mean, sign_test

STYLES = ("competing", "collaborating", "compromising", "accommodating", "avoiding")
AGENTS = ("p1", "p2")
NAMES = {"p1": "Developer 1", "p2": "Developer 2"}
ASKS = re.compile(r"\b(matter|important|priorit|care about|what do you need|which .* need|prefer)", re.I)
DISCLOSES = re.compile(r"\b\d{1,3}\s*(points?|pts)\b|\bworth\s+\d", re.I)
ROW_COLUMNS = ("cell", "instance", "p1", "p2", "op", "first_mover", "error", "agreement", "ended_by", "moves",
               "payoff_p1", "payoff_p2", "joint", "max_joint", "joint_gap", "on_frontier", "nash_p1", "nash_p2",
               "middle_share", "p1_first", "p1_last", "p1_concession", "p1_proposals", "p1_asks", "p1_discloses",
               "p1_logroll", "p1_trade", "p1_complies", "p2_first", "p2_last", "p2_concession", "p2_proposals", "p2_asks",
               "p2_discloses", "p2_logroll", "p2_trade", "p2_complies", "p1_style", "p2_style", "p1_assertiveness",
               "p1_cooperativeness", "invalid_replies", "cost_usd")


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "model", "environment", "instances", "strategies", "cells", "control", "output"):
        if key not in config:
            raise ValueError(f"config lacks {key}")
    return config


# ---------------------------------------------------------------- instances

def composition(rng, k, total=100, floor=5):
    """`k` integers of at least `floor` that sum to `total`, drawn uniformly over the simplex."""
    cuts = sorted(rng.random() for _ in range(k - 1))
    parts = [b - a for a, b in zip([0.0] + cuts, cuts + [1.0])]
    free = total - floor * k
    values = [floor + p * free for p in parts]
    ints = [int(v) for v in values]
    for i in sorted(range(k), key=lambda i: values[i] - int(values[i]), reverse=True)[: total - sum(ints)]:
        ints[i] += 1
    return ints


def normalize(values, total=100):
    scale = total / sum(values)
    scaled = [v * scale for v in values]
    ints = [int(v) for v in scaled]
    for i in sorted(range(len(values)), key=lambda i: scaled[i] - int(scaled[i]), reverse=True)[: total - sum(ints)]:
        ints[i] += 1
    return ints


def reversed_ranks(values):
    """The same multiset of values with the ranking reversed: the largest goes where the smallest was."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    descending = sorted(values, reverse=True)
    out = [0] * len(values)
    for rank, i in enumerate(order):
        out[i] = descending[rank]
    return out


def issue_names(k):
    return [f"Decision {i + 1}" for i in range(k)]


def option_names(m):
    return [f"Option {chr(65 + j)}" for j in range(m)]


def make_instance(env, seed, index, first_mover):
    rng = random.Random(seed)
    k, m = env["issues"], env["options"]
    # Both importance vectors are mixtures of one latent vector and its rank reversal, mirror images of each
    # other, so that neither seat has the more concentrated profile: at a = 0 they are equal, at a = 1 reversed.
    latent, b = composition(rng, k), (1 - env["asymmetry"]) / 2
    mirror = reversed_ranks(latent)
    imp1 = normalize([(1 - b) * x + b * y for x, y in zip(latent, mirror)])
    imp2 = normalize([b * x + (1 - b) * y for x, y in zip(latent, mirror)])
    rank1 = [rng.sample(range(m), m) for _ in range(k)]  # option indices from top to bottom
    opposed_set = set(rng.sample(range(k), round(env["opposition"] * k)))
    rank2 = [list(reversed(r)) if i in opposed_set else list(r) for i, r in enumerate(rank1)]
    weights = {}
    for agent, imp, ranks in (("p1", imp1, rank1), ("p2", imp2, rank2)):
        weights[agent] = [[round(imp[i] * (1 - r.index(o) / (m - 1)), 1) for o in range(m)] for i, r in enumerate(ranks)]
    outside = env.get("outside", [20, 20])
    inst = {"id": index, "seed": seed, "k": k, "m": m, "opposition": env["opposition"], "asymmetry": env["asymmetry"],
            "importance": {"p1": imp1, "p2": imp2}, "weights": weights, "opposed": [i in opposed_set for i in range(k)],
            "outside": {"p1": outside[0], "p2": outside[1]}, "first_mover": first_mover,
            "visible_weights": bool(env.get("visible_weights", False))}
    inst["frontier"] = frontier(inst)
    return inst


def instances(config):
    env, spec = config["environment"], config["instances"]
    rng = random.Random(spec["seed"])
    out = []
    for i in range(spec["n"]):
        first = ("p1", "p2")[i % 2] if spec.get("first_mover", "alternate") == "alternate" else spec["first_mover"]
        out.append(make_instance(env, rng.randrange(2 ** 31), i, first))
    return out


def value(inst, agent, design):
    return round(sum(inst["weights"][agent][i][o] for i, o in enumerate(design)), 1)


def frontier(inst):
    points = [(value(inst, "p1", d), value(inst, "p2", d), list(d)) for d in itertools.product(range(inst["m"]), repeat=inst["k"])]
    pareto = [p for p in points if not any(q[0] >= p[0] and q[1] >= p[1] and (q[0] > p[0] or q[1] > p[1]) for q in points)]
    d1, d2 = inst["outside"]["p1"], inst["outside"]["p2"]
    feasible = [p for p in points if p[0] >= d1 and p[1] >= d2]
    nash = max(feasible, key=lambda p: ((p[0] - d1) * (p[1] - d2), p[0] + p[1]), default=None)
    egal = max(points, key=lambda p: (min(p[0], p[1]), p[0] + p[1]))
    # the issue-by-issue compromise: the shared top option where rankings agree, the middle option where they clash
    t1 = [max(range(inst["m"]), key=lambda o: inst["weights"]["p1"][i][o]) for i in range(inst["k"])]
    compromise = [1 if (opp and inst["m"] == 3) else t1[i] for i, opp in enumerate(inst["opposed"])]
    compromise_joint = value(inst, "p1", compromise) + value(inst, "p2", compromise)
    max_joint = max(p[0] + p[1] for p in points)
    return {"max_joint": max_joint, "pareto": sorted({(p[0], p[1]) for p in pareto}),
            "nash": {"p1": nash[0], "p2": nash[1], "design": nash[2]} if nash else None,
            "egalitarian": {"p1": egal[0], "p2": egal[1], "design": egal[2]},
            "compromise": {"p1": value(inst, "p1", compromise), "p2": value(inst, "p2", compromise), "design": compromise},
            "integrative_gain": round(max_joint - compromise_joint, 1)}


# ---------------------------------------------------------------- prompts

def guidance(config, strategy, op, outside=None):
    """The guidance text for a strategy: None for the default, a prefixed sentence or rule otherwise.

    Rule texts may use {outside} and {outside10} for the agent's outside option and that plus 10."""
    if strategy == "default":
        return None
    texts = config["strategies"]
    text = texts["shared"].get(strategy) if strategy in texts.get("shared", {}) else texts[op][strategy]
    if outside is not None:
        text = text.replace("{outside10}", f"{outside + 10:g}").replace("{outside}", f"{outside:g}")
    return config.get("instruction_prefix", "") + text


def scores_table(inst, agent):
    names, opts = issue_names(inst["k"]), option_names(inst["m"])
    return "\n".join(f"{names[i]}: " + ", ".join(f"{opts[o]} = {inst['weights'][agent][i][o]:g}" for o in range(inst["m"]))
                     for i in range(inst["k"]))


def design_text(inst, design):
    names, opts = issue_names(inst["k"]), option_names(inst["m"])
    return ", ".join(f"{names[i]}: {opts[o]}" for i, o in enumerate(design))


def system_prompt(config, inst, agent, strategy, op):
    other = "p2" if agent == "p1" else "p1"
    T = config["environment"]["moves"]
    partner_scores = (f"The other developer's scores are:\n{scores_table(inst, other)}\n" if inst["visible_weights"]
                      else "The other developer has their own private scores, which you do not see.\n")
    example = json.dumps({n: option_names(inst["m"])[0] for n in issue_names(inst["k"])})
    text = (f"You are {NAMES[agent]}, one of two developers who must agree on a shared design with {inst['k']} decisions. "
            f"For each decision exactly one option will be chosen. Your private scores for each option are below; "
            f"a design is worth the sum of your scores for its chosen options, at most 100 points.\n{partner_scores}"
            f"If you both accept one complete design, each of you gets your own value of it. If no design is accepted "
            f"within {T} moves in total, or either of you walks away, you each get your outside option: yours is "
            f"{inst['outside'][agent]:g} points. Your goal is to get as many points as you can.\n\n"
            f"Your scores:\n{scores_table(inst, agent)}\n\n"
            f"Moves alternate. On your move, reply with one JSON object and nothing else, in one of these forms:\n"
            f'{{"message": "<what you say to the other developer>", "action": "propose", "proposal": {example}}}\n'
            f'{{"message": "<...>", "action": "accept"}}   (accepts the other developer\'s latest proposal as stated)\n'
            f'{{"message": "<...>", "action": "walk"}}   (ends the negotiation; both get their outside options)\n'
            f"A proposal names one option for every decision. You may say anything in the message, including your "
            f"scores, or nothing.")
    note = config["environment"].get("protocol_note")
    if note:
        text += " " + note.format(T=T)
    extra = guidance(config, strategy, op, inst["outside"][agent])
    return text + (f"\n\n{extra}" if extra else "")


def describe(inst, move, agent, n, T):
    """What `agent` is told about the other developer's last move before moving."""
    if move is None:
        return f"Move 1 of {T}. You move first."
    said = f'The other developer said: "{move["message"]}"' if move["message"] else "The other developer sent no message"
    if move["action"] == "propose":
        did = f" and proposed: {design_text(inst, move['design'])} (worth {move['values'][agent]:g} points to you)."
    elif move["action"] == "pass":
        did = " and made no valid move."
    else:
        did = "."
    return f"{said}{did}\nMove {n} of {T}. Your move."


# ---------------------------------------------------------------- model calls and parsing

def call(config, messages):
    import litellm
    last = None
    for _ in range(3):
        try:
            response = litellm.completion(model=f"openai/{config['model']}", messages=messages,
                                          max_completion_tokens=config.get("max_tokens", 700), drop_params=True,
                                          timeout=180, **config.get("model_kwargs", {}))
            try:
                cost = litellm.completion_cost(completion_response=response)
            except Exception:
                cost = None
            return response.choices[0].message.content or "", cost or 0.0
        except Exception as exc:  # transient API errors
            last = exc
    raise RuntimeError(f"model call failed: {last}")


def parse_reply(text, inst):
    """The move in a reply: action, message and, for a proposal, the design as option indices; raises ValueError."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}")
    action = str(data.get("action", "")).strip().lower()
    if action not in ("propose", "accept", "walk"):
        raise ValueError("action must be propose, accept or walk")
    message = str(data.get("message") or "").strip()[:2000]
    design = None
    if action == "propose":
        proposal = data.get("proposal")
        if not isinstance(proposal, dict):
            raise ValueError("a proposal must map every decision to an option")
        names, opts = issue_names(inst["k"]), option_names(inst["m"])
        lookup = {n.lower(): i for i, n in enumerate(names)}
        lookup.update({str(i + 1): i for i in range(inst["k"])})
        chosen = {}
        for key, val in proposal.items():
            i = lookup.get(str(key).strip().lower())
            if i is None:
                raise ValueError(f"unknown decision {key!r}")
            v = str(val).strip().lower().replace("option", "").strip()
            letters = [o.split()[-1].lower() for o in opts]
            if v not in letters:
                raise ValueError(f"unknown option {val!r} for {names[i]}")
            chosen[i] = letters.index(v)
        if len(chosen) != inst["k"]:
            raise ValueError("the proposal must name an option for every decision")
        design = [chosen[i] for i in range(inst["k"])]
    return {"action": action, "message": message, "design": design}


# ---------------------------------------------------------------- playing

def play(config, inst, cell):
    T = config["environment"]["moves"]
    strategies = {"p1": cell["p1"], "p2": cell["p2"]}
    ops = {"p1": cell.get("op", "rules"), "p2": cell.get("p2_op", cell.get("op", "rules"))}
    conv = {a: [{"role": "system", "content": system_prompt(config, inst, a, strategies[a], ops[a])}] for a in AGENTS}
    mover = inst["first_mover"]
    standing, last, moves, agreement, ended_by, cost, invalid = None, None, [], None, "moves_exhausted", 0.0, 0
    for n in range(1, T + 1):
        other = "p2" if mover == "p1" else "p1"
        conv[mover].append({"role": "user", "content": describe(inst, last, mover, n, T)})
        move = None
        for attempt in range(2):
            reply, c = call(config, conv[mover])
            cost += c
            conv[mover].append({"role": "assistant", "content": reply})
            try:
                parsed = parse_reply(reply, inst)
                if parsed["action"] == "accept" and not (standing and standing["by"] == other):
                    raise ValueError("there is no proposal from the other developer to accept")
                move = {**parsed, "raw": reply}
                break
            except ValueError as exc:
                invalid += 1
                if attempt == 0:
                    conv[mover].append({"role": "user", "content": f"Your reply could not be used ({exc}). "
                                        "Reply again with exactly one JSON object in one of the given forms."})
        if move is None:
            move = {"action": "pass", "message": "", "design": None, "raw": reply}
        move.update({"n": n, "by": mover})
        if move["action"] == "propose":
            move["values"] = {a: value(inst, a, move["design"]) for a in AGENTS}
            standing = {"by": mover, "design": move["design"], "values": move["values"]}
        moves.append(move)
        if move["action"] == "accept":
            agreement, ended_by = standing, f"{mover}_accepted"
            break
        if move["action"] == "walk":
            ended_by = f"{mover}_walked"
            break
        last, mover = move, other
    return {"cell": cell["name"], "instance": inst, "strategies": strategies, "ops": ops, "moves": moves,
            "agreement": agreement, "ended_by": ended_by, "invalid_replies": invalid, "cost_usd": cost, "error": None,
            **outcome(inst, moves, agreement), "metrics": {a: agent_metrics(inst, moves, agreement, a) for a in AGENTS},
            "compliance": {a: compliance(strategies[a], inst, moves, agreement, a) for a in AGENTS}}


def outcome(inst, moves, agreement):
    if agreement:
        u1, u2 = agreement["values"]["p1"], agreement["values"]["p2"]
    else:
        u1, u2 = inst["outside"]["p1"], inst["outside"]["p2"]
    f = inst["frontier"]
    return {"payoff": {"p1": u1, "p2": u2}, "joint": round(u1 + u2, 1), "max_joint": f["max_joint"],
            "joint_gap": round(f["max_joint"] - (u1 + u2), 1),
            "on_frontier": bool(agreement) and [u1, u2] in [list(p) for p in f["pareto"]],
            "nash": f["nash"]}


def tops(inst, agent):
    return [max(range(inst["m"]), key=lambda o: inst["weights"][agent][i][o]) for i in range(inst["k"])]


def is_trade(inst, design):
    """A design that gives each side its top option on at least one contested decision."""
    t1, t2 = tops(inst, "p1"), tops(inst, "p2")
    contested = [i for i, opp in enumerate(inst["opposed"]) if opp]
    return (any(design[i] == t1[i] for i in contested) and any(design[i] == t2[i] for i in contested)) if contested else False


def middle_share(inst, design):
    """The share of contested decisions set to the middle option (defined for three options)."""
    contested = [i for i, opp in enumerate(inst["opposed"]) if opp]
    if inst["m"] != 3 or not contested or design is None:
        return None
    return round(sum(1 for i in contested if design[i] == 1) / len(contested), 2)


def agent_metrics(inst, moves, agreement, agent):
    mine = [m for m in moves if m["by"] == agent]
    proposals = [m for m in mine if m["action"] == "propose"]
    own = [m["values"][agent] for m in proposals]
    steps = [round(a - b, 1) for a, b in zip(own, own[1:])]
    changed = [sum(1 for x, y in zip(a["design"], b["design"]) if x != y) for a, b in zip(proposals, proposals[1:])]
    text = " ".join(m["message"] for m in mine)
    first_msg = mine[0]["message"] if mine else ""
    return {"proposals": own, "first": own[0] if own else None, "last": own[-1] if own else None,
            "concession": round(own[0] - own[-1], 1) if len(own) >= 2 else (0.0 if own else None),
            "max_step": max(steps, default=None), "max_changed": max(changed, default=None),
            "monotone": all(s >= 0 for s in steps), "messages": len([m for m in mine if m["message"]]),
            "chars": len(text), "asks": bool(ASKS.search(first_msg)), "asks_any": bool(ASKS.search(text)),
            "discloses": bool(DISCLOSES.search(text)),
            "logroll": any(sum(m["values"].values()) >= 0.9 * inst["frontier"]["max_joint"] for m in proposals),
            "trade": any(is_trade(inst, m["design"]) for m in proposals),
            "middle_share": middle_share(inst, agreement["design"]) if agreement else None,
            "accepted_value": agreement["values"][agent] if agreement and moves[-1]["by"] == agent and moves[-1]["action"] == "accept" else None,
            "walked": any(m["action"] == "walk" for m in mine)}


def compliance(strategy, inst, moves, agreement, agent):
    """Whether the agent's moves satisfy the rules of its strategy (checked for every operationalization)."""
    m = agent_metrics(inst, moves, agreement, agent)
    own, other = m["proposals"], "p2" if agent == "p1" else "p1"
    accepted = m["accepted_value"]
    partner_offers = [(x["n"], x["values"][agent]) for x in moves if x["by"] == other and x["action"] == "propose"]
    checks = {}
    if strategy == "competing":
        checks = {"opens_high": (own[0] >= 90) if own else None,
                  "one_decision_per_step": m["max_changed"] is None or m["max_changed"] <= 1,
                  "accepts_60": accepted is None or accepted >= 60}
    elif strategy == "collaborating":
        checks = {"asks_first": m["asks"], "accepts_60": accepted is None or accepted >= 60,
                  "proposes_trade": m["trade"] if own else None}
    elif strategy == "compromising":
        checks = {"accepts_50": accepted is None or accepted >= 50,
                  "steps_of_two_decisions": m["max_changed"] is None or m["max_changed"] <= 2,
                  "monotone": m["monotone"]}
    elif strategy == "accommodating":
        floor = inst["outside"][agent] + 10
        first_ok = next((n for n, v in partner_offers if v >= floor), None)
        accepted_next = (agreement is not None and moves[-1]["by"] == agent and moves[-1]["action"] == "accept"
                         and first_ok is not None and moves[-1]["n"] == first_ok + 1)
        checks = {"asks_first": m["asks"], "accepts_first_above_floor": accepted_next if first_ok is not None else None}
    elif strategy == "avoiding":
        mine = [x for x in moves if x["by"] == agent]
        ends_quickly = len(mine) <= 2 or mine[1]["action"] in ("accept", "walk")
        checks = {"one_proposal": len(own) <= 1, "ends_by_second_move": ends_quickly}
    else:
        return {"overall": None}
    known = [v for v in checks.values() if v is not None]
    return {**checks, "overall": all(known) if known else None}


# ---------------------------------------------------------------- jobs

def game_path(config, root, cell, inst):
    return root / config["output"] / "games" / cell["name"] / f"g{inst['id']:02d}.json"


def records(config, root):
    out = []
    for cell in config["cells"]:
        for inst in instances(config):
            path = game_path(config, root, cell, inst)
            if path.is_file():
                out.append(json.loads(path.read_text()))
    return out


def jobs(config, root, cells=None):
    out = []
    for cell in config["cells"]:
        if cells and cell["name"] not in cells:
            continue
        for inst in instances(config):
            path = game_path(config, root, cell, inst)
            if path.is_file() and json.loads(path.read_text()).get("error") is None:
                continue
            out.append((cell, inst))
    return out


def spent_so_far(config, root):
    return sum((r.get("cost_usd") or 0.0) + (r.get("label") or {}).get("cost_usd", 0.0) for r in records(config, root))


def execute(config, root, workers=4, limit=None, cells=None):
    todo = jobs(config, root, cells)[:limit]
    budget, spent, lock = config.get("budget_usd", 5.0), spent_so_far(config, root), threading.Lock()
    print(f"{len(todo)} games to play; ${spent:.2f} spent so far, budget ${budget:.2f}", flush=True)

    def one(job):
        nonlocal spent
        cell, inst = job
        with lock:
            if spent >= budget:
                return "skipped: budget"
        path = game_path(config, root, cell, inst)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            record = play(config, inst, cell)
        except Exception:
            record = {"cell": cell["name"], "instance": inst, "error": traceback.format_exc(), "cost_usd": 0.0}
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
        with lock:
            spent += record.get("cost_usd") or 0.0
        if record.get("error"):
            return f"{cell['name']} g{inst['id']:02d}: ERROR"
        return (f"{cell['name']} g{inst['id']:02d}: {record['ended_by']} p1={record['payoff']['p1']:g} "
                f"p2={record['payoff']['p2']:g} gap={record['joint_gap']:g} moves={len(record['moves'])} ${record['cost_usd']:.4f}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)
    print(f"done; ${spent:.2f} spent in total", flush=True)


# ---------------------------------------------------------------- labeling

def transcript(record):
    inst = record["instance"]
    lines = []
    for m in record["moves"]:
        who = f"Agent {1 if m['by'] == 'p1' else 2}"
        did = {"propose": f"proposes {design_text(inst, m['design'])}" if m["design"] else "proposes",
               "accept": "ACCEPTS the last proposal", "walk": "WALKS AWAY", "pass": "makes no valid move"}[m["action"]]
        lines.append(f"#{m['n']} {who} {did}: {m['message'] or '(no message)'}")
    return "\n".join(lines)


def label_prompt(codebook, record):
    inst = record["instance"]
    return (f"{codebook}\n\n---\n\nTwo developers negotiate one shared design with {inst['k']} decisions "
            f"({', '.join(issue_names(inst['k']))}), each with options {', '.join(option_names(inst['m']))}. "
            f"Each has private scores you do not see.\n\nMoves in the order they happened:\n{transcript(record)}\n\n"
            f"Return the JSON object only.")


def parse_label(reply):
    text = reply.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(text[start:end + 1])
    for agent in ("agent1", "agent2"):
        a = data.get(agent) or {}
        if a.get("assertiveness") not in (0, 1, 2) or a.get("cooperativeness") not in (0, 1, 2):
            raise ValueError(f"{agent} scores must be 0, 1 or 2")
        for fact in ("asks_needs", "discloses_priorities"):
            if not isinstance(a.get(fact), bool):
                raise ValueError(f"{agent}.{fact} must be true or false")
        a["style"] = STYLE[(a["assertiveness"], a["cooperativeness"])]
    if data.get("first_concession") not in ("agent1", "agent2", "none"):
        raise ValueError("first_concession must be agent1, agent2 or none")
    return data


def label_one(config, root, codebook, record, path):
    from .merge import ask_model
    lab = config["labeler"]
    text = label_prompt(codebook, record)
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
    record["label"] = {"codebook_id": codebook_id(codebook), "model": lab["model"], "label": label, "tries": tries, "cost_usd": cost}
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    return label


def label(config, root, workers=4, limit=None):
    codebook = (root / config["labeler"]["codebook"]).read_text()
    todo = []
    for cell in config["cells"]:
        for inst in instances(config):
            path = game_path(config, root, cell, inst)
            if not path.is_file():
                continue
            record = json.loads(path.read_text())
            done = (record.get("label") or {}).get("codebook_id") == codebook_id(codebook) and record["label"].get("label")
            if record.get("error") is None and not done:
                todo.append((record, path))
    todo = todo[:limit]
    print(f"{len(todo)} games to label", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda rp: label_one(config, root, codebook, *rp), todo))
    print(f"labeled {sum(1 for r in results if r)} of {len(todo)}", flush=True)


# ---------------------------------------------------------------- analysis

def rows(config, root):
    out = []
    for r in records(config, root):
        inst = r["instance"]
        cell = next(c for c in config["cells"] if c["name"] == r["cell"])
        row = {"cell": r["cell"], "instance": inst["id"], "p1": cell["p1"], "p2": cell["p2"], "op": cell.get("op", "rules"),
               "first_mover": inst["first_mover"], "error": bool(r.get("error")), "cost_usd": r.get("cost_usd", 0.0)}
        if not r.get("error"):
            lab = (r.get("label") or {}).get("label") or {}
            row.update({"agreement": bool(r["agreement"]), "ended_by": r["ended_by"], "moves": len(r["moves"]),
                        "payoff_p1": r["payoff"]["p1"], "payoff_p2": r["payoff"]["p2"], "joint": r["joint"],
                        "max_joint": r["max_joint"], "joint_gap": r["joint_gap"], "on_frontier": r["on_frontier"],
                        "nash_p1": (r["nash"] or {}).get("p1"), "nash_p2": (r["nash"] or {}).get("p2"),
                        "invalid_replies": r["invalid_replies"],
                        "p1_style": lab.get("agent1", {}).get("style"), "p2_style": lab.get("agent2", {}).get("style"),
                        "p1_assertiveness": lab.get("agent1", {}).get("assertiveness"),
                        "p1_cooperativeness": lab.get("agent1", {}).get("cooperativeness")})
            row["middle_share"] = r["metrics"]["p1"].get("middle_share")
            for a in AGENTS:
                m, c = r["metrics"][a], r["compliance"][a]
                row.update({f"{a}_first": m["first"], f"{a}_last": m["last"], f"{a}_concession": m["concession"],
                            f"{a}_proposals": len(m["proposals"]), f"{a}_asks": m["asks"], f"{a}_discloses": m["discloses"],
                            f"{a}_logroll": m["logroll"], f"{a}_trade": m.get("trade"), f"{a}_complies": c.get("overall")})
        out.append(row)
    return out


def summarize(config, table):
    control = config["control"]
    by = {c["name"]: {r["instance"]: r for r in table if r["cell"] == c["name"] and not r["error"]} for c in config["cells"]}
    out = {}
    for cell in config["cells"]:
        name = cell["name"]
        g = list(by[name].values())
        styles = {s: sum(1 for r in g if r.get("p1_style") == s) for s in STYLES}
        entry = {"p1": cell["p1"], "p2": cell["p2"], "op": cell.get("op", "rules"), "games": len(g),
                 "errors": sum(1 for r in table if r["cell"] == name and r["error"]),
                 "agreements": sum(1 for r in g if r.get("agreement")), "payoff_p1": mean([r["payoff_p1"] for r in g]),
                 "payoff_p2": mean([r["payoff_p2"] for r in g]), "joint": mean([r["joint"] for r in g]),
                 "joint_gap": mean([r["joint_gap"] for r in g]), "on_frontier": sum(1 for r in g if r.get("on_frontier")),
                 "moves": mean([r["moves"] for r in g]), "p1_first": mean([r["p1_first"] for r in g]),
                 "p1_concession": mean([r["p1_concession"] for r in g]),
                 "p1_complies": sum(1 for r in g if r.get("p1_complies")), "p1_compliance_known": sum(1 for r in g if r.get("p1_complies") is not None),
                 "p1_asks": sum(1 for r in g if r.get("p1_asks")), "p1_discloses": sum(1 for r in g if r.get("p1_discloses")),
                 "p1_logroll": sum(1 for r in g if r.get("p1_logroll")), "p1_trade": sum(1 for r in g if r.get("p1_trade")),
                 "middle_share": mean([r["middle_share"] for r in g if r.get("middle_share") is not None]),
                 "p1_walked": sum(1 for r in g if r.get("ended_by") == "p1_walked"),
                 "invalid_replies": sum(r.get("invalid_replies", 0) for r in g), "p1_style": styles,
                 "labeled": sum(1 for r in g if r.get("p1_style")), "cost_usd": round(sum(r["cost_usd"] for r in g), 3)}
        if cell["p1"] in STYLES:
            entry["p1_in_target_style"] = styles[cell["p1"]]
        if name != control and control in by:
            pairs = [(by[name][i], by[control][i]) for i in by[name] if i in by[control]]
            vs = {"pairs": len(pairs)}
            for key in ("payoff_p1", "payoff_p2", "joint", "moves", "p1_concession"):
                vs[key] = sign_test([(a[key], c[key]) for a, c in pairs if a.get(key) is not None and c.get(key) is not None])
            if cell["p1"] in STYLES:
                vs["p1_in_target_style"] = {"arm": styles[cell["p1"]],
                                            "control": sum(1 for r in by[control].values() if r.get("p1_style") == cell["p1"])}
            entry["vs_control"] = vs
        out[name] = entry
    return out


def matrix(table, strategies=("neutral", *STYLES)):
    """Mean own payoff by (own strategy, partner strategy), pooling both seats; None where no games."""
    cells = {}
    for r in table:
        if r["error"]:
            continue
        cells.setdefault((r["p1"], r["p2"]), []).append(r["payoff_p1"])
        cells.setdefault((r["p2"], r["p1"]), []).append(r["payoff_p2"])
    return {s: {t: mean(cells.get((s, t), [])) for t in strategies} for s in strategies}


def best_responses(M):
    out = {}
    for partner in M:
        column = {s: M[s][partner] for s in M if M[s][partner] is not None}
        out[partner] = max(column, key=column.get) if column else None
    return out


def replicator(M, steps=2000, rate=0.01):
    """Replicator dynamics on the symmetric empirical game; the final mixed population."""
    names = [s for s in M if all(M[s][t] is not None for t in M)]
    if len(names) < 2:
        return {}
    x = {s: 1.0 / len(names) for s in names}
    for _ in range(steps):
        fitness = {s: sum(M[s][t] * x[t] for t in names) for s in names}
        avg = sum(fitness[s] * x[s] for s in names)
        x = {s: max(x[s] + rate * x[s] * (fitness[s] - avg), 0.0) for s in names}
        total = sum(x.values()) or 1.0
        x = {s: v / total for s, v in x.items()}
    return {s: round(v, 3) for s, v in x.items()}


def chart(config, summary):
    """An SVG: per cell, the two payoffs as bars, with agreement and compliance written beside them."""
    names = [c["name"] for c in config["cells"]]
    W, rowh, x0, w = 900, 26, 190, 420
    H = 60 + rowh * len(names) + 40
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<text x="{x0}" y="24" font-size="14" font-weight="bold">Mean payoff in points: Developer 1 (instructed, dark) and Developer 2</text>',
         f'<text x="{x0 + w + 14}" y="24">agree</text><text x="{x0 + w + 64}" y="24">comply</text><text x="{x0 + w + 124}" y="24">gap</text>']
    sx = lambda v: x0 + v / 100 * w
    for v in (0, 25, 50, 75, 100):
        s.append(f'<line x1="{sx(v):.1f}" y1="36" x2="{sx(v):.1f}" y2="{40 + rowh * len(names)}" stroke="#eee"/>'
                 f'<text x="{sx(v):.1f}" y="{52 + rowh * len(names)}" text-anchor="middle">{v}</text>')
    for i, name in enumerate(names):
        e, y = summary[name], 40 + i * rowh
        s.append(f'<text x="{x0 - 6}" y="{y + 16}" text-anchor="end">{name}</text>')
        if e["games"]:
            s.append(f'<rect x="{x0}" y="{y + 3}" width="{(e["payoff_p1"] or 0) / 100 * w:.1f}" height="9" fill="#2c3e50"/>')
            s.append(f'<rect x="{x0}" y="{y + 13}" width="{(e["payoff_p2"] or 0) / 100 * w:.1f}" height="9" fill="#95a5a6"/>')
            comply = f'{e["p1_complies"]}/{e["p1_compliance_known"]}' if e["p1_compliance_known"] else "n/a"
            s.append(f'<text x="{x0 + w + 14}" y="{y + 16}">{e["agreements"]}/{e["games"]}</text>'
                     f'<text x="{x0 + w + 64}" y="{y + 16}">{comply}</text>'
                     f'<text x="{x0 + w + 124}" y="{y + 16}">{e["joint_gap"]:g}</text>')
    s.append("</svg>")
    return "\n".join(s)


def report(config, root):
    table = rows(config, root)
    summary = summarize(config, table)
    M = matrix(table)
    prefix = root / config.get("results_prefix", f"results/{config['study']}")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{prefix}_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ROW_COLUMNS))
        writer.writeheader()
        writer.writerows({k: r.get(k) for k in ROW_COLUMNS} for r in table)
    out = {"study": config["study"], "question": config["question"], "control": config["control"], "cells": summary,
           "matrix": M, "best_responses": best_responses(M), "replicator": replicator(M),
           "cost_usd": round(sum(r["cost_usd"] for r in table) + sum((rec.get("label") or {}).get("cost_usd", 0.0)
                                                                     for rec in records(config, root)), 3)}
    Path(f"{prefix}_summary.json").write_text(json.dumps(out, indent=1))
    Path(f"{prefix}_chart.svg").write_text(chart(config, summary))
    for name, e in summary.items():
        comply = f'{e["p1_complies"]}/{e["p1_compliance_known"]}' if e["p1_compliance_known"] else "-"
        print(f"{name:26} n={e['games']:>2} agree={e['agreements']:>2} p1={e['payoff_p1']} p2={e['payoff_p2']} "
              f"joint={e['joint']} gap={e['joint_gap']} moves={e['moves']} comply={comply} styles={e['p1_style']}")
    return out


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--execute", action="store_true", help="Play the missing games (paid model calls)")
    parser.add_argument("--label", action="store_true", help="Label the styles in finished games (paid model calls)")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cells", help="Comma-separated subset of cell names to play")
    args = parser.parse_args(argv)
    root = Path.cwd()
    config = load_config(args.config)
    if args.execute:
        execute(config, root, args.workers, args.limit, args.cells.split(",") if args.cells else None)
    if args.label:
        label(config, root, args.workers, args.limit)
    if args.report or not (args.execute or args.label):
        report(config, root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
