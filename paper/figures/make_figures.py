"""Draw the paper's data figures as TikZ from the committed results (run from the repository root).

Writes paper/figures/generated.tex, which defines one macro per figure (\FigOverview, \FigMatrix, \FigReward,
\FigCost, \FigGap) and is \input once in the preamble of main.tex; and paper/figures/settings.json, the
per-setting statistics behind \FigGap and the transfer table. The strategy colours (stratneutral, ...) are
defined in main.tex. Needs no API key: everything is read from results/*.csv and results/*.json.
"""
import csv
import json
import math
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "results"
STYLES = ["competing", "collaborating", "compromising", "accommodating", "avoiding"]
STRATS = ["neutral"] + STYLES
ABBR = {"neutral": "neu.", "competing": "cmpt.", "collaborating": "coll.", "compromising": "cmpr.",
        "accommodating": "acc.", "avoiding": "avd."}
NUM = ("payoff_p1", "payoff_p2", "joint", "max_joint", "joint_gap", "p1_tokens", "p2_tokens", "moves")


def rows(study):
    out = {}
    with open(RES / f"bargain_{study}_rows.csv") as handle:
        for r in csv.DictReader(handle):
            if r["error"] == "True":
                continue
            for k in NUM:
                r[k] = float(r[k]) if r.get(k) not in (None, "") else None
            r["agreement"] = r["agreement"] == "True"
            r["on_frontier"] = r["on_frontier"] == "True"
            out[(r["cell"], int(r["instance"]))] = r
    return out


def sign_p(h, l):
    n = h + l
    if n == 0:
        return 1.0
    k = min(h, l)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def paired(d):
    d = [x for x in d if x is not None]
    if not d:
        return None
    h, l = sum(v > 0 for v in d), sum(v < 0 for v in d)
    return {"mean": st.mean(d), "wins": h, "losses": l, "n": len(d), "p": sign_p(h, l)}


def seat_max(study, seat):
    if "low_stakes_p2" in study and seat == "p2":
        return 30.0
    if study == "casino":
        return 36.0
    return 100.0


def other(a):
    return "p2" if a == "p1" else "p1"


def alone(R, study, s, seats=("p1", "p2")):
    """Own gain and partner's change (percent of the seat's maximum, and points) of s adopted alone against neutral."""
    own, part, own_pts, joint = [], [], [], []
    ids = sorted({i for (c, i) in R if c == "neutral-neutral"})
    for a in seats:
        cell = f"{s}-neutral" if a == "p1" else f"neutral-{s}"
        for i in ids:
            r, nn = R.get((cell, i)), R.get(("neutral-neutral", i))
            if r is None or nn is None:
                continue
            d = r[f"payoff_{a}"] - nn[f"payoff_{a}"]
            own_pts.append(d)
            own.append(100 * d / seat_max(study, a))
            part.append(100 * (r[f"payoff_{other(a)}"] - nn[f"payoff_{other(a)}"]) / seat_max(study, other(a)))
            joint.append(r["joint"] - nn["joint"])
    return {"own": paired(own), "partner": paired(part), "own_points": paired(own_pts), "joint": paired(joint)}


def setting(label, layer, study, model="luna"):
    R = rows(study)
    ids = sorted({i for (c, i) in R if c == "neutral-neutral"})
    nn = [R[("neutral-neutral", i)] for i in ids]
    maxj = st.mean(r["max_joint"] for r in nn)
    out = {"label": label, "layer": layer, "study": study, "model": model, "n": len(nn),
           "gap_pct": 100 * st.mean(r["joint_gap"] for r in nn) / maxj, "frontier": sum(r["on_frontier"] for r in nn),
           "joint": st.mean(r["joint"] for r in nn), "max_joint": maxj, "sym": {}, "alone": {}}
    for s in STYLES:
        d = [R[(f"{s}-{s}", i)]["joint"] - R[("neutral-neutral", i)]["joint"] for i in ids if (f"{s}-{s}", i) in R]
        p = paired(d)
        if p:
            out["sym"][s] = {**p, "pct": 100 * p["mean"] / maxj,
                             "agree": sum(R[(f"{s}-{s}", i)]["agreement"] for i in ids if (f"{s}-{s}", i) in R)}
        a = alone(R, study, s)
        if a["own"]:
            out["alone"][s] = a
    return out


