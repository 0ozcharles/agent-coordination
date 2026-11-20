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
