"""Manipulation check of the style instructions in NegotiationArena's buy-sell game.

One player (the focal player) gets a conflict-handling instruction, the other plays with the
game's plain prompt. Every condition sees the same instances (same cost, buyer limit and focal
role), so conditions are compared pairwise by instance. The record of each game holds every
proposal and message; a model labeler scores each player's style from the public transcript
with docs/N_NEGOTIATION_STYLE_CODEBOOK.md. `execute` and `label` need the OpenAI key and the
NegotiationArena checkout under external/; `report` needs neither.
"""
import argparse
import csv
import json
import math
import random
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .conflict_states import STYLE, codebook_id

ARENA = "external/NegotiationArena"
STYLES = ("competing", "collaborating", "compromising", "accommodating", "avoiding")
ROW_COLUMNS = ("condition", "instance", "focal_role", "cost", "limit", "surplus", "error", "deal", "price",
               "focal_share", "partner_share", "turns", "focal_proposals", "focal_messages", "first_claim",
               "last_claim", "concession", "accepted_by", "rejected_by", "focal_style", "partner_style",
               "focal_assertiveness", "focal_cooperativeness", "first_concession", "focal_asks_needs", "cost_usd")


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "model", "instances", "conditions", "control", "labeler", "output"):
        if key not in config:
            raise ValueError(f"config lacks {key}")
    return config


# ---------------------------------------------------------------- design

def instances(config):
    """The same instances for every condition: cost, buyer limit and which side gets the instruction."""
    spec = config["instances"]
    rng = random.Random(spec["seed"])
    out = []
    for i in range(spec["n"]):
        cost = rng.randint(*spec["cost"])
        surplus = rng.choice(spec["surplus"])
        role = ("seller", "buyer")[i % 2] if spec.get("focal_role", "alternate") == "alternate" else spec["focal_role"]
        out.append({"id": i, "cost": cost, "limit": cost + surplus, "surplus": surplus, "focal_role": role})
    return out


def instruction(config, condition):
    text = config["conditions"][condition].get("instruction")
    return None if text is None else config.get("instruction_prefix", "") + text


def game_path(config, root, condition, inst):
    return root / config["output"] / "games" / condition / f"g{inst['id']:02d}.json"


# ---------------------------------------------------------------- playing

def arena(root):
    """NegotiationArena's modules, imported from the checkout under external/."""
    path = str(root / ARENA)
    if path not in sys.path:
        sys.path.insert(0, path)
    import importlib.util
    import types
    if importlib.util.find_spec("anthropic") is None:  # the package imports its Claude agent unconditionally
        stub = types.ModuleType("anthropic")
        stub.Anthropic, stub.HUMAN_PROMPT, stub.AI_PROMPT = object, "", ""
        sys.modules["anthropic"] = stub
    import negotiationarena.constants as constants
    from negotiationarena.agents.agents import Agent
    from negotiationarena.game_objects.goal import BuyerGoal, SellerGoal
    from negotiationarena.game_objects.resource import Resources
    from negotiationarena.game_objects.trade import Trade
    from negotiationarena.game_objects.valuation import Valuation
    from games.buy_sell_game.game import BuySellGame

    class QuietBuySellGame(BuySellGame):
        def view_state(self, *args, **kwargs):  # no per-turn dump to stdout
            pass

        def game_over(self):  # the prompt says REJECT ends the game; the engine only stops on ACCEPT
            state = self.game_state[-1]
            answer = state["player_public_info_dict"].get(constants.PLAYER_ANSWER_TAG) if state else None
            return answer in (constants.ACCEPTING_TAG, constants.REJECTION_TAG) or super().game_over()

        def after_game_ends(self):  # the arena's summary breaks when the accepted move follows no proposal
            try:
                super().after_game_ends()
            except Exception:
                self.game_state.append({"current_iteration": "END", "turn": "None"})

    return {"constants": constants, "Agent": Agent, "BuyerGoal": BuyerGoal, "SellerGoal": SellerGoal,
            "Resources": Resources, "Trade": Trade, "Valuation": Valuation, "BuySellGame": QuietBuySellGame}


