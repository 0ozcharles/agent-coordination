# Human annotation study

Three tasks check by hand the three places where the study relies on a model or a program: the judge model's style labels, the compliance checker, and the extraction of the grounded instances. Six annotators (R1 to R6) form 15 pairs and every item is rated independently by one pair, so each annotator's load is the same and every pair of annotators shares items. Built by `scripts/make_annotation_pack.py` with a fixed seed; analysed by `scripts/analysis/annotation_agreement.py`.

| Task | Items | What the annotator sees | What the annotator decides | Each annotator |
|---|---|---|---|---|
| A. Style | 120 games: 75 from E1 (every cell) and 15 from each of E3 on haiku, sonnet and sol | the public transcript only, as the judge model saw it | per agent: assertiveness and cooperativeness on 0, 1, 2 by the codebook (`docs/B_BARGAIN_STYLE_CODEBOOK.md`), and whether it asks about the other side's needs and discloses its own priorities | 40 games, about 65 minutes |
| B. Compliance | 120 rule-following agents: competing 24, collaborating 36, compromising 24, accommodating 24, avoiding 12; half are games the checker failed (avoiding has two) | the rule split into numbered clauses, the agent's points, the contested decisions, the value of every proposal to that agent | did the agent follow every clause, yes or no; if no, which clauses | 40 agents, about 65 minutes |
| C. Extraction | 30 of the 85 grounded instances | the decisions, options, needed options and importances the extractor produced, and a link to the ParallACT ticket pair (the ticket text is not copied) | four yes / partly / no judgments against the two tickets | 10 instances, about 55 minutes |

Five, five and one more items are practice items that all six annotators rate before the main items; they are reported apart and are not part of the agreement figures. One worked example per task is shown filled in.

## Files

- `GUIDE_zh.md`: the instructions the annotators read (Chinese; the codebook definitions are quoted in English as the judge model received them). It is also the first sheet of every workbook.
- `workbooks/annotator_R1.xlsx` to `annotator_R6.xlsx`: what each annotator fills in. One sheet per task; answers go in the yellow cells.
- `task_a_style_items.csv`, `task_b_compliance_items.csv`, `task_c_extraction_items.csv`: every item as text. `assignment.csv`: which two annotators rate each item.
- `keys/`: which game each item is, the judge's labels and the checker's verdicts. Not in the repository until the answers are in; `keys.sha256` commits to their content now, so the keys published later can be checked against it. The builder reproduces them from the game records. A first, smaller pack (commit 7aceb39) had its key in the repository; the 209 games it used are listed in `excluded_games.json` and are not drawn again.
- `returned/`: the workbooks as they come back (not in the repository, they carry names). `answers.csv`, written by the analysis, is the record of every answer under the annotator ids.

## Procedure

1. Give each person one workbook. Nothing else is needed; the guide is inside.
2. Everyone rates the practice items and returns the file. Put the files in `returned/` and run the analysis: it prints the practice answers side by side. Discuss the differences once, about the definitions only.
3. Everyone rates the main items alone, without discussing items and without asking a model, and returns the file.
4. Run the analysis again. A third person adjudicates the items in `results/annotation_disagreements.csv` if a single human label per item is wanted.

```
python3 scripts/make_annotation_pack.py          # needs runs/bargain and, for the workbooks, openpyxl
python3 scripts/analysis/annotation_agreement.py # standard library only
```

## What is reported

- Task A: for each score and each fact, agreement between the two annotators (exact, within one, Krippendorff's alpha) and of the judge with the annotators (exact, weighted kappa, and alpha with the judge as one more rater, to set beside the alpha between annotators), by source model and by annotator; the same for the style that follows from the two scores.
- Task B: agreement between annotators and of the checker with them, by rule, and separately for the games the checker failed and passed, because the sample over-represents failures. The checker verifies some clauses of each rule and not others (`annotation.CHECKED`): opening value, steps and acceptance floors are checked; "only from your top option to your middle option", "say which decisions matter most to you", "build the design around what they said they need" and "do not argue" are not. The annotators judge the whole rule, so the analysis also gives the agreement on the checked clauses alone and counts the violations that involve unchecked clauses only.
- Task C: the counts of yes, partly and no for each question, the items both annotators call yes, and alpha.

Two things in the guide go beyond the codebook text the judge received, and are to be reported with the results: stating what a whole package is worth to oneself does not count as disclosing priorities, and the conventions for reading the rules in task B (values are taken from the game, not from what the agent says; a clause that cannot apply is not broken).