def cross_setting(adopter):
    """A luna agent and a sonnet agent: the adopter's model follows the rule, the other is the default agent."""
    arms = {"e3_cross_luna_sonnet": {"luna": "p1", "sonnet": "p2"}, "e3_cross_sonnet_luna": {"sonnet": "p1", "luna": "p2"}}
    partner = "sonnet" if adopter == "luna" else "luna"
    out = {"label": f"{adopter} adopts, {partner} partner", "layer": "cross", "study": "e3_cross", "model": adopter,
           "sym": {}, "alone": {}}
    gaps, maxj, n = [], [], 0
    acc = {s: {"own": [], "partner": [], "joint": []} for s in STYLES}
    for study, seats in arms.items():
        R = rows(study)
        ids = sorted({i for (c, i) in R if c == "neutral-neutral"})
        gaps += [R[("neutral-neutral", i)]["joint_gap"] for i in ids]
        maxj += [R[("neutral-neutral", i)]["max_joint"] for i in ids]
        a = seats[adopter]
        for s in STYLES:
            cell = f"{s}-neutral" if a == "p1" else f"neutral-{s}"
            for i in ids:
                r, nn = R.get((cell, i)), R[("neutral-neutral", i)]
                if r is None:
                    continue
                acc[s]["own"].append(r[f"payoff_{a}"] - nn[f"payoff_{a}"])
                acc[s]["partner"].append(r[f"payoff_{other(a)}"] - nn[f"payoff_{other(a)}"])
                acc[s]["joint"].append(r["joint"] - nn["joint"])
    out.update({"n": len(gaps), "gap_pct": 100 * st.mean(gaps) / st.mean(maxj), "max_joint": st.mean(maxj)})
    for s in STYLES:
        if acc[s]["own"]:
            out["alone"][s] = {"own": paired(acc[s]["own"]), "partner": paired(acc[s]["partner"]),
                               "own_points": paired(acc[s]["own"]), "joint": paired(acc[s]["joint"])}
    return out


E2 = [("base", "base (E1 regime)", None), ("urgent", "urgent ($T{=}4$)", "competing"),
      ("time_cost", "time cost (3 pts/move)", "compromising"), ("tight_budget", "reply budget (250 tok.)", "avoiding"),
      ("distributive", "nothing to trade ($a{=}0$)", "compromising"), ("integrative", "all to trade ($a{=}1$)", "collaborating"),
      ("all_contested", "all contested ($p{=}1$)", None), ("low_stakes_p2", "unequal stakes (1 / 0.3)", "accommodating"),
      ("power_p1", "unequal power (45 / 15)", None), ("open_info", "open information", None)]


def settings():
    out = [setting("E1 core", "core", "e1")]
    out += [setting(f"E2 {c}", "core", f"e2_{c}") for c, _, _ in E2]
    out += [setting(f"skin {k}", "skin", f"e4_{k}") for k in ("software_design", "team_plan", "release_plan")]
    out += [setting("grounded, tickets", "grounded", "grounded"), setting("grounded, abstract", "grounded", "grounded_abstract"),
            setting("grounded, tickets (haiku)", "grounded", "grounded_claude_haiku_5_5", "haiku")]
    out += [setting("luna, default temp.", "model", "e3_gpt_6_luna_t1"), setting("haiku", "model", "e3_claude_haiku_5_5", "haiku"),
            setting("sonnet", "model", "e3_claude_sonnet_5_5", "sonnet"), setting("sol", "model", "e3_gpt_6_1_sol", "sol"),
            setting("sonnet, open info", "model", "e3_open_info_claude_sonnet_5_5", "sonnet"),
            setting("sol, open info", "model", "e3_open_info_gpt_6_1_sol", "sol"),
            setting("sonnet, unequal stakes", "model", "e3_low_stakes_p2_claude_sonnet_5_5", "sonnet"),
            setting("sol, unequal stakes", "model", "e3_low_stakes_p2_gpt_6_1_sol", "sol")]
    out += [cross_setting("luna"), cross_setting("sonnet")]
    out += [setting("CaSiNo", "benchmark", "casino"), setting("LLM-Deliberation", "benchmark", "deliberation")]
    return out