def agent_class(A):
    """A NegotiationArena agent that talks through litellm, like the rest of this project."""
    C = A["constants"]

    class LiteLLMAgent(A["Agent"]):
        def __init__(self, agent_name, model="", model_kwargs=None, max_tokens=600, conversation=None, calls=None,
                     run_epoch_time_ms=None, **_):
            super().__init__(agent_name)
            self.model = model
            self.model_kwargs = dict(model_kwargs or {})
            self.max_tokens = max_tokens
            self.conversation = list(conversation or [])
            self.calls = list(calls or [])
            self.run_epoch_time_ms = run_epoch_time_ms or "0"
            self.prompt_entity_initializer = "system"

        def init_agent(self, system_prompt, role):
            if C.AGENT_ONE in self.agent_name:  # the first mover needs a user turn to answer
                self.update_conversation_tracking("system", system_prompt)
                self.update_conversation_tracking("user", role)
            else:
                self.update_conversation_tracking("system", system_prompt + role)

        def chat(self):
            import litellm
            last = None
            for _ in range(3):
                try:
                    response = litellm.completion(model=f"openai/{self.model}", messages=self.conversation,
                                                  max_completion_tokens=self.max_tokens, drop_params=True, timeout=180,
                                                  **self.model_kwargs)
                    try:
                        cost = litellm.completion_cost(completion_response=response)
                    except Exception:
                        cost = None
                    usage = getattr(response, "usage", None)
                    self.calls.append({"cost_usd": cost, "prompt_tokens": getattr(usage, "prompt_tokens", None),
                                       "completion_tokens": getattr(usage, "completion_tokens", None)})
                    return response.choices[0].message.content or ""
                except Exception as exc:  # transient API errors
                    last = exc
            raise RuntimeError(f"model call failed: {last}")

        def update_conversation_tracking(self, role, message):
            self.conversation.append({"role": role, "content": message})

    return LiteLLMAgent


def price_of(trade, A):
    if not hasattr(trade, "resources_from_second_agent"):
        return None
    return trade.resources_from_second_agent.resource_dict.get(A["constants"].MONEY_TOKEN)


def turns_of(game, A):
    """The public moves in order: who, what they answered, the price they proposed, their message."""
    C = A["constants"]
    out = []
    for state in game.game_state[1:]:
        if state.get("current_iteration") == "END":
            continue
        public = state["player_public_info_dict"]
        private = state.get("player_private_info_dict", {})
        out.append({"player": ("seller", "buyer")[state["turn"]], "answer": str(public.get(C.PLAYER_ANSWER_TAG, "")).strip(),
                    "price": price_of(public.get(C.PROPOSED_TRADE_TAG), A),
                    "message": str(public.get(C.MESSAGE_TAG, "")).strip(),
                    "reasoning": str(private.get(C.REASONING_TAG, "")).strip()})
    return out


def effective(turns):
    """The moves that count: up to and including the first ACCEPT or REJECT."""
    for k, t in enumerate(turns):
        if t["answer"] in ("ACCEPT", "REJECT"):
            return turns[:k + 1]
    return list(turns)


def claim(role, price, inst):
    """The share of the surplus a proposal claims for `role`; above 1 means more than the whole surplus."""
    own = inst["cost"] if role == "seller" else inst["limit"]
    return (price - own) / inst["surplus"] if role == "seller" else (own - price) / inst["surplus"]


def outcome(turns, inst):
    """Deal, price and each side's share of the surplus (0 when there is no deal)."""
    turns = effective(turns)
    accepted = next((t for t in turns if t["answer"] == "ACCEPT"), None)
    price = None
    if accepted:
        before = turns[:turns.index(accepted)]
        proposals = [t["price"] for t in before if t["answer"] == "PROPOSAL" and t["price"] is not None]
        price = proposals[-1] if proposals else None
    deal = price is not None
    shares = {"seller": claim("seller", price, inst) if deal else 0.0,
              "buyer": claim("buyer", price, inst) if deal else 0.0}
    return {"deal": deal, "price": price, "shares": shares,
            "accepted_by": accepted["player"] if deal else None,
            "rejected_by": next((t["player"] for t in turns if t["answer"] == "REJECT"), None)}


def focal_metrics(turns, inst, role):
    mine = [t for t in effective(turns) if t["player"] == role]
    proposals = [t["price"] for t in mine if t["answer"] == "PROPOSAL" and t["price"] is not None]
    claims = [claim(role, p, inst) for p in proposals]
    return {"proposals": proposals, "claims": [round(c, 3) for c in claims],
            "messages": sum(1 for t in mine if t["message"]),
            "first_claim": claims[0] if claims else None, "last_claim": claims[-1] if claims else None,
            "concession": (claims[0] - claims[-1]) if len(claims) >= 2 else (0.0 if claims else None)}


