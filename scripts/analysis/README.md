# Paired analyses

Each script recomputes one of the paired-statistics files in `results/` from the game records under `runs/bargain/`. Run them from the repository root:

```bash
for s in e3_models e4_framings e3_conditions e3_vs_competing benchmarks e3_cross grounded_haiku labels; do python3 scripts/analysis/$s.py > /dev/null; done
```

| Script | Study | Output in `results/` |
|---|---|---|
| `e3_models.py` | E3: E1's reading cells on four model arms | `bargain_e3_paired.json`, `bargain_e3_models.svg` |
| `e4_framings.py` | E4: three skins against their abstract twins | `bargain_e4_paired.json` |
| `e3_conditions.py` | E3 conditions: open information and unequal stakes on sonnet and sol | `bargain_e3_conditions_paired.json` |
| `e3_vs_competing.py` | E3 conditions: the eight cells against a competing partner (run after `e3_conditions.py`) | adds `vs_competing` to `bargain_e3_conditions_paired.json` |
| `benchmarks.py` | CaSiNo and LLM-Deliberation | `bargain_benchmarks_paired.json` |
| `e3_cross.py` | Cross-model pairs, luna against sonnet | `bargain_e3_cross_paired.json` |
| `grounded_haiku.py` | The grounded layer on claude-haiku-5-5 against luna's | `bargain_grounded_haiku_paired.json` |
| `labels.py` | Style labels per study | `bargain_labels_summary.json` |
| `annotation_agreement.py` | The human annotation study: agreement between annotators, with the judge and with the checker. Reads what came back under `annotation/returned/` (workbooks, or exports of the annotation page) and the keys, not the game records | `annotation_agreement.json`, `annotation_disagreements.csv`, `annotation/answers.csv` |

The game records (one JSON file per game: the prompts, every move and message, token usage and the computed outcome) are not in the repository. `runs/` is ignored because it holds about 20,000 files and 184 MB. With the records in place, the scripts reproduce the committed files byte for byte (checked 2026-11-22).

Everything else in `results/` (rows, summaries, charts, maps) is written by `python3 scripts/project.py bargain <config> --report` and `--map`, which also need the records. The paper's figures are written by `python3 paper/figures/make_figures.py`, which needs only `results/`.

All tests are exact two-sided sign tests on differences paired by instance, or by instance and seat where both seats are pooled.
