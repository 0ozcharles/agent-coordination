# Agent Coordination

Why do teams of LLM agents fail at coding tasks, and which fix is most cost-effective for each cause of failure?

## Overview

Two agents each implement one feature, and their code is merged at the end. A run can fail for four reasons:

- **Capability**: the task is beyond what the model can do.
- **Context**: after the task is split, an agent lacks information it needs for its own part.
- **Coordination**: agents duplicate work or don't know what the other is changing.
- **Integration**: each agent's code is correct on its own, but the two don't merge.

We started from the hypothesis that each cause calls for a different fix. We first measure how often each cause occurs, then compare targeted interventions for the main causes, and finally ask whether the cause can be predicted while a run is still in progress. Tasks and tests come from [CooperBench](https://github.com/cooperbench/CooperBench).

| Stage | Question | Status |
|---|---|---|
| M0 Exploration | Build the experiment pipeline | Done, see [M0 retrospective](docs/M0_RETROSPECTIVE.md) |
| S1 Failure census | Which causes produce failures, and how often? | Done: integration is the main cause |
| S2 Targeted interventions | Does a fix matched to the cause beat other fixes? | Done: no fix beats the baseline once conflicts are merged |
| S3 Online diagnosis | Can we detect the cause mid-run and pick a fix? | Not going ahead as planned |

The full design is in the [research plan](docs/RESEARCH_PLAN.md).

## Findings so far

S1 main run: gpt-6-luna, 53 tasks, 3 coop and 3 solo runs each, 318 runs in total, $2.32.

- Coop passed 35 / 159. Of the 124 failures, 84 (68%) were integration failures, and 82 of those were git merge conflicts: both agents wrote correct code but edited the same lines. If each feature counted as passed whenever its own patch passed, coop would pass 85 / 159.
- Capability caused 18 failures (15%) and context 21. A secondary handoff label moves 13 of these 39: to avoid merge conflicts, an agent left part of its feature to its partner, and the combined work failed.
- Solo officially passed only 27 / 159, but another 62 runs wrote correct code and never opened a PR. CooperBench only grades submitted code.

The pre-registered condition for continuing (non-capability causes ≥ 30% of failures) is met at 86%. Details, confidence intervals and the go_chi/typst caveat are in the [research plan](docs/RESEARCH_PLAN.md#s1-main-run-2026-11-02).

Offline merge test: we re-merged the existing patch pairs of the 88 coop runs whose merge conflicted. No agent was re-run, and the whole test cost $0.02.

- With gpt-6-luna resolving the conflicts, 112 of 134 integration features (84%) pass. Coop then passes 78 / 159 instead of 35, close to solo's 89 when unsubmitted code is counted.
- Keeping both sides of each conflict (git's union merge) recovers only 51.
- After the merge, about half of the remaining coop failures are capability. See the [research plan](docs/RESEARCH_PLAN.md#offline-merge-test-2026-11-02).

S2: the same 53 tasks, 3 runs each in four arms, 636 runs in total, $6.42. Conflicted coop runs are re-merged with the merge test's llm method, and that score is primary.

- The baseline passes 88 / 159 after the re-merge, against 38 officially.
- Telling agents that overlapping edits are merged automatically (own-feature) cuts handoff failures from 14 to 3, but more conflicts follow and integration failures the re-merge cannot fix rise from 11 to 29. Net: −3.1 points against the baseline [−11.3, +5.0].
- A shared git remote does not reduce context failures, and capability failures rise. Net: −4.4 [−10.7, +1.9].
- On CooperBench's official score, shared git would beat the baseline by 10.7 points [+3.1, +18.9]. About half of that gain comes from the conflict fallback, which tests agent 1's patch alone after agent 1 has pulled in its partner's code. The scoring rule changes the verdict on the intervention. See the [research plan](docs/RESEARCH_PLAN.md#s2-targeted-interventions-2026-11-03).

"Official score" above means the released CooperBench harness (`git merge`, then agent 1's patch alone on a conflict). The CooperBench paper used a gentler pipeline: `git merge-file`, a union merge, then a small learned resolver.

The CooperBench paper's own runs (GPT-5 and Claude Sonnet 4.5, published on Hugging Face): we re-checked the paper's results without re-running any agent. This used Docker and a few cents of re-merging.

- **The paper's merge step often loses code.** In most pairs it recorded as merging cleanly, the merged patch it tested holds only agent 2's code: agent 1's work never entered the merge. We tested the paper's own merged patches in our harness. They reproduce its failures: 62 of 64, with the same first error in 55 of 56. Control pairs the paper recorded as passing pass for us too.
- **Merged for real, conflicts are common.** 48% of GPT-5's failed pairs and 74% of Claude's conflict, against the 30% and 21% the paper recorded. We withdraw our earlier reading that strong models rarely conflict and fail mostly on code that merges cleanly.
- **The paper's coop numbers are unreliable in both directions.** The bug fails pairs by dropping code the tests need. It also passes pairs whose real merge conflicts or breaks: in a random sample of 150 Claude pairs, 9 of the 39 recorded passes fail once merged correctly.
- **How large the real gap is depends on conflict resolution.** We scored coop and solo in one harness on 150 random pairs per model. Solo agrees with the paper's records.
  - With union merging, the coop–solo gap barely moves: 7% closed for GPT-5, 0% for Claude.
  - With gpt-6-luna resolving conflicts, it closes 28% [11%, 59%] and 64% [17%, 144%].
  - An earlier estimate that mixed our coop with the paper's records (68% and 85% closed) is withdrawn.
- **What remains.** Among the failed pairs re-scored on our merges, an agent failing its own feature when paired is the largest cause where solo succeeds. Integration is 23% (GPT-5) and 36% (Claude).
- **A smaller bug.** CooperBench counts a test run as failed if "failed" appears anywhere in its output. The paper used this check (it reproduces all 2,211 recorded results), but it changes only 6 of them.

Details, in order: the [offline check](docs/RESEARCH_PLAN.md#offline-check-on-the-cooperbench-papers-runs-2026-11-03), the [failure census](docs/RESEARCH_PLAN.md#failure-census-on-the-papers-runs-2026-11-04), [re-scoring the logs](docs/RESEARCH_PLAN.md#re-scoring-the-papers-test-logs-2026-11-04), the [merged-patch check](docs/RESEARCH_PLAN.md#the-papers-merged-patches-tested-in-our-harness-2026-11-04) the [merge audit](docs/RESEARCH_PLAN.md#audit-of-the-papers-clean-merges-2026-11-04) and the [sensitivity check](docs/RESEARCH_PLAN.md#sensitivity-of-the-merge-correction-2026-11-05).

Coordination conflict states (the start of the conflict-handling direction): in the S1 and S2 coop runs, both agents claim the same file in almost every run, at their first exchange, and 95% of agent trajectories have written nothing by then, so a branch can restart from the message history. Under the default prompt the agents mostly collaborate or compromise when they negotiate; accommodation shows up mainly as handoffs (39% of conflicts). Labels come from a model that reads only the messages, checked for consistency (style kappa 0.82–0.95) and by hand (56 of 60). See the [research plan](docs/RESEARCH_PLAN.md#coordination-conflict-states-2026-11-10) and the [codebook](docs/C_CONFLICT_CODEBOOK.md).

Branching pilot: 263 of those runs can be restarted at the conflict point in a fresh container. We did this twice for each of 10 states, changing nothing ($0.23). The restarts pass about as often as the originals. In 3 of 10 states the two restarts disagree on the outcome, so outcome effects will need around 100 states. Handling styles repeat in 75–82% of agents, so a change in style should show up with far fewer. See the [research plan](docs/RESEARCH_PLAN.md#branching-pilot-from-conflict-states-2026-11-10).

Style interventions: we restarted 52 conflict states (one per feature pair) under six instructions, a neutral control and the five Thomas–Kilmann styles, given to both agents right after their claims collide. 312 branches, $3.31.

- The labeled style barely moves. Agents keep every edit their feature needs and almost never give up the contested file, because both features must edit it.
- The instructions do change communication: avoiding sends 60% fewer messages, competing cuts handoffs from 26 runs to 4, and collaborating adds 18% more messages.
- No instruction beats neutral (30/52 pass after the re-merge). Compromising hurts, with 18/52 passing; all 12 states where it differs from neutral favour neutral (p = 0.0005). Its agents more often write code that fails their own feature.

See the [research plan](docs/RESEARCH_PLAN.md#style-interventions-at-the-conflict-point-2026-11-11).

ParallACT pilot: in CooperBench conceding means failing one's own feature, so we moved to [ParallACT](https://github.com/rm206/ParallACT_data_and_code)'s contradictory tasks, where two requests change the same code in incompatible ways but can be reconciled. Our coop agents now run them in the tasks' SWE-Bench Pro images, scored with ParallACT's grader; the reference patches check out on all 20 pilot instances. On one run each ($0.24), both requests pass in 4 of 20. In 4 runs one agent handed the shared code to its partner, and in 3 of those the partner implemented both requests and both pass. A single agent given both requests satisfies both in 6 of 20 (its local work); the 3 instances it solves and the pair does not are runs where both agents implemented their request in the shared code and the merge failed. Most failures are capability, so a handling style could recover at most about 3 of 20 here. See the [research plan](docs/RESEARCH_PLAN.md#parallact-pilot-2026-11-11).

ParallACT conflict states: the CooperBench method of restarting both agents at the first conflict point does not carry over. The agents announce plans at once but discover the shared code late (step 20 against 6), after 43% of them have written files, so only 4 of 20 runs can be restarted there. The agents almost never say that the two requests contradict, and where coordination failed the conflict was not discussed at all. A style instruction therefore has to come at the start of a run. The codebook transfers: it scores the conceding agent as accommodating in the four runs with a concession. See the [research plan](docs/RESEARCH_PLAN.md#parallact-conflict-states-2026-11-12).

ParallACT style arms (design): eight arms, each instruction given to both agents at the start of the run: the default prompt, a neutral control, the five Thomas–Kilmann styles with the CooperBench sentences, and a structured arm that asks each agent to state what its feature needs from the shared code before editing. A dev check on 8 instances (64 runs, $0.66) shows the instructions are delivered and that collaborating and avoiding change the messages; the test set is the other 65 contradictory instances. See the [research plan](docs/RESEARCH_PLAN.md#parallact-style-interventions-design-2026-11-12).

ParallACT style interventions, results on the 65 test instances (520 runs, $8): no instruction changes whether both requests pass (21 to 26 of 65 against 23 under neutral; no arm significant). The instructions do change behaviour: collaborating, compromising and avoiding shift the labeled style, avoiding cuts messages from 6.4 to 1.5 per run, and the structured arm doubles the runs where agents state exact interfaces. Concessions (one agent leaves the shared code to its partner) pass both requests no more often than other runs. A single agent satisfies both requests on 25 of the 65 instances, and the pairs already solve 17 to 20 of those, so capability, not handling style, decides most outcomes. See the [research plan](docs/RESEARCH_PLAN.md#parallact-style-interventions-results-2026-11-15).

ParallACT synthesis readouts (offline, $0.81): a manager that reads both requests and picks among the eight teams' merged patches does no better than the consensus patch (28 against 27 of 65; some team solves 38). Re-merging the 148 conflicted runs with both requests, the agents' messages and the whole conflicted file in view raises both-pass from 33 to 61 (better in 30 runs, worse in 2); the requests alone or the whole file alone help less, and resampling does nothing. Where both agents' own patches pass, the new merge passes both requests in 55 of 67 runs against 30. See the [research plan](docs/RESEARCH_PLAN.md#parallact-synthesis-readouts-2026-11-16).

Negotiation pilot (120 games, $0.11): the next study analyses how handling strategies change each agent's payoff, so we checked the style sentences in NegotiationArena's buy-sell game with an explicit payoff. The default player compromises (14 of 20) and the seller states its cost in its first message in every game. The sentences move the instructed player's share of the surplus from 0.52 (neutral, also competing) down to 0.37 (collaborating), 0.28 (accommodating) and 0.22 (compromising); deals are reached in 115 of 120 games. Accommodating is induced; collaborating changes the talk but ends as conceding in a one-issue game; compromising turns the default into accommodating; competing does not move the label or the share; avoiding is never labeled but is the most distinct behaviour (2.7 moves, no concession). So the main environment needs several issues, and competing, avoiding and compromising need behavioural rules rather than sentences. See the [research plan](docs/RESEARCH_PLAN.md#negotiation-pilot-manipulation-check-of-the-style-instructions-2026-11-20).

Strategy payoff study, environment and E0 ($0.33): `bargain.py` is a parametric multi-issue bargaining game with exact payoffs, a computable frontier, and knobs for how opposed and how asymmetric the two agents' interests are. In the mixed regime the default agent leaves a quarter of the joint value on the table. Strategies stated as rules on the agent's own point scale are followed in 9 or 10 games of 10 (competing, accommodating, avoiding) or 7 of 10 (collaborating, compromising); the style sentences are not. Competing by rule exploits a neutral partner (69 against 46 points), collaborating by rule pays the instructed side the most and leaves the smallest gap, avoiding walks away in two games of three. The design of the full study (strategy matrix, conflict structure, information and power, models, external validity) is in the [research plan](docs/RESEARCH_PLAN.md#strategy-payoff-study-design-2026-11-20), with E0 [here](docs/RESEARCH_PLAN.md#strategy-payoff-study-e0-manipulation-check-2026-11-20).

E1 strategy matrix (1680 games, $0.83): all 36 ordered pairs of the five strategies and a neutral control on 40 instances of the mixed regime. Collaborating by rule is the best response to almost every partner and the attractor of replicator dynamics: against a neutral partner it earns 72 points against the partner's 64, creating value and taking it. Competing does not pay (it gains 3 points against neutral, p = 0.5, and against itself produces the worst outcome in the table: 55 each, 62% agreement, 4000 tokens per agent). Accommodating is exploited by everyone, yet two accommodators reach the most efficient agreements of all. Compromising and avoiding destroy value. Token cost orders the strategies competing > collaborating > neutral > compromising > accommodating > avoiding. See the [research plan](docs/RESEARCH_PLAN.md#strategy-payoff-study-e1-strategy-matrix-2026-11-20); E2, the condition map, is running.

Environment layers (2026-11-20): the same bargaining instance can be rendered under a scenario skin (a software design, a quarterly plan for two teams, a release plan) without changing its payoffs, and `ground.py` builds instances from real conflicts: the design decisions behind each of ParallACT's 85 contradictory ticket pairs, extracted by gpt-6-luna and validated, with each side's importance and the ticket context as the framing ($0.07). Details in the [research plan](docs/RESEARCH_PLAN.md#strategy-payoff-study-environment-layers-scenario-skins-and-grounded-instances-2026-11-20).

E2 condition map (4800 games, $2.50): ten conditions, each one knob moved from the E1 regime (urgency, move cost, reply budget, nothing to trade, everything to trade, full opposition, unequal stakes, unequal power, open information), on the same 24 cells. Both sides soft (accommodating pairs) is the efficient cell in 7 of 10 conditions; adopted alone against the default agent, collaborating pays in five conditions and competing in four, and competing pays only where the partner cannot push back (open information, a short reply budget, nothing to trade, a worse outside option) and by transfer; against a hard bargainer collaborating is the best reply in 8 of 10 and matching it never pays. Cost orders the strategies the same way everywhere (competing 2 to 3 times neutral's tokens, avoiding half), so the best net strategy drifts to the cheap ones as tokens are priced. Map: [results/bargain_e2_map.svg](results/bargain_e2_map.svg); details in the [research plan](docs/RESEARCH_PLAN.md#strategy-payoff-study-e2-condition-map-2026-11-20).

Grounded instances (2040 games, $0.86): E2 base's 24 cells on the 85 instances extracted from ParallACT's contradictory ticket pairs. The default agent is already efficient there (85 of 85 agreements, 77 on the frontier, gap 6 of 155 points against 15 of 140 in the generated regime), so no strategy beats neutral when adopted alone, while the ranking of what not to do is unchanged: avoiding loses 15 points, conceding unilaterally 4 to 5, and competing or avoiding pairs 10; collaborating stays the best reply to a competing partner. Replaying the same 85 structures under the abstract skin (2040 games, $0.82) brings the map back (collaborating alone +6.7 over neutral, p = .01; the default pair leaves the frontier), so the flattening is semantic: the ticket context makes the partner's needs legible, and what survives the framing is the list of what not to do. Details in the [research plan](docs/RESEARCH_PLAN.md#strategy-payoff-study-grounded-instances-2026-11-20).

E4 scenario skins (960 games, $0.56): E1's 40 instances rendered as a software design, a quarterly team plan and a joint release plan, with the rule diagonal and the two unilateral deviations against neutral, so that every game has an abstract twin in E1. The map does not depend on the framing: the order of the symmetric pairs (collaborating and accommodating pairs above the neutral pair, compromising, competing and avoiding pairs below), the condition map's best strategy and the verdict on competing as a unilateral deviation are the same under all four renderings, and the joint payoff of a skinned cell is within 5 points of its abstract twin in 17 of 24 comparisons (the exceptions are the knife-edge avoiding and competing pairs). What the names change is who captures collaborating's unilateral gain: the deviator when the options are abstract (+7.4 own, p = .017), the partner once the options are named (+0.7 to +5.3 own, +2.7 to +5.1 partner), the grounded finding in a weaker form. Option names also carry a prior: the second-listed option, which reads as the moderate choice in these skins, is agreed on 43 to 46 percent of contested decisions against 37 abstract, with no rise in the true middle option and no visible cost. Details in [docs/RESEARCH_PLAN.md](docs/RESEARCH_PLAN.md); chart in `results/bargain_e4_renderings.svg`.

E3 models (1,280 games, $15): E1's eight reading cells on claude-haiku-5-5, claude-sonnet-5-5 and gpt-6.1-sol (through the University of Arizona AI Assistant proxy, thinking and reasoning off), and on gpt-6-luna at its default temperature as the control, which reproduces E1 within noise. The signs of the map hold on every model: collaborating pairs are the best symmetric cell and above the default pair (+5.3, +2.5, +1.7 joint, p ≤ .013), competing pairs the worst (−18 to −34, p < .001), compromising and avoiding pairs below the default, competing alone worthless, matching a hard bargainer ruinous. The margins are the model's: the three newer models' default agents reach the frontier in 28 to 34 of 40 games against 20 for luna (joint 135 to 139 against 128), so two accommodators no longer beat two default agents and collaborating adopted alone no longer pays its user (own +0.9, −0.8, −3.1; the partner gains +1.7 to +3.8), the same pattern the skins and the grounded context produced. The stronger models also follow the collaborating trade clause more often (52 to 71 of 80 against 45) and are not exploited by a competing partner. Details in [docs/RESEARCH_PLAN.md](docs/RESEARCH_PLAN.md); chart in `results/bargain_e3_models.svg`.

E3 follow-up (1,280 games, $26 reported, $38 with a discarded run): E2's open-information and unequal-stakes conditions on claude-sonnet-5-5 and gpt-6.1-sol (16 cells, E2's instances). The conditions that moved luna's map most move the stronger models' maps less: under open information their default pair is on the frontier in 19 to 20 of 20 games, competing alone no longer exploits the partner (−0.4, +0.2 against +9.4 on luna) and every soft strategy adopted alone is a pure transfer to the partner; under unequal stakes the high-stakes side's collaborating edge disappears while the low-stakes side still does worst by accommodating on every model (15 to 18 of 30 against 19 to 21 in the default pair), the one textbook prescription that fails everywhere. Sonnet's open-information arm hit the 700-token reply cap in a fifth of its calls in a first run and was replayed with a 1,500-token cap (1 rejected reply in 1,275 calls); the numbers are the replay's.

E3 follow-up, completed (640 more games, $14): the eight cells in which a strategy faces a competing partner, on both conditions and both models. Under open information the stronger models' default agent gives a hard bargainer nothing (69 against 68 for the competing side, where luna's kept 50 against 73), so collaborating is no longer the best reply (within a point of neutral; +6.8 on luna) and every other reply loses, matching competing by 15 to 17 and avoiding by 25 to 33. Under unequal stakes collaborating is the best reply to competing on every model for both sides (+1 to +5), and the low-stakes side does worst by avoiding, accommodating or matching, not best by competing as predicted. Compliance in these cells is 39 to 40 of 40 for every rule on sonnet and sol.

Cross-model pairs (840 games, $12): gpt-6-luna against claude-sonnet-5-5 on E1's 40 instances, each model adopting each strategy against the other's default agent (both seats; the second arm for the five central cells). The partner's model decides what a rule earns, not the adopter's: collaborating by rule pays against a luna partner whichever model adopts it (+5.7, +2.0) and costs against a sonnet partner whichever adopts it (−2.8, p = .009; −0.8), the joint payoff up in every cell and the gain going to the sonnet partner; competing takes 12 to 15 points from luna's default agent and under 3 from sonnet's, and keeps little either way. The mixed default pair is near the frontier (joint 135 to 136, between the two same-model pairs) and the weaker model takes the larger share (71 against 64, p = .038). Details in [docs/RESEARCH_PLAN.md](docs/RESEARCH_PLAN.md); statistics in `results/bargain_e3_cross_paired.json`.

Grounded layer on a second model (1,360 games, $1.5): the 85 ParallACT-derived instances with the ticket context on claude-haiku-5-5. Haiku's default pair leaves twice as much on the table as luna's under the context (gap 12.3 against 6.0, p < .001), and the strategies have room again: collaborating pairs +8.1 over the default pair (p < .001), collaborating alone +3.2 for its user and +5.4 joint (+7.0 own on the all-contested instances, p = .02), avoiding and compromising pairs −14 and −8. The value of the collaborating rule tracks the room the default pair leaves, as across the models and the cross-model pairs.

Style labels (4,760 games, $1.9): E1's labeler on E2 base, the three E4 skins, the grounded run and the four E3 arms. Luna's default agent reads as a compromiser or accommodator that asks in 40 percent of its games or fewer; the three stronger models' default agents read as accommodating or collaborating, almost never compromising, and ask in 64 to 85 percent of their games and disclose in 94 to 96, the behavioural side of their frontier finding. The labeler recognizes the cooperative rules (collaborating 18 to 51 percent, accommodating 54 to 79) and not competing or avoiding (at most 3 and 16 percent), so programmatic compliance stays the primary measure for those. Summary in `results/bargain_labels_summary.json`.

External benchmarks (1,488 games, $0.8): CaSiNo (the 36 pairs of priority orders over food, water and firewood, the benchmark's points and walk-away) and LLM-Deliberation reduced to two parties (57 pairs from its four games, the parties' own score tables), both with the rule diagonal and every strategy against neutral in both seats on gpt-6-luna. On both the default agent is within 6 to 8 percent of the frontier and the map looks as it does on the stronger models: collaborating pairs at the top but not above the default pair, competing and avoiding pairs the worst cells (competing pairs never agree on CaSiNo), compromising pairs below the default, and every soft strategy adopted alone a transfer to the partner. Competing alone pays on CaSiNo (+5.6 of 36, p < .001) and nowhere else, because a walk-away worth 5 of 36 leaves its partner unable to refuse. Details in [docs/RESEARCH_PLAN.md](docs/RESEARCH_PLAN.md).

Per-run data: [S1 main](results/s1_main_rows.csv), [S1 screening](results/s1_screen_rows.csv), [merge test](results/merge_test_rows.csv), [S2](results/s2_rows.csv), [paper runs](results/upstream_rows.csv), [paper census](results/paper_census_rows.csv), [re-scored logs](results/paper_rescore_rows.csv), [merged-patch check](results/paper_merge_check_rows.csv), [merge audit](results/paper_merge_audit_rows.csv), [sensitivity](results/paper_sensitivity_rows.csv), [ParallACT pilot](results/parallact_pilot_rows.csv), [ParallACT solo](results/parallact_solo_rows.csv), [ParallACT conflict states](results/parallact_conflict_states_rows.csv), [ParallACT synthesis select](results/parallact_synthesis_select_rows.csv), [ParallACT synthesis re-merge](results/parallact_synthesis_remerge_rows.csv).

## Quick start

These commands need only Python 3.9+, with no Docker or API key:

```bash
python3 scripts/project.py census                               # failure causes in M0 runs
python3 scripts/project.py s1 configs/s1_main.json --report     # S1 main results (needs local run logs)
python3 -m unittest discover -s tests
```

Running S1 experiments requires Docker, Redis, and an OpenAI API key. See [Development](docs/DEVELOPMENT.md).

## Repository layout

```text
src/agent_coordination/   current code: CLI, M0 census, S1 and S2 runners, merge test
configs/                  experiment configs
results/                  shareable result data
experiments/m0/           M0 code (frozen)
docs/                     research plan, M0 retrospective, development guide
tests/
```

## Documentation

- [Research plan](docs/RESEARCH_PLAN.md): question, how failure causes are labeled, stage designs, progress
- [M0 retrospective](docs/M0_RETROSPECTIVE.md): what the exploration phase did and learned
- [Development](docs/DEVELOPMENT.md): setup, running S1, the merge test and S2, resuming M0 batches
- [M0 code map](docs/CODEBASE.md): structure of `experiments/m0/`

## License

MIT. See [LICENSE](LICENSE). CooperBench, which the experiments run on, is a separate project (also MIT) and is not included here; the setup steps clone it.
