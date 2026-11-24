"""The five rule sets played by a program, without a model (the scripted policies of E8).

A seat whose strategy is "script_<rule>" (or "script_<rule>_<variant>") takes its moves from `move`. A script sees
what an agent in its seat sees: its own scores and outside option, the move number, and the moves made so far (the
other side's messages and proposals). It never reads the other side's scores or which decisions are contested: a
decision counts as contested for a script once the other side has proposed an option on it that is not the script's
top option, and that option is taken to be the one the other side prefers there.

Each script does what its rule says. Where the rule leaves a choice open it follows these conventions:

- With no proposal from the other side to go on, a script proposes its own top design.
- Where a rule has the agent give ground on some decisions and does not say which, the script gives ground first
  where it costs it least.
- A script says only what its rule tells it to say (the collaborating rule's first message, the accommodating rule's
  question); every other message is empty.
- competing: one decision per proposal from its top to its middle option, only where the other side's latest proposal
  differs from its own and never to a design under its acceptance floor; it accepts at the floor and walks away on its
  last move. Variant "firm": it never gives ground.
- collaborating: the decision the other side "says matters most" is the first contested decision named in a sentence
  of theirs about what matters (see `stated`); if they have said none, the contested decision that matters least to
  the script. It takes its top option on its own most important other contested decision and its middle option on
  the rest; with one contested decision it proposes its middle option there. On its last move it accepts anything
  above its outside option. Variant "keep": its top option on the rest.
- compromising: two contested decisions per proposal to its middle option. Variant "one": one per proposal.
- accommodating: it builds on the other side's latest proposal and takes back its top option on as few decisions as
  bring the design to its acceptance floor. Variant "middle": with nothing to go on it opens with its middle options.
- avoiding: its one proposal is its first move; on its second move it accepts or walks away. Variant "quiet": if a
  proposal it can accept is already on the table at its first move, it accepts at once.
"""
import re

from . import bargain as B

PREFIX = "script_"
VARIANTS = {"competing": ("firm",), "collaborating": ("keep",), "compromising": ("one",), "accommodating": ("middle",),
            "avoiding": ("quiet",)}
ABOUT_NEEDS = re.compile(r"matter|important|priorit|care|need|most", re.I)
ASK_NEEDS = "What do you need from this design?"


def is_script(strategy):
    return isinstance(strategy, str) and strategy.startswith(PREFIX)


def rule_of(strategy):
    """("competing", None) for "script_competing"; ("competing", "firm") for "script_competing_firm"."""
    rule, _, variant = strategy[len(PREFIX):].partition("_")
    if rule not in VARIANTS or (variant and variant not in VARIANTS[rule]):
        raise ValueError(f"unknown scripted policy {strategy!r}")
    return rule, variant or None


def named(inst, text):
    """The decisions named in a text, in the order of their first mention."""
    found = []
    for i, name in enumerate(B.issue_names(inst)):
        hit = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)
        if hit:
            found.append((hit.start(), i))
    return [i for _, i in sorted(found)]


class View:
    """What a script knows at its move."""

    def __init__(self, inst, agent, moves, n, T):
        self.inst, self.agent, self.n, self.T, self.k = inst, agent, n, T, inst["k"]
        self.other = "p2" if agent == "p1" else "p1"
        w = inst["weights"][agent]
        ranked = [sorted(range(B.n_opts(inst, i)), key=lambda o: (-w[i][o], o)) for i in range(self.k)]
        self.top = [r[0] for r in ranked]
        self.mid = [r[1] if len(r) >= 3 else None for r in ranked]  # no middle option where a decision has two options
        self.imp = [max(w[i]) - min(w[i]) for i in range(self.k)]
        self.t = B.thresholds(inst, agent)
        self.mine = [m for m in moves if m["by"] == agent]
        self.own = [list(m["design"]) for m in self.mine if m["action"] == "propose"]
        self.theirs = [list(m["design"]) for m in moves if m["by"] == self.other and m["action"] == "propose"]
        last = moves[-1] if moves else None
        # the proposal that can be accepted: the other side's, if their latest move was a proposal
        self.offer = list(last["design"]) if last and last["by"] == self.other and last["action"] == "propose" else None
        self.heard = [m["message"] for m in moves if m["by"] == self.other and m.get("message")]
        self.wants = {}  # contested decisions as far as the script can tell, with the option the other side first asked for
        for design in self.theirs:
            for i, o in enumerate(design):
                if o != self.top[i]:
                    self.wants.setdefault(i, o)
        self.last_move = n >= T - 1  # the script will not move again

    def value(self, design):
        return B.value(self.inst, self.agent, design)

    def loss(self, i, o):
        w = self.inst["weights"][self.agent][i]
        return w[self.top[i]] - w[o]

    def offered(self):
        return self.value(self.offer) if self.offer is not None else None


