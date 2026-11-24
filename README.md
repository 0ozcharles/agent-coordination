# Agent Coordination

Research code and results for two connected studies of LLM agents that work on the same thing and want different things.

1. **Strategy payoff study** (current; the paper draft in [`paper/`](paper/)): which conflict-handling strategy should an LLM agent use? Two agents bargain over a multi-issue design in a parametric environment with an exact efficiency frontier. Each of the five Thomas-Kilmann strategies (competing, collaborating, compromising, accommodating, avoiding) is given as behavioural rules whose compliance is checked from the moves. 67,240 games, four models, $102: 21,048 in the studies below, 28,500 in a pre-registered replication on fresh instances and in rule variants, 8,192 in three pre-registered studies in which the agent chooses its strategy itself (without and with what each strategy earned before, and after reasoning), and 9,500 in a pre-registered study in which programs play the rule sets (7,500 of them between two programs, without a model).
2. **Diagnosis phase** (earlier): why do two coding agents fail on CooperBench and ParallACT, and does a conflict-handling style help? It does not there, because the conflict is binary (whose change lands). That result is why the first study exists.

## Strategy payoff study

### Findings

The numbers are from the confirmation sample (E1-R and E2-R: fresh instances, 26 hypotheses and the analysis fixed before any game, Wilcoxon tests over instances with Holm correction; 22 of the 26 confirmed) unless a study is named.

- **Rules are enacted, style sentences mostly are not.** Four of the five rules are followed in 86 to 100 percent of games and the collaborating rule, which also says what to talk about and what to trade, in 56; a prompted "be competitive" sentence changes the words, not the moves. Every analysis counts all games.
- **Collaborating is the one strategy that pays its user without taking from the partner.** Adopted alone it earns its user 6.0 points of 100 more than the default agent earns [3.7, 8.2] and the partner loses nothing; the gain is there in nine of ten conditions and under three wordings of the rule. It is the best response to four of six partners and the replicator attractor. Two collaborating agents gain 6.1 [0.9, 11.0], an effect one of two paraphrases does not reproduce.
- **Competing pays its user about as much, out of the partner's pocket.** Adopted alone it earns +4.7 [2.7, 6.6] over 240 instances of the core regime, not distinguishable from collaborating's +6.1, and takes 11.7 from the partner; the pair loses 8.0, two competing agents lose 37.6, and a harder competing rule earns nothing more. (The discovery sample read this as "competing gains nothing"; the replication corrected it.)
- **Compromising and avoiding lose everywhere; accommodating gives.** Adopted alone they cost their user 8.1 and 16.6 points and pairs of them fall 11.0 and 27.4 below the default pair, in all ten conditions. Accommodating costs its user 7.2 and gives the partner 10.3; two accommodators are the most efficient pair (+7.5).
- **The situation moves the map, but not as the textbook prescribes.** Of the six conditions for which the Thomas-Kilmann model names a strategy, that strategy is right in one (everything to trade: collaborate), wrong in four, and makes no difference in one (urgency). Which strategy has the largest mean in a condition changed in five of ten conditions between 20 and 80 instances; the signs of the strategies did not.
- **Cost orders the strategies the other way round.** Competing is the most expensive strategy in tokens; a modest price on tokens hands the answer to the cheap strategies.
- **What not to do transfers; who keeps collaborating's gain does not.** Across 29 settings (framings, real-conflict structures, four models, cross-model pairs, two external benchmarks) competing, compromising and avoiding pairs fall below the default pair in 80 of 81 comparisons. A collaborating rule raises the joint payoff in 25 of 29 settings, and its user keeps the gain only against a partner that cannot find the frontier alone: pairs of different models show that the partner's model decides, not the adopter's.
- **Left to choose, the agent takes none of the strategies; made to choose, it takes the ones that lose** (E6, pre-registered, `gpt-6-luna` with reasoning off). Shown the five rule sets and the option of following none, it chose none in 1,600 of 1,600 choices over the ten conditions, nearly always to stay flexible. Made to pick one, it picked accommodating in 42 percent, collaborating in 28, avoiding in 22 and competing in 6, mostly for the low acceptance floor, and earned 5.0 points less than the default agent [3.7, 6.2] and 8.9 less than an agent assigned the collaborating rule. Its picks did no better on the instances where they were made than its average pick (−0.05 [−1.0, +0.9]) and shifted little with the condition; against a competing partner it drifted toward avoiding, not toward the best reply. Choosing by condition from the measured map would itself add nothing over collaborating everywhere (+0.1 [−0.8, +1.0]). The same two answers come back on the grounded instances and on three external benchmarks.
- **Told what each strategy earned, the agent takes the best one; it gains what the measurement allows and no more** (E7, pre-registered, `gpt-6-luna`, 40 instances). With each option of the same menus followed by its average payoff in the discovery sample, the agent that must pick takes the option with the highest figure in 89 percent of its picks and earns 8.6 points more than without the figures [6.8, 10.4], 3.7 more than the default agent [1.9, 5.5], and the same as an agent simply assigned the collaborating rule (−0.2 [−1.8, +1.4]). Where the figures name the competing rule its partner pays (−1.7 [−3.2, −0.1] over the ten conditions). Free to decline, it still declines in 46 percent of its choices, mostly where the advantage shown is small.
- **Allowed to reason, the agent that must pick takes the collaborating rule; free to decline, it still declines** (E9, pre-registered; the choice call at reasoning effort high). `gpt-6-luna` then picks collaborating in 66 percent of its picks, earns 5.6 points more than with its unreasoned pick [4.0, 7.2], and is level with the default agent (+0.8 [−0.7, +2.4]) but 3.1 below an agent simply assigned that rule [−4.7, −1.5]. `gpt-6.1-sol` picks the collaborating rule in 40 of 40 picks, on the model where that rule costs its user 3.4 points [−5.4, −1.3]. With the option of no rules, both decline (96 and 100 percent). Reasoning gives the pick a prior, not knowledge of what pays; in E7 the measured figures did.
- **Part of the map is in the rules, and part is in the model that carries them out** (E8, pre-registered; `gpt-6-luna` as the default agent). Each rule set was also played by a program that sees what an agent sees. Avoiding and accommodating lose either way, a competing agent's gain is its partner's loss either way, and the five symmetric pairs keep nearly the same order (rank correlation .90; the two 5 x 5 payoff matrices correlate at .71). But against the default agent the competing program earns 15.9 points more than the default agent [13.5, 18.3] where the model given the same rule earns 3.7, the compromising program gains 5.6 where the model loses 8.1, and the collaborating program gains 2.6 where the model gains 6.0. Within one rule's wording, a convention the rule leaves open moves its user's payoff by 4 to 26 points. The claims above are about the model given a rule, not about the rule.

