"""Reductions of two external negotiation benchmarks to the bargaining environment's instance format.

CaSiNo (Chawla et al., NAACL 2021; CC BY 4.0): two campers split three packages each of food, water and firewood.
A camper's packages of its high-, medium- and low-priority item are worth 5, 4 and 3 points each (36 at most) and a
walk-away pays 5 points to both. Every ordered pair of priority orders is one instance (36 in all), so the set is the
full factorial of the benchmark's structures rather than a sample of its 1,030 dialogues; no dialogue text is used.

LLM-Deliberation (Abdelnabi et al., NeurIPS 2024 Datasets and Benchmarks; MIT): six parties negotiate five issues with
three to five options each; every party has a score table over the options and an acceptance threshold. The reduction
keeps two parties and their score tables over all five issues; a decision is contested when the two parties' top
options differ and both parties' scores vary on it, and pairs without a contested decision are dropped. Outside
options are 30 percent of each party's maximum, as in the core (the original thresholds are kept on the instance for a
secondary check); the scenario text comes from the game's own description.

Run as a module to write data/bargain_casino_instances.json and data/bargain_deliberation_instances.json.
"""
import itertools
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIDDLE_NOTE = ("Where a decision has more than three options, 'the middle option' in any guidance means an option between "
               "the two extremes.")

# ---------------------------------------------------------------- CaSiNo

CASINO_ITEMS = ["Food", "Water", "Firewood"]
CASINO_POINTS = {"High": 5, "Medium": 4, "Low": 3}
CASINO_UNITS = 3


def casino_skin():
    options = [f"{u} to Camper 1, {CASINO_UNITS - u} to Camper 2" for u in range(CASINO_UNITS, -1, -1)]
    return {"name": "casino", "parties": ["Camper 1", "Camper 2"], "party_kind": "campsite neighbours",
            "other": "the other camper", "artifact": "how to split the extra supplies", "thing": "split",
            "intro": ("You are camping next to each other and a few extra packages are available: three of food, three of water "
                      "and three of firewood. Each decision splits one kind of package between you; your scores say what each "
                      f"package is worth to you. {MIDDLE_NOTE}"),
            "issues": [{"name": item, "options": list(options)} for item in CASINO_ITEMS]}


def casino_instances():
    """All 36 ordered pairs of priority orders (high, medium, low) over food, water and firewood."""
    specs = []
    for p1, p2 in itertools.product(itertools.permutations(CASINO_ITEMS), repeat=2):
        pts = {a: {item: CASINO_POINTS[level] for item, level in zip(order, ("High", "Medium", "Low"))} for a, order in (("p1", p1), ("p2", p2))}
        weights = {"p1": [[u * pts["p1"][item] for u in range(CASINO_UNITS, -1, -1)] for item in CASINO_ITEMS],
                   "p2": [[(CASINO_UNITS - u) * pts["p2"][item] for u in range(CASINO_UNITS, -1, -1)] for item in CASINO_ITEMS]}
        top = CASINO_UNITS * sum(CASINO_POINTS.values())
        specs.append({"source": f"casino/{'-'.join(x[:2] for x in p1)}_vs_{'-'.join(x[:2] for x in p2)}", "benchmark": "casino",
                      "priorities": {"p1": list(p1), "p2": list(p2)}, "weights": weights,
                      "outside": [5, 5], "max": {"p1": top, "p2": top}, "skin": casino_skin()})
    return specs


# ---------------------------------------------------------------- LLM-Deliberation

DELIBERATION_ROOT = ROOT / "external" / "LLM-Deliberation" / "games_descriptions"
DELIBERATION_GAMES = {"base": "the terms of the Harbour Sport Park project",
                      "game1": "the terms of the new airport project",
                      "game2": "the terms of the new solar power plant",
                      "game3": "the terms of the new airport in Saarland"}
OUTSIDE_SHARE = 0.3


def read_config(game):
    """(display name, file stem) per party, in the game's order."""
    out = []
    for line in (DELIBERATION_ROOT / game / "config.txt").read_text().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            out.append((parts[0], parts[1]))
    return out


def read_scores(game, stem):
    """(score table: one list per issue, acceptance threshold)."""
    lines = [l.strip() for l in (DELIBERATION_ROOT / game / "scores_files" / f"{stem}.txt").read_text().splitlines() if l.strip()]
    table = [[int(x) for x in l.split(",") if x.strip()] for l in lines[:-1]]
    return table, int(lines[-1])