def play(config, root, condition, inst, A=None):
    """Play one game and return its record; raises on a model or parsing failure."""
    A = A or arena(root)
    C = A["constants"]
    Agent = agent_class(A)
    text = instruction(config, condition) or ""
    partner = config.get("partner_instruction", "")
    seller_text, buyer_text = (text, partner) if inst["focal_role"] == "seller" else (partner, text)
    make = lambda name: Agent(name, model=config["model"], model_kwargs=config.get("model_kwargs", {}),
                              max_tokens=config.get("max_tokens", 600))
    log_dir = root / config["output"] / "arena_logs" / condition / f"g{inst['id']:02d}"
    pay = config.get("payoff_statement", {})
    roles = [(f"You are {C.AGENT_ONE}. " + pay.get("seller", "").format(cost=inst["cost"])).strip(),
             (f"You are {C.AGENT_TWO}. " + pay.get("buyer", "").format(limit=inst["limit"])).strip()]
    game = A["BuySellGame"](
        players=[make(C.AGENT_ONE), make(C.AGENT_TWO)], iterations=config.get("iterations", 10),
        player_goals=[A["SellerGoal"](cost_of_production=A["Valuation"]({"X": inst["cost"]})),
                      A["BuyerGoal"](willingness_to_pay=A["Valuation"]({"X": inst["limit"]}))],
        player_starting_resources=[A["Resources"]({"X": 1}),
                                   A["Resources"]({C.MONEY_TOKEN: config["instances"].get("buyer_money", 1000)})],
        player_conversation_roles=roles, player_social_behaviour=[seller_text, buyer_text], log_dir=str(log_dir))
    game.run()
    turns = turns_of(game, A)
    result = outcome(turns, inst)
    role = inst["focal_role"]
    calls = [c for p in game.players for c in p.calls]
    return {"condition": condition, "instance": inst, "focal_role": role, "instruction": text or None, "roles": roles,
            "turns": turns, **result, "focal": focal_metrics(turns, inst, role),
            "partner": focal_metrics(turns, inst, "buyer" if role == "seller" else "seller"),
            "cost_usd": sum(c["cost_usd"] or 0.0 for c in calls), "model_calls": len(calls),
            "tokens": sum((c["prompt_tokens"] or 0) + (c["completion_tokens"] or 0) for c in calls),
            "arena_log": str(log_dir.relative_to(root)), "error": None}


def jobs(config, root, conditions=None):
    out = []
    for condition in conditions or config["conditions"]:
        for inst in instances(config):
            path = game_path(config, root, condition, inst)
            if path.is_file() and json.loads(path.read_text()).get("error") is None:
                continue
            out.append((condition, inst))
    return out


def execute(config, root, workers=4, limit=None, conditions=None):
    todo = jobs(config, root, conditions)[:limit]
    budget = config.get("budget_usd", 5.0)
    spent = spent_so_far(config, root)
    lock = threading.Lock()
    A = arena(root)
    print(f"{len(todo)} games to play; ${spent:.2f} spent so far, budget ${budget:.2f}", flush=True)

    def one(job):
        nonlocal spent
        condition, inst = job
        with lock:
            if spent >= budget:
                return "skipped: budget"
        path = game_path(config, root, condition, inst)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            record = play(config, root, condition, inst, A)
        except Exception:
            previous = json.loads(path.read_text()) if path.is_file() else {}
            record = {"condition": condition, "instance": inst, "focal_role": inst["focal_role"],
                      "error": traceback.format_exc(), "attempts": previous.get("attempts", 0) + 1, "cost_usd": 0.0}
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
        with lock:
            spent += record.get("cost_usd") or 0.0
        return (f"{condition} g{inst['id']:02d}: " + ("ERROR" if record.get("error") else
                f"deal={record['deal']} price={record['price']} focal_share={record['shares'][inst['focal_role']]:.2f} "
                f"turns={len(record['turns'])} ${record['cost_usd']:.4f}"))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)
    print(f"done; ${spent:.2f} spent in total", flush=True)


