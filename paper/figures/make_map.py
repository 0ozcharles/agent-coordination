"""Draw the condition map as TikZ from results/bargain_e2_map.json (run from the repository root).

Writes paper/figures/e2_map.tex: conditions down, readings x lambda across, each cell filled with the best
strategy's colour. The colours (stratneutral, ...) are defined in main.tex.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
m = json.loads((ROOT / "results" / "bargain_e2_map.json").read_text())
conds = list(m["conditions"])
labels = {"base": "base (E1 regime)", "urgent": "urgent ($T{=}4$)", "time_cost": "time cost (3 pts/move)",
          "tight_budget": "reply budget (250 tok.)", "distributive": "distributive ($a{=}0$)",
          "integrative": "integrative ($a{=}1$)", "all_contested": "all contested ($p{=}1$)",
          "low_stakes_p2": "unequal stakes (1 / 0.3)", "power_p1": "unequal power (45 / 15)",
          "open_info": "open information"}
readings = [("symmetric", "symmetric"), ("vs_neutral", "against neutral"), ("vs_competing", "against competing")]
lams = ["0", "5", "20"]
abbr = {"neutral": "neu.", "competing": "cmpt.", "collaborating": "coll.", "compromising": "cmpr.",
        "accommodating": "acc.", "avoiding": "avd."}
cw, ch, gap = 1.0, 0.4, 0.3  # cm
out = [r"\begin{tikzpicture}[x=1cm,y=1cm,every node/.style={font=\footnotesize,inner sep=1pt}]"]
for g, (key, title) in enumerate(readings):
    x0 = g * (3 * cw + gap)
    out.append(rf"\node[anchor=south,font=\footnotesize\bfseries] at ({x0 + 1.5 * cw:.2f},{0.42:.2f}) {{{title}}};")
    for k, l in enumerate(lams):
        out.append(rf"\node[anchor=south] at ({x0 + (k + 0.5) * cw:.2f},{0.05:.2f}) {{$\lambda{{=}}{l}$}};")
for i, c in enumerate(conds):
    y = -(i + 1) * ch
    out.append(rf"\node[anchor=east] at (-0.1,{y + ch / 2:.2f}) {{{labels.get(c, c)}}};")
    for g, (key, _) in enumerate(readings):
        x0 = g * (3 * cw + gap)
        for k, l in enumerate(lams):
            best = m["conditions"][c]["readings"][key]["both"]["best"].get(l)
            x = x0 + k * cw
            if best:
                out.append(rf"\fill[strat{best}] ({x + 0.02:.2f},{y + 0.02:.2f}) rectangle ({x + cw - 0.02:.2f},{y + ch - 0.02:.2f});")
                out.append(rf"\node[text=white] at ({x + cw / 2:.2f},{y + ch / 2:.2f}) {{{abbr[best]}}};")
            else:
                out.append(rf"\draw[gray!50] ({x + 0.02:.2f},{y + 0.02:.2f}) rectangle ({x + cw - 0.02:.2f},{y + ch - 0.02:.2f});")
# legend
ly = -(len(conds) + 1) * ch - 0.1
lx = 0.0
for s in ["neutral", "competing", "collaborating", "compromising", "accommodating", "avoiding"]:
    out.append(rf"\fill[strat{s}] ({lx:.2f},{ly:.2f}) rectangle ({lx + 0.25:.2f},{ly + 0.22:.2f});")
    out.append(rf"\node[anchor=west] at ({lx + 0.27:.2f},{ly + 0.11:.2f}) {{{s}}};")
    lx += 0.3 + 0.16 * len(s) + 0.3
out.append(r"\end{tikzpicture}")
(ROOT / "paper" / "figures" / "e2_map.tex").write_text("\n".join(out) + "\n")
print(f"wrote paper/figures/e2_map.tex: {len(conds)} conditions")