Caveats that belong next to these: the paper draft and its figures still carry the discovery sample's wording and numbers ([what changes](docs/RESEARCH_PLAN.md#strategy-payoff-study-the-confirmatory-replication-and-the-rule-variants-results-2026-11-22)); the transfer settings below were not replicated and most have 20 to 40 instances; the relation between the room the default pair leaves and collaborating's gain holds among the 23 settings with the standard protocol and equal stakes (r = .81), not over all 29 (r = .32); the strategies are rules we wrote; games are one-shot.

### Experiments

| Study | Games | Config | Results | What it shows |
|---|---|---|---|---|
| E0 manipulation check | 360 | [`bargain_dev.json`](configs/bargain_dev.json) | `bargain_dev_*` | rules are followed, prompted sentences mostly not; fixed the rule texts |
| E1-R, E2-R replication | 22,800 | [`bargain_replication.json`](configs/bargain_replication.json), [`bargain_e1r.json`](configs/bargain_e1r.json), [`bargain_e2r.json`](configs/bargain_e2r.json) | `bargain_e1r_*`, `bargain_e2r_*`, `bargain_replication.json` | E1's 36 cells on 100 fresh instances and E2's ten conditions on 80; 22 of 26 pre-registered hypotheses confirmed |
| E5 rule variants | 5,700 | [`bargain_e5_rules.json`](configs/bargain_e5_rules.json) | `bargain_e5_rules_*` | two paraphrase sets, moved floors, the collaborating rule taken apart; signs hold in 26 of 26 |
| E6 selection | 4,912 | [`bargain_selection.json`](configs/bargain_selection.json) and fifteen arms `bargain_e6_*.json` (the Deal or No Deal arm added afterwards) | `bargain_e6_*`, `bargain_selection.json` | the agent chooses its own strategy: none when it may, the conceding rules when it must; 2 of 8 pre-registered hypotheses confirmed |
| E7 informed choice | 1,600 | [`bargain_informed.json`](configs/bargain_informed.json) and ten arms `bargain_e7_*.json` | `bargain_e7_*`, `bargain_informed.json` | the same menus with what each option earned before: the agent takes the highest figure in 89 percent of forced picks and gains 8.6 points on its uninformed pick; 4 of 4 pre-registered hypotheses confirmed |
| E8 scripted rules | 9,500 | [`bargain_e8.json`](configs/bargain_e8.json) | `bargain_e8_*`, `bargain_scripted.json` | each rule set played by a program: 7,500 games between programs and 2,000 against the default agent; the losers and the order of the pairs are in the rules, the ranking of competing, collaborating and compromising adopted alone is the model's; 6 of 11 pre-registered hypotheses confirmed |
| E9 reasoned choice | 1,680 | [`bargain_reasoned.json`](configs/bargain_reasoned.json), ten arms `bargain_e9_<condition>.json` and `bargain_e9_sol.json` | `bargain_e9_*`, `bargain_reasoned.json` | E6's menus with the choice made at reasoning effort high, on `gpt-6-luna` (ten conditions) and `gpt-6.1-sol`: the forced pick becomes the collaborating rule and no longer loses on `gpt-6-luna`, the free choice stays none; 3 of 4 pre-registered hypotheses confirmed |
| E1 strategy matrix | 1,680 | [`bargain_e1.json`](configs/bargain_e1.json) | `bargain_e1_*` | all 36 pairs of six strategies; collaborating dominates, competing does not pay |
| E2 condition map | 4,800 | [`bargain_e2.json`](configs/bargain_e2.json) and ten arms | `bargain_e2_*` | ten situations, one knob each; the textbook is right in one of six |
| E4 framings | 960 | [`bargain_e4.json`](configs/bargain_e4.json) | `bargain_e4_*` | three realistic skins; the map is unchanged, collaborating's gain goes to the partner |
| Grounded | 5,440 | [`bargain_grounded.json`](configs/bargain_grounded.json) and three more | `bargain_grounded*` | 85 instances from real code conflicts; flat when each side is told the other's ticket, the map returns under the abstract rendering |
| Grounded, second model | 1,360 | [`bargain_grounded_claude_haiku_5_5.json`](configs/bargain_grounded_claude_haiku_5_5.json) | `bargain_grounded_claude_haiku_5_5_*` | haiku's default pair leaves twice the value; collaborating pairs gain 8.1 |
| E3 models | 1,280 | [`bargain_e3.json`](configs/bargain_e3.json) | `bargain_e3_<model>_*` | the signs hold on four models; stronger default agents leave no gain for a rule adopted alone |
| E3 conditions | 1,920 | [`bargain_e3_conditions.json`](configs/bargain_e3_conditions.json) | `bargain_e3_<condition>_<model>_*` | open information and unequal stakes on two stronger models; their default agent is not exploited |
| Cross-model pairs | 840 | [`bargain_e3_cross.json`](configs/bargain_e3_cross.json) | `bargain_e3_cross_*` | luna against sonnet; the partner's model decides what a rule earns |
| External benchmarks | 2,768 | [`bargain_casino.json`](configs/bargain_casino.json), [`bargain_deliberation.json`](configs/bargain_deliberation.json), [`bargain_dond.json`](configs/bargain_dond.json) | `bargain_casino_*`, `bargain_deliberation_*`, `bargain_dond_*` | CaSiNo and LLM-Deliberation; the negative claims transfer, competing alone pays only where the partner cannot refuse; Deal or No Deal (80 scenarios) gives the same picture, and competing adopted alone does not pay there |

