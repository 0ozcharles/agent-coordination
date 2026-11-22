"""Agreement of the human annotations (annotation/*_answers_<name>.csv) with each other, the judge and the checker.

Writes results/annotation_agreement.json. Run from the repository root once at least one annotator has answered.
"""
import csv
import glob
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination.conflict_states import STYLE  # noqa: E402  (assertiveness, cooperativeness) -> style


def answers(task):
    out = {}
    for f in sorted(glob.glob(f"annotation/task_{task}_answers_*.csv")):
        name = Path(f).stem.split("_answers_")[1]
        if name != "TEMPLATE":
            out[name] = {r["item"]: r for r in csv.DictReader(open(f))}
    return out


def kappa(pairs, weighted=False):
    """Cohen's kappa over (a, b) pairs; linearly weighted for ordinal 0-2 scores when weighted is True."""
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not pairs:
        return None
    cats = sorted({x for p in pairs for x in p})
    span = (max(cats) - min(cats)) if weighted and len(cats) > 1 else 1
    w = (lambda a, b: 1 - abs(a - b) / span) if weighted else (lambda a, b: 1.0 if a == b else 0.0)
    n = len(pairs)
    po = sum(w(a, b) for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[x] * cb[y] * w(x, y) for x in cats for y in cats) / n ** 2
    return None if pe == 1 else (po - pe) / (1 - pe)


def score(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return None


def agree(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    return {"n": len(pairs), "exact": sum(a == b for a, b in pairs), "within_one": sum(abs(a - b) <= 1 for a, b in pairs) if pairs and isinstance(pairs[0][0], int) else None}


def main():
    out = {}
    # ---- task A
    key, ann = json.loads(Path("annotation/keys/task_a_key.json").read_text()), answers("a_style")
    fields = [("agent1", "assertiveness"), ("agent1", "cooperativeness"), ("agent2", "assertiveness"), ("agent2", "cooperativeness")]
    def series(get):
        return {dim: [get(item, agent, dim) for item in key for agent in ("agent1", "agent2")] for dim in ("assertiveness", "cooperativeness")}
    judge = series(lambda item, agent, dim: key[item]["judge"][agent][dim])
    people = {name: series(lambda item, agent, dim, a=a: score(a.get(item, {}).get(f"{agent}_{dim}"))) for name, a in ann.items()}
    def style_of(s):
        return [STYLE.get((x, y)) if x is not None and y is not None else None for x, y in zip(s["assertiveness"], s["cooperativeness"])]
    a_out = {"annotators": list(people), "against_judge": {}, "between_annotators": {}}
    for name, s in people.items():
        a_out["against_judge"][name] = {dim: {**agree(list(zip(s[dim], judge[dim]))), "weighted_kappa": kappa(zip(s[dim], judge[dim]), True)} for dim in s}
        a_out["against_judge"][name]["style"] = {**agree(list(zip(style_of(s), style_of(judge)))), "kappa": kappa(zip(style_of(s), style_of(judge)))}
    for (n1, s1), (n2, s2) in itertools.combinations(people.items(), 2):
        a_out["between_annotators"][f"{n1} / {n2}"] = {dim: {**agree(list(zip(s1[dim], s2[dim]))), "weighted_kappa": kappa(zip(s1[dim], s2[dim]), True)} for dim in s1}
    out["task_a_style"] = a_out
    # ---- task B
    key, ann = json.loads(Path("annotation/keys/task_b_key.json").read_text()), answers("b_compliance")
    b_out = {}
    for name, a in ann.items():
        col = next(c for c in next(iter(a.values())) if c.startswith("agent1_followed"))
        yes = lambda v: {"yes": True, "y": True, "no": False, "n": False}.get(str(v).strip().lower())
        per = {}
        for s in ("all", "competing", "collaborating", "compromising", "accommodating", "avoiding"):
            pairs = [(yes(a.get(item, {}).get(col)), k["checker"]["overall"]) for item, k in key.items() if s in ("all", k["strategy"])]
            per[s] = {**{k2: v for k2, v in agree(pairs).items() if k2 != "within_one"}, "kappa": kappa(pairs)}
        b_out[name] = per
    out["task_b_compliance"] = b_out
    # ---- task C
    ann = answers("c_extraction")
    out["task_c_extraction"] = {name: {col: dict(Counter(str(r[col]).strip().lower() for r in a.values() if str(r[col]).strip())) for col in next(iter(a.values())) if col not in ("item", "note")} for name, a in ann.items()}
    Path("results/annotation_agreement.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(json.dumps(out, indent=1, default=str)[:3000])
    if not any(out[k] if not isinstance(out[k], dict) or k != "task_a_style" else out[k]["annotators"] for k in out):
        print("no answers yet: copy the TEMPLATE files to *_answers_<name>.csv and fill them in")


if __name__ == "__main__":
    main()
