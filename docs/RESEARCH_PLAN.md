# Research Plan

## Question

When a team of agents fails at a coding task, what caused the failure? For each cause, which intervention works best under the same compute budget? Can the cause be identified while the run is still in progress?

The work has three stages, each with a standalone result:

| Stage | Question | Condition to continue |
|---|---|---|
| S1 Failure census | Which causes produce failures, and how often? | Non-capability causes account for at least 30% of failures |
| S2 Targeted interventions | Does an intervention matched to the cause beat other interventions? | At least one cause has a clearly better matched intervention |
| S3 Online diagnosis | Can mid-run signals identify the cause and select an intervention? | — |

S1 is a complete result on its own. Prior work such as MAST provides failure taxonomies, but does not separate causes with controlled comparisons.

## Labeling failure causes

In the coop setting, two agents each own one feature. CooperBench's official score merges the two patches and runs the tests. We add three measurements: each agent's own patch is tested alone; each agent's local code is recorded, whether or not it was submitted; and a solo run on the same task serves as a reference. None of these call the model, so they add almost no cost.

| Cause | Rule |
|---|---|
| Integration | Each agent's own patch passes its feature, but the merged result fails |
| Submission | The agent's submitted patch fails, but its local code passes |
| Context | The agent fails its feature, but solo can do it |
| Capability | The agent fails its feature, and solo cannot do it either |
| Coordination (extra flag) | The two patches are identical, or one agent implemented the other's feature |

- When a run has several causes, the first one in table order is used.
- "Solo can do it" means solo passed in at least half of its repeated runs, which reduces the effect of a single random outcome.
- The context and coordination rules are coarse. We will check them against 20 manually reviewed runs.

## Stages

### S1 Failure census

Sample 58 tasks (each a pair of features) from CooperBench lite, stratified by repository. Screen each task with 2 coop and 2 solo runs, drop tasks that always pass or never pass, then run each remaining task 3 times. The whole stage uses one model (gpt-6-luna).

### S2 Targeted interventions

Interventions are designed only for causes that are common in S1:

| Cause | Intervention |
|---|---|
| Integration | One agent owns the merge; or re-merge existing patches with a better method |
| Coordination | Agree on interfaces before starting; claim files or functions |
| Context | Let each agent see the other's progress and code |
| Capability | Use a stronger model or a larger budget (a control: organizational changes should not help) |

Main test: for failures caused by X, does the intervention for X recover more of them than other interventions? All conditions get the same budget and are compared against solo.

### S3 Online diagnosis

Predict the failure cause mid-run from signals such as file overlap, self-test results, and messages, then choose an intervention. This stage reuses the checkpoint branching and V(o,a) framework from the original proposal. Baselines: the best single fixed intervention, always solo, and letting an LLM choose directly.

## Experiment rules

In M0, conditions and models changed mid-study, which made results hard to interpret. All later studies follow these rules:

- Write the question and decision rule into the config before running.
- One model and one scoring rule per study.
- Every study includes solo.
- No new conditions while a study is running.
- No conclusions from fewer than 20 tasks; confidence intervals are clustered by task.
- Tasks that always pass or always fail are reported separately and excluded from comparisons.

## Progress

### S1 screening (2026-11-01)

58 tasks × {coop, solo} × 2 runs = 232 runs, $1.93, all completed without infrastructure errors. Per-run data: [results/s1_screen_rows.csv](../results/s1_screen_rows.csv).

| | Official pass | Failures |
|---|---|---|
| Coop | 32 / 116 | Integration 58, capability 15, context 10, submission 1 |
| Solo | 25 / 116 | 44 runs wrote correct code but did not submit it; counting those, 69 / 116 |

Preliminary observations (each task ran only twice; the main run must confirm them):

1. Integration accounts for 69% of coop failures, and 55 of those 58 are git merge conflicts: both agents wrote correct code but edited the same lines. With a perfect merge, coop could pass up to about 90 / 116.
2. Capability and context together account for only 30% of coop failures.
3. CooperBench only grades code submitted through a PR. Almost half of solo's failures are correct code that was never submitted, so the official score substantially underestimates solo.

Following the pre-registered rule, 53 tasks go into the main run (3 never passed and 2 always passed). The list is in [configs/s1_main.json](../configs/s1_main.json).

### Label check (2026-11-02)

We read 5 screening coop failures in full and checked simple indicators across all 84. Three problems came up.

1. **Agents often re-split the work by file or function instead of by feature.** To avoid merge conflicts, one agent hands its feature, or part of it, to the partner, for example "please implement both parameters in the shared function". Its own patch is then empty or incomplete by design, so a failing own patch no longer means the agent could not do its feature. In 8 of the 24 features labeled context, the own patch was empty, skipped files the reference solution changes, or the partner's patch passed the feature; all 4 context cases read in full were handoffs. The context label therefore mixes missing information with handoff failures, such as the delegating agent giving its partner a wrong spec.
2. **Go and Rust toolchains were missing for agents.** CooperBench runs agent commands in a login shell, which resets PATH and drops Go (go_chi) and cargo (typst). Agents on these two repos could not build or test: 28 of 39 go_chi and 39 of 41 typst trajectories mention it. These repos are 22 of 116 coop runs but account for 9 of 24 context and 10 of 39 capability feature labels. The main run uses a non-login shell, which fixes both repos and leaves the other images unchanged. Screening results and task selection for these two repos were affected.
3. **The solo reference pools pairs.** "Solo can do feature f" uses solo runs from every pair that contains f. Restricting it to the same pair changes 1 of 34 labels. We keep the pre-registered rule and report the same-pair version as a sensitivity check.

Planned analysis changes, reported next to the pre-registered labels:

- Add a handoff label: the failing agent's own patch is empty or incomplete and its messages assign the feature to the partner. The feature is then judged on the merged result.
- Report screening results for go_chi and typst separately.

## Next steps

1. S1 main run: 53 tasks × {coop, solo} × 3 runs, 318 runs, about $2.7. Running.
2. Implement the handoff label and validate it on more manually read runs.
3. Test better merge methods offline: re-merge existing patch pairs with function-level or model-assisted merging and measure how many integration failures are recovered. This does not require re-running agents.