Every config was frozen with its predictions before its run. Models: `gpt-6-luna` (main), `claude-haiku-5-5`, `claude-sonnet-5-5`, `gpt-6.1-sol`. File names in [`results/`](results/README.md) follow the config names.

## Diagnosis phase

| Study | What it shows |
|---|---|
| S1 failure census (318 runs) | on CooperBench with gpt-6-luna, 68 percent of two-agent failures are integration failures, almost all git merge conflicts |
| Offline merge test | re-merging the same patches with an LLM resolving conflicts lifts the two-agent pass count from 35 to 78 of 159 |
| S2 targeted interventions (636 runs) | once conflicts are merged, no fix beats the baseline |
| Re-check of the CooperBench paper's runs | the paper's merge step often drops one agent's code; merged for real, conflicts are far more common than recorded, and the size of the coop-solo gap depends on conflict resolution |
| Conflict states and style instructions on CooperBench (52 states, six instructions) | no instruction beats neutral; compromising hurts |
| ParallACT pilot, style arms and synthesis (520 runs and more) | no instruction changes whether both requests pass; a manager choosing among teams' patches does no better |
| Negotiation pilot (120 games) | in a one-issue buy-sell game the style sentences induce accommodating but not competing, and collaborating ends as conceding; hence several issues and strategies as rules |

