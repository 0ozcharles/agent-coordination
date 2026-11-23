"""Agreement in the human annotation study: between the annotators, with the judge model and with the checker.

Reads what the annotators returned (annotation/returned/: the workbooks, *.xlsx, or the exports of the annotation
page, *.json), the assignment and the keys; writes
annotation/answers.csv (every answer, one row a cell), results/annotation_agreement.json and
results/annotation_disagreements.csv (the items a third person should look at). Works on a partial return. Run from
the repository root.
"""
import argparse
import csv
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import annotation as ann  # noqa: E402
from agent_coordination.conflict_states import STYLE  # noqa: E402  (assertiveness, cooperativeness) -> style

ORDINAL = ("assertiveness", "cooperativeness")
PARSE_A = {"assertiveness": ann.score, "cooperativeness": ann.score, "asks_needs": ann.yes_no, "discloses_priorities": ann.yes_no}


def between(units, ordinal=False):
    """Agreement between the annotators over units rated by two or more of them: {unit: {annotator: value}}."""
    values = [[v for v in u.values() if v is not None] for u in units.values()]
    pairs = [(v[0], v[1]) for v in values if len(v) == 2]
    return {**ann.agree(pairs, ordinal), "alpha": ann.alpha(values, "interval" if ordinal else "nominal")}