# ---------------------------------------------------------------- TikZ helpers

def num(v, bold=False, digits=1):
    s = ("+" if v > 0 else r"\mn" if v < 0 else "") + f"{abs(v):.{digits}f}"
    if abs(v) < 0.05:
        s = "0.0"
    return rf"\textbf{{{s}}}" if bold else s


def shade(v, full, pos="hpos", neg="hneg", cap=62):
    pct = min(cap, round(cap * abs(v) / full))
    return "white" if pct < 3 else f"{pos if v > 0 else neg}!{pct}!white"


HEADER = r"""% Generated by paper/figures/make_figures.py from results/ (run from the repository root). Do not edit by hand.
\definecolor{hpos}{HTML}{2166AC}\definecolor{hneg}{HTML}{C0392B}\definecolor{hseq}{HTML}{2980B9}\definecolor{hjoint}{HTML}{27AE60}
\tikzset{hcell/.style={minimum width=#1,minimum height=0.40cm,inner sep=0pt,outer sep=0pt,font=\scriptsize},hcell/.default=0.74cm}
\newcommand{\hc}[4]{\node[hcell,fill=#3] at (#1,#2) {#4};}
\newcommand{\hm}[4]{\node[hcell=0.92cm,fill=#3] at (#1,#2) {#4};}
\newcommand{\mn}{$-$}
\newcommand{\mkc}[3]{\fill[#3] (#1,#2) circle (0.075);}
\newcommand{\mks}[3]{\fill[#3,shift={(#1,#2)}] (-0.07,-0.07) rectangle (0.07,0.07);}
\newcommand{\mkt}[3]{\fill[#3,shift={(#1,#2)}] (0,0.1) -- (-0.09,-0.06) -- (0.09,-0.06) -- cycle;}
\newcommand{\mkd}[3]{\fill[#3,shift={(#1,#2)}] (0,0.105) -- (-0.085,0) -- (0,-0.105) -- (0.085,0) -- cycle;}
\newcommand{\mkx}[3]{\draw[#3,line width=1.1pt,shift={(#1,#2)}] (-0.075,-0.075) -- (0.075,0.075) (-0.075,0.075) -- (0.075,-0.075);}
\newcommand{\mkp}[3]{\draw[#3,line width=1.1pt,shift={(#1,#2)}] (0,-0.095) -- (0,0.095) (-0.095,0) -- (0.095,0);}
\newcommand{\mko}[3]{\draw[#3,line width=0.8pt,fill=white] (#1,#2) circle (0.075);}
\newcommand{\mkq}[3]{\draw[#3,line width=0.8pt,fill=white,shift={(#1,#2)}] (-0.07,-0.07) rectangle (0.07,0.07);}
"""


# ---------------------------------------------------------------- figure: the strategy matrix (E1)

def e1_matrix():
    R = rows("e1")
    ids = sorted({i for (c, i) in R if c == "neutral-neutral"})
    own = {s: {} for s in STRATS}
    joint = {s: {} for s in STRATS}
    tokens = {s: {} for s in STRATS}
    for s in STRATS:
        for t in STRATS:
            o, j, k = [], [], []
            for i in ids:
                a, b = R.get((f"{s}-{t}", i)), R.get((f"{t}-{s}", i))
                if a:
                    o.append(a["payoff_p1"]); j.append(a["joint"]); k.append(a["p1_tokens"])
                if b:
                    o.append(b["payoff_p2"]); j.append(b["joint"]); k.append(b["p2_tokens"])
            own[s][t], joint[s][t], tokens[s][t] = st.mean(o), st.mean(j), st.mean(k)
    return own, joint, tokens


