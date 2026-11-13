# Research Plan

## Question

When a team of agents fails at a coding task, what caused the failure? For each cause, which intervention works best under the same compute budget? Can the cause be identified while the run is still in progress?

The work has three stages, each with a standalone result:

| Stage | Question | Condition to continue |
|---|---|---|
| S1 Failure census | Which causes produce failures, and how often? | Non-capability causes account for at least 30% of failures (met: 86%) |
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
| Handoff (secondary) | Replaces context or capability when part of the feature was left to the partner: the partner's patch passes the feature, or the agent's patch skips a file the reference solution changes and the partner's patch edits it |

- When a run has several causes, the first one in table order is used.
- "Solo can do it" means solo passed in at least half of its repeated runs, which reduces the effect of a single random outcome.
- The handoff label was added after the screening label check (below). It is reported next to the pre-registered labels, not instead of them.

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

**Design after S1 (frozen 2026-11-02).** The merge test settles integration: an automatic merge recovers 84% of it, so it becomes part of scoring instead of an intervention arm. The handoff check (below) suggests the main remaining non-capability failures come from two features of the setup. First, CooperBench's coop prompt lists "both patches merge cleanly with zero conflicts" as a success condition. Second, agents cannot see each other's code. Config with the question and decision rule: [configs/s2.json](../configs/s2.json).

| Arm | Change from S1 coop | Targets |
|---|---|---|
| coop baseline | none | — |
| coop own-feature | Prompt: overlapping edits are merged automatically, so implement your whole feature yourself, including shared files, and use messages only to agree on interfaces | Handoff |
| coop shared-git | CooperBench's shared git remote is enabled, so agents can fetch and test each other's branches | Context |
| solo | none | Reference |

- **Tasks and budget:** the 53 S1 main tasks, 3 runs per arm, 636 runs in total. That is about $5.5 and about 19 hours with 4 workers.
- **Scoring:** every coop run is scored with CooperBench's official score and with the llm re-merge. The re-merge score is primary.
- **Primary comparison:** each arm against the baseline, paired by task.
- **Secondary comparison:** failure counts by cause in each arm.

The matched-intervention test is underpowered: the baseline had only 13 handoff and 9 context failures in S1.

### S3 Online diagnosis

Predict the failure cause mid-run from signals such as file overlap, self-test results, and messages, then choose an intervention. This stage reuses the checkpoint branching and V(o,a) framework from the original proposal. Baselines: the best single fixed intervention, always solo, and letting an LLM choose directly.