def against(units, reference, ordinal=False):
    """Every human rating against the reference label of its unit (the judge or the checker)."""
    pairs = [(v, reference[u]) for u, by in units.items() for v in by.values() if v is not None and reference.get(u) is not None]
    out = {**ann.agree(pairs, ordinal), "kappa": ann.kappa(pairs, weighted=ordinal)}
    agreed = [(next(iter(by.values())), reference[u]) for u, by in units.items()
              if len(by) >= 2 and len(set(by.values())) == 1 and None not in by.values() and reference.get(u) is not None]
    out["where_the_annotators_agree"] = ann.agree(agreed)
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--returned", default="annotation/returned")
    p.add_argument("--pack", default="annotation")
    p.add_argument("--out", default="results/annotation_agreement.json")
    p.add_argument("--answers", default=None, help="where to write every answer; default <pack>/answers.csv")
    args = p.parse_args(argv)
    pack = Path(args.pack)
    keys = {t: json.loads((pack / "keys" / f"task_{t}_key.json").read_text()) for t in "abc"}
    assigned = defaultdict(dict)   # task -> item -> its two annotators
    for r in csv.DictReader(open(pack / "assignment.csv")):
        assigned[r["task"].lower()][r["item"]] = {r["annotator_1"], r["annotator_2"]}

    raw, problems = {}, []
    for f in sorted(glob.glob(f"{args.returned}/*.xlsx") + glob.glob(f"{args.returned}/*.json")):
        if Path(f).name.startswith("~$"):
            continue
        try:
            who, answers = ann.workbook_answers(f) if f.endswith(".xlsx") else ann.web_answers(f)
        except (ValueError, KeyError, OSError) as e:
            problems.append(f"{f}: cannot be read ({e}); skipped")
            continue
        if who is None or who in raw:
            problems.append(f"{f}: {'no annotator id found' if who is None else 'a second file for ' + who}; skipped")
            continue
        raw[who] = answers
    if not raw:
        print(f"nothing under {args.returned}: put the returned workbooks (*.xlsx) or the page's exports (*.json) there")
        return 1

    # ---- every answer, and how complete each return is
    rows, complete = [], {}
    for who, answers in sorted(raw.items()):
        complete[who] = {}
        for task in "abc":
            mine = [i for i, raters in assigned[task].items() if who in raters]
            per_item = 8 if task == "a" else 1 if task == "b" else 4
            need = {"a": [f"{a}_{f}" for a in ("agent1", "agent2") for f in ann.A_FIELDS], "b": ["followed"], "c": list(ann.C_FIELDS)}[task]
            filled = sum(1 for i in mine for k in need if str(answers[task].get(i, {}).get(k, "")).strip() != "")
            complete[who][task] = {"done": sum(1 for i in mine if all(str(answers[task].get(i, {}).get(k, "")).strip() != "" for k in need)),
                                   "assigned": len(mine), "cells_filled": filled, "cells": per_item * len(mine)}
            for item, fields in sorted(answers[task].items()):
                if who not in assigned[task].get(item, ()):
                    problems.append(f"{who} answered {item}, which is not one of their items; ignored")
                    continue
                rows.extend([task.upper(), item, who, k, v] for k, v in sorted(fields.items()))
    answers_path = Path(args.answers) if args.answers else pack / "answers.csv"
    with open(answers_path, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["task", "item", "annotator", "field", "value"])
        w.writerows(rows)

    def collect(task, parse):
        """{field: {unit: {annotator: parsed value}}} for one task; a unit is an item (task A: an agent of it)."""
        out = defaultdict(lambda: defaultdict(dict))
        for t, item, who, field, value in rows:
            if t.lower() == task:
                unit, name = ((item, field[:6]), field[7:]) if task == "a" else (item, field)
                if name in parse and parse[name](value) is not None:
                    out[name][unit][who] = parse[name](value)
        return out

    out = {"annotators": sorted(raw), "complete": complete, "problems": problems}
    disagreements = []

    # ---- task A: style scores and episode facts
    judge = {(item, agent): k["judge"][agent] for item, k in keys["a"].items() for agent in ("agent1", "agent2")}
    source = {item: ("e1" if k["run"] == "e1" else k["run"][3:]) for item, k in keys["a"].items()}
    got = collect("a", PARSE_A)
    res = {}
    for field in ann.A_FIELDS:
        units, ordinal = got[field], field in ORDINAL
        ref = {u: judge[u][field] for u in units}
        res[field] = {"between_annotators": between(units, ordinal), "judge": against(units, ref, ordinal),
                      # the judge as one more rater, on the same scale as the agreement between annotators
                      "judge_alpha": ann.alpha([[v, ref[u]] for u, by in units.items() for v in by.values()], "interval" if ordinal else "nominal")}
        res[field]["judge_by_source"] = {s: against({u: by for u, by in units.items() if source[u[0]] == s}, ref, ordinal) for s in sorted(set(source.values()))}
        res[field]["judge_by_annotator"] = {who: ann.kappa([(by[who], ref[u]) for u, by in units.items() if who in by], weighted=ordinal) for who in sorted(raw)}
        for u, by in sorted(units.items()):
            if len(by) == 2 and len(set(by.values())) == 2:
                (w1, v1), (w2, v2) = sorted(by.items())
                if not ordinal or abs(v1 - v2) == 2:
                    disagreements.append(["A", u[0], f"{u[1]} {field}", w1, v1, w2, v2])
    styles = defaultdict(dict)
    for u in set(got["assertiveness"]) | set(got["cooperativeness"]):
        for who in set(got["assertiveness"].get(u, {})) & set(got["cooperativeness"].get(u, {})):
            styles[u][who] = STYLE[(got["assertiveness"][u][who], got["cooperativeness"][u][who])]
    res["style"] = {"between_annotators": between(styles), "judge": against(styles, {u: judge[u]["style"] for u in styles})}
    out["task_a"] = res

    # ---- task B: the rule followed or not
    parse_b = {"followed": ann.yes_no, "broken_clauses": ann.clause_numbers}
    got = collect("b", parse_b)
    units = got["followed"]
    checker = {i: keys["b"][i]["checker"]["overall"] for i in units}
    res = {"between_annotators": between(units), "checker": against(units, checker), "by_rule": {}}
    # the same comparison on the clauses the checker verifies: a human "no" counts only when it cites a checked clause
    # (or cites none); what is left over is tabulated as violations of clauses the program does not check
    on_checked, unchecked = defaultdict(dict), Counter()
    for i, by in units.items():
        k = keys["b"][i]
        checked = set(k["checked_clauses"].values())
        for who, followed in by.items():
            cited = set(got["broken_clauses"].get(i, {}).get(who, []))
            on_checked[i][who] = followed or bool(cited and not cited & checked)
            if not followed and cited and not cited & checked:
                unchecked[f"{k['strategy']} clause " + ",".join(map(str, sorted(cited)))] += 1
    res["checker_on_checked_clauses"] = against(on_checked, checker)
    res["violations_of_unchecked_clauses_only"] = dict(sorted(unchecked.items()))
    for verdict, name in ((False, "checker_failed"), (True, "checker_passed")):
        ratings = [v for i, by in units.items() if checker[i] is verdict for v in by.values()]
        res[name] = {"ratings": len(ratings), "annotators_say_followed": round(sum(ratings) / len(ratings), 3) if ratings else None}
    for rule in ann.CHECKED:
        sub = {i: by for i, by in units.items() if keys["b"][i]["strategy"] == rule}
        res["by_rule"][rule] = {"between_annotators": between(sub), "checker": against(sub, checker),
                                "checker_on_checked_clauses": against({i: on_checked[i] for i in sub}, checker)}
    for i, by in sorted(units.items()):
        if len(by) == 2 and len(set(by.values())) == 2:
            (w1, v1), (w2, v2) = sorted(by.items())
            disagreements.append(["B", i, f"followed ({keys['b'][i]['strategy']})", w1, v1, w2, v2])
    out["task_b"] = res

    # ---- task C: the extraction against the tickets
    got = collect("c", {f: ann.three_way for f in ann.C_FIELDS})
    res = {}
    for field in ann.C_FIELDS:
        units = got[field]
        counts = Counter(v for by in units.values() for v in by.values())
        res[field] = {"yes": counts[2], "partly": counts[1], "no": counts[0], "between_annotators": between(units, ordinal=True),
                      "items_all_yes": sum(1 for by in units.values() if len(by) >= 2 and set(by.values()) == {2}), "items_rated_twice": sum(1 for by in units.values() if len(by) >= 2)}
        for i, by in sorted(units.items()):
            if len(by) == 2 and len(set(by.values())) == 2:
                (w1, v1), (w2, v2) = sorted(by.items())
                disagreements.append(["C", i, field, w1, "no partly yes".split()[v1], w2, "no partly yes".split()[v2]])
    out["task_c"] = res

    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    with open(Path(args.out).with_name("annotation_disagreements.csv"), "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["task", "item", "what", "annotator_1", "value_1", "annotator_2", "value_2"])
        w.writerows(disagreements)

    # ---- what to read
    print(f"annotators returned: {', '.join(sorted(raw))}")
    for who, c in complete.items():
        print(f"  {who}: " + ", ".join(f"task {t.upper()} {c[t]['done']}/{c[t]['assigned']}" for t in "abc"))
    for line in problems:
        print("  ! " + line)
    a, b, c = out["task_a"], out["task_b"], out["task_c"]
    if a["assertiveness"]["between_annotators"]["n"] or a["assertiveness"]["judge"]["n"]:
        print("\ntask A (alpha between annotators | judge against the annotators: exact, kappa, alpha)")
        for field in ann.A_FIELDS:
            x = a[field]
            print(f"  {field:22} n={x['between_annotators']['n']:>3} exact {x['between_annotators']['exact']} alpha {x['between_annotators']['alpha']} | "
                  f"n={x['judge']['n']:>3} exact {x['judge']['exact']} kappa {x['judge']['kappa']} alpha {x['judge_alpha']}")
        print(f"  {'style':22} n={a['style']['between_annotators']['n']:>3} exact {a['style']['between_annotators']['exact']} alpha {a['style']['between_annotators']['alpha']} | "
              f"n={a['style']['judge']['n']:>3} exact {a['style']['judge']['exact']} kappa {a['style']['judge']['kappa']}")
    if b["checker"]["n"]:
        print("\ntask B")
        print(f"  between annotators: n={b['between_annotators']['n']} exact {b['between_annotators']['exact']} alpha {b['between_annotators']['alpha']}")
        print(f"  checker against the annotators: exact {b['checker']['exact']} kappa {b['checker']['kappa']}; on the clauses it checks: exact {b['checker_on_checked_clauses']['exact']} kappa {b['checker_on_checked_clauses']['kappa']}")
        print(f"  games the checker failed: annotators say followed in {b['checker_failed']['annotators_say_followed']} of {b['checker_failed']['ratings']} ratings; "
              f"games it passed: {b['checker_passed']['annotators_say_followed']} of {b['checker_passed']['ratings']}")
        print(f"  violations of unchecked clauses only: {b['violations_of_unchecked_clauses_only']}")
    if any(c[f]["yes"] + c[f]["partly"] + c[f]["no"] for f in ann.C_FIELDS):
        print("\ntask C (ratings yes / partly / no; items both annotators call yes)")
        for field in ann.C_FIELDS:
            x = c[field]
            print(f"  {field:20} {x['yes']:>3} / {x['partly']:>2} / {x['no']:>2}; both yes on {x['items_all_yes']} of {x['items_rated_twice']}; alpha {x['between_annotators']['alpha']}")
    print(f"\n{len(disagreements)} disagreements to adjudicate -> {Path(args.out).with_name('annotation_disagreements.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