def spent_so_far(config, root):
    return sum((r.get("cost_usd") or 0.0) + (r.get("label", {}) or {}).get("cost_usd", 0.0) for r in records(config, root))


def records(config, root):
    out = []
    for condition in config["conditions"]:
        for inst in instances(config):
            path = game_path(config, root, condition, inst)
            if path.is_file():
                out.append(json.loads(path.read_text()))
    return out


# ---------------------------------------------------------------- labeling

def transcript(record):
    """What each player could see, in order; valuations and private reasoning are left out."""
    names = {"seller": "Agent 1 (seller, Player RED)", "buyer": "Agent 2 (buyer, Player BLUE)"}
    lines = []
    for n, t in enumerate(effective(record["turns"]), 1):
        move = {"PROPOSAL": f"proposes {t['price']} ZUP", "ACCEPT": "ACCEPTS the last proposal",
                "REJECT": "REJECTS and ends the negotiation"}.get(t["answer"], t["answer"] or "(no move)")
        lines.append(f"#{n} {names[t['player']]} {move}: {t['message'] or '(no message)'}")
    return "\n".join(lines)


def label_prompt(codebook, record):
    return (f"{codebook}\n\n---\n\nOne object X is for sale. Agent 1 sells it, Agent 2 buys it with ZUP. "
            f"Each knows only its own cost or limit; you do not see either.\n\nMoves in the order they happened:\n"
            f"{transcript(record)}\n\nReturn the JSON object only.")


def parse_style_label(reply):
    text = reply.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(text[start:end + 1])
    for agent in ("agent1", "agent2"):
        a = data.get(agent) or {}
        if a.get("assertiveness") not in (0, 1, 2) or a.get("cooperativeness") not in (0, 1, 2):
            raise ValueError(f"{agent} scores must be 0, 1 or 2")
        if not isinstance(a.get("asks_needs"), bool):
            raise ValueError(f"{agent}.asks_needs must be true or false")
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
            label, error = parse_style_label(reply), None
        except (ValueError, TypeError) as exc:
            label, error = None, str(exc)
        tries.append({"reply": reply, "error": error})
        if label:
            break
    record["label"] = {"codebook_id": codebook_id(codebook), "model": lab["model"], "label": label,
                       "tries": tries, "cost_usd": cost}
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    return label


def label(config, root, workers=4, limit=None):
    codebook = (root / config["labeler"]["codebook"]).read_text()
    todo = []
    for condition in config["conditions"]:
        for inst in instances(config):
            path = game_path(config, root, condition, inst)
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
    for record in records(config, root):
        inst, role = record["instance"], record["focal_role"]
        row = {"condition": record["condition"], "instance": inst["id"], "focal_role": role, "cost": inst["cost"],
               "limit": inst["limit"], "surplus": inst["surplus"], "error": bool(record.get("error")),
               "cost_usd": record.get("cost_usd", 0.0)}
        if not record.get("error"):
            turns = effective(record["turns"])  # recomputed from the moves, so old and new records agree
            result = outcome(turns, inst)
            focal = focal_metrics(turns, inst, role)
            partner_role = "buyer" if role == "seller" else "seller"
            lab = (record.get("label") or {}).get("label") or {}
            me, other = ("agent1", "agent2") if role == "seller" else ("agent2", "agent1")
            first = lab.get("first_concession")
            row.update({"deal": result["deal"], "price": result["price"],
                        "focal_share": round(result["shares"][role], 3), "partner_share": round(result["shares"][partner_role], 3),
                        "turns": len(turns), "focal_proposals": " ".join(str(p) for p in focal["proposals"]),
                        "focal_messages": focal["messages"], "first_claim": focal["first_claim"],
                        "last_claim": focal["last_claim"], "concession": focal["concession"],
                        "accepted_by": "focal" if result["accepted_by"] == role else result["accepted_by"] and "partner",
                        "rejected_by": "focal" if result["rejected_by"] == role else result["rejected_by"] and "partner",
                        "focal_style": lab.get(me, {}).get("style"), "partner_style": lab.get(other, {}).get("style"),
                        "focal_assertiveness": lab.get(me, {}).get("assertiveness"),
                        "focal_cooperativeness": lab.get(me, {}).get("cooperativeness"),
                        "first_concession": {me: "focal", other: "partner", "none": "none"}.get(first),
                        "focal_asks_needs": lab.get(me, {}).get("asks_needs")})
        out.append(row)
    return out