**Status after S2:** not going ahead as planned, because no S2 arm beat the baseline (see [S2 results](#s2-targeted-interventions-2026-11-03)).

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

**Handoff label.** Following problem 1, we added a secondary handoff label (rule in the table above). On screening it moves 12 of the 84 failed coop runs: 5 from context and 7 from capability. Coop failures then read integration 58, handoff 12, capability 8, context 5, submission 1. We read the messages and patches of all 12: 9 are explicit handoffs (in 3 the agent published nothing and the partner implemented both features), 2 are agents cutting their own scope to stay out of files the partner was editing, and 1 is both agents implementing the same feature. None was a false positive. We have not yet checked how many handoffs the rule misses.

Screening results for go_chi and typst will be reported separately because of problem 2.

### S1 main run (2026-11-02)

53 tasks × {coop, solo} × 3 runs = 318 runs, $2.32, no infrastructure errors. Go and cargo worked in all go_chi and typst trajectories. Per-run data: [results/s1_main_rows.csv](../results/s1_main_rows.csv). Intervals are 95% bootstrap intervals clustered by task.

| | Official pass | Failures |
|---|---|---|
| Coop | 35 / 159 | Integration 84, context 21, capability 18, submission 1 |
| Solo | 27 / 159 | 62 runs wrote correct code but did not submit it; counting those, 89 / 159 |

**Decision rule.** Non-capability causes account for 86% of coop failures [77%, 93%], well above the 30% threshold, so S1 passes and S2 goes ahead. Counting failing features instead of runs (a run can fail both features), capability is 51 of 219 (23%).

Main results:

1. **Integration is the dominant cause:** 84 of 124 failures, 68% [57%, 78%]. 82 of the 84 are git merge conflicts. The other 2 are runs where one agent published nothing and the scorer skipped the merge. No failure came from a clean merge that then failed tests. If each feature counted as passed whenever its own patch passed, coop would pass 85 / 159 instead of 35, close to solo's 89 with unsubmitted code included.
2. **Capability and context are small:** 18 (15% [7%, 23%]) and 21 (17%). With the handoff label, 13 runs move: 12 from context and 1 from capability. Failures then read integration 84, capability 17, handoff 13, context 9, submission 1. We have not yet read the main run's handoff cases.
3. **Integration depends on the task:** 39 of 53 tasks had an integration failure in at least one run, and 18 had one in all three. By repository, it ranges from 15 of 15 coop runs (dirty_equals) to 0 of 15 (jinja, where coop passed 10).
4. **Sensitivity.** Using only same-pair solo runs as the reference changes 3 of 84 context or capability feature labels, all from context to capability.

**go_chi and typst**, reported separately because their tasks were selected using the broken screening runs: 8 tasks, coop passed 9 / 24, and the 15 failures are integration 12 and context 3 (all 3 are handoffs). Without these two repos, integration is 72 of 109 failures (66%) and capability 18 (17%), so the conclusion does not depend on them.

### Offline merge test (2026-11-02)

CooperBench merges the two patches with a plain `git merge` and scores any conflict as a failure. We re-merged the existing patch pairs of every S1 main coop run whose merge conflicted (88 runs: 82 integration, 4 context, 2 passed) and tested the result against both features. No agent was re-run. Config and decision rule: [configs/merge_test.json](../configs/merge_test.json). Per-run data: [results/merge_test_rows.csv](../results/merge_test_rows.csv).

- **union** keeps both sides of every conflict block (git's union merge). No model is used.
- **llm**: gpt-6-luna resolves the conflict blocks of each file in one call. It sees the code around the conflicts, but not the feature descriptions or tests.

The prompt was checked on 20 screening runs first (llm recovered 29 of 30 integration features there; union recovered 10). It changed twice, both times to fix the output format, and was then frozen for the 88 main-run runs.

| | Integration features recovered | Coop runs passing (of 159) | Regressions | Model cost |
|---|---|---|---|---|
| Official (`git merge`) | 0 / 134 | 35 | — | — |
| union | 51 / 134, 38% [22%, 55%] | 53 | 4 | $0 |
| llm | 112 / 134, 84% [73%, 92%] | 78 | 4 | $0.02 |

For reference, solo passed 27 / 159 officially and 89 / 159 counting unsubmitted code. The upper bound for a perfect merge is 85.

**Decision rule.** The llm merge recovers 84% of integration features, above the 50% threshold. Most integration failures are mechanical merge conflicts, so S2 adds the llm merge to the coop baseline and targets the failures that remain.

Details:

1. **Where the llm merge fails.** It recovered none of the 7 typst features: the merged Rust did not compile, with mismatched braces in 6 cases and a type error in 1. It recovered 6 of 12 in dspy. Each of the other repositories had at most 2 failures.
2. **Regressions.** All 4 regressions come from two go_chi runs, and both methods have them. The official score passed these runs through CooperBench's fallback, which tests agent 1's patch alone when the merge conflicts. Both agents had written the same helper function outside the conflict block, so the merged code defines it twice and does not compile.
3. **What remains after the merge.** 81 coop runs still fail: capability 39, context 21, integration 20, submission 1. With the handoff label, the split is capability 38, integration 20, handoff 13, context 9, submission 1. Capability more than doubles from 18 runs because many integration runs also had a second feature that failed for another reason, which the label order used to hide. After the merge, non-capability causes are about half of the remaining failures.

### Handoff check on the main run (2026-11-02)

We read the messages and patches of every main-run feature labeled handoff: 15 features in 15 runs, 13 of them labeled handoff at run level.

- **Precision: 14 of 15 are correct.** The exception is react_hook_form 85 (run 3), where the agent chose a different implementation path rather than leaving work to its partner.
- **Kinds of handoff:**
  - 8 are layer splits: one agent writes the public API wrapper and the other implements the behavior behind it.
  - 3 hand over a whole feature, with an empty own patch. In one of these, both agents wrongly believed they had the same task.
  - In 3, the partner never implemented the part it was asked to do.
- **Recall.** We read 13 of the 26 runs still labeled context or capability: every run where the failing agent asked its partner to take work or had a very small patch, plus a sample of the rest. We found 4 more handoffs the rule misses: 2 split work inside a single file, which the file-based rule cannot see, and in 2 the partner published nothing. We also found 2 runs with mutual dependencies: each agent's patch calls code its partner was supposed to write, so its own patch fails when tested alone. The rule therefore catches roughly three quarters of handoffs, with few false positives.
- **Why handoffs happen.** CooperBench's coop prompt tells agents that their work succeeds only if "both patches merge cleanly with zero conflicts". So agents divide shared files by layer and code against interfaces their partner promised, but they work in separate workspaces and can never run the combined code. 11 of the 15 handoff runs merged cleanly, so the merge test did not change them; the combined code itself is wrong or incomplete.

### S2 targeted interventions (2026-11-03)

53 tasks × 4 arms × 3 runs = 636 runs, $6.34 for the agents and $0.08 for the 279 re-merges. Two runs hit API errors (a dropped connection and a false content-policy flag) and were retried once, as the runner does for every study, so the final data has no infrastructure errors. Per-run data: [results/s2_rows.csv](../results/s2_rows.csv). Differences are mean per-task differences against the baseline, in points, with 95% bootstrap intervals clustered by task.

| Arm | Pass after re-merge | vs baseline | Official pass | vs baseline (official) | Failures (with handoff) |
|---|---|---|---|---|---|
| coop baseline | 88 / 159 (55%) | — | 38 (24%) | — | capability 36, handoff 14, integration 11, context 10 |
| coop own-feature | 83 / 159 (52%) | −3.1 [−11.3, +5.0] | 34 (21%) | −2.5 [−10.7, +5.7] | capability 38, integration 29, submission 4, handoff 3, context 2 |
| coop shared-git | 81 / 159 (51%) | −4.4 [−10.7, +1.9] | 55 (35%) | +10.7 [+3.1, +18.9] | capability 44, context 13, handoff 12, integration 8, submission 1 |
| solo | 33 / 159 (21%) | | | | 98 / 159 counting unsubmitted code |

**Decision rule.** Neither arm beats the baseline. The matched test holds only in part: own-feature cuts handoff failures more than shared-git does (−5.7 points [−11.3, −0.6]), but shared-git does not cut context failures; own-feature cuts them instead (shared-git minus own-feature: +6.9 [+1.9, +12.6]). No arm drops capability failures by more than 10 points, so the labels pass the sanity check. Neither branch of the rule applies as written. Our reading: no arm helps, and there is no pair of cause-matched fixes for an online selector to choose between, so the coop baseline with the llm re-merge stays the default and S3 as planned does not go ahead.

Main results:

1. **Own-feature removes the failures it targets and creates integration failures.** Handoff failures drop from 14 to 3 runs (−6.9 points [−12.6, −1.9]) and context from 10 to 2. But both agents now edit the shared files: 113 runs conflict instead of 85, and integration failures that the re-merge cannot fix rise from 11 to 29 (+11.3 [+3.8, +19.5]). Agents also send about half as many messages (4.9 per run instead of 8.9). Avoiding conflicts by handing work to the partner looks like a reasonable response when merges are unreliable.
2. **Shared git does not reduce context failures.** Agents do use it to pull in each other's code: in 49 runs a partner's patch passes the agent's feature, against 19 in the baseline. Context failures stay flat (13 against 10) and capability failures rise from 36 to 44 (+5.0 [−0.0, +10.1]). We have not yet read the trajectories to see why.
3. **The official score would call shared git a success.** On CooperBench's score, shared git beats the baseline by 10.7 points with the interval above 0, which meets our threshold for "helps". 14 of its 55 official passes are conflicted runs, where CooperBench falls back to testing agent 1's patch alone, and that patch often already contains the partner's feature; the baseline has 5 such passes out of 38. After the re-merge the advantage is gone. The scoring rule changes the verdict on the intervention, not only the level.
4. **The re-merge more than doubles coop again.** The baseline passes 38 runs officially and 88 after the re-merge, the same pattern as the S1 merge test.
5. **Sensitivity.** Without go_chi and typst, own-feature is −4.4 [−11.1, +3.0] and shared-git −3.7 [−10.4, +2.2] over 45 tasks.

### Offline check on the CooperBench paper's runs (2026-11-03)

**Correction first.** The CooperBench paper does not score coop runs with a plain `git merge`. It runs `git merge-file`, then a union merge, then a small learned resolver (paper Sec. 2.2 and App. B). The released harness we pinned uses `git merge`, drops the union step and falls back to agent 1's patch alone on a conflict. "Official score" in S1 and S2 means the released harness, not the paper's pipeline.

The paper's coop and solo runs are public ([CooperBench/trajectories](https://huggingface.co/datasets/CooperBench/trajectories), revision 906bc2f): one run per pair, OpenHands agents with messaging, and for each pair the two agent patches and the result of each merge stage. We took the GPT-5 and Claude Sonnet 4.5 runs, re-merged every pair that conflicted and failed all three stages with the merge test's frozen llm method, and tested the result. No agent was re-run; the re-merges cost $0.08. firecrawl is left out because the pinned dataset does not include it. Config and decision rule: [configs/upstream_check.json](../configs/upstream_check.json). Per-pair data: [results/upstream_rows.csv](../results/upstream_rows.csv).

**Calibration.** On 30 random clean-merge pairs per model, our harness matched the recorded per-feature results on 55 of 60 features for GPT-5 and 51 of 60 for Claude, below the 90% bar. The misses come mostly from the merge, not the tests: our `git merge` conflicted on 5 (GPT-5) and 13 (Claude) of the 30 pairs that the paper's `git merge-file` merged cleanly. Where both merges were clean, 47 of 50 and 32 of 34 features matched (94%). So the released harness conflicts more often than the paper's pipeline, and its scores are lower for that reason alone.

| | GPT-5 | Claude Sonnet 4.5 |
|---|---|---|
| Coop pairs (tasks) | 571 (26) | 607 (29) |
| Coop success, paper pipeline | 28.2% | 22.6% |
| With our re-merge added | 32.9% | 26.0% |
| Gain | +4.7 [+0.7, +9.1] | +3.5 [+0.4, +8.9] |
| Solo success on pairs with both | 45.8% (441 pairs) | 46.2% (532 pairs) |
| Coop–solo gap closed | 20% | 3% |

**Decision rule.** The re-merge helps significantly but closes less than a third of the gap for both models, so the claim that much of the coop–solo gap is a merge artifact does not hold for the paper's runs.

**Why (post hoc).** After the primary result we tested each agent's own patch alone on the re-merged pairs, which the config marks as post hoc:

- Pairs where both agents' own patches pass their own features, the integration cases our merge can fix, are rare: 16 of 121 conflicted pairs for GPT-5 and 20 of 98 for Claude, about 4% of each model's failed pairs. The re-merge recovers 12 of 16 and 18 of 20, close to its 84% in the S1 merge test.
- Most failures are not conflicts at all: 289 of GPT-5's 410 failed pairs and 372 of Claude's 470 merged cleanly and still failed the tests.

So the merge works as well on these runs as on ours, but these agents rarely produce the failure it fixes. The large merge effect in S1 and S2 belongs to our setup: gpt-6-luna with mini-swe-agent, the released harness, and a prompt that rewards zero conflicts. In the paper's setup the coordination gap is real and comes mostly from code that merges cleanly but does not work.

**Correction (2026-11-04).** The audit below withdraws the last two bullets and this reading. Most of the paper's "clean" merges never contained agent 1's code. Merged for real, 48% of GPT-5's failed pairs and 74% of Claude's conflict. Once every merge problem is corrected, the coop–solo gap closes by 68% and 85%.

### Failure census on the paper's runs (2026-11-04)

We labeled every GPT-5 and Claude Sonnet 4.5 coop pair that failed the paper's pipeline, 880 pairs in all. Each agent's published patch was tested alone against both features in our harness. For pairs the paper merged cleanly, we also merged the two patches with the released harness and tested the result. The solo reference is the published solo runs. Labels follow S1 and were frozen before any of these tests: [configs/paper_census.json](../configs/paper_census.json). Docker only, no API cost. Per-pair data: [results/paper_census_rows.csv](../results/paper_census_rows.csv).

| | GPT-5 | Claude Sonnet 4.5 |
|---|---|---|
| Failed pairs (clean merges) | 410 (289) | 470 (372) |
| Labeled | 396 | 470 |
| All failed pairs | integration 171, context 101, capability 67, unknown 54 | integration 223, capability 112, context 110, unknown 25 |
| Integration: clean merge / conflict | 100 / 71 | 156 / 67 |
| Gap pairs (solo passes both, coop fails) | 100 | 158 |
| Integration share of gap pairs | 47% [28%, 66%] | 67% [56%, 75%] |
| Robust share (gap pairs left) | 33% [16%, 49%] (67) | 41% [19%, 59%] (32) |

14 GPT-5 pairs have no recorded merged result, and our merge conflicted, so they are left out. In 3 more, our re-test passes both features.

**Decision rule.** GPT-5 is below half and Claude above, so the verdict is split and reported per model. By the rule, GPT-5's gap comes mainly from agents failing their own feature, and Claude's from code that does not fit together. GPT-5's interval includes one half.

**The robustness check weakens the integration result for both models.** Counting only failures that our merged re-test reproduces, the integration share drops below half for both. The integration labels are the ones that fail to reproduce:

- Take gap pairs labeled integration after a clean merge, where our merge is also clean. Our merged re-test fails the feature in only 8 of 19 for GPT-5 and 3 of 13 for Claude. In the rest, each own patch and the merged code all pass in our harness.
- Context labels reproduce in 34 of 37 and 16 of 25.
- Over all clean pairs that also merge cleanly for us, recorded feature failures reproduce at 88% (243 / 277) and 81% (140 / 173).

This follows from how the label is built. The own-patch result comes from our harness and the merged result from the paper's, so a feature that the paper's evaluation failed but ours passes looks like an integration failure. We do not yet know whether these are flaky tests or differences between the two evaluations. For Claude the robust set is small because our `git merge` conflicts on 250 of the 372 clean pairs, including 82 of the 95 gap pairs labeled integration.

Our reading: once the disagreement between harnesses is removed, the data do not show code that does not fit together as the main cause of the coop–solo gap for either model. Agents failing their own feature when paired (context, including handoff) is at least as large. The two evaluations disagree most where the integration label is decided, so the way merged code is evaluated is itself a major source of uncertainty in failure attribution.

**Test timeouts.** CooperBench's Docker backend does not enforce its 600 s test timeout: it calls exec_run inside a `with ThreadPoolExecutor` block, which waits for the command even after the timeout. In one Claude pair (pillow 290, features 1 and 4), agent 4's code loops in `test_image_quantize`. We killed the merged re-test by hand after about 25 minutes, and [scripts/test_watchdog.py](../scripts/test_watchdog.py) killed agent 4's own-patch test at 600 s. Both count as failures, as they would under the intended timeout. Log: [results/paper_census_timeouts.log](../results/paper_census_timeouts.log).

### Re-scoring the paper's test logs (2026-11-04)

Each published coop pair comes with a merge report that holds the full test output of every merge stage. CooperBench's evaluation code counts a run as failed when the output contains "failed" anywhere, in any case. In the first log we read (GPT-5, pillow 290, features 1 and 4), every test passed, but a numpy deprecation warning ("passing copy=False failed") made the run count as failed. We re-scored all 2,243 logs of the GPT-5 and Claude pairs with that check re-implemented and with a check that reads the test framework's own summary. Config: [configs/paper_rescore.json](../configs/paper_rescore.json). Per-log data: [results/paper_rescore_rows.csv](../results/paper_rescore_rows.csv).

- The re-implemented check matches the recorded result on all 2,211 logs that have one, so this is the check the paper used. 7 pairs have no report.
- It rarely changes the result: 6 GPT-5 naive-merge results flip from fail to pass, and no Claude result does. GPT-5 coop success moves from 161 to 164 of 571 pairs.
- **Decision rule.** Of the 67 unreproduced merged failures, only 2 pass when re-scored (3% [0%, 9%]). The pass check does not explain the non-reproduction, so the rule sends us to Docker re-runs. The census barely changes: GPT-5's gap-pair integration share becomes 49% [31%, 68%]; Claude's is unchanged.

**What the logs show instead (post hoc; tested in the next section).** We read the paper's failing output for the 67 unreproduced features:

- Most fail because the feature's code is not in the program under test: an unexpected keyword argument, a missing attribute, a failed import. In 3, the merged patch was empty.
- The paper also published its naive-merge patch. In 45 of the 67, that patch lacks at least half of the lines the agent's own patch adds. Among non-empty reproduced failures, this happens in 30 of 113.
- Example: GPT-5, tiktoken, features 1 and 4. The published merged patch is byte-identical to agent 4's patch, and the report calls the merge clean. Feature 1's test fails because `max_tokens` does not exist.

So the unreproduced failures look like code lost in the paper's merge or patch step, not agents' code that does not fit together. Two things keep this from being a conclusion:

- Published merged patches that lack one side's lines also occur in 68 of the 239 clean pairs the paper recorded as passing.
- In the tiktoken example, feature 4 also failed for a missing argument that agent 4's patch adds.

So the published patch may not be exactly the code the paper tested. Testing the published merged patches in our harness would settle it.

### The paper's merged patches tested in our harness (2026-11-04)

We applied the paper's published naive-merge patch alone to the clean repository and ran both features' tests, once per pair. We covered every pair with an unreproduced failure (51 pairs) and three control sets. Docker only, no API cost. Config and decision rule: [configs/paper_merge_check.json](../configs/paper_merge_check.json). Per-feature data: [results/paper_merge_check_rows.csv](../results/paper_merge_check_rows.csv).

| Set | Features | Pass with the paper's merged patch |
|---|---|---|
| Unreproduced failures (non-empty patch) | 64 | 2 (62 fail, 97% [90%, 100%]) |
| Control: paper passed, patch has both sides | 30 | 30 |
| Control: paper passed, patch lacks one side by the line check | 40 | 39 |
| Reference: reproduced failures, both features of 15 pairs | 30 | 10 |

**Decision rule.** 97% of the unreproduced features also fail with the paper's own merged patch, above the 80% threshold. Our harness passes every feature of the both-sides control, so it agrees with the paper's environment. In 55 of the 56 failures we could compare, our output contains the same first error line as the paper's. So the paper's merged code explains why our re-merge passes. The cause is not our environment or flaky tests.

- **The paper's merge step loses code.** Take the 48 pairs behind the 64 features. In 27, the paper's merged patch is identical to one agent's patch: the other agent's work is missing entirely. In the other 21, part of it is missing or changed. Our `git merge` of the same two agent patches merges cleanly and passes the feature in every case.
- **The line check does not find these pairs on its own.** In the one-side control, published patches that lack half of one agent's lines still pass 39 of 40 features. Other code in the patch can cover the feature, for example when the partner implemented part of it. So we count affected pairs by our re-merge, not by the line check.
- **Effect on the paper's result (post hoc).** We replaced the paper's recorded failures with our clean re-merge's result wherever both features pass: 18 GPT-5 pairs and 19 Claude pairs. Coop success then rises from 28.2% to 31.3% for GPT-5 and from 22.6% to 25.7% for Claude. On the pairs with a solo run, adding these to the re-merge of conflicted pairs closes 43% of the coop–solo gap for GPT-5 (20% from conflicts, 23% from lost code) and 19% for Claude (3% and 16%). This is a lower bound, because our merge also conflicts on 79 of GPT-5's and 250 of Claude's clean failed pairs, which we could not check. Solo runs involve no merge, so this does not affect them.
- **For the census**, the robust share, which counts only failures our re-merge reproduces, becomes the main estimate: integration is 33% [16%, 49%] of GPT-5's gap pairs and 41% [19%, 59%] of Claude's. Failures from the paper's lost code are not failures of the agents. The audit below replaces this with a census on our own merges.

### Audit of the paper's clean merges (2026-11-04)

The paper merges by checking out agent 2's branch and merging agent 1's branch into it. For each pair it keeps the diff that merge produces, and it tests a merged patch. In most pairs it recorded as clean, that diff is empty and the tested patch holds only agent 2's code: agent 1's work never entered the merge, so the merge could not conflict. The paper-era merge code we can read (commit 95cd5a2, released after the runs) looks correct. So the published files show what happened, but not why.

We counted, for every clean pair, which agents' code the tested patch holds. Then we re-merged the failed "clean" pairs whose merge conflicts in our harness, using the upstream check's frozen llm method, and tested them. The patch counts were seen before freezing; the re-merges were not. Config and decision rule: [configs/paper_merge_audit.json](../configs/paper_merge_audit.json). Per-pair data: [results/paper_merge_audit_rows.csv](../results/paper_merge_audit_rows.csv). The re-merges cost $0.14. Eight test runs on outlines 1371 hung and were killed at 600 s; they count as failures.

| | GPT-5 | Claude Sonnet 4.5 |
|---|---|---|
| Failed clean pairs whose tested patch lacks agent 1's code (of those with a published patch and a non-empty agent 1 patch) | 96 / 124 | 277 / 310 |
| Failed pairs that conflict when merged for real (of those with a known merge) | 196 / 406 (48%) | 345 / 467 (74%) |
| Conflicts the paper recorded among failed pairs | 121 / 410 (30%) | 98 / 470 (21%) |
| Mislabeled pairs re-merged / passing both features | 75 / 19 | 247 / 80 |
| Solo success, pairs with a solo run | 202 / 441 (45.8%) | 246 / 532 (46.2%) |
| Coop, paper pipeline | 126 (28.6%) | 125 (23.5%) |
| + paper's conflicts re-merged (upstream check) | 141 (gap closed 20%) | 129 (3%) |
| + clean pairs our clean merge passes (merged-patch check) | 159 (43%) | 148 (19%) |
| + mislabeled pairs re-merged (this audit) | 178 (40.4%, 68%) | 228 (42.9%, 85%) |

**Decision rule.** With every merge correction applied, the coop–solo gap closes by 68% for GPT-5 and 85% for Claude. Both are above a third, so for both models much of the paper's coop–solo gap is a merge artifact. This reverses the upstream check's conclusion. The tested patch lost agent 1's code in more than a quarter of each model's failed clean pairs, so the paper's clean-merge share does not measure conflicts. Our statement that most strong-model failures merge cleanly is withdrawn. When the code is actually merged, conflicts are common for both models, most of all for Claude.

**Correction (2026-11-05).** The sensitivity check below withdraws "much of the gap is a merge artifact". Two things go wrong when coop and solo are scored in the same harness. First, the paper's merge bug also produces false passes: in the Claude sample, 9 of the 39 pairs it recorded as passing fail once merged correctly. Second, the 68% and 85% above depend on gpt-6-luna resolving the conflicts. How much of the gap is an artifact depends on how conflicts are resolved.

Caveats:

- Conflicts are resolved with gpt-6-luna, which may be stronger than the paper's small resolver.
- 12 failed pairs have no usable merge of ours and keep the paper's result.
- Solo results are the paper's. They come from the same "failed" check, but they involve no merge.

**Census on our own merges (post hoc).** We re-labeled the census with our merge in place of the paper's: our merged re-test where our merge is clean, the llm re-merge where it conflicts.

| | GPT-5 | Claude Sonnet 4.5 |
|---|---|---|
| Failed pairs that pass once merged correctly | 64 | 120 |
| Gap pairs left (solo passes both, coop fails) | 73 | 75 |
| Context (own feature fails, solo can do it) | 49 | 43 |
| Integration | 17, 23% [9%, 47%] | 27, 36% [20%, 50%] |
| Capability | 7 | 5 |

Among real failures that solo can avoid, an agent failing its own feature when paired is the largest group for both models. Integration is a quarter to a third. Most of Claude's integration failures are conflicts even the llm re-merge cannot resolve: 56 of its 66 across all failed pairs. This census starts from the paper's failed pairs only, so it misses the false passes the sensitivity check found.

### Sensitivity of the merge correction (2026-11-05)

We checked whether the audit's conclusion depends on two choices:

- **Conflict resolution.** We re-merged every failed pair whose real merge conflicts with git's union merge (keep both sides of every conflict block), not gpt-6-luna. That gives four levels:
  - L0: the paper's results.
  - L1: no conflict resolution of ours, only clean merges.
  - L2: union.
  - L3: llm.
- **One harness for coop and solo.** On 150 random pairs per model, we re-tested the published solo patch and scored coop in our harness too.

Docker only, plus a few cents for the llm re-merges of sampled pairs. Config and decision rule: [configs/paper_sensitivity.json](../configs/paper_sensitivity.json). Per-pair data: [results/paper_sensitivity_rows.csv](../results/paper_sensitivity_rows.csv). One test run hung (outlines 1371, union) and counts as a failure.

Share of the paper's coop–solo gap closed:

| | GPT-5 | Claude Sonnet 4.5 |
|---|---|---|
| All pairs with a solo run, solo as recorded: L1 / L2 / L3 | 24% / 34% [13%, 67%] / 68% | 16% / 47% [24%, 84%] / 85% |
| Sample, solo as recorded: L2 / L3 | 17% / 31% | 45% / 95% |
| Sample, coop and solo in our harness: L1 | −21% [−60%, 18%] | −64% [−300%, −17%] |
| Sample, coop and solo in our harness: L2 | 7% [−14%, 43%] | 0% [−140%, 43%] |
| Sample, coop and solo in our harness: L3 | 28% [11%, 59%] | 64% [17%, 144%] |
| Sample: solo passes, recorded / ours | 71 / 72 (148 pairs) | 61 / 59 (144 pairs) |
| Sample: coop passes, paper / ours at L1, L2, L3 | 42 / 37, 45, 51 | 39 / 23, 37, 51 |

**Decision rule.** On all pairs, union closes at least a third of the gap for both models. In one harness, though, it closes 7% and 0%, more than 15 points off. By the rule, the same-harness figure is the main one, and it is below a third. So the conclusion depends on conflict resolution, and it is reported as a range. (The report code first applied the threshold to the full-population figure even when the same-harness one took over. We fixed that before reading the verdict.)

What the sample shows:

- **Solo is scored the same in both harnesses.** 5 of 148 GPT-5 pairs and 2 of 144 Claude pairs disagree. The paper's solo results are not biased.
- **The paper's coop passes are not all real.** In our harness, 9 of the 39 Claude pairs the paper recorded as passing fail even with the llm re-merge. In 5 of them the two patches merge cleanly but fail the tests: agent 1's code, which the paper never tested, breaks something. All 42 of GPT-5's recorded passes still pass at L3; 9 of them conflict and need a resolver.
- **So the merge bug cuts both ways.** It fails pairs by dropping code the tests need. It passes pairs by never merging code that conflicts or breaks the build.

**Reading.** The paper's coop numbers for GPT-5 and Claude are not a valid measurement, in either direction. With everything scored in one harness, the real coop–solo gap depends on conflict resolution:
- Without an llm resolver it is about the size the paper reports.
- With gpt-6-luna resolving conflicts it shrinks by about a quarter for GPT-5 (28% [11%, 59%]) and by about two thirds for Claude (64% [17%, 144%]).

The sample is 150 pairs per model, so these intervals are wide.

### Coordination conflict states (2026-11-10)

The research direction moved to how agents handle conflicts with each other (Thomas–Kilmann styles), starting with coordination conflicts (class C in the team's benchmark plan). This study builds the first conflict-state library from the coop runs we already have: all gpt-6-luna, no new agent runs. Config: [configs/c_conflict_states.json](../configs/c_conflict_states.json). Codebook: [C_CONFLICT_CODEBOOK.md](C_CONFLICT_CODEBOOK.md). Per-run data: [results/c_conflict_states_rows.csv](../results/c_conflict_states_rows.csv). Labeling cost $0.49 in total.

- **Conflict point (detector).** The first message after which both agents claim the same file. For each agent, the trajectory step where it has sent its claim and received the partner's.
- **Labels (model).** gpt-6-luna reads the feature titles and the messages in order, never the code or test results, and labels conflict types, each agent's assertiveness and cooperativeness (0–2, mapped to a style), and a few facts about the episode.
- **Codebook revisions.** Three revisions on the screening runs (dev). Version 3 was then applied to the library. A hand check of 30 library runs agreed with 42 of 60 agent styles, almost every miss scoring a plain split of the shared file as collaborating, so those labels were withdrawn. That check showed only a few messages around the conflict point, so it overstates the error somewhat. Versions 4 and 5, revised on dev only, make keeping edits apart cooperativeness 1. The library was relabeled with version 5.

Library: S1 main and the S2 baseline, default CooperBench prompt, 318 runs.

| | S1 main | S2 baseline |
|---|---|---|
| Runs with a conflict point | 159 / 159 | 158 / 159 |
| ...with a request to stay out of the file (strict reading) | 91 | 88 |
| Conflict step per agent, median (trajectory length ~41) | 6 | 7 |
| Agent trajectories with a file write before the conflict point | 15 / 286 | 11 / 282 |
| Conflict types: interface / responsibility / requirement | 96 / 51 / 1 | 112 / 73 / 1 |
| Agent styles: collaborating / compromising / accommodating / avoiding / competing | 143 / 133 / 33 / 5 / 4 | 156 / 97 / 53 / 7 / 3 |
| Handoff (an agent asks its partner to implement part of its own feature) | 52 | 71 |
| A direct question left unanswered | 27 | 32 |

Every run has a resource conflict, since all 652 CooperBench pairs touch a common file (86% common lines). About 16% of runs have an agent whose trajectory was compacted into a summary; their messages are ordered by sender only and their conflict step is unknown.

Checks:

- **Labeler consistency.** 40 library runs labeled twice. Cohen's kappa: styles 0.82 and 0.95 (agent 1, agent 2), assertiveness 0.78–0.84, cooperativeness 0.90–1.00, conflict types 0.89–1.00, handoff 0.89, unanswered question 0.87. Lower for whether the claims were incompatible (0.50) and for the kind of agreement (0.54).
- **Hand check.** The same 30 runs, now with whole episodes: 56 of 60 agent styles agree. The four misses score agreement on where each change goes inside one function as collaborating. This is one reader (Claude), not independent annotation. A blind sheet for teammates is in [results/c_conflict_annotation_sheet.csv](../results/c_conflict_annotation_sheet.csv).
- **Detector against labeler.** Where the detector saw a request to stay out of the file, the labeler judged the claims incompatible in 150 of 179 library runs.

What it shows:

- **Conflicts need not be constructed for class C.** In CooperBench they happen in almost every run, at the agents' first exchange.
- **Branching is cheap.** 95% of agent trajectories have written nothing when the conflict point is reached. A branch can restart from the message history in a fresh container, with no code state to replay.
- **The default style is not avoidance.** Under the default prompt, gpt-6-luna agents mostly collaborate or compromise when they negotiate. This contradicts our first hypothesis that agents default to avoiding and accommodating. Accommodating appears mainly as handoffs, in 39% of conflicts.
- **The prompt moves the style.** Telling agents that overlapping edits merge automatically (own-feature) leaves 42 of 159 runs without a conflict point. It cuts handoffs from 71 to 9 and accommodating agents from 53 to 4, and raises avoiding (14) and competing (10).
- **No clear link from style to outcome in observational data.** In S1, collaborating pairs passed less often than compromising pairs (6/49 against 16/57). In S2, where conflicted runs are re-merged, they pass equally often (28/45 against 25/40). Collaboration happens most when both features change the same function, which also causes more merge conflicts. Task structure confounds these comparisons, which is why style has to be set by intervention.

### Branching pilot from conflict states (2026-11-10)

Interventions on style need runs that restart at the conflict point. This pilot builds that restart and measures how much unchanged restarts vary (A/A). Config: [configs/c_branch_pilot.json](../configs/c_branch_pilot.json). Code: `src/agent_coordination/branch.py`. Per-branch data: [results/c_branch_pilot_rows.csv](../results/c_branch_pilot_rows.csv). Cost: $0.23 for the agents, $0.01 for labels, plus cents of re-merging.

- **The cut.**
  - The sender of the conflict message keeps its history up to the step that sent it.
  - The receiver keeps its history up to just before it receives the message.
  - Messages sent but not yet received go back into the receiver's inbox.
  - Both agents then start in fresh containers from the task image.
  - Most eligible runs are cut after each agent's first step, while both claims are still in flight.
- **Eligible runs.** 263 of the 318 library runs. Excluded: 34 with a compacted trajectory, 12 with a file write before the cut, 5 where the cut is not consistent, 3 where the conflict message was never received, and 1 with no conflict point.
- **Design.** 10 states: 5 from S1 main and 5 from the S2 baseline, one per task, drawn with a fixed seed. Each state is continued twice with nothing changed. The original run serves as a third continuation. Scoring follows S2: the llm re-merge score is primary.

Checks:

- All 20 branches finished without agent errors. One OpenAI policy flag was retried successfully; S1 and S2 logs show 9 such flags.
- Each agent's prompt rendered as in its original run. The one difference was the host OS line, which changed with a macOS update. It is ignored, and the restored history keeps the original text.
- In all 20 branches, each agent's first new step received exactly the messages left in its inbox.
- 3 branches compacted their context during the run. Their messages fall back to sender order, as in the library, and they are left out of the style comparisons.

| | Result |
|---|---|
| Both features pass after the re-merge: originals / branches | 6 / 10, 11 / 20 |
| Official score: originals / branches | 1 / 10, 3 / 20 |
| Same re-merged outcome: continuation 1 vs 2 / branch vs original | 7 / 10, 15 / 20 |
| States with the same outcome in all three continuations | 6 / 10 |
| Same agent style: continuation 1 vs 2 / branch vs original | 12 / 16, 28 / 34 |
| Same agent style when one transcript is labeled twice (library) | 74 / 80 |
| Same handoff flag: continuation 1 vs 2 / branch vs original | 7 / 8, 10 / 17 |
| Branch duration, median (longest) | 2.5 min (42 min) |

What it shows:

- **Restarting works.** The branches pass about as often as their originals (55% against 60%, on 10 states).
- **Outcomes are noisy.** In 3 of 10 states, the two unchanged continuations disagree on whether both features pass. With about 30% discordant pairs, a paired comparison of two arms needs roughly 100 states to detect a 15-point difference in pass rate.
- **Style is mostly set by the state.** 75–82% of agent styles repeat across continuations, against 92% when the same transcript is labeled twice. A prompt that really moves the style should show up on far fewer states than an effect on outcomes. Handoffs vary more: they matched the original in only 10 of 17 branches.
- **Throughput is limited by a long tail.** 20 branches took 75 minutes with 4 workers. A few branches ran 20–40 minutes while most finished in under 3. An intervention study with hundreds of branches will take a day or more unless long branches are capped.

### Style interventions at the conflict point (2026-11-11)

Does telling both agents how to handle the overlap change how they negotiate, and whether both features pass? Config, frozen before the first branch: [configs/c_style_interventions.json](../configs/c_style_interventions.json). Per-branch data: [results/c_style_interventions_rows.csv](../results/c_style_interventions_rows.csv). Cost: $3.06 for the agents, $0.20 for labels, $0.05 for re-merges. 312 branches ran in about 7 hours with 6 workers.

- **States.** 52, one per feature pair, the most the library allows. All are class C conflicts, and all are resource conflicts. 35 also involve an interface and 15 a question of who writes shared code. None involves contradictory requirements.
- **Arms.** Six: a neutral control and the five Thomas–Kilmann styles. Each state is restarted once per arm. Right after each agent receives its partner's claim, it gets one instruction, the same for both agents.
  - The instructions describe behaviour and never name the style, so agents cannot pass the label on to the labeler.
  - The neutral control ("handle the overlap in whatever way you think best") has the same form, so every arm draws the same attention to the overlap.
- **Measures.**
  - Primary: both features pass after the llm re-merge. Each arm is compared with neutral, paired by state, with a sign test on the states where the two disagree.
  - Manipulation check: the frozen codebook-v5 labeler, which sees only the messages between agents.
  - Patch-based check: whether an agent's final patch leaves the contested file alone.

Checks: every branch resumed with its inbox and instruction in place. 27 branches compacted their context and are left out of the style counts.

| Arm | Pass after re-merge | Only this arm / only neutral passes | Official | Merge conflicts | Messages per run | Runs with a handoff | Agents in the target style | Agents leaving the contested file |
|---|---|---|---|---|---|---|---|---|
| Neutral | 30 / 52 | | 16 | 31 | 9.6 | 26 | | 6 / 98 |
| Competing | 26 / 52 | 3 / 7 | 13 | 38 | 7.0 | 4 | 2 / 96 | 1 / 96 |
| Collaborating | 29 / 52 | 5 / 6 | 17 | 30 | 11.3 | 16 | 57 / 86 (neutral 50 / 98) | 3 / 86 |
| Compromising | **18 / 52** | **0 / 12** (p = 0.0005) | 9 | 27 | 9.3 | 23 | 33 / 96 (neutral 28 / 98) | 5 / 96 |
| Accommodating | 28 / 52 | 3 / 5 | 16 | 30 | 9.8 | 17 | 16 / 90 (neutral 20 / 98) | 5 / 90 |
| Avoiding | 24 / 52 | 1 / 7 (p = 0.07) | 7 | 34 | 3.8 | 15 | 11 / 104 | 6 / 104 |

What it shows:

- **The labeled style barely moves.** Under every instruction most agents are still labeled collaborating or compromising.
  - Collaborating raises its style from 51% to 66% of agents. More states rise than fall (15 against 8), but that is not significant (p = 0.21).
  - Avoiding is the only arm with a clear per-state shift: 10 states have more avoiding agents and none fewer (p = 0.002). It also produces 12 competing agents.
  - Competing, accommodating and compromising leave their target style where it was.
  - Reading the transcripts explains why. Agents follow the instruction in tone ("I'll defer to your feature", "please work around my changes") but keep every edit their own feature needs, and they still say where they will edit. In almost no branch does an agent give up the contested file (1–6% in every arm). Both reference solutions edit that file, so giving it up would mean failing one's own feature. CooperBench conflicts are unavoidable resource conflicts, and conceding is not a real option in them.
- **The instructions change communication and handoffs.**
  - Avoiding cuts messages by 60%: fewer in 50 of 52 states.
  - Competing cuts messages by 27%, and runs with a handoff from 26 to 4.
  - Collaborating adds 18% more messages (p = 0.04).
- **No instruction beats neutral, and compromising hurts.**
  - Compromising passes 18 of 52 against 30. All 12 states where the two arms differ favour neutral (p = 0.0005; still below 0.01 after a Bonferroni correction for five arms).
  - Each agent's own patch, tested alone after the fact for these two arms, shows the mechanism. Under compromising, both agents' own patches pass their own features in 19 runs, against 26 under neutral. In 8 of the 12 discordant states, an agent whose feature passed under neutral fails it under compromising, consistent with the instruction to "give up a little". The other 4 are merge failures.
  - Avoiding trends worse: 24 against 30 after the re-merge, and 7 against 16 on the official score, because merge conflicts rise.
  - Collaborating's extra messages buy nothing (29 against 30).
- **No sign that style matters more for interface conflicts.** Pass rates per arm follow the same pattern in the 35 states with an interface dependency and in the 17 without.
- **Limits.**
  - 52 states detect only large effects. For example, the competing and accommodating arms cannot be told apart from neutral.
  - The own-patch tests and the patch-based measures were added after the outcomes were seen, and are exploratory.
  - One model (gpt-6-luna) and one benchmark.
- **What it means for the dataset.** To study real concession and competition, we need tasks where giving up a resource or insisting on it are both feasible: for example, alternative implementations, optional shared helpers, or contested design choices, rather than two features that both must edit the same lines.

### ParallACT pilot (2026-11-11)

The style interventions suggest that CooperBench leaves no room for conceding. [ParallACT](https://github.com/rm206/ParallACT_data_and_code) (arXiv 2611.00314) has contradictory tasks: two separate requests change the same code in incompatible ways, and a reconciled implementation that satisfies both exists. Can our coop agents run them, and does conceding happen there? Config: [configs/parallact_pilot.json](../configs/parallact_pilot.json). Per-run data: [results/parallact_pilot_rows.csv](../results/parallact_pilot_rows.csv). 20 instances (all 12 hand-built seeds and 8 random generated ones), one coop run each, gpt-6-luna with CooperBench's coop prompt. Cost $0.24, 7.6 minutes per run on average (Go repositories up to 32).

- **Harness.** Each instance runs as a two-feature CooperBench task in its SWE-Bench Pro image, scored with ParallACT's grader: each agent's patch against its own request's tests, and the git merge of both patches (llm re-merge on conflict) against the combined tests. For all 20 instances the reference patches behave as the dataset claims in our harness: the base fails, each reference patch passes its own tests, the reconciled patch passes the combined tests, and a plain git merge of the two reference patches conflicts.
- **Outcomes.** Both requests pass after the merge in 4 of 20 runs; A only in 1, B only in 6, neither in 9.
  - Of the 40 requests, 15 pass after the merge. 17 already fail in the owning agent's own patch.
  - 8 pass in the agent's own patch and fail after the merge: the two implementations do not combine. None of the 6 runs whose merge needed the llm resolver passes both requests, as the dataset intends.
- **The agents negotiate the conflict.** Every run has messages about the shared code (6.5 on average). In one, both agents first claim the same parameter of `getObjects` for their own request, then agree on the reference design: an array selects fields, an object carries options.
- **Conceding is feasible and works.** In 4 runs one agent handed the shared code to its partner and submitted no code of its own. In 3 of them the partner implemented both requests and both pass; these are 3 of the 4 runs where both requests pass. In the fourth, the partner's implementation failed. In CooperBench, leaving part of one's feature to the partner was a failure mode (13 S1 failures).
- **Harness fixes during the pilot.** A stalled API connection held three runs for over an hour. The first fix, setting `litellm.request_timeout`, had no effect: `completion` does not read it, and a stall recurred on the dev set of the style arms (2026-11-12). Every model call now has a hard wall-clock limit of 330 s, after which the agent retries. An empty patch made the merge step fail; four runs were re-scored, no agent was re-run.
- **Solo baseline.** One agent given both requests, on the same 20 instances, one run each ([configs/parallact_solo.json](../configs/parallact_solo.json), [results/parallact_solo_rows.csv](../results/parallact_solo_rows.csv); $0.17).
  - Its local work at the end satisfies both requests in 6 of 20 instances (22 of 40 requests), against 4 of 20 (15 of 40) for the coop pair. Its published patch does in only 2: in 9 runs the solo agent never published, as in S1. Local work is the fairer measure of what the model can do.
  - The three instances that solo solves and coop does not are the three coop runs where both agents' own patches passed and the merged patch failed. In each, both agents implemented their request in the shared code and the incompatible implementations did not combine. The one instance coop solves and solo does not is a run where one agent conceded and its partner implemented both requests.
  - So on these 20 instances most failures are capability: neither setting satisfies both requests in 13. The coordination gap is about 3 instances (15 points), and that is the most a better handling style could recover here.
- **Limits.** One run per instance and 20 instances, so every count above is noisy. The observation about conceding is observational and rests on 4 runs.

### ParallACT conflict states (2026-11-12)

Does the method we used on CooperBench (find the first conflict point, restart both agents there, label styles from the messages) carry over to ParallACT? We applied the detector and codebook version 5, unchanged, to the 20 pilot coop runs. Config: [configs/parallact_conflict_states.json](../configs/parallact_conflict_states.json). Per-run data: [results/parallact_conflict_states_rows.csv](../results/parallact_conflict_states_rows.csv). Every run was labeled twice; labels cost $0.02.

- **The conflict surfaces late, after the agents have started writing.**
  - Agents announce their plan at once: the first message is sent at step 2 (median) and the partner's first message arrives at step 6. Only 1 of 36 agents had written a file by then.
  - A conflict point (both agents claim the same file) is detected in 14 of 20 runs, at step 20 (median; trajectories are 23 to 81 steps long). In CooperBench it came at step 6 or 7. By then 12 of the 28 agents had written files, against 5% in CooperBench.
  - 7 runs have an agent that compacted its context (35%; about 16% in CooperBench). Their message order is now rebuilt from the snapshots in the full trajectory.
- **Restarting at the conflict point works for few runs.** The branching code accepts 4 of 20 runs (5 are excluded for files written before the cut, 7 for compaction, 4 have no conflict point). Restoring compacted agents from their history before the compaction would raise that to 7. In CooperBench 263 of 318 runs could be restarted.
- **In 6 runs the detector finds no conflict point.** In four the agents hardly exchange anything (1 to 5 messages, one side announcing). In one both claim the same three methods without naming a file, which the detector misses. In one an agent hands a component to its partner.
- **Default styles.** Over the 40 agents: compromising 18, collaborating 10, accommodating 8, competing 2, avoiding 2. Accommodating is more common than in CooperBench (20% against 14%), and so are handoffs (11 of 20 runs against 39%).
- **The agents almost never say that the requests contradict.** The labeler marks a requirement conflict in 2 of 20 runs, and its two passes do not agree on which. The agents treat the overlap as "we both edit this function" and split it or hand it over.
- **Where coordination failed, the conflict was not discussed.** Of the three instances that solo solves and coop does not, two had 1 and 3 messages and no exchange about the shared code; in the third one agent accommodated and the other competed.
- **The labeler recognizes conceding.** In the four runs where an agent submitted no code of its own, the first pass scores that agent accommodating in all four and the second in three. Of the 8 runs with an accommodating agent, 3 pass both requests; of the 12 without, 1 does (observational, small counts).
- **Labeler consistency is lower than on CooperBench.** Cohen's kappa over the 20 runs: styles 0.76 and 0.71 (0.82 and 0.95 on CooperBench), assertiveness 0.79 and 0.70, cooperativeness 0.72 and 0.75, handoff 0.90, incompatible claims 0.89.

What it means for the intervention design:

- A style instruction at the detected conflict point would reach a third of the runs at most, and not the ones where the conflict is never discussed. The instruction has to come at the start of the run, or at the first exchange (step 6, before almost any write), worded so that it does not presuppose a known overlap.
- Getting the agents to state what their request needs from the shared code comes before handling style. An arm that asks for this in the first message (the structured-message idea) is the natural companion to the style arms.
- Limits: 20 runs, one per instance; the conflict detector keys on file names and was not adapted to ParallACT.

### ParallACT style interventions: design (2026-11-12)

Status: draft, dev check done; the config is frozen before the first test run. Config: [configs/parallact_styles.json](../configs/parallact_styles.json), dev: [configs/parallact_styles_dev.json](../configs/parallact_styles_dev.json). No test results yet.

- **Question.** Where conceding is feasible, does telling both agents how to handle an overlap change how they negotiate, whose request is satisfied, and whether both requests pass? Does asking them to state what they need from the shared code help?
- **Timing.** The instruction is a user message to both agents before their first model call. It cannot wait for a conflict point as on CooperBench: on ParallACT that point comes late or never (see above).
- **Arms.** Eight, each on every instance:
  - default: CooperBench's coop prompt as it is.
  - neutral (control): "handle the overlap in whatever way you think best". Same form as the style arms, so every guided arm draws the same attention to a possible overlap.
  - the five Thomas–Kilmann styles, with the sentences used on CooperBench, unchanged. Only the opening differs ("your features may need changes to the same code"), because the overlap is not yet known.
  - structured: before editing, each agent lists the files and functions it will change, what its feature requires of each (signature, arguments, return value, behaviour) and what it needs from its partner; then handles the overlap as it sees fit. This is not a style. It tests whether stating needs up front helps, since the pilot agents rarely said that their requests contradict.
- **Sample.** Test: the 65 contradictory instances the pilot did not use, one run per arm (520 runs). Dev: 8 pilot instances, used to check that the instructions are delivered and followed; wording may change there only.
- **Measures.**
  - Primary: both requests pass on the merged patch. Each guided arm against neutral, paired by instance, exact sign test, Holm correction over six arms.
  - Requests: how many requests pass (0, 1 or 2) and whose.
  - Manipulation check: styles from codebook version 5, labeled from the messages between agents (the instruction is not among them).
  - Behaviour: messages, handoffs, concessions (one agent submits no code of its own) and whether both requests then pass, merge conflicts.
  - Capability: a solo baseline on the same instances; both-pass is also reported on the instances solo satisfies.
- **Predictions.** Accommodating and collaborating raise both-pass against neutral, competing and avoiding lower it, structured raises it. On CooperBench no style helped and compromising hurt.
- **Dev check (2026-11-12).** 8 dev instances in all eight arms, 64 runs, $0.66 for the agents and $0.04 for labels. Per-run data: [results/parallact_styles_dev_rows.csv](../results/parallact_styles_dev_rows.csv), labels: [results/parallact_styles_dev_conflict_states_rows.csv](../results/parallact_styles_dev_conflict_states_rows.csv). Every guided agent received its instruction before its first reply; no run failed and no test errored.

  | Arm | Both pass | Requests passed (of 16) | Messages per run | Concessions (both then pass) | Agents in the target style (neutral) |
  |---|---|---|---|---|---|
  | Default | 4 / 8 | 11 | 6.4 | 3 (2) | |
  | Neutral | 5 / 8 | 11 | 3.8 | 2 (1) | |
  | Competing | 4 / 8 | 11 | 5.5 | 0 | 0 / 16 (0 / 16) |
  | Collaborating | 6 / 8 | 13 | 7.9 | 0 | 8 / 16 (2 / 16) |
  | Compromising | 3 / 8 | 9 | 6.1 | 2 (1) | 6 / 16 (9 / 16) |
  | Accommodating | 6 / 8 | 12 | 6.9 | 1 (1) | 3 / 16 (2 / 16) |
  | Avoiding | 3 / 8 | 9 | 1.6 | 3 (0) | 10 / 16 (3 / 16) |
  | Structured | 6 / 8 | 13 | 8.1 | 3 (3) | |

  - The instructions are followed in the messages for collaborating (more messages than neutral in 7 of 8 instances, p = 0.016) and avoiding (10 of 16 agents labeled avoiding). The labeled style does not move for competing, accommodating and compromising, as on CooperBench; competing does remove concessions (0 against 2 under neutral).
  - Outcomes point the way the predictions say, but with 8 instances per arm no difference from neutral is near significance. The dev set was chosen from instances the model can solve, so its pass rates are far above what the test set will show.
  - Under structured, three runs end with one agent conceding and both requests passing.
  - A stalled API connection held one run for 40 minutes: the earlier timeout setting had no effect. Model calls now have a hard limit of 330 s (see the pilot notes); seven calls hit it afterwards and were retried.

## Next steps

1. Run the same-harness scoring on all pairs, not a sample, to narrow these intervals. Then run the audit, the census and the same-harness scoring on the paper's other three models and on its no-communication runs.
2. Run the audit and the census on the paper's other three models and on its no-communication runs.
3. Ablations the advisor asked for: number of agents, team composition (different, stronger and weaker, commercial and open models together) and the communication channel.
4. Read the shared-git trajectories to find out why capability failures rose in S2.
5. Choose what replaces S3. Candidates: a repair round after the merge, where an agent sees the merged code and the failing tests; and three-agent tasks with CooperBench's team mode.