def fig_matrix(own, joint):
    cw, ch = 0.92, 0.40
    out = [r"\begin{tikzpicture}[x=1cm,y=1cm,every node/.style={font=\scriptsize,inner sep=1pt}]"]
    blocks = [(own, "own payoff of the row strategy", "hseq", 36, 80), (joint, "joint payoff of the pair", "hjoint", 95, 139)]
    for g, (M, title, col, lo, hi) in enumerate(blocks):
        x0 = g * (6 * cw + 0.55)
        out.append(rf"\node[anchor=south,font=\scriptsize\bfseries] at ({x0 + 3 * cw:.2f},0.42) {{{title}}};")
        for k, t in enumerate(STRATS):
            out.append(rf"\node[anchor=south] at ({x0 + (k + 0.5) * cw:.2f},0.04) {{{ABBR[t]}}};")
        for i, s in enumerate(STRATS):
            y = -(i + 0.5) * ch
            if g == 0:
                out.append(rf"\node[anchor=east] at (-0.08,{y:.2f}) {{{s}}};")
            for k, t in enumerate(STRATS):
                v = M[s][t]
                pct = max(0, min(70, round(70 * (v - lo) / (hi - lo))))
                out.append(rf"\hm{{{x0 + (k + 0.5) * cw:.2f}}}{{{y:.2f}}}{{{col}!{pct}!white}}{{{v:.1f}}}")
        for k, t in enumerate(STRATS):  # frame the best response to each column (own) or the best pair in each column (joint)
            best = max(STRATS, key=lambda s: M[s][t])
            i = STRATS.index(best)
            out.append(rf"\draw[line width=0.9pt] ({x0 + k * cw + 0.02:.2f},{-(i + 1) * ch + 0.02:.2f}) rectangle "
                       rf"({x0 + (k + 1) * cw - 0.02:.2f},{-i * ch - 0.02:.2f});")
    out.append(r"\end{tikzpicture}")
    return "\n".join(out)


# ---------------------------------------------------------------- figure: reward by condition (E2)

def e2_readings(study):
    """Per strategy and reading: mean own payoff (seats pooled) and the paired gain over neutral in the same reading."""
    R = rows(study)
    ids = sorted({i for (c, i) in R if c == "neutral-neutral"})
    out = {}
    def cellseat(s, reading, a):
        if reading == "symmetric":
            return f"{s}-{s}"
        t = "neutral" if reading == "vs_neutral" else "competing"
        return f"{s}-{t}" if a == "p1" else f"{t}-{s}"
    for reading in ("symmetric", "vs_neutral", "vs_competing"):
        out[reading] = {}
        for s in STRATS:
            vals, diffs = [], []
            for a in ("p1", "p2"):
                for i in ids:
                    r, base = R.get((cellseat(s, reading, a), i)), R.get((cellseat("neutral", reading, a), i))
                    if r is None or base is None:
                        continue
                    vals.append(r[f"payoff_{a}"]); diffs.append(r[f"payoff_{a}"] - base[f"payoff_{a}"])
            out[reading][s] = {"payoff": st.mean(vals), "gain": paired(diffs)}
    return out


def fig_reward():
    cw, ch, gap = 0.74, 0.40, 0.30
    titles = [("symmetric", "both sides use it"), ("vs_neutral", "adopted alone, partner neutral"), ("vs_competing", "against a competing partner")]
    out = [r"\begin{tikzpicture}[x=1cm,y=1cm,every node/.style={font=\scriptsize,inner sep=1pt}]"]
    data = {c: e2_readings(f"e2_{c}") for c, _, _ in E2}
    for g, (key, title) in enumerate(titles):
        x0 = g * (6 * cw + gap)
        out.append(rf"\node[anchor=south,font=\scriptsize\bfseries] at ({x0 + 3 * cw:.2f},0.42) {{{title}}};")
        for k, s in enumerate(STRATS):
            out.append(rf"\node[anchor=south,text=strat{s}] at ({x0 + (k + 0.5) * cw:.2f},0.04) {{{ABBR[s]}}};")
    for i, (c, label, textbook) in enumerate(E2):
        y = -(i + 0.5) * ch
        out.append(rf"\node[anchor=east] at (-0.08,{y:.2f}) {{{label}}};")
        for g, (key, _) in enumerate(titles):
            x0 = g * (6 * cw + gap)
            d = data[c][key]
            out.append(rf"\node[text=black!55] at ({x0 + 0.5 * cw:.2f},{y:.2f}) {{{d['neutral']['payoff']:.1f}}};")
            best = max(STYLES, key=lambda s: d[s]["gain"]["mean"])
            for k, s in enumerate(STYLES, start=1):
                gn = d[s]["gain"]
                out.append(rf"\hc{{{x0 + (k + 0.5) * cw:.2f}}}{{{y:.2f}}}{{{shade(gn['mean'], 15)}}}{{{num(gn['mean'], gn['p'] < 0.05)}}}")
            if textbook:
                k = STRATS.index(textbook)
                out.append(rf"\draw[line width=0.9pt] ({x0 + k * cw + 0.02:.2f},{y - ch / 2 + 0.02:.2f}) rectangle "
                           rf"({x0 + (k + 1) * cw - 0.02:.2f},{y + ch / 2 - 0.02:.2f});")
    out.append(r"\end{tikzpicture}")
    return "\n".join(out), data


