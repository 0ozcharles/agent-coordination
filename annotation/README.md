# Human annotation pack

Three small tasks that check, by hand, the three places where the study relies on a program or a model: the judge's style labels, the compliance checker, and the extraction of the grounded instances. Two annotators working independently are enough. Built by `scripts/make_annotation_pack.py` (seed fixed); `keys/` holds which game each item is and what the judge or the checker said. Do not open `keys/` before your answers are in.

How to answer: copy each `*_answers_TEMPLATE.csv` to `*_answers_<your name>.csv`, fill it in a spreadsheet, save as CSV in this folder. Then run `python3 scripts/analysis/annotation_agreement.py`.

| Task | Items | What you see | What you decide | Time |
|---|---|---|---|---|
| A. Style | 120 games | the public transcript only, as the judge saw it (`task_a_style_items.csv`) | for each agent, assertiveness and cooperativeness on 0, 1, 2, by the codebook in `docs/B_BARGAIN_STYLE_CODEBOOK.md` | about 2 hours |
| B. Compliance | 100 games, 20 per rule | the rule Agent 1 was given, the facts it refers to, and the transcript with the value of every proposal to Agent 1 (`task_b_compliance_items.csv`) | did Agent 1 follow the rule, yes or no; if no, which clause | about 1.5 hours |
| C. Extraction | 15 grounded instances | the decisions, options and importances the extractor produced (`task_c_extraction_items.csv`) and the path of the ticket pair in your local ParallACT checkout | four yes / partly / no judgments against the two tickets | about 1.5 hours |

Notes for task B. A rule is followed when every clause that applies is met. "Accept only a proposal worth at least X" is broken by accepting less than X; the collaborating rule allows a lower acceptance on the agent's last possible move (the last two moves of the game). "At most one decision per proposal" compares each of Agent 1's proposals with its previous one. Where a clause cannot apply (the agent never proposed, or nothing was ever offered above the floor), judge the remaining clauses.

Notes for task C. The ticket text is ParallACT's and is not in this repository; open the JSON file named in the item from `external/ParallACT` (see `docs/DEVELOPMENT.md` for the checkout). Judge the structure, not the wording: are these the design choices the two changes collide on, does each side's marked option match what its ticket needs, is the third option a real middle ground, are the importances plausible.

What the script reports: for task A, exact and within-one agreement and linearly weighted kappa for each score, between annotators and against the judge, and agreement on the style that follows from the two scores; for task B, agreement and kappa with the checker per rule; for task C, the counts of each judgment.
