"""Build the human annotation study under annotation/: three tasks, six annotators, every item rated by two of them.

Task A (style): 120 games, the public transcript only, as the judge model saw it; per agent the two codebook scores and
the two episode facts the paper uses (asks about the other side's needs, discloses its own priorities). 75 games from
E1 (every cell) and 15 from each of three other models (E3 on haiku, sonnet and sol).
Task B (compliance): 120 rule-following agents, with the rule split into numbered clauses, the facts the rule refers
to and the value of every proposal to that agent; half are games the checker failed, where a rule has that many.
Task C (extraction): 30 grounded instances, the decisions, options and importances the extractor produced, with a link
to the ParallACT ticket pair they came from (the ticket text is not copied).

The six annotators form 15 pairs; every item goes to one pair (8, 8 and 2 items a pair), so each annotator has 40, 40
and 10 items and every pair of annotators shares some. One worked example per task is shown filled in and is nobody's
item. There are no practice items; the pack as first built had eleven, and their games are still drawn and set aside,
so that the main items are the ones drawn then.

Writes the item files, assignment.csv, keys/ (which game an item is, the judge's labels, the checker's verdicts; kept
out of the repository until the answers are in, with their hashes in keys.sha256; the games of the first pack, whose
key was published, are excluded) and, with openpyxl installed, one
workbook per annotator under workbooks/, and the same items as data for the annotation web page under web/. Run from
the repository root; needs runs/bargain (not in the repository).
"""
import csv
import glob
import hashlib
import itertools
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination import annotation, bargain  # noqa: E402

OUT = Path("annotation")
SEED = 20261122
RATERS = [f"R{i}" for i in range(1, 7)]
RUNS = {"e1": "configs/bargain_e1.json", "e3_claude_haiku_5_5": "configs/bargain_e3_claude_haiku_5_5.json",
        "e3_claude_sonnet_5_5": "configs/bargain_e3_claude_sonnet_5_5.json", "e3_gpt_6_1_sol": "configs/bargain_e3_gpt_6_1_sol.json",
        "e3_gpt_6_luna_t1": "configs/bargain_e3_gpt_6_luna_t1.json"}
E3_FOR_A = {"neutral-neutral": 3, "collaborating-neutral": 2, "competing-neutral": 2, "collaborating-collaborating": 2,
            "competing-competing": 2, "compromising-compromising": 2, "accommodating-accommodating": 1, "avoiding-avoiding": 1}
QUOTA_B = {"competing": 24, "collaborating": 36, "compromising": 24, "accommodating": 24, "avoiding": 12}
TICKETS = "https://github.com/rm206/ParallACT_data_and_code/blob/main/dataset/v1/generated/contradictory/{source}.json"
# where each clause of a rule starts in its text; the clause numbers are what the annotators cite
CLAUSES = {"competing": ["open with the design", "in each later proposal change at most one decision", "and only from your top option", "accept only a proposal worth"],
           "collaborating": ["in your first message, say which decisions", "and ask which matter most", "On the decisions where you want different options", "Accept a proposal worth"],
           "compromising": ["on each decision where you and the other developer", "Each proposal of yours must be worth no more", "Accept any proposal worth"],
           "accommodating": ["in your first message, ask the other developer", "accept the first proposal worth", "when you propose, build the design"],
           "avoiding": ["make at most one proposal of your own", "do not argue about the design", "after that, accept the other developer"]}
# the worked examples: (run, cell, instance), the answers and why
EXAMPLE_A = (("e1", "competing-avoiding", 37),
             {"agent1": (2, 1, "no", "no", "示范：Agent 1 拒绝对方方案并坚持自己分最高的设计，一步没让（强硬度 2）；只是来回出方案，没有问对方或照顾对方的优先级（合作度 1）。只说“这个方案对我值 19 分”不算说出优先级。"),
              "agent2": (0, 1, "no", "no", "示范：Agent 2 开了一个价之后，对方一还价就直接接受，没有再争（强硬度 0）；有回应、达成了协议，但仅限于交换方案（合作度 1）。")})
EXAMPLE_B = (("e1", "competing-compromising", 29, "p1"),
             ("no", "4", "示范：Agent 1 第一个方案值 100 分（满足第 1 条），之后没有再提方案（第 2、3 条用不上）；但它在 #4 接受了只值 47 分的方案，低于 60 分，违反第 4 条。它自己说“值 67 分”是算错了，以 worth 列为准。"))