[`docs/FINDINGS.md`](docs/FINDINGS.md) has a paragraph with the numbers for every study of both phases.

## Quick start

These need Python 3.9+ and no API key or Docker:

```bash
python3 -m unittest discover -s tests            # 141 tests
python3 paper/figures/make_figures.py            # redraw the paper's figures and the transfer table from results/
cd paper && tectonic main.tex                    # build the paper (brew install tectonic)
```

Playing games needs model API keys in `.env` (see [`.env.example`](.env.example)); a study is one command:

```bash
python3 scripts/project.py bargain configs/bargain_e1.json --execute --workers 8   # play the missing games (paid)
python3 scripts/project.py bargain configs/bargain_e1.json --report                # rows, summary, charts
python3 scripts/project.py bargain configs/bargain_e2.json --map                   # the condition map
```

Reports and the paired analyses in [`scripts/analysis/`](scripts/analysis/README.md) read the game records under `runs/`, which are not in the repository (about 20,000 files, 184 MB). The diagnosis-phase experiments need Docker, Redis and the benchmark checkouts; see [Development](docs/DEVELOPMENT.md).

## Repository layout

```text
src/agent_coordination/
  bargain.py        the bargaining environment: generator, exact frontier, strategies, play, compliance, reports, maps
  benchmarks.py     CaSiNo, LLM-Deliberation and Deal or No Deal reduced to the environment's instance format
  scripted.py       the five rule sets played by a program, without a model (E8)
  ground.py         instances extracted from ParallACT's contradictory ticket pairs
  models.py         model routing by provider prefix and per-call cost
  stats.py          instance-level tests: sign test, Wilcoxon, bootstrap intervals, Holm correction
  annotation.py     the human annotation study: assignment, reading returned workbooks, agreement statistics
  negotiation.py    the single-issue negotiation pilot and shared statistics
  s1.py, s2.py, merge.py, census.py, ...   the diagnosis phase on CooperBench
  parallact.py, conflict_states.py, branch.py, synthesis.py   conflict states and style arms on coding benchmarks
configs/            one frozen config per study arm, with its predictions
data/               instance sets: grounded, CaSiNo, LLM-Deliberation, Deal or No Deal (structure only)
results/            rows, summaries, charts and paired statistics (see results/README.md)
scripts/analysis/   the paired analyses behind results/*_paired.json
annotation/         the human check of the judge's labels, the compliance checker and the grounded extraction (see annotation/README.md)
paper/              the ICML 2027 draft; figures/make_figures.py draws its figures from results/
docs/               research plan (the full log), findings by experiment, development guide, codebooks
experiments/m0/     the exploration phase (frozen)
tests/
```

## Documentation

- [Findings by experiment](docs/FINDINGS.md): one paragraph with the numbers for every study.
- [Research plan](docs/RESEARCH_PLAN.md): the chronological log with each study's design, frozen predictions, results and corrections.
- [Development](docs/DEVELOPMENT.md): setup, running each kind of study, model providers, the paper build and the Overleaf sync.
- [Human annotation study](annotation/README.md): three tasks, six annotators, every item rated twice; what is measured and how to run it.
- Codebooks for the judge models: [bargaining styles](docs/B_BARGAIN_STYLE_CODEBOOK.md), [negotiation styles](docs/N_NEGOTIATION_STYLE_CODEBOOK.md), [conflict states](docs/C_CONFLICT_CODEBOOK.md).
- [M0 retrospective](docs/M0_RETROSPECTIVE.md) and [M0 code map](docs/CODEBASE.md): the exploration phase.

## License

MIT. See [LICENSE](LICENSE). CooperBench, which the experiments run on, is a separate project (also MIT) and is not included here; the setup steps clone it. One data file is not under MIT: `data/bargain_dond_instances.json` is derived from the Deal or No Deal dataset (Lewis et al. 2017) and keeps its licence, CC BY-NC 4.0 (non-commercial use, with attribution).