def sign_test(pairs):
    """Exact two-sided sign test on (arm, control) pairs; ties are dropped."""
    higher = sum(1 for a, c in pairs if a > c)
    lower = sum(1 for a, c in pairs if a < c)
    n, k = higher + lower, min(higher, lower)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0
    return {"higher": higher, "lower": lower, "p": round(p, 4)}


def mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize(config, table):
    control = config["control"]
    by = {c: {r["instance"]: r for r in table if r["condition"] == c and not r["error"]} for c in config["conditions"]}
    out = {}
    for condition, games in by.items():
        g = list(games.values())
        target = config["conditions"][condition].get("target")
        styles = {s: sum(1 for r in g if r.get("focal_style") == s) for s in STYLES}
        entry = {"games": len(g), "errors": sum(1 for r in table if r["condition"] == condition and r["error"]),
                 "deals": sum(1 for r in g if r.get("deal")), "focal_share": mean([r["focal_share"] for r in g]),
                 "partner_share": mean([r["partner_share"] for r in g]), "turns": mean([r["turns"] for r in g]),
                 "focal_messages": mean([r["focal_messages"] for r in g]),
                 "first_claim": mean([r["first_claim"] for r in g]), "concession": mean([r["concession"] for r in g]),
                 "walked_away": sum(1 for r in g if r.get("rejected_by") == "focal"),
                 "focal_style": styles, "labeled": sum(1 for r in g if r.get("focal_style")),
                 "first_concession_focal": sum(1 for r in g if r.get("first_concession") == "focal"),
                 "asks_needs": sum(1 for r in g if r.get("focal_asks_needs")),
                 "cost_usd": round(sum(r["cost_usd"] for r in g), 3), "target": target}
        if target:
            entry["in_target_style"] = styles.get(target, 0)
        if condition != control:
            pairs = [(games[i], by[control][i]) for i in games if i in by[control]]
            vs = {"pairs": len(pairs)}
            for key in ("focal_share", "concession", "turns", "focal_messages", "first_claim"):
                vs[key] = sign_test([(a[key], c[key]) for a, c in pairs if a.get(key) is not None and c.get(key) is not None])
            if target:
                vs["in_target_style"] = {"arm": styles.get(target, 0),
                                         "control": sum(1 for r in by[control].values() if r.get("focal_style") == target)}
            entry["vs_control"] = vs
        out[condition] = entry
    return out


def curves(config, table):
    """Mean claimed share of the surplus by the focal player's proposal index, per condition."""
    out = {}
    for condition in config["conditions"]:
        series = {}
        for r in table:
            if r["condition"] != condition or r["error"] or not r["focal_proposals"]:
                continue
            inst = {"cost": r["cost"], "limit": r["limit"], "surplus": r["surplus"]}
            for k, p in enumerate(float(x) for x in r["focal_proposals"].split()):
                series.setdefault(k + 1, []).append(claim(r["focal_role"], p, inst))
        out[condition] = {k: {"mean": mean(v), "n": len(v)} for k, v in sorted(series.items())}
    return out