# ---------------------------------------------------------------- figure: overview (TKI grid and the payoff plane)

def fig_overview(own):
    out = [r"\begin{tikzpicture}[x=1cm,y=1cm,every node/.style={font=\scriptsize,inner sep=1pt}]"]
    # (a) the TKI grid
    out.append(r"\draw[-stealth,black!70] (0,0) -- (3.6,0); \draw[-stealth,black!70] (0,0) -- (0,3.35);")
    out.append(r"\node[anchor=north] at (1.8,-0.08) {cooperativeness};")
    out.append(r"\node[anchor=south,rotate=90] at (-0.08,1.65) {assertiveness};")
    for s, (x, y) in {"competing": (0.9, 2.85), "collaborating": (2.68, 2.85), "compromising": (1.8, 1.68),
                      "avoiding": (0.9, 0.5), "accommodating": (2.68, 0.5)}.items():
        out.append(rf"\node[fill=strat{s},text=white,rounded corners=1.5pt,inner sep=2.2pt] at ({x},{y}) {{{s}}};")
    out.append(r"\node[anchor=south,font=\scriptsize\bfseries] at (1.8,3.4) {(a) five strategies};")
    # (b) the payoff plane: what each rule does to the default outcome when one side adopts it
    x0, y0, lo_x, lo_y, span, side = 4.75, 0.0, 38.0, 46.0, 40.0, 3.05
    sc = side / span
    def X(v): return x0 + (v - lo_x) * sc
    def Y(v): return y0 + (v - lo_y) * sc
    out.append(rf"\draw[black!70] ({X(lo_x):.2f},{Y(lo_y):.2f}) rectangle ({X(lo_x + span):.2f},{Y(lo_y + span):.2f});")
    for v in (40, 50, 60, 70):
        out.append(rf"\draw[black!70] ({X(v):.2f},{Y(lo_y):.2f}) -- ++(0,-0.05) node[anchor=north] {{{v}}};")
    for v in (50, 60, 70, 80):
        out.append(rf"\draw[black!70] ({X(lo_x):.2f},{Y(v):.2f}) -- ++(-0.05,0) node[anchor=east] {{{v}}};")
    n = own["neutral"]["neutral"]
    j = 2 * n  # the default pair's joint payoff: the dashed line holds it constant
    out.append(rf"\draw[dashed,black!45] ({X(j - (lo_y + span)):.2f},{Y(lo_y + span):.2f}) -- ({X(lo_x + span):.2f},{Y(j - (lo_x + span)):.2f});")
    out.append(rf"\node[anchor=north east,text=black!55,font=\tiny,inner sep=2pt] at ({X(lo_x + span):.2f},{Y(lo_y + span):.2f}) {{joint payoff up}};")
    place = {"competing": "north east", "collaborating": "south", "compromising": "south east", "accommodating": "west", "avoiding": "north"}
    for s in STYLES:
        ox, px = own[s]["neutral"], own["neutral"][s]
        out.append(rf"\draw[-stealth,line width=0.9pt,strat{s}] ({X(n):.2f},{Y(n):.2f}) -- ({X(ox):.2f},{Y(px):.2f}) "
                   rf"node[anchor={place[s]},inner sep=2pt] {{{ABBR[s]}}};")
    out.append(rf"\fill ({X(n):.2f},{Y(n):.2f}) circle (0.055) node[anchor=north west,inner sep=2pt] {{neu.}};")
    out.append(rf"\node[anchor=north] at ({X(lo_x + span / 2):.2f},{Y(lo_y) - 0.3:.2f}) {{own payoff}};")
    out.append(rf"\node[anchor=south,rotate=90] at ({X(lo_x) - 0.42:.2f},{Y(lo_y + span / 2):.2f}) {{partner's payoff}};")
    out.append(rf"\node[anchor=south,font=\scriptsize\bfseries] at ({X(lo_x + span / 2):.2f},3.4) {{(b) adopted alone}};")
    out.append(r"\end{tikzpicture}")
    return "\n".join(out)


