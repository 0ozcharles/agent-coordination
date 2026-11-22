"""Style labels of the default agents and of rule-following agents, per study. Writes results/bargain_labels_summary.json.

Run from the repository root; reads the game records under runs/bargain/ (not in the repository, see scripts/analysis/README.md).
"""
import json, glob, collections
from pathlib import Path
STYLES = ["competing", "collaborating", "compromising", "accommodating", "avoiding"]
STUDIES = {"E1 (luna 0.7)": "e1", "E2 base (luna)": "e2/base", "E4 software_design": "e4_software_design", "E4 team_plan": "e4_team_plan", "E4 release_plan": "e4_release_plan",
           "grounded (luna)": "grounded", "E3 luna default-T": "e3_gpt_6_luna_t1", "E3 haiku": "e3_claude_haiku_5_5", "E3 sonnet": "e3_claude_sonnet_5_5", "E3 sol": "e3_gpt_6_1_sol"}
out = {}
print(f"{'study':22} {'labeled':>8} | default agents: style distribution (both seats in neutral-neutral) | rule agents labeled in their own style, per rule (seat with the rule, against neutral and in its pair)")
for name, run in STUDIES.items():
    recs = [json.load(open(f)) for f in glob.glob(f"runs/bargain/{run}/games/*/*.json")]
    recs = [r for r in recs if not r.get("error") and (r.get("label") or {}).get("label")]
    if not recs:
        continue
    neutral = collections.Counter(); n_neutral = 0; asks = disc = 0
    hit = {s: [0, 0] for s in STYLES}
    for r in recs:
        lab = r["label"]["label"]
        for a, key in (("p1", "agent1"), ("p2", "agent2")):
            s = r["strategies"][a]
            if r["cell"] == "neutral-neutral":
                neutral[lab[key]["style"]] += 1; n_neutral += 1; asks += bool(lab[key].get("asks_needs")); disc += bool(lab[key].get("discloses_priorities"))
            elif s in STYLES and r.get("ops", {}).get(a, "rules") == "rules" and (r["strategies"]["p1" if a == "p2" else "p2"] in ("neutral", s)):
                hit[s][1] += 1; hit[s][0] += lab[key]["style"] == s
    dist = {s: round(100 * neutral[s] / n_neutral) for s in STYLES} if n_neutral else {}
    out[name] = {"labeled": len(recs), "default_style_pct": dist, "default_n": n_neutral, "default_asks_pct": round(100 * asks / n_neutral) if n_neutral else None,
                 "default_discloses_pct": round(100 * disc / n_neutral) if n_neutral else None, "rule_in_own_style": hit}
    print(f"{name:22} {len(recs):>8} | " + " ".join(f"{s[:5]} {dist.get(s, 0):>3}%" for s in STYLES) + f" (n={n_neutral}, asks {out[name]['default_asks_pct']}%, discloses {out[name]['default_discloses_pct']}%) | "
          + " ".join(f"{s[:5]} {hit[s][0]}/{hit[s][1]}" for s in STYLES))
Path("results/bargain_labels_summary.json").write_text(json.dumps(out, indent=1)); print("wrote results/bargain_labels_summary.json")