def propose(design, message=""):
    return {"action": "propose", "message": message, "design": list(design)}


def accept(message=""):
    return {"action": "accept", "message": message, "design": None}


def walk():
    return {"action": "walk", "message": "", "design": None}


def competing(v, variant):
    if v.offer is not None and v.offered() >= v.t["t60"]:
        return accept()
    if v.last_move:
        return walk()
    if not v.own:
        return propose(v.top)
    prev = v.own[-1]
    if variant != "firm" and v.offer is not None:
        steps = [prev[:i] + [v.mid[i]] + prev[i + 1:] for i in range(v.k)
                 if prev[i] == v.top[i] and v.offer[i] != prev[i] and v.mid[i] is not None]
        steps = [d for d in steps if v.value(d) >= v.t["t60"]]
        if steps:
            return propose(max(steps, key=lambda d: (v.value(d), [-o for o in d])))
    return propose(prev)


def stated(v, contested):
    """The contested decision the other side says matters most to them: the first one named in a sentence of theirs
    that speaks of what matters, is important, a priority or a need; None if they have named none."""
    for text in v.heard:
        for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
            if ABOUT_NEEDS.search(sentence):
                for i in named(v.inst, sentence):
                    if i in contested:
                        return i
    return None


def trade(v, variant):
    contested, design = sorted(v.wants), list(v.top)
    if len(contested) == 1:
        i = contested[0]
        design[i] = v.mid[i] if v.mid[i] is not None else v.wants[i]
    elif contested:
        give = stated(v, contested)
        if give is None:
            give = min(contested, key=lambda i: (v.imp[i], i))
        take = max((i for i in contested if i != give), key=lambda i: (v.imp[i], -i))
        design[give] = v.wants[give]
        for i in contested:
            if i not in (give, take) and variant != "keep" and v.mid[i] is not None:
                design[i] = v.mid[i]
    return design


def collaborating(v, variant):
    message = ""
    if not v.mine:  # its first message, whatever its first move is
        names = B.issue_names(v.inst)
        first = [names[i] for i in sorted(range(v.k), key=lambda i: (-v.imp[i], i))[:2]]
        message = f"The decisions that matter most to me are {' and '.join(first)}. Which decisions matter most to you?"
    if v.offer is not None and (v.offered() >= v.t["t60"] or (v.last_move and v.offered() > v.t["outside"])):
        return accept(message)
    return propose(trade(v, variant), message)


def compromising(v, variant):
    if v.offer is not None and v.offered() >= v.t["t50"]:
        return accept()
    design = list(v.own[-1]) if v.own else list(v.top)
    movable = sorted((i for i in v.wants if design[i] == v.top[i] and v.mid[i] is not None), key=lambda i: (v.loss(i, v.mid[i]), i))
    for i in movable[:1 if variant == "one" else 2]:
        design[i] = v.mid[i]
    return propose(design)


def accommodating(v, variant):
    floor = v.t["outside10"]
    message = "" if v.mine else ASK_NEEDS  # its first message, whatever its first move is
    if v.offer is not None and v.offered() >= floor:
        return accept(message)
    if v.theirs:
        design = list(v.theirs[-1])
        while v.value(design) < floor:  # ends at the script's top design at the latest
            i = max((i for i in range(v.k) if design[i] != v.top[i]), key=lambda i: (v.loss(i, design[i]), -i))
            design[i] = v.top[i]
        return propose(design, message)
    if variant == "middle":
        return propose([m if m is not None else t for m, t in zip(v.mid, v.top)], message)
    return propose(v.top, message)


def avoiding(v, variant):
    acceptable = v.offer is not None and v.offered() >= v.t["outside"]
    if not v.mine:
        return accept() if (variant == "quiet" and acceptable) else propose(v.top)
    return accept() if acceptable else walk()


POLICIES = {"competing": competing, "collaborating": collaborating, "compromising": compromising,
            "accommodating": accommodating, "avoiding": avoiding}


def move(strategy, inst, agent, moves, n, T):
    """The script's move: {"action", "message", "design"} as `bargain.parse_reply` returns it."""
    rule, variant = rule_of(strategy)
    return POLICIES[rule](View(inst, agent, moves, n, T), variant)