def chart(config, summary, curve_data):
    """An SVG with the concession curves (left) and the focal player's labeled styles (right)."""
    conditions = list(config["conditions"])
    colors = ["#555555", "#c0392b", "#2980b9", "#8e44ad", "#27ae60", "#e67e22", "#16a085", "#7f8c8d"]
    W, H = 1000, 420
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>']
    # left panel: curves
    x0, y0, w, h = 60, 40, 340, 300
    ks = sorted({k for c in curve_data.values() for k in c})
    kmax = max(ks) if ks else 4
    ys = [v["mean"] for c in curve_data.values() for v in c.values() if v["mean"] is not None]
    lo, hi = min(0.0, min(ys, default=0)), max(1.0, max(ys, default=1))
    sx = lambda k: x0 + (k - 1) / max(kmax - 1, 1) * w
    sy = lambda v: y0 + h - (v - lo) / (hi - lo) * h
    s.append(f'<text x="{x0}" y="{y0 - 18}" font-size="14" font-weight="bold">Focal player: share of the surplus claimed, by own proposal</text>')
    for v in (0.0, 0.5, 1.0):
        s.append(f'<line x1="{x0}" y1="{sy(v):.1f}" x2="{x0 + w}" y2="{sy(v):.1f}" stroke="#ddd"/>'
                 f'<text x="{x0 - 8}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>')
    for k in ks:
        s.append(f'<text x="{sx(k):.1f}" y="{y0 + h + 16}" text-anchor="middle">{k}</text>')
    s.append(f'<text x="{x0 + w / 2:.1f}" y="{y0 + h + 34}" text-anchor="middle">proposal index</text>')
    for i, condition in enumerate(conditions):
        pts = [(sx(k), sy(v["mean"])) for k, v in curve_data.get(condition, {}).items() if v["mean"] is not None and v["n"] >= 3]
        if not pts:
            continue
        col = colors[i % len(colors)]
        s.append(f'<polyline fill="none" stroke="{col}" stroke-width="2" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>')
        s.extend(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{col}"/>' for x, y in pts)
        s.append(f'<text x="{x0 + w + 8}" y="{pts[-1][1] + 4:.1f}" fill="{col}">{condition}</text>')
    # right panel: style bars
    bx, by_, bw, bh = 640, 40, 330, 300
    s.append(f'<text x="{bx}" y="{by_ - 18}" font-size="14" font-weight="bold">Focal player: labeled style</text>')
    pal = {"competing": "#c0392b", "collaborating": "#2980b9", "compromising": "#8e44ad", "accommodating": "#27ae60", "avoiding": "#95a5a6"}
    rowh = bh / max(len(conditions), 1)
    for i, condition in enumerate(conditions):
        styles = summary[condition]["focal_style"]
        total = sum(styles.values()) or 1
        y = by_ + i * rowh
        s.append(f'<text x="{bx - 6}" y="{y + rowh / 2 + 4:.1f}" text-anchor="end">{condition}</text>')
        x = bx
        for style in STYLES:
            n = styles.get(style, 0)
            if n:
                ww = n / total * bw
                s.append(f'<rect x="{x:.1f}" y="{y + 4:.1f}" width="{ww:.1f}" height="{rowh - 8:.1f}" fill="{pal[style]}"/>')
                s.append(f'<text x="{x + ww / 2:.1f}" y="{y + rowh / 2 + 4:.1f}" text-anchor="middle" fill="white">{n}</text>')
                x += ww
    lx = bx
    for style in STYLES:
        s.append(f'<rect x="{lx}" y="{by_ + bh + 12}" width="12" height="12" fill="{pal[style]}"/>'
                 f'<text x="{lx + 16}" y="{by_ + bh + 22}">{style}</text>')
        lx += 20 + 7 * len(style)
    s.append("</svg>")
    return "\n".join(s)


def report(config, root):
    table = rows(config, root)
    summary = summarize(config, table)
    curve_data = curves(config, table)
    prefix = root / config.get("results_prefix", f"results/{config['study']}")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{prefix}_rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ROW_COLUMNS))
        writer.writeheader()
        writer.writerows({k: r.get(k) for k in ROW_COLUMNS} for r in table)
    out = {"study": config["study"], "question": config["question"], "control": config["control"],
           "conditions": summary, "curves": curve_data,
           "cost_usd": round(sum(r["cost_usd"] for r in table) + sum(
               (rec.get("label") or {}).get("cost_usd", 0.0) for rec in records(config, root)), 3)}
    Path(f"{prefix}_summary.json").write_text(json.dumps(out, indent=1))
    Path(f"{prefix}_curves.svg").write_text(chart(config, summary, curve_data))
    print(json.dumps({c: {k: v for k, v in e.items() if k in ("games", "deals", "focal_share", "concession", "turns",
                                                             "focal_style", "in_target_style", "vs_control")}
                      for c, e in summary.items()}, indent=1))
    return out


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--execute", action="store_true", help="Play the missing games (paid model calls)")
    parser.add_argument("--label", action="store_true", help="Label the styles in finished games (paid model calls)")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--conditions", help="Comma-separated subset of conditions to play")
    args = parser.parse_args(argv)
    root = Path.cwd()
    config = load_config(args.config)
    if args.execute:
        execute(config, root, args.workers, args.limit, args.conditions.split(",") if args.conditions else None)
    if args.label:
        label(config, root, args.workers, args.limit)
    if args.report or not (args.execute or args.label):
        report(config, root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