# ---------------------------------------------------------------- figure: net payoff against the price of tokens (E1)

def fig_cost(own, tokens):
    out = [r"\begin{tikzpicture}[x=1cm,y=1cm,every node/.style={font=\scriptsize,inner sep=1pt}]"]
    W, H, lo, hi = 3.1, 2.8, 20.0, 76.0
    info = {}
    panels = [("(a) both sides use it", lambda s: (own[s][s], tokens[s][s])),
              ("(b) adopted alone", lambda s: (own[s]["neutral"], tokens[s]["neutral"]))]
    for g, (title, get) in enumerate(panels):
        x0 = g * (W + 0.95)
        def X(l): return x0 + W * l / 20.0
        def Y(v): return H * (v - lo) / (hi - lo)
        out.append(rf"\draw[black!70] ({x0:.2f},0) rectangle ({x0 + W:.2f},{H:.2f});")
        for l in (0, 5, 10, 20):
            out.append(rf"\draw[black!70] ({X(l):.2f},0) -- ++(0,-0.05) node[anchor=north] {{{l}}};")
        for v in (20, 40, 60):
            out.append(rf"\draw[black!70] ({x0:.2f},{Y(v):.2f}) -- ++(-0.05,0) node[anchor=east] {{{v}}};")
        out.append(rf"\begin{{scope}}\clip ({x0:.2f},0) rectangle ({x0 + W:.2f},{H:.2f});")
        lines = {s: get(s) for s in STRATS}
        for s in STRATS:
            p, t = lines[s]
            out.append(rf"\draw[line width=0.9pt,strat{s}] ({X(0):.2f},{Y(p):.2f}) -- ({X(20):.2f},{Y(p - 20 * t / 1000):.2f});")
        out.append(r"\end{scope}")
        # where the best strategy changes hands along the upper envelope
        switches, lam, best = [], 0.0, max(STRATS, key=lambda s: lines[s][0])
        first = best
        while lam < 20:
            nxt = None
            for s in STRATS:
                if s == best or lines[s][1] >= lines[best][1]:
                    continue
                x = 1000 * (lines[best][0] - lines[s][0]) / (lines[best][1] - lines[s][1])
                if lam < x <= 20 and (nxt is None or x < nxt[0]):
                    nxt = (x, s)
            if nxt is None:
                break
            lam, best = nxt
            switches.append((lam, best))
            p, t = lines[best]
            out.append(rf"\fill ({X(lam):.2f},{Y(p - lam * t / 1000):.2f}) circle (0.05);")
        info[title] = {"best_at_0": first, "switches": [(round(l, 1), s) for l, s in switches],
                       "lines": {s: (round(lines[s][0], 1), round(lines[s][1])) for s in STRATS}}
        out.append(rf"\node[anchor=south,font=\scriptsize\bfseries] at ({x0 + W / 2:.2f},{H + 0.05:.2f}) {{{title}}};")
        out.append(rf"\node[anchor=north] at ({x0 + W / 2:.2f},-0.32) {{price of tokens $\lambda$}};")
    out.append(rf"\node[anchor=south,rotate=90] at (-0.5,{H / 2:.2f}) {{net payoff}};")
    lx = -0.35
    for s in STRATS:
        out.append(rf"\draw[line width=1.2pt,strat{s}] ({lx:.2f},-0.95) -- ++(0.28,0) node[anchor=west,text=black] {{{ABBR[s]}}};")
        lx += 1.2
    out.append(r"\end{tikzpicture}")
    return "\n".join(out), info