def parse_issues(game):
    """Issue names and option names from the game's description; tolerant of the four games' formats."""
    text = (DELIBERATION_ROOT / game / "global_instructions.txt").read_text()
    parties = {name for name, _ in read_config(game)}
    issues = {}
    for m in re.finditer(r"Issue ([A-E])\s*:?\s*(.+)", text):
        letter, rest = m.group(1), m.group(2).strip()
        q = re.match(r'"([^"]+)"', rest)
        name = q.group(1) if q else re.split(r"[.:(]| There are", rest)[0]
        issues.setdefault(letter, {"name": name.strip().rstrip(".").strip(), "options": {}})
    for m in re.finditer(r"^[ \t\-]*([A-E])([1-5])\s*:?\s*(.+)", text, flags=re.M):
        letter, num, rest = m.group(1), int(m.group(2)), m.group(3).strip()
        if letter not in issues:
            continue
        issues[letter]["options"].setdefault(num, option_label(rest, parties) or f"option {num}")
    return [issues[l] for l in sorted(issues)]


def option_label(rest, parties=(), limit=60):
    """A short option name from the text after 'A1': the quoted name when the line opens with one that is not a
    party's name (A1 "water-based": ...), else the text before '==>', the first colon or the first sentence end."""
    q = re.match(r'^"([^"]+)"', rest)
    label = q.group(1) if q and q.group(1) not in parties else re.split(r"==>|: |\. |\.$", rest, maxsplit=1)[0]
    label = re.sub(r"\s+", " ", label).strip().rstrip(".,;").strip()
    if len(label) > limit:
        label = label[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return label


def scenario_intro(game):
    text = (DELIBERATION_ROOT / game / "global_instructions.txt").read_text().strip()
    first = re.split(r"\n\s*\n", text)[0].strip()
    first = re.sub(r"\s+", " ", first)
    first = re.sub(r"\s*[^.]*:\s*$", "", first)  # drop a trailing "The parties involved are:" that introduced a list
    return first if len(first) <= 700 else first[:700].rsplit(". ", 1)[0] + "."


def deliberation_instances(games=DELIBERATION_GAMES):
    """Every pair of parties of every game with at least one contested decision."""
    specs = []
    for game, artifact in games.items():
        parties = read_config(game)
        scores = {stem: read_scores(game, stem) for _, stem in parties}
        issues = parse_issues(game)
        intro = scenario_intro(game)
        for (name1, stem1), (name2, stem2) in itertools.combinations(parties, 2):
            (t1, thr1), (t2, thr2) = scores[stem1], scores[stem2]
            assert [len(x) for x in t1] == [len(x) for x in t2], (game, stem1, stem2)
            top = {"p1": sum(max(x) for x in t1), "p2": sum(max(x) for x in t2)}
            def contested(i):
                v1, v2 = t1[i], t2[i]
                return max(v1) > min(v1) and max(v2) > min(v2) and v1.index(max(v1)) != v2.index(max(v2))
            if not any(contested(i) for i in range(len(t1))):
                continue
            skin = {"name": f"deliberation/{game}", "parties": [name1, name2], "party_kind": "parties", "other": "the other party",
                    "artifact": artifact, "thing": "deal",
                    "intro": f"{intro} You are {{role}}; the other party is {{other_role}}. {MIDDLE_NOTE}",
                    "roles": [f'the "{name1}"', f'the "{name2}"'],
                    "issues": [{"name": iss["name"], "options": [iss["options"].get(n + 1, f"option {n + 1}") for n in range(len(t1[i]))]}
                               for i, iss in enumerate(issues)]}
            specs.append({"source": f"llm_deliberation/{game}/{stem1}_vs_{stem2}", "benchmark": f"llm_deliberation/{game}",
                          "pair": [name1, name2], "weights": {"p1": t1, "p2": t2},
                          "outside": [round(OUTSIDE_SHARE * top["p1"], 1), round(OUTSIDE_SHARE * top["p2"], 1)], "max": top,
                          "thresholds": {"p1": thr1, "p2": thr2}, "skin": skin})
    return specs


def main():
    out = ROOT / "data"
    casino = casino_instances()
    (out / "bargain_casino_instances.json").write_text(json.dumps(casino, indent=1) + "\n")
    delib = deliberation_instances()
    (out / "bargain_deliberation_instances.json").write_text(json.dumps(delib, indent=1) + "\n")
    print(f"casino: {len(casino)} instances; deliberation: {len(delib)} instances from {len(DELIBERATION_GAMES)} games")


if __name__ == "__main__":
    main()