EXAMPLE_C = ("yes", "partly", "yes", "yes", "示范只演示怎么填，不代表这道题的正确判断。备注里写原因，例如：决策 2 里标给 B 的选项，B 的 ticket 其实没有要求。")


def agent_name(agent):
    return f"Agent {1 if agent == 'p1' else 2}"


def move_cells(inst, m):
    """One move as the judge model saw it (bargain.transcript), split into who and what."""
    did = {"propose": f"proposes {bargain.design_text(inst, m['design'])}" if m.get("design") else "proposes",
           "accept": "ACCEPTS the last proposal", "walk": "WALKS AWAY", "pass": "makes no valid move"}[m["action"]]
    return f"#{m['n']} {agent_name(m['by'])}", f"{did}: {m['message'] or '(no message)'}"


def worth(record, agent):
    """For every move, what the proposal on the table is worth to the agent: the proposal made, or the one accepted."""
    out, standing = [], None
    for m in record["moves"]:
        if m["action"] == "propose" and m.get("values"):
            standing = m["values"][agent]
        out.append(standing if m["action"] in ("propose", "accept") else None)
    return out


def numbered_rule(config, strategy, inst, agent):
    text = bargain.guidance(config, strategy, "rules", inst, agent).replace(config.get("instruction_prefix", ""), "")
    at = [text.index(start) for start in CLAUSES[strategy]]
    assert at == sorted(at) and all(text.count(s) == 1 for s in CLAUSES[strategy]), (strategy, text)
    parts = [text[a:b].strip() for a, b in zip(at, at[1:] + [len(text)])]
    return "\n".join(f"({n}) {part}" for n, part in enumerate(parts, 1))


def facts_data(record, agent):
    """What the rule refers to, for the agent it was given to."""
    inst, other = record["instance"], "p2" if agent == "p1" else "p1"
    T = inst.get("moves", 10)
    theirs = bargain.tops(inst, other)
    names = bargain.issue_names(inst)
    t = bargain.thresholds(inst, agent)
    return {"decisions": [{"name": names[i], "contested": bool(inst["opposed"][i]), "other_prefers": bargain.option_labels(inst, i)[theirs[i]],
                           "options": [[label, inst["weights"][agent][i][o]] for o, label in enumerate(bargain.option_labels(inst, i))]} for i in range(inst["k"])],
            "max": t["max"], "outside": t["outside"], "moves": T, "first": 1 if inst["first_mover"] == "p1" else 2,
            "last": T if (T % 2 == 0) == (inst["first_mover"] != agent) else T - 1}


def facts(record, agent):
    f, name = facts_data(record, agent), agent_name(agent)
    return (f"{name}'s points for each option (its top option on a decision is the one with the most points, its middle option the second):\n"
            f"{bargain.scores_table(record['instance'], agent)}\n"
            f"Contested decisions (the two agents want different options): {', '.join(d['name'] for d in f['decisions'] if d['contested']) or 'none'}. "
            f"The other agent's preferred options: {', '.join(d['name'] + ' ' + d['other_prefers'] for d in f['decisions'])}.\n"
            f"{name}'s ideal design is worth {f['max']:g} points to it; no deal is worth {f['outside']:g} points to it. "
            f"The game has {f['moves']} moves; Agent {f['first']} makes move #1, so {name}'s last possible move is #{f['last']}.")


def load_runs():
    recs = {}
    for run in RUNS:
        recs[run] = []
        for f in sorted(glob.glob(f"runs/bargain/{run}/games/*/*.json")):
            r = json.loads(Path(f).read_text())
            if not r.get("error"):
                r["_run"] = run
                recs[run].append(r)
    return recs


def game(r):
    return (r["_run"], r["cell"], r["instance"]["id"])


