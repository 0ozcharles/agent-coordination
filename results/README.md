# Results

Shareable result data: one row per run or game, summaries, and the figures drawn from them. Nothing here needs an API key to read.

**Strategy payoff study** (`bargain_*`). For each study arm `<s>`:

- `bargain_<s>_rows.csv`: one row per game (cell, instance, payoffs, joint payoff and gap, frontier, agreement, moves, tokens, compliance, labels, models).
- `bargain_<s>_summary.json`: per cell means and sign tests against the neutral pair; the own-payoff matrix, best responses and replicator attractor.
- `bargain_<s>_chart.svg`, `bargain_<s>_matrix.svg`: charts of the same.
- `bargain_<index>_map.{json,svg}`: the best strategy per condition, reading and price of tokens, for an index of arms (`e2`, `e3`, `e3_conditions`, `e4`).
- `bargain_*_paired.json`, `bargain_labels_summary.json`: paired statistics written by `scripts/analysis/`.
- `bargain_replication.json`: the 26 pre-registered hypotheses on the confirmation sample (E1-R, E2-R), with the discovery and pooled samples beside them. `bargain_e5_rules_robustness.json`: every rule variant against the original. `bargain_selection.json`: E6, the eight pre-registered hypotheses on an agent that chooses its own strategy, with the choice shares, the place in the menu, the kept and the repeated pick, the pairs of picks, the external layers and what a choice by condition could earn from the assigned cells. `bargain_instance_level.json`, `bargain_e3_cross_factorial.json`: the instance-level tests of the earlier arms.

Study arms: `e1`; `e2_<condition>` (ten); `e1r` and `e2r_<condition>` (the replication on fresh instances); `e5_rules` (rule variants); `e6_<arm>` (the selection study: the ten conditions, `core`, `grounded`, `casino`, `deliberation`, and `dond`, added afterwards; their rows carry ten more columns, the choices of each seat); `e4_<skin>` (three); `grounded`, `grounded_abstract`, `grounded_collab`, `grounded_abstract_collab`, `grounded_claude_haiku_5_5`; `e3_<model>` (four); `e3_<condition>_<model>` (four); `e3_cross_<seats>` (two); `casino`, `deliberation`, `dond` (Deal or No Deal); `dev*` (the manipulation check). The configs of the same names are in `configs/`.

**Earlier phase** (coding benchmarks): `s1_*`, `s2_*`, `merge_test_*`, `paper_*` (re-checks of the CooperBench paper's runs), `c_*` (conflict states and style interventions on CooperBench), `parallact_*`, `negotiation_pilot_*`, `m0_runs.csv`, `upstream_*`.