# ---------------------------------------------------------------- figure: the default pair's gap against collaborating's gain

TALK = ("e2_urgent", "e2_time_cost", "e2_tight_budget")  # conditions that take away the moves or tokens the rule needs


def outside(s):
    """'talk' for the three conditions that restrict talk, 'stakes' for unequal stakes, None for the standard protocol."""
    if s["study"] in TALK:
        return "talk"
    return "stakes" if "low_stakes_p2" in s["study"] else None


def fit(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = st.mean(xs), st.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return {"r": r, "slope": slope, "intercept": my - slope * mx, "n": len(pts)}


def fig_gap(S):
    pts = [(s["gap_pct"], s["alone"]["collaborating"]["own"]["mean"], s) for s in S if "collaborating" in s["alone"]]
    std = [p for p in pts if outside(p[2]) is None]
    f_std, f_all = fit(std), fit(pts)
    W, H, xhi, ylo, yhi = 6.5, 3.9, 30.0, -6.0, 10.0
    def X(v): return W * v / xhi
    def Y(v): return H * (v - ylo) / (yhi - ylo)
    out = [r"\begin{tikzpicture}[x=1cm,y=1cm,every node/.style={font=\scriptsize,inner sep=1pt}]"]
    out.append(rf"\draw[black!70] (0,0) rectangle ({W:.2f},{H:.2f});")
    for v in (0, 10, 20, 30):
        out.append(rf"\draw[black!70] ({X(v):.2f},0) -- ++(0,-0.05) node[anchor=north] {{{v}}};")
    for v in (-5, 0, 5, 10):
        out.append(rf"\draw[black!70] (0,{Y(v):.2f}) -- ++(-0.05,0) node[anchor=east] {{{num(v, digits=0) if v else '0'}}};")
    out.append(rf"\draw[dashed,black!40] (0,{Y(0):.2f}) -- ({W:.2f},{Y(0):.2f});")
    x1 = max(p[0] for p in std) + 1.5
    out.append(rf"\draw[black!55,line width=0.7pt] ({X(0):.2f},{Y(f_std['intercept']):.2f}) -- ({X(x1):.2f},{Y(f_std['intercept'] + f_std['slope'] * x1):.2f});")
    mark = {"core": ("mkc", "stratcollaborating"), "skin": ("mks", "stratcompromising"), "grounded": ("mkt", "strataccommodating"),
            "model": ("mkd", "stratcompeting"), "cross": ("mkx", "black"), "benchmark": ("mkp", "black!60")}
    for x, y, s in pts:
        m, col = {"talk": ("mko", "black!70"), "stakes": ("mkq", "black!70")}.get(outside(s), mark[s["layer"]])
        out.append(rf"\{m}{{{X(x):.2f}}}{{{Y(y):.2f}}}{{{col}}}")
    names = [("mkc", "stratcollaborating", "core, luna"), ("mks", "stratcompromising", "skins"), ("mkt", "strataccommodating", "grounded"),
             ("mkd", "stratcompeting", "other models"), ("mkx", "black", "cross-model"), ("mkp", "black!60", "benchmarks"),
             ("mko", "black!70", "talk restricted"), ("mkq", "black!70", "unequal stakes")]
    spots = [(-0.3, 0), (2.0, 0), (4.3, 0), (-0.3, 1), (2.0, 1), (4.3, 1), (-0.3, 2), (2.0, 2)]
    for k, (m, col, name) in enumerate(names):
        lx, ly = spots[k][0], -0.82 - 0.3 * spots[k][1]
        out.append(rf"\{m}{{{lx:.2f}}}{{{ly:.2f}}}{{{col}}} \node[anchor=west] at ({lx + 0.14:.2f},{ly:.2f}) {{{name}}};")
    out.append(rf"\node[anchor=north] at ({W / 2:.2f},-0.3) {{joint value the default pair leaves (\%)}};")
    out.append(rf"\node[anchor=south,rotate=90] at (-0.62,{H / 2:.2f}) {{own gain of \textsc{{collaborating}} alone}};")
    out.append(r"\end{tikzpicture}")
    return "\n".join(out), {"standard_protocol": f_std, "all_settings": f_all,
                            "outside": [p[2]["label"] for p in pts if outside(p[2])]}


# ---------------------------------------------------------------- table: what transfers across the settings

def claims(S):
    """Per claim: settings where the direction holds, of how many, significant among them, and significant against."""
    sym = [s for s in S if s["sym"]]
    rows_, data = [], {}
    def add(key, text, values, sign):
        """values: (mean, p) per setting; sign: +1 if the claim is 'above zero', -1 if 'below zero'."""
        hold = sum(sign * m > 0 for m, p in values)
        sig = sum(sign * m > 0 and p < .05 for m, p in values)
        against = sum(sign * m < 0 and p < .05 for m, p in values)
        data[key] = {"holds": hold, "of": len(values), "significant": sig, "significant_against": against}
        rows_.append(rf"{text} & {hold}/{len(values)} & {sig} & {against} \\")
    for k, text in (("competing", r"\competing{} pairs below default pair"), ("compromising", r"\compromising{} pairs below it"),
                    ("avoiding", r"\avoiding{} pairs below it")):
        add(f"{k}_pairs_below", text, [(s["sym"][k]["pct"], s["sym"][k]["p"]) for s in sym], -1)
    for k, text in (("collaborating", r"\collaborating{} pairs above it"), ("accommodating", r"\accommodating{} pairs above it")):
        add(f"{k}_pairs_above", text, [(s["sym"][k]["pct"], s["sym"][k]["p"]) for s in sym], +1)
    rows_.append(r"\midrule")
    for k, text in (("avoiding", r"\avoiding{} alone: loses"), ("accommodating", r"\accommodating{} alone: loses"), ("compromising", r"\compromising{} alone: loses")):
        add(f"{k}_alone_loses", text, [(s["alone"][k]["own"]["mean"], s["alone"][k]["own"]["p"]) for s in S if k in s["alone"]], -1)
    v = [s["alone"]["competing"] for s in S]
    add("competing_alone_costs_partner", r"\competing{} alone: partner loses", [(x["partner"]["mean"], x["partner"]["p"]) for x in v], -1)
    add("competing_alone_gains", r"\competing{} alone: user gains", [(x["own"]["mean"], x["own"]["p"]) for x in v], +1)
    v = [s["alone"]["collaborating"] for s in S]
    add("collaborating_alone_joint", r"\collaborating{} alone: joint payoff up", [(x["joint"]["mean"], x["joint"]["p"]) for x in v], +1)
    add("collaborating_alone_gains", r"\collaborating{} alone: user gains", [(x["own"]["mean"], x["own"]["p"]) for x in v], +1)
    return "\n".join(rows_), data


def main():
    own, joint, tokens = e1_matrix()
    S = settings()
    reward, reward_data = fig_reward()
    cost, cost_info = fig_cost(own, tokens)
    gap, gap_info = fig_gap(S)
    transfer_rows, claim_data = claims(S)
    figs = {"FigOverview": fig_overview(own), "FigMatrix": fig_matrix(own, joint), "FigReward": reward, "FigCost": cost, "FigGap": gap,
            "TransferRows": transfer_rows}
    text = HEADER + "".join(f"\\newcommand{{\\{name}}}{{%\n{body}}}\n" for name, body in figs.items())
    (ROOT / "paper" / "figures" / "generated.tex").write_text(text)
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return round(o, 3) if isinstance(o, float) else o
    (ROOT / "paper" / "figures" / "settings.json").write_text(json.dumps(clean({"settings": S, "gap_fit": gap_info, "claims": claim_data, "cost": cost_info,
                                                                               "reward": reward_data}), indent=1) + "\n")
    print(f"wrote paper/figures/generated.tex ({len(text)} bytes) and settings.json; gap fit {clean(gap_info)}")
    return S, cost_info, reward_data


if __name__ == "__main__":
    main()