def build(rng):
    recs, configs = load_runs(), {run: bargain.load_config(path) for run, path in RUNS.items()}
    by_game = {game(r): r for rs in recs.values() for r in rs}
    pairs = list(itertools.combinations(RATERS, 2))
    tasks, keys, assignment = {}, {}, []
    # games of the first pack (commit 7aceb39), whose key is in the repository's history, are not drawn again
    published = {tuple(x) for x in json.loads((OUT / "excluded_games.json").read_text())}

    def number(task, main, example):
        """Deal the main items (in the stratified order they were drawn) to the pairs, then number them in a random order."""
        given = annotation.deal(range(len(main)), pairs, rng)
        order = list(range(len(main)))
        rng.shuffle(order)
        items = [{"item": "EXAMPLE", "kind": "example", "raters": (), "what": example}]
        items += [{"item": f"{task}{n:03d}", "kind": "main", "raters": given[i], "what": main[i]} for n, i in enumerate(order, 1)]
        assignment.extend([task, x["item"], *x["raters"]] for x in items[1:])
        return items

    # ---- Task A
    used = {EXAMPLE_A[0]} | published
    def take(run, cell, k):
        pool = [r for r in recs[run] if r["cell"] == cell and game(r) not in used and (r.get("label") or {}).get("label")]
        chosen = rng.sample(pool, k)
        used.update(game(r) for r in chosen)
        return chosen
    e1_cells = sorted({r["cell"] for r in recs["e1"]})
    rule_cells = [c for c in e1_cells if not c.startswith(("prompted", "default"))]
    twice = set(e1_cells) - set(rule_cells) | set(rng.sample(rule_cells, 27))
    main = [r for cell in e1_cells for r in take("e1", cell, 2 if cell in twice else 1)]
    main += [r for run in ("e3_claude_haiku_5_5", "e3_claude_sonnet_5_5", "e3_gpt_6_1_sol") for cell, k in E3_FOR_A.items() for r in take(run, cell, k)]
    # set aside (see the module docstring): the five practice games of the pack as first built
    for x in (("e1", "collaborating-competing"), ("e1", "accommodating-neutral"), ("e1", "prompted-avoiding-avoiding"),
              ("e3_claude_sonnet_5_5", "neutral-neutral"), ("e3_gpt_6_1_sol", "competing-competing")):
        take(*x, 1)
    tasks["A"] = number("A", main, by_game[EXAMPLE_A[0]])
    keys["A"] = {}
    for x in tasks["A"][1:]:
        r = x["what"]
        assert "\n".join(" ".join(move_cells(r["instance"], m)) for m in r["moves"]) == bargain.transcript(r)
        lab = r["label"]["label"]
        keys["A"][x["item"]] = {"run": r["_run"], "cell": r["cell"], "instance": r["instance"]["id"],
                                "judge": {a: {k: lab[a].get(k) for k in (*annotation.A_FIELDS, "style")} for a in ("agent1", "agent2")}}

    # ---- Task B: one rule-following agent of a game
    units = {}
    for rs in recs.values():
        for r in rs:
            for agent in ("p1", "p2"):
                verdict = r["compliance"][agent].get("overall")
                if r["ops"][agent] == "rules" and r["strategies"][agent] in QUOTA_B and verdict is not None:
                    units.setdefault((r["strategies"][agent], verdict), []).append((r, agent))
    example_b = (by_game[EXAMPLE_B[0][:3]], EXAMPLE_B[0][3])
    taken = {(game(example_b[0]), example_b[1])}
    def draw(strategy, verdict, k):
        seen = {g for g, _ in taken} | published
        pool = [u for u in units.get((strategy, verdict), []) if game(u[0]) not in seen]
        chosen = rng.sample(pool, min(k, len(pool)))
        taken.update((game(r), a) for r, a in chosen)
        return chosen
    main = []
    for strategy, quota in QUOTA_B.items():
        failed = draw(strategy, False, quota // 2)
        main += failed + draw(strategy, True, quota - len(failed))
    for s, v in (("competing", False), ("collaborating", True), ("compromising", False), ("accommodating", True), ("avoiding", True)):
        draw(s, v, 1)   # set aside, as in task A
    tasks["B"] = number("B", main, example_b)
    keys["B"] = {}
    for x in tasks["B"][1:]:
        r, agent = x["what"]
        s = r["strategies"][agent]
        keys["B"][x["item"]] = {"run": r["_run"], "cell": r["cell"], "instance": r["instance"]["id"], "agent": agent, "strategy": s,
                                "checker": r["compliance"][agent], "checked_clauses": annotation.CHECKED[s]}

    # ---- Task C
    grounded = json.loads(Path("data/bargain_grounded_instances.json").read_text())
    picked = rng.sample(range(len(grounded)), 32)   # 30 items, one set aside (as in task A), the example
    tasks["C"] = number("C", [grounded[i] for i in picked[:30]], grounded[picked[31]])
    index = {g["source"]: i for i, g in enumerate(grounded)}
    keys["C"] = {x["item"]: {"index": index[x["what"]["source"]], "source": x["what"]["source"]} for x in tasks["C"][1:]}
    return tasks, keys, assignment, configs


def extraction_rows(g):
    """The extracted structure of a grounded instance as (part, text) rows."""
    skin = g["skin"]
    rows = [("context", f"Context told to both developers: {skin['intro'].replace('{role}', 'ticket A').replace('{other_role}', 'ticket B')}\n"
                        f"Ticket A (Developer A), our title: {skin['roles'][0]}\nTicket B (Developer B), our title: {skin['roles'][1]}")]
    for j, issue in enumerate(skin["issues"]):
        top_a, top_b = g["rankings"]["p1"][j][0], g["rankings"]["p2"][j][0]
        lines = [f"{issue['name']}  [{'contested' if g['opposed'][j] else 'shared'}; importance to A {g['raw_importance']['A'][j]}/5, to B {g['raw_importance']['B'][j]}/5]"]
        lines += [f"   {chr(65 + o)}. {text}" + ("   <- A needs this" if o == top_a else "") + ("   <- B needs this" if o == top_b else "") for o, text in enumerate(issue["options"])]
        rows.append((f"decision {j + 1}", "\n".join(lines)))
    return rows


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(header)
        w.writerows(rows)


def write_files(tasks, keys, assignment, configs):
    rows = [[x["item"], x["kind"], bargain.transcript(x["what"])] for x in tasks["A"]]
    write_csv(OUT / "task_a_style_items.csv", ["item", "kind", "transcript"], rows)
    rows = []
    for x in tasks["B"]:
        r, agent = x["what"]
        values = worth(r, agent)
        lines = [" ".join(move_cells(r["instance"], m)) + (f"  [worth {v:g} to {agent_name(agent)}]" if v is not None else "") for m, v in zip(r["moves"], values)]
        rows.append([x["item"], x["kind"], agent_name(agent), numbered_rule(configs[r["_run"]], r["strategies"][agent], r["instance"], agent), facts(r, agent), "\n".join(lines)])
    write_csv(OUT / "task_b_compliance_items.csv", ["item", "kind", "rule agent", "rule", "facts", "transcript"], rows)
    rows = [[x["item"], x["kind"], x["what"]["repo"], TICKETS.format(source=x["what"]["source"]), x["what"]["artifact"],
             "\n".join(text for _, text in extraction_rows(x["what"]))] for x in tasks["C"]]
    write_csv(OUT / "task_c_extraction_items.csv", ["item", "kind", "repository", "ticket pair", "colliding code", "extracted structure"], rows)
    write_csv(OUT / "assignment.csv", ["task", "item", "annotator_1", "annotator_2"], assignment)
    (OUT / "keys").mkdir(parents=True, exist_ok=True)
    digests = []
    for task in "ABC":
        path = OUT / "keys" / f"task_{task.lower()}_key.json"
        path.write_text(json.dumps(keys[task], indent=1) + "\n")
        digests.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  keys/{path.name}")
    (OUT / "keys.sha256").write_text("\n".join(digests) + "\n")


# ---------------------------------------------------------------- the web page

def write_web(tasks, configs):
    """The items as data for the annotation page (annotation/web/index.html): one JSON array per task, a record per line."""
    def moves_of(r, values=None):
        out = []
        for n, m in enumerate(r["moves"]):
            who, text = move_cells(r["instance"], m)
            act, msg = text.split(": ", 1) if m["action"] != "propose" or not m.get("design") else (f"proposes {bargain.design_text(r['instance'], m['design'])}", m["message"] or "(no message)")
            assert f"{act}: {msg}" == text
            out.append({"n": m["n"], "by": 1 if m["by"] == "p1" else 2, "act": act, "msg": msg, **({"worth": values[n]} if values and values[n] is not None else {})})
        return out
    def base(x):
        return {"id": x["item"], "kind": x["kind"], "raters": list(x["raters"])}
    a, b, c = [], [], []
    for x in tasks["A"]:
        rec = {**base(x), "moves": moves_of(x["what"])}
        if x["kind"] == "example":
            rec["example"] = {f"{agent}_{f}": v for agent, vals in EXAMPLE_A[1].items() for f, v in zip((*annotation.A_FIELDS, "note"), vals)}
        a.append(rec)
    for x in tasks["B"]:
        r, agent = x["what"]
        rule = numbered_rule(configs[r["_run"]], r["strategies"][agent], r["instance"], agent)
        rec = {**base(x), "agent": 1 if agent == "p1" else 2, "clauses": [line.split(") ", 1)[1] for line in rule.split("\n")], "facts": facts_data(r, agent), "moves": moves_of(r, worth(r, agent))}
        if x["kind"] == "example":
            rec["example"] = dict(zip(("followed", "broken_clauses", "note"), EXAMPLE_B[1]))
        b.append(rec)
    for x in tasks["C"]:
        g = x["what"]
        skin = g["skin"]
        rec = {**base(x), "url": TICKETS.format(source=g["source"]), "repo": g["repo"], "code": g["artifact"],
               "context": skin["intro"].replace("{role}", "ticket A").replace("{other_role}", "ticket B"), "titles": skin["roles"],
               "decisions": [{"name": issue["name"], "contested": bool(g["opposed"][j]), "importance": [g["raw_importance"]["A"][j], g["raw_importance"]["B"][j]],
                              "options": [{"text": text, "a": o == g["rankings"]["p1"][j][0], "b": o == g["rankings"]["p2"][j][0]} for o, text in enumerate(issue["options"])]}
                             for j, issue in enumerate(skin["issues"])]}
        if x["kind"] == "example":
            rec["example"] = dict(zip((*annotation.C_FIELDS, "note"), EXAMPLE_C))
        c.append(rec)
    (OUT / "web").mkdir(parents=True, exist_ok=True)
    for name, records in (("a", a), ("b", b), ("c", c)):
        (OUT / "web" / f"data_{name}.json").write_text("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n]\n")
    # the codebook's score definitions are quoted word for word wherever the annotators read them
    codebook = [line[2:] for line in Path("docs/B_BARGAIN_STYLE_CODEBOOK.md").read_text().split("\n") if line[:5] in ("- 0: ", "- 1: ", "- 2: ")]
    assert len(codebook) == 6
    for path in (OUT / "GUIDE_zh.md", OUT / "web" / "index.html"):
        if path.exists():
            text = path.read_text().replace("&#39;", "'")
            missing = [line[:40] for line in codebook if line not in text]
            assert not missing, f"{path} does not quote the codebook: {missing}"


# ---------------------------------------------------------------- workbooks

def lines_of(text, width):
    """Lines a wrapped text takes in a column of this width: Arial sets about 1.12 characters per unit of width, a CJK
    character is two wide."""
    return sum(max(1, math.ceil(sum(2 if ord(ch) > 0x2E7F else 1 for ch in para) / (1.12 * width))) for para in str(text).split("\n"))


def write_workbooks(tasks, configs):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    def font(**kw):
        return Font(name="Arial", size=kw.pop("size", 10), **kw)
    fill = {k: PatternFill("solid", start_color=v) for k, v in {"answer": "FFF2CC", "agent1": "EAF2FB", "grey": "EDEDED", "head": "D9E1F2", "banner": "1F3864", "rule": "F4F0E6"}.items()}
    wrap, centre = Alignment(wrap_text=True, vertical="top"), Alignment(horizontal="center", vertical="center", wrap_text=True)
    edge = Side(style="thin", color="999999")
    box = Border(left=edge, right=edge, top=edge, bottom=edge)

    class Sheet:
        def __init__(self, wb, name, columns):
            self.ws, self.columns, self.row = wb.create_sheet(name), columns, 1
            for i, (key, width, label) in enumerate(columns, 1):
                self.ws.column_dimensions[chr(64 + i)].width = width
                c = self.ws.cell(1, i, f"{key}\n{label}")
                c.font, c.fill, c.alignment, c.border = font(bold=True), fill["head"], centre, box
            self.ws.row_dimensions[1].height = 58
            self.ws.freeze_panes = "A2"
            self.ws.sheet_view.zoomScale = 90
            self.checks = {}

        def check(self, kind, values, message):
            dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True, showErrorMessage=True, errorTitle="请从下拉列表里选", error=message)
            self.ws.add_data_validation(dv)
            self.checks[kind] = dv

        def banner(self, text):
            self.row += 1
            self.ws.merge_cells(start_row=self.row, start_column=1, end_row=self.row, end_column=len(self.columns))
            c = self.ws.cell(self.row, 1, text)
            c.font, c.fill, c.alignment = font(bold=True, size=11, color="FFFFFF"), fill["banner"], Alignment(vertical="center", wrap_text=True)
            self.ws.row_dimensions[self.row].height = 15 * lines_of(text, 150) + 8

        def line(self, item, kind, text, shade=None, bold=False, extra=None, right=False):
            self.row += 1
            for i, v in enumerate((item, kind, text), 1):
                c = self.ws.cell(self.row, i, v)
                c.font, c.alignment = font(bold=bold or i < 3 and kind in ("agent1", "agent2", "answer", "rule"), color="666666" if i == 1 else "000000"), Alignment(wrap_text=True, vertical="top", horizontal="right" if right and i == 3 else None)
                if shade:
                    c.fill = fill[shade]
            for col, v in (extra or {}).items():
                c = self.ws.cell(self.row, col, v)
                c.font, c.alignment = font(), Alignment(horizontal="center", vertical="top")
                if shade:
                    c.fill = fill[shade]
            self.ws.row_dimensions[self.row].height = min(409, 12.8 * lines_of(text, self.columns[2][1]) + 5)
            return self.row

        def answers(self, item, kind, prompt, cells, filled=None, note=None):
            """A row of answer cells: {column: validation kind or None}; `filled` are the worked example's answers."""
            row = self.line(item, kind, prompt, right=True)
            for n, (col, check) in enumerate(cells.items()):
                c = self.ws.cell(row, col, filled[n] if filled else None)
                c.fill, c.border, c.alignment, c.font = fill["grey" if filled else "answer"], box, Alignment(horizontal="center", vertical="center"), font(bold=True, size=11)
                if check and not filled:
                    self.checks[check].add(c)
            c = self.ws.cell(row, len(self.columns), note)
            c.fill, c.border, c.alignment, c.font = fill["grey" if filled else "answer"], box, wrap, font(size=9)
            self.ws.row_dimensions[row].height = max(20, min(409, 12.5 * lines_of(note or "", self.columns[-1][1]) + 6))

        def gap(self):
            self.row += 1
            self.ws.row_dimensions[self.row].height = 9

    def block_a(sh, x, example=None):
        r = x["what"]
        for m in r["moves"]:
            who, text = move_cells(r["instance"], m)
            sh.line(x["item"], who, text, shade="agent1" if m["by"] == "p1" else None)
        for n, agent in enumerate(("agent1", "agent2"), 1):
            ex = example[agent] if example else None
            sh.answers(x["item"], agent, f"▶ 给 Agent {n} 打分（看它在整场谈判里的表现）", {4: "score", 5: "score", 6: "yn", 7: "yn"}, ex[:4] if ex else None, ex[4] if ex else None)
        sh.gap()

    def block_b(sh, x, example=None):
        r, agent = x["what"]
        name = agent_name(agent)
        rule = numbered_rule(configs[r["_run"]], r["strategies"][agent], r["instance"], agent)
        sh.line(x["item"], "rule", f"The rule given to {name} (judge {name} only):\n{rule}\n\n{facts(r, agent)}", shade="rule")
        for m, v in zip(r["moves"], worth(r, agent)):
            who, text = move_cells(r["instance"], m)
            sh.line(x["item"], who, text, shade="agent1" if m["by"] == agent else None, extra={4: v} if v is not None else None)
        sh.answers(x["item"], "answer", f"▶ {name} 是否遵守了上面每一条规则？不遵守的话，违反了第几条？", {5: "yn", 6: None}, example[:2] if example else None, example[2] if example else None)
        sh.gap()

    def block_c(sh, x, example=None):
        g = x["what"]
        url = TICKETS.format(source=g["source"])
        row = sh.line(x["item"], "tickets", url, shade="rule")
        sh.ws.cell(row, 3).hyperlink = url
        sh.ws.cell(row, 3).font = font(color="0563C1", underline="single")
        sh.line(x["item"], "code", f"Repository: {g['repo']}\nColliding code: {g['artifact']}", shade="rule")
        for part, text in extraction_rows(g):
            sh.line(x["item"], part, text)
        sh.answers(x["item"], "answer", "▶ 对照两张 ticket 判断这四项（yes / partly / no）", {4: "ypn", 5: "ypn", 6: "ypn", 7: "ypn"}, example[:4] if example else None, example[4] if example else None)
        sh.gap()

    guide = (OUT / "GUIDE_zh.md").read_text().split("\n")
    (OUT / "workbooks").mkdir(parents=True, exist_ok=True)
    for rater in RATERS:
        wb = Workbook()
        ws = wb.active
        ws.title = "README"
        ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 125, 24
        ws["A1"], ws["A2"] = f"annotator: {rater}", "你的姓名（填在右边黄色格子里）→"
        ws["A1"].font, ws["A2"].font, ws["B2"].fill, ws["B2"].border = font(bold=True, size=12), font(bold=True), fill["answer"], box
        ws["A2"].alignment = Alignment(horizontal="right")
        for n, text in enumerate(guide, 4):
            size, bold = (14, True) if text.startswith("# ") else (12, True) if text.startswith("## ") else (10, text.startswith("**"))
            text = text.lstrip("# ").replace("**", "").replace("`", "")
            text = "    • " + text[2:] if text.startswith("- ") else text
            c = ws.cell(n, 1, text or None)
            c.font, c.alignment = font(size=size, bold=bold), wrap
            ws.row_dimensions[n].height = (size + 3.5) * lines_of(text, 112) + (8 if size > 10 else 2)

        mine = {task: [x for x in items if rater in x["raters"]] for task, items in tasks.items()}
        count = {task: len(items) for task, items in mine.items()}
        note = "备注（可空）"
        specs = {"A": ("A_style", [("item", 10, "题号"), ("move", 12, "第几步、谁"), ("text", 100, "谈判记录：方案和留言（英文原文）"), ("assertiveness", 15, "强硬度\n0 / 1 / 2"), ("cooperativeness", 16.5, "合作度\n0 / 1 / 2"),
                                   ("asks_needs", 15, "问过对方需求？\nyes / no"), ("discloses_priorities", 19.5, "说过自己的优先级？\nyes / no"), ("note", 32, note)], block_a, EXAMPLE_A[1]),
                 "B": ("B_compliance", [("item", 10, "题号"), ("move", 12, "第几步、谁"), ("text", 105, "规则、事实、谈判记录（英文原文）"), ("worth", 13, "这个方案对规则方值多少分"), ("followed", 13, "全部遵守了？\nyes / no"),
                                        ("broken_clauses", 16, "违反第几条\n如 2 或 1,3"), ("note", 34, note)], block_b, EXAMPLE_B[1]),
                 "C": ("C_extraction", [("item", 10, "题号"), ("part", 12, "内容"), ("text", 95, "抽取出来的结构（英文原文）"), ("q1_decisions", 18, "这些决策是两张 ticket 真正冲突的设计选择？"), ("q2_needed_options", 18, "标给 A、B 的选项是各自 ticket 需要的？"),
                                        ("q3_middle_option", 18, "剩下的选项是可行的折中？"), ("q4_importance", 18, "重要性 (1-5) 合理？"), ("note", 32, note)], block_c, EXAMPLE_C)}
        for task, (name, columns, block, example) in specs.items():
            sh = Sheet(wb, name, columns)
            sh.check("score", ("0", "1", "2"), "请填 0、1 或 2")
            sh.check("yn", ("yes", "no"), "请填 yes 或 no")
            sh.check("ypn", ("yes", "partly", "no"), "请填 yes、partly 或 no")
            sh.banner("示范（已经填好，不用动）。只填黄色格子；规则见 README。")
            block(sh, tasks[task][0], example)
            sh.banner(f"题目：{count[task]} 题。请独立完成，不要和别人讨论具体题目，也不要让 AI 代为判断。")
            for x in mine[task]:
                block(sh, x)
        wb.save(OUT / "workbooks" / f"annotator_{rater}.xlsx")
    return count


def main():
    tasks, keys, assignment, configs = build(random.Random(SEED))
    write_files(tasks, keys, assignment, configs)
    write_web(tasks, configs)
    print("items: " + ", ".join(f"task {t} {len(items) - 1}" for t, items in tasks.items()))
    try:
        count = write_workbooks(tasks, configs)
        print(f"wrote {len(RATERS)} workbooks under {OUT / 'workbooks'}: " + ", ".join(f"task {t} {n}" for t, n in count.items()) + " each")
    except ImportError:
        print("openpyxl is not installed: item files, assignment and keys written, workbooks skipped (pip install openpyxl)")


if __name__ == "__main__":
    main()
