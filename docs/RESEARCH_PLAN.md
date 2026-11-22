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

Status: frozen 2026-11-13 after the dev check, with the instructions unchanged. Also frozen: the style labels ([configs/parallact_styles_conflict_states.json](../configs/parallact_styles_conflict_states.json), 80 runs labeled twice) and the solo baseline on the same 65 instances ([configs/parallact_styles_solo.json](../configs/parallact_styles_solo.json)). Instances whose reference patches fail the harness check are left out of every comparison. Config: [configs/parallact_styles.json](../configs/parallact_styles.json), dev: [configs/parallact_styles_dev.json](../configs/parallact_styles_dev.json). No test results yet.

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

### ParallACT style interventions: results (2026-11-15)

The frozen design above, run on the 65 test instances: 8 arms, one run each, 520 runs. Per-run data: [results/parallact_styles_rows.csv](../results/parallact_styles_rows.csv); labels: [results/parallact_styles_conflict_states_rows.csv](../results/parallact_styles_conflict_states_rows.csv); solo baseline: [results/parallact_styles_solo_rows.csv](../results/parallact_styles_solo_rows.csv). Cost: $7.16 for the coop agents, $0.49 for solo, $0.37 for labels. All 65 instances passed the reference check, so none was excluded. One solo run ended in an API connection error and was retried, as the rules say. Six test runs (own-patch tests of two instances, one merged test) timed out and count as failed.

| Arm | Both pass | Arm better / neutral better | Requests passed (of 130) | Messages per run | Agents in the target style (neutral) | Concessions (both then pass) |
|---|---|---|---|---|---|---|
| Default | 23 | 5 / 5 | 69 | 6.5 | | 13 (3) |
| Neutral | 23 | | 67 | 6.4 | | 14 (6) |
| Competing | 23 | 7 / 7 | 69 | 5.0 | 10 / 130 (8) | 10 (4) |
| Collaborating | 25 | 6 / 4 | 69 | 7.6 | **60** / 130 (37) | 11 (4) |
| Compromising | 22 | 8 / 9 | 68 | 5.7 | **73** / 130 (52) | 10 (3) |
| Accommodating | 22 | 6 / 7 | 68 | 6.8 | 16 / 130 (17) | 14 (2) |
| Avoiding | 21 | 7 / 9 | 60 | **1.5** | **84** / 130 (16) | 6 (1) |
| Structured | 26 | 9 / 6 | 73 | **8.2** | | 14 (8) |

- **No instruction changes whether both requests pass.** Both-pass ranges from 21 to 26 of 65 against 23 under neutral. No arm differs from neutral by more than three instances in either direction; the smallest unadjusted p is 0.61 (Holm-adjusted 1.0). The predictions are not supported: accommodating and collaborating do not raise both-pass, competing and avoiding do not lower it, and structured (26, better in 9 instances and worse in 6) is not a significant gain.
- **The instructions move behaviour.**
  - Labeled style: collaborating (60 agents against 37; more in 27 instances, fewer in 8, p = 0.002), compromising (73 against 52, p = 0.017) and avoiding (84 against 16, p < 0.001) shift toward their target. Competing (10 against 8) and accommodating (16 against 17) do not, as on CooperBench.
  - Messages: avoiding cuts them to 1.5 per run (fewer than neutral in 59 of 62 differing instances); collaborating (p < 0.001) and structured (p = 0.005) add them.
  - Structured gets the agents to state interfaces: exact names, signatures or formats in 47 runs against 24 under neutral, and a contradiction between the requests named in 7 runs against 3.
- **Conceding does not pay off here either.** Across all arms, 92 runs end with one agent submitting no code of its own; both requests pass in 31 of them (34%), against 154 of 425 other runs (36%). The 3 of 4 in the pilot were chance. The accommodating instruction does not raise concessions (14, as under neutral).
- **Capability bounds the room for coordination.** Solo local work satisfies both requests on 25 of the 65 instances (its published patch on 17). On those 25 the coop arms pass both in 17 to 20; on the other 40 in 2 to 7. Every instance solo solves is solved by at least one arm, and 10 instances see no request pass in any arm. Coordination loses at most about 8 of the 25 solvable instances, and the arms do not move that number either (structured 20 against neutral 17, p = 0.45).
- **Labeler consistency** on 80 runs labeled twice: style kappa 0.67 and 0.70, handoff 0.88, incompatible claims 0.82. Lower than on CooperBench (0.82 and 0.95).

What it shows, together with the CooperBench study:

- On both benchmarks, instructions that describe a handling style change how agents talk (how much, whether they state interfaces, the labeled style for three of the five styles) but not what code they end up writing, and not whether both features pass. Giving the agents room to concede (ParallACT) does not change that.
- On ParallACT most failures are capability, as the pilot suggested: whether both requests pass is decided mostly by the instance. A study of handling styles needs tasks the model can solve alone, or a stronger model, before differences in coordination can show.
- Limits: one model (gpt-6-luna) and one run per instance and arm. With 65 instances the comparisons detect only large effects; a difference of three or four instances per arm cannot be ruled out.

### ParallACT synthesis readouts (2026-11-16)

Prompted by Zhao et al., "When Debate Helps" (NeurIPS 2026): when several agents propose, does the final readout lose what they found? Two readouts on the finished style runs, with no agent re-run. Config, frozen before the test runs after a dev check on 8 other instances: [configs/parallact_synthesis.json](../configs/parallact_synthesis.json). Per-run data: [select](../results/parallact_synthesis_select_rows.csv), [re-merge](../results/parallact_synthesis_remerge_rows.csv); summary: [results/parallact_synthesis_summary.json](../results/parallact_synthesis_summary.json). Cost: $0.10 for selection, $0.71 for the 592 re-merges.

- **Select.** For each of the 65 instances a manager (gpt-6-luna) reads both requests and the eight teams' merged patches, in shuffled order, and picks one. Baselines: the consensus patch (the one sharing the most changed lines with the others), a random team and the oracle (some team passes both).
- **Re-merge.** Each of the 148 conflicted runs is merged again from the same two patches. A 2 × 2 design: the resolver sees either only the conflict blocks (as the original merge) or the whole conflicted file, and is either blind or given both requests and the agents' messages. A second blind block run (blind repeat) measures resampling alone. Primary: aware whole-file against the original blind merge.

| Select (65 instances) | Both pass |
|---|---|
| Random team (expected) | 23.1 |
| Consensus | 27 |
| Manager | 28 |
| Oracle | 38 |

| Re-merge (148 conflicted runs) | Both pass | Better / worse than original | Requests passed (of 296) | When both own patches pass (of 67) |
|---|---|---|---|---|
| Original (blind, blocks) | 33 | | 107 | 30 |
| Blind repeat | 31 | 2 / 4 | 102 | 29 |
| Aware, blocks | 40 | 12 / 5 | 120 | 39 |
| Blind, whole file | 45 | 15 / 3 (p = 0.008) | 138 | 39 |
| **Aware, whole file** | **61** | **30 / 2 (p < 0.001)** | **166** | **55** |

- **The manager does not beat consensus.** Of the 11 instances where consensus misses but some team passes both, the manager recovers 3, and it loses 2 that consensus had (p = 1.0). Both beat a random team by 4 to 5 instances. Judging from patches alone, without running tests, recovers little of the headroom. The dev result (6 against 4 of 8) did not hold.
- **The re-merge recovers most of the merge loss, but only with both changes.** Seeing the requests alone (+7 net, p = 0.14) or the whole file alone (+12) helps less than both together (+28; against blind whole-file 17 / 1, p < 0.001). Resampling alone does nothing (−2). Where both agents' own patches pass, the aware whole-file merge passes both requests in 55 of 67 runs against 30.
- **It raises the floor, not the ceiling.** Substituting the aware whole-file merge into the style runs raises both-pass from 21–26 to 24–29 of 65 per arm, and on the 25 instances a single agent solves from 17–20 to 19–23. The number of instances some team solves moves only from 38 to 39: the merges it fixes are on instances another team had already solved.

What it shows:

- In the debate paper's terms, the teams supply the right answer on 38 of 65 instances, and a readout that only reads the proposals adds almost nothing over consensus. Where the readout gets the information the blind step lacked (what each side was asked to do, and the code around the conflict), it recovers most of what the plain merge lost.
- For our study, part of what looked like coordination failure on ParallACT was a weak merge step. Comparisons of handling styles should be scored with the aware whole-file merge. The exploratory rescoring above does not change the style result: no arm moves above neutral (29).
- Limits: one model, one resolution per run and method; the rescoring of style arms was not planned before the run.

### Negotiation pilot: manipulation check of the style instructions (2026-11-20)

The next study analyses how handling strategies change each agent's payoff in a conflict of interest, in an environment with explicit per-agent payoffs (design draft shared with the team on 2026-11-20). Before building that environment, this pilot asks whether the style sentences from the coding studies change how an LLM negotiates when the payoff is explicit and continuous. Setting: [NegotiationArena](https://github.com/vinid/NegotiationArena)'s buy-sell game (MIT, commit c447faf): one object, the seller knows its cost, the buyer its limit, alternating proposals in ZUP, at most four own proposals each, ACCEPT or REJECT ends the game. We add one line to each role stating its payoff (price minus cost, or limit minus price); without it the seller opened at cost and the buyer accepted at once. Config, written before any game: [configs/negotiation_pilot.json](../configs/negotiation_pilot.json); 20 instances (cost 30 to 60, surplus 10 to 30), the instructed player is the seller in even instances and the buyer in odd ones, the other player gets the plain prompt; six conditions (neutral and the five styles, each sentence reworded from the coding arms), 120 games, gpt-6-luna on both sides. Styles were labeled from the public moves with [docs/N_NEGOTIATION_STYLE_CODEBOOK.md](N_NEGOTIATION_STYLE_CODEBOOK.md), the negotiation counterpart of the coordination codebook. Cost: $0.07 for the games and $0.04 for the labels. Per-game data: [results/negotiation_pilot_rows.csv](../results/negotiation_pilot_rows.csv); curves: [results/negotiation_pilot_curves.svg](../results/negotiation_pilot_curves.svg).

Share is the instructed player's share of the surplus (0 when there is no deal); comparisons with neutral are paired by instance (exact sign test).

| Condition | Deals | Share (partner) | Moves | First claim | Concession | Labeled style: competing / collaborating / compromising / accommodating / avoiding | In target (neutral) | Assertiveness, cooperativeness |
|---|---|---|---|---|---|---|---|---|
| Neutral | 19 | 0.52 (0.43) | 4.2 | 1.19 | 0.37 | 1 / 1 / 14 / 4 / 0 | | 1.15, 1.05 |
| Competing | 20 | 0.52 (0.48) | 4.2 | 0.95 | 0.17 | 2 / 0 / 13 / 5 / 0 | 2 (1) | 1.45, 1.10 |
| Collaborating | 20 | 0.37 (0.63) | 3.9 | 0.81 | 0.10 | 0 / 5 / 3 / 12 / 0 | 5 (1) | 1.10, 1.85 |
| Compromising | 20 | **0.22** (0.78), lower in 13 of 16, p = 0.02 | 3.3 | 0.64 | 0.15 | 0 / 4 / 4 / 12 / 0 | 4 (14) | 0.80, 1.45 |
| Accommodating | 18 | 0.28 (0.62) | 2.8 | 0.67 | 0.09 | 0 / 2 / 7 / 11 / 0 | 11 (4) | 0.95, 1.50 |
| Avoiding | 18 | 0.39 (0.51) | 2.7 | 1.33 | 0.00 | 5 / 0 / 6 / 9 / 0 | 0 (0) | 0.85, 0.75 |

- **The default is compromising, and the seller gives its cost away.** Under neutral 14 of 20 instructed players are labeled compromising, and the seller states its production cost in its first message in 20 of 20 games. The first mover does better: instructed sellers take 0.68 of the surplus, instructed buyers 0.36.
- **The instructions move the payoff.** The instructed player's share falls from 0.52 under neutral to 0.37 (collaborating), 0.28 (accommodating) and 0.22 (compromising); only the last is significant with 20 games. Deals are reached in 115 of 120 games, so the sentences redistribute the surplus rather than destroy it; in a one-issue game there is no value to create.
- **Which styles the sentences induce.** Accommodating: yes (11 labeled against 4 under neutral; share 0.28). Collaborating: in talk (11 of 20 ask what the other side needs, against 0; cooperativeness 1.85) but not in the outcome: 12 of 20 are labeled accommodating, because with one issue "a deal good for both" can only mean conceding. Compromising: no; the sentence turns the compromising default into accommodating (4 compromising against 14, 12 accommodating), as on CooperBench, where the compromising arm was the one that hurt. Competing: no; the label does not move (2 against 1, 13 still compromising) and the share stays at 0.52: the player concedes less (0.17 against 0.37) but also opens lower (0.95 against 1.19). Avoiding: never labeled avoiding, because the game forces a move and the player makes its one proposal and then accepts or holds, but its behaviour is the most distinct: 2.7 moves, no concession, the fewest messages, and only 8 of 20 sellers disclose their cost.
- **The two scores move where the categorical label does not.** Every sentence shifts assertiveness and cooperativeness in the predicted direction (competing +0.30 assertiveness; collaborating +0.80 cooperativeness; avoiding −0.30 and −0.30); the 3 × 3 grid rounds several of these away.

What it decides for the main study:

- A one-issue price game is the zero-sum pole: collaborating cannot differ from accommodating in it. The main environment needs several decision points with different private weights, as the design draft says, so that integrative deals exist.
- Competing, avoiding and compromising need behavioural rules (a reservation price, a concession schedule, a walk-away rule, an explicit split of the difference); the sentences alone give compromising-as-accommodating and competing-as-neutral. Accommodating and collaborating sentences work as talk.
- Report assertiveness and cooperativeness alongside the style label, and measure disclosure: the seller's habit of stating its cost is an information treatment in itself.
- Cost is not a constraint: $0.001 per game, so the planned 1200 games cost about $2 plus labels.
- Limits: one model, 20 games per condition, single-pass labels (no repeat, so no agreement figure). The arena's engine does not end a game on REJECT; moves after a REJECT are discarded (5 games), and the three games whose end-of-game summary crashed were replayed with the engine ending on REJECT.

### Strategy payoff study: design (2026-11-20)

The paper this builds toward is an analysis paper: how conflict-handling strategies change each agent's payoff when two LLM agents must agree on one shared artifact while wanting different things from it, and how that depends on the structure of the conflict and on the resources the task allows. The product is the payoff landscape over strategies and conditions, not a method. The coding studies above are its boundary case (binary, capability-gated payoff); the negotiation pilot is its zero-sum pole. This section is the design as it stands; the exact prompts, instance seeds and predictions are frozen in the config before each experiment runs.

**The question, in the terms of the Thomas–Kilmann model.** The model pairs each style with the situation it suits: competing for urgent decisions, collaborating for complex problems that need both sides' perspectives, compromising for conflicting goals between equals when a temporary settlement will do, avoiding for trivial issues or when confrontation costs more than it yields, accommodating when the issue matters more to the other side. These are claims about a mapping from task conditions to strategies, stated for people and never measured for LLM agents. Every condition in that list corresponds to a knob of the environment below, so the study can draw the model's map empirically: for each condition, which strategy pays, for whom, and at what cost in tokens and time. The headline figure is that map, with the model's own axes; the expected finding is that no strategy is best everywhere, that the LLM default (compromising shading into accommodating) is best only in a narrow region, and that cost moves the boundaries.

**Positioning against Hua et al. (2024, arXiv 2411.05990).** That paper asks whether LLMs are rational in fixed classic games and in Deal-or-No-Deal, and proposes game-theoretic workflows to make them so; it is half evaluation and half method, and its environments are hand-written matrices whose results, as it notes, depend on the numerical instantiation. This study differs in the variable manipulated (an assigned handling strategy, not a reasoning workflow), the outcome (the distribution of payoff, cost and efficiency over strategies and conditions, not distance from equilibrium), the environment (a parametric generator with exact frontiers and seat-balanced instances, averaged over hundreds of draws per condition), the theory (dual-concern conflict research and empirical game analysis, not equilibrium concepts) and the stance (analysis only). Two of their findings are starting points here: their "undue trust" is our default agent leaving a quarter of the value on the table, and their one-sided workflow being exploited is one cell of our exploitability matrix. No method is proposed anywhere in the paper; the finding that rules can be enacted and style sentences cannot is reported as a result about instructability.

**The environment as a contribution.** Three layers: the synthetic core (generator, exact frontier, strategy library, compliance checker); scenario skins, the same instance rendered as a software design, a team plan or a release plan, to show that the map does not depend on the surface framing; and grounded instances derived from real conflicts (ParallACT's contradictory request pairs reduced to the design decisions at stake; LLM-Deliberation's scenarios reduced to two parties, still to do). All three are built as of 2026-11-20 (see [the environment layers](#strategy-payoff-study-environment-layers-scenario-skins-and-grounded-instances-2026-11-20)). The core and the skins are ours to release; the grounded layer ties the map back to the coding studies.

**Environment: parametric multi-issue bargaining** (`src/agent_coordination/bargain.py`). Two agents must agree on a shared design with K issues (default 4), each with M options (default 3). Each agent has a private importance vector over issues (summing to 100) and, within each issue, a ranking of the options: its top option is worth the issue's importance, the middle option half, the bottom option nothing. An agent's payoff is the sum over issues; 100 is its ideal design. Two generator knobs set the structure of the conflict:

- *opposition* p: the fraction of issues on which the two rankings are reversed (the middle option is then the compromise); on the other issues the rankings agree.
- *asymmetry* a: how differently the two agents weigh the issues, from identical importance (a = 0, purely distributive) to reversed importance (a = 1, maximal gain from trading issues).

Other knobs, each the operational form of one of the model's situational conditions: the outside option d each agent gets without a deal (default 30 after E0; unequal outside options are the power condition), the stakes of each side (a scale on its points and outside option; a side with stakes 0.3 is one for whom the issue matters less), the number of moves T (urgency), a cost per move that passes (time pressure in points), a budget of reply tokens after which an agent can only accept or walk (communication cost), whether the partner's weights are visible (information), and whether the same pair plays several instances in a row (relationship). Every call's prompt and completion tokens and latency are recorded, so each strategy's cost is measured, not assumed. Because weights are known to the environment, every instance's Pareto frontier, maximal joint payoff, Nash bargaining point and equal split are computed exactly, so efficiency and fairness are measured against the instance's own optimum.

Protocol: alternating moves. A move is a message plus one of: a full proposal (an option for every issue), ACCEPT of the partner's standing proposal, or WALK. The environment tells each agent the value of every proposal in its own points. Agreement pays each agent its value of the accepted design; no agreement by move T, or a WALK, pays the outside options. The framing is two developers settling a shared design, so the setting stays continuous with the coding studies; issue and option names are neutral so that no world knowledge fixes a "right" answer.

**Strategies.** The five Thomas–Kilmann styles, a neutral control and a no-guidance default, in two operationalizations:

- *Prompted*: the style sentences used in the coding and negotiation studies, reworded for a multi-issue design.
- *Rule-based*: each style as behavioural rules stated on the agent's own point scale, so that compliance is checked from the moves without any judge. Competing: open at or near 100, concede at most 5 points per own proposal, accept nothing under 60, walk rather than go lower. Collaborating: exchange which issues matter most before proposing, propose packages that trade issues, accept at 60 or more once the partner says the package works for them. Compromising: take the middle option on contested issues or alternate who gets their way, concede 10 to 15 points per proposal, accept at 50 or more. Accommodating: ask what the partner needs, accept the first proposal worth at least 30, propose packages built around the partner's stated needs. Avoiding: make at most one proposal, do not argue, then accept whatever is worth at least the outside option or walk.

E0 decides which operationalization the main experiments use (rules, unless the prompted version turns out to comply as well); the other is kept as an ablation. The negotiation pilot predicts the outcome: prompted compromising behaves like accommodating and prompted competing like neutral.

**Measures.** Outcome: own and partner payoff in points, joint payoff, agreement, Pareto efficiency (distance of the agreement to the frontier), fairness (difference of the two payoffs), the Nash product. Cost: tokens (prompt and completion) and seconds per agent per game, moves to the end, the move cost paid. Process: the concession curve (own value of own proposals by move), who conceded first, message length, whether an agent discloses its priorities, asks about the partner's, proposes a trade across decisions, or settles on middle options. Compliance with the rule of the assigned strategy. Labeled style from the public transcript (a multi-issue version of the negotiation codebook), secondary to the programmatic measures, with repeat labels for agreement.

**Quantification.** Three readings of every cell: payoff, cost, and net payoff, defined as points minus λ times the agent's tokens in thousands. λ is the task's price of communication; rather than fix it, the analysis sweeps λ from 0 upward and reports where the best strategy changes hands, so a claim such as "under a tight budget avoiding beats compromising" comes with the λ at which it starts to hold. Across conditions, strategies are compared by regret: how many points a strategy earns below the best strategy in that condition, against the same partner. "Best" is read three ways: in symmetric play (both sides use the strategy), against the neutral partner (the population default) and against competing (the adversarial case).

**Experiments.**

| | Design | Decides or tests |
|---|---|---|
| E0 manipulation check (done) | dev set, 10 instances × 6 strategies × 2 operationalizations, partner neutral | which operationalization complies; the labeler on multi-issue transcripts |
| E1 strategy matrix (frozen 2026-11-20) | test set, 40 instances × all 36 ordered rule pairs, the prompted sentences on the diagonal and no guidance on both sides, first mover balanced; mixed regime p = 0.75, a = 0.5, d = 30, T = 10 | own-payoff and joint-payoff matrices, best responses, empirical equilibria, exploitability; cost per strategy |
| E2 condition map | ten conditions, each one of the model's situational variables moved from the E1 base: urgency (T = 4), time cost (3 points per move), communication cost (150 reply tokens), distributive (a = 0), integrative (a = 1), all contested (p = 1), unequal stakes (1 and 0.3), unequal power (outside 45 and 15), open information (weights visible), plus the base again; 24 cells per condition (the diagonal, every strategy against neutral and against competing in both seats), 20 shared instances | the empirical map: which strategy is best, by payoff and by net payoff, in each condition; the λ at which cost changes the answer |
| E3 models | E1's diagonal and key pairs, and the two conditions that move the map most, with two further models; cross-model pairs | whether the map is a property of the model or of the game |
| E4 scenario skins | E1's diagonal with three framings of the same instances | that the map does not depend on the surface framing |
| E5 external validity | buy-sell (done: zero-sum pole), LLM-Deliberation reduced to two parties (positive-sum), grounded instances from ParallACT's contradictory requests, the coding studies (binary payoff) | that the map holds outside the synthetic core |
| E6 decomposition | all of the above | whether payoff differences come from the agreement rate, the split, or efficiency; concession dynamics; logrolling; what each strategy costs |

**Analysis, registered before E1.** Primary: joint payoff by strategy pair against neutral–neutral, and the own-payoff matrix, both paired by instance with exact sign tests, bootstrap intervals clustered by instance and Holm correction within each family. Secondary: best responses per column of the matrix; the attractors of replicator dynamics on the 6 × 6 empirical game; exploitability of each strategy (own loss when the partner switches to competing). E2 to E4 are moderator analyses and are reported as such. Effect sizes are in payoff points out of 100. Power: in the negotiation pilot a shift of 0.3 of the surplus was significant with 20 paired instances; 40 instances detect a paired difference of about 0.45 standard deviations at 80% power.

**Predictions** (E1's are frozen in its config; E2's will be frozen in the condition configs): collaborating–collaborating gives the highest joint payoff and efficiency, competing–competing the lowest agreement rate; competing exploits accommodating and compromising, so accommodating is a best response to nothing and the empirical equilibrium is competing–competing or compromising–compromising depending on the cost of impasse; collaborating's advantage over compromising grows with asymmetry and vanishes at a = 0; visible weights shrink both collaborating's advantage and competing's exploitation; a higher outside option raises that side's share under every pair, most under accommodating. For the map: urgency and time cost favour the fast closers (competing against a soft partner, accommodating otherwise) and punish collaborating; a tight reply budget makes avoiding and accommodating the best net strategies and collaborating the worst; the low-stakes side does best by accommodating and the high-stakes side by competing; collaborating is best only where integrative gain exists and talk is affordable.

**Validity threats and what answers them.** Strategies as prompts are vague: rules with programmatic compliance. Judge-based labels: programmatic measures are primary, labels secondary with agreement figures. One model: three models and cross-model pairs. A synthetic game: two external benchmarks and the coding boundary case. First-mover advantage (sellers took 0.68 in the pilot): balanced and reported. Parsing failures: a corrective retry, then the move is a pass, and the rate is reported. Leakage and tuning: dev and test instances are disjoint and prompts are frozen before each run. Repetition and learning: one-shot by default, repetition as a separate knob.

**Cost and order.** About $0.003 per game: E0 under $1, E1 about $5 per model, the whole plan under $100. Order: build the environment and run E0; freeze E1 and run it on gpt-6-luna; E2 and E3; the other models; LLM-Deliberation in parallel with a teammate.

### Strategy payoff study: E0 manipulation check (2026-11-20)

Config: [configs/bargain_dev.json](../configs/bargain_dev.json); 10 dev instances in the mixed regime (4 decisions, 3 options, 3 decisions contested, asymmetry 0.5, outside option 30, 10 moves); Developer 1 gets the strategy, Developer 2 the neutral sentence; 12 cells, 120 games per round, gpt-6-luna on both sides. Per-game data: [results/bargain_dev_rows.csv](../results/bargain_dev_rows.csv); summary: [results/bargain_dev_summary.json](../results/bargain_dev_summary.json). Three rounds, $0.33 in all including labels:

- *Calibration* (4 instances, both neutral). With outside option 20 and no protocol note, the responder accepted the first mover's ideal design, worth 26 points to it, at once. A sentence saying that proposals can be countered removed first-offer acceptance, and an outside option of 30 makes accepting the partner's ideal (about 25 points) irrational while the issue-by-issue compromise (about 50) stays feasible. gpt-6-luna rejects `temperature` with any reasoning effort, so reasoning stays off.
- *Round 1* ([round 1 summary](../results/bargain_dev_round1_summary.json)). Developer 1 earned more in both control cells whoever moved first: the generator derived Developer 2's importance from Developer 1's, which left seat 1 with the more concentrated profile and a higher Nash payoff (75 against 62). The generator now builds both profiles as mirror images of one latent vector (Nash 71 against 71 over 300 instances; at asymmetry 0 the integrative gain is 0, at 1 it is 34 points). Rule compliance was competing 2 of 10, collaborating 6, compromising 6: the rules asked for point-sized concession steps that a discrete design cannot deliver (switching one decision moves 5 to 20 points). The rules were restated in decisions changed per proposal.
- *Round 2* ([round 2 summary](../results/bargain_dev_round2_summary.json)) and *round 3* (collaborating and compromising rewritten once more and replayed, which is the table below).

| Cell (Developer 1's strategy) | Deals | Payoff D1 (D2) | Joint gap | Moves | Rule compliance | D1 labeled: competing / collaborating / compromising / accommodating / avoiding |
|---|---|---|---|---|---|---|
| default (no guidance) | 10 | 60.5 (52.8) | 30.8 | 4.6 | | 0 / 2 / 4 / 4 / 0 |
| neutral | 10 | 61.1 (57.9) | 25.0 | 4.2 | | 0 / 2 / 3 / 5 / 0 |
| prompted competing | 10 | 65.9 (64.6) | 13.5 | 5.4 | 6 / 10 | 1 / 1 / 7 / 1 / 0 |
| prompted collaborating | 10 | 59.8 (70.3) | 14.0 | 4.3 | 3 / 10 | 0 / 4 / 0 / 6 / 0 |
| prompted compromising | 10 | 58.7 (65.4) | 20.0 | 4.4 | 7 / 10 | 0 / 0 / 8 / 2 / 0 |
| prompted accommodating | 10 | 55.7 (69.3) | 19.1 | 4.6 | 7 / 10 | 0 / 0 / 1 / 9 / 0 |
| prompted avoiding | 3 | 34.7 (45.6) | 63.8 | 3.3 | 10 / 10 | 7 / 0 / 1 / 2 / 0 |
| rules competing | 9 | 69.4 (46.4) | 28.3 | 6.5 | **10 / 10** | 1 / 0 / 8 / 1 / 0 |
| rules collaborating | 10 | 73.4 (61.0) | 9.6 | 5.2 | 7 / 10 | 0 / 5 / 0 / 5 / 0 |
| rules compromising | 10 | 67.3 (66.1) | 10.7 | 6.3 | 7 / 10 | 0 / 0 / 9 / 1 / 0 |
| rules accommodating | 10 | 58.1 (76.2) | 9.8 | 4.5 | **9 / 10** | 0 / 3 / 0 / 7 / 0 |
| rules avoiding | 4 | 36.6 (46.5) | 61.0 | 3.0 | **10 / 10** | 4 / 0 / 2 / 2 / 2 |

The joint gap is the distance of the outcome from the instance's maximal joint payoff (about 142 points in this regime).

- **The default agent leaves a quarter of the joint value on the table.** Under no guidance or the neutral sentence the gap is 25 to 31 points; the agents take the middle option on a fifth to a third of the contested decisions (0.20 with no guidance, 0.30 under the neutral sentence; corrected 2026-11-20, see the E4 section) and trade whole decisions in 2 to 4 games of 10, although they disclose their scores in 7 to 9.
- **Rules stated in moves are followed; rules that need a constructed package are followed 7 times in 10.** Competing, avoiding and accommodating comply in 9 or 10 games. Collaborating agents ask and disclose in every game but in 3 of 10 propose a split instead of a trade (one announced a concession on Decision 4 and encoded a different option); compromising agents raise their own value again in 3 of 10. Two rewrites moved these from 6 and 4 to 7 of 10; a third was not tried, to avoid tuning the rule text to these ten instances.
- **The strategies separate in payoff as predicted.** Competing by rule exploits the neutral partner (69 against 46, 9 deals of 10) but leaves a 28-point gap; collaborating by rule pays the instructed side the most (73) with the partner at 61 and the smallest gap (9.6); accommodating costs the instructed side (58 against 76) while the pair stays efficient; avoiding walks away in 6 or 7 games of 10, because the partner's opening design is worth less than the outside option, and is by far the worst for both (joint 80 to 83).
- **The prompted sentences repeat the negotiation pilot.** Prompted competing is firm but fair (66 against 65, labeled compromising in 7 of 10); prompted collaborating is labeled accommodating in 6 of 10 and pays the partner more than the instructed side; prompted compromising opens at the middle and holds.
- **The labeler separates the cooperative styles, not competing from compromising.** Collaborating is labeled in 5 of 10 rule games against 2 under neutral, compromising in 9, accommodating in 7. Competing is labeled at most once per cell even where the rule is followed 10 times, because one-decision concessions read as compromising; avoiding is labeled competing when it walks. The programmatic measures (compliance, concession curve, trades, middle share) are therefore primary in E1 and the labels secondary.
- After the generator fix neither seat earns systematically more in the control cells (5-game subsets by first mover differ in both directions).

Decisions for E1: the rule-based operationalization with these rule texts, the prompted sentences as an ablation on the diagonal; compliance reported in every cell, with the analysis on all games (intention to treat) primary and on compliant games secondary; outside option 30, the protocol note, opposition 0.75 and asymmetry 0.5 unchanged; avoiding is operationalized as withdrawal, and its predicted place is the lowest agreement rate. Limits: 10 instances per cell; the rule texts were adjusted on these dev instances, so E1 uses a fresh seed; labels are single-pass.

### Strategy payoff study: E1 strategy matrix (2026-11-20)

Config, frozen before the run: [configs/bargain_e1.json](../configs/bargain_e1.json). 40 fresh instances of the mixed regime; all 36 ordered pairs of {neutral, competing, collaborating, compromising, accommodating, avoiding} as rules, the five prompted sentences on the diagonal, and no guidance on both sides: 42 cells, 1680 games, gpt-6-luna on both sides, $0.83 for the games and $0.68 for the labels. No game errored and no reply was unparseable. Per-game data: [results/bargain_e1_rows.csv](../results/bargain_e1_rows.csv); summary with the matrices, best responses and replicator dynamics: [results/bargain_e1_summary.json](../results/bargain_e1_summary.json); figure: [results/bargain_e1_matrix.svg](../results/bargain_e1_matrix.svg). Comparisons are paired by instance (exact sign tests over 40 instances); the matrix pools both seats, so each entry is 80 games.

Own payoff (row: own strategy; column: the partner's), in points out of 100; the maximal joint payoff of an instance is about 142:

| | neutral | competing | collaborating | compromising | accommodating | avoiding |
|---|---|---|---|---|---|---|
| neutral | 63.8 | 52.8 | 64.1 | 63.9 | **79.8** | 58.2 |
| competing | 67.0 | 54.7 | 58.6 | **68.3** | 75.3 | 63.6 |
| collaborating | **72.2** | **55.2** | **67.5** | 66.5 | 73.3 | **67.5** |
| compromising | 57.6 | 50.5 | 58.3 | 59.3 | 66.8 | 58.8 |
| accommodating | 51.7 | 51.5 | 61.2 | 61.0 | 66.9 | 59.4 |
| avoiding | 45.3 | 36.1 | 47.8 | 56.6 | 60.4 | 50.9 |

| Pair (rules) | Joint | vs neutral–neutral | Agreement | Joint gap | Moves | Tokens per agent |
|---|---|---|---|---|---|---|
| collaborating–collaborating | 135.5 | +7.9 (22 / 10, p = 0.05) | 0.97 | 6.1 | 5.6 | 2694 |
| accommodating–accommodating | 136.3 | +8.7 (23 / 7, p = 0.005) | 1.00 | 5.3 | 3.8 | 1383 |
| neutral–neutral | 127.6 | | 1.00 | 14.0 | 4.9 | 1881 |
| default–default (no guidance) | 128.9 | +1.4 (p = 0.74) | 1.00 | 12.7 | 4.3 | 1437 |
| compromising–compromising | 114.9 | −12.7 (10 / 26, p = 0.011) | 0.93 | 26.7 | 3.7 | 1396 |
| avoiding–avoiding | 112.8 | −14.8 (p = 0.058) | 0.80 | 28.8 | 2.6 | 831 |
| competing–competing | 102.5 | −25.0 (10 / 29, p = 0.003) | 0.62 | 39.1 | 8.6 | 4010 |

- **Collaborating by rule dominates.** It is the best response to neutral, competing, collaborating and avoiding (bold), and replicator dynamics on the 6 × 6 game converge to all-collaborating. Against a neutral partner it earns 72.2 against the partner's 63.8, that is 7.4 points more than neutral earns in neutral–neutral (25 / 10, p = 0.017), while the neutral partner keeps what it had (64.1): the collaborating agent creates value and takes it. The result holds among the games that comply with the collaborating rule (72.8 against neutral) as much as among those that do not (71.9).
- **Competing does not pay.** Against neutral it gains 2.7 points (p = 0.52) at the cost of 12% impasses; against itself it is the worst pair in the table (54.7 each, 62% agreement, a 39-point gap, 8.6 moves and 4000 tokens per agent). It is the best response only to compromising. The predicted exploitation of soft partners goes to the default agent instead: against accommodating, neutral earns 79.8, competing 75.3, because the competing rule's one-decision steps and 60-point floor cost it agreements.
- **Accommodating is exploited by everyone, but two accommodators are efficient.** It earns 14.4 points less than neutral against a neutral partner (6 / 29, p < 0.001), and least against every partner except avoiding. Yet accommodating–accommodating has the highest joint payoff (136.3, gap 5.3) and the fewest tokens of any agreeing pair: each side takes the other's priorities and the trades happen by default.
- **Compromising and avoiding destroy value.** Compromising–compromising leaves 26.7 points on the table (middle options on contested decisions); avoiding walks away in 20% of games against itself and 44% against competing, and earns 16 points less than neutral against a neutral partner.
- **Cost orders the strategies the other way round from payoff**, except for collaborating: tokens per agent, averaged over partners, are competing 2688, collaborating 2366, neutral 2082, compromising 1762, accommodating 1570, avoiding 1109. Competing is the most expensive and among the least rewarding; collaborating pays for its talk.
- **Prompted sentences.** On the diagonal the prompted competing sentence is softer than the rule (32 against 25 agreements, 21 / 40 comply), prompted avoiding harsher (17 agreements, both walk), and prompted collaborating, compromising and accommodating within a few points of the rules.
- **Seats.** In the symmetric rule cells the two seats differ by at most 5 points and the second mover does slightly better in four of six; neither effect changes any ordering above.
- Compliance over all partners: competing 229 / 240, avoiding 240 / 240, accommodating 218 / 240, compromising 194 / 240, collaborating 134 / 240 (it always asks and almost always holds its 60-point floor; what fails is proposing a package that gives each side its top option on a contested decision, 21 to 33 of 40 depending on the partner). The 11 collaborating acceptances below 60 were 7 last-move acceptances, which the rule allows; the check now allows them.
- **Labels follow the talk, rules the moves.** Over the 240 games of each rule strategy in seat 1, the labeler reads accommodating in 195 (accommodating rule), compromising in 168 (compromising rule), collaborating in 102 and accommodating in 138 (collaborating rule), but competing in only 10 of the competing rule's games (195 read as compromising: one-decision concessions look like meeting halfway) and avoiding in 17 of the avoiding rule's (46 read as competing when it walks, 75 as accommodating when it accepts). The prompted competing sentence is labeled competing in 11 of 40, more than the rule in 240, while its payoff is softer: the sentence changes the words, the rule changes the moves. The neutral agent is read as accommodating in 108 of 240 and compromising in 86; with no guidance at all, compromising in 25 of 40.

Against the frozen predictions: the joint-payoff ordering (collaborating and accommodating pairs above neutral, competing and avoiding pairs below) and the agreement and cost orderings held; the own-payoff predictions did not: competing was predicted to exploit soft partners and to be the equilibrium, and instead collaborating is the best response almost everywhere and the equilibrium, and the default agent exploits accommodators better than the competing rule does. The prediction that accommodating–accommodating would sit far below the frontier was wrong by the largest margin.

What it means for the map (E2): in this regime, with integrative gain available and no cost on talk, collaborating is the strategy to beat; the conditions that should move it are the ones that remove the integrative gain (a = 0), price the talk (reply budget, move cost) or shorten the horizon (T = 4).

### Strategy payoff study: environment layers, scenario skins and grounded instances (2026-11-20)

The two outer layers of the environment are built; the game, the strategies and the measures are unchanged, so every E1/E2 cell runs on them as is.

**Scenario skins** ([configs/bargain_skins.json](../configs/bargain_skins.json)) render a generated instance as a realistic conflict without touching its payoffs: `software_design` (two developers and a shared module: storage layer, call style, error handling, configuration), `team_plan` (two team leads and next quarter: hiring, office days, tooling budget, review cadence) and `release_plan` (two product owners: release date, first platform, pricing, launch region). The option names were chosen to carry as little prior as possible, and the generator still draws which option scores what, so a skin changes the words and not the game; the default `abstract` rendering is byte-identical to the prompts E1 and E2 were played with (checked on 24 rendered prompts). In four smoke games under `software_design` the agents argued in the domain's terms ("SQL is worth 0 to me and callbacks are worth 0; a design with both leaves me at 60") and reached agreements above both outside options.

**Grounded instances** come from ParallACT's contradictory pairs: two tickets on the same repository that rewrite the same code in incompatible ways, with a reconciliation that serves both. [ground.py](../src/agent_coordination/ground.py) gives gpt-6-luna (reasoning low) both tickets, the collision explanation and the reconciliation, and asks for the two to four design decisions the developers would have to settle, three options per decision labeled with the option each ticket needs and a middle ground, each side's importance on a 1-5 scale, a role phrase per developer and a neutral context paragraph. A first version asked for the options in a fixed order (A's need, middle, B's need) and the model, with reasoning off, put them out of order in about half of the decisions; labeling the three roles explicitly, with validation (the middle option distinct from both needs, at least one contested decision, distinct names) and one corrective retry, extracted all 85 pairs (4 needed a second run) for $0.07. Conversion ([bargain.assemble](../src/agent_coordination/bargain.py)): contested decisions rank the options in opposite orders and shared ones in the same order, with the middle ground second for both; importances are normalized to 100 per side; the option letters are shuffled per instance; outside option 30 and 10 moves as in E2's base; the context paragraph becomes an inline skin that tells each seat its own ticket and the other's. The set ([data/bargain_grounded_instances.json](../data/bargain_grounded_instances.json), the extracted structure only, none of ParallACT's text): 85 instances from 11 repositories; 34 have two decisions, 47 three and 4 four (225 decisions, 152 of them contested); 30 instances are contested on every decision, the median share of contested decisions is 0.67. Opposition and asymmetry are therefore properties of each conflict here, not knobs. Two smoke games read as design discussions: on the NodeBB slug-check pair the batch-API developer conceded lookup order and held on to strict validation, and the other side accepted ("the batch shape plus short-circuit lookup are the key points for me").

**Limits and what is left.** The middle options and the importances are the extractor's judgment, and the payoff structure (top, half, nothing) is imposed as in the synthetic core; a human spot-check of a sample of extractions against the tickets is still to do, and the grounded run itself (E2-base's 24 cells on 85 instances, 2040 games, about $2 on gpt-6-luna) has not been played. The skins run planned as E4 (E1's diagonal under three framings) is unchanged.

### Strategy payoff study: E2 condition map (2026-11-20)

Configs, frozen before the run: [configs/bargain_e2.json](../configs/bargain_e2.json) and the ten condition configs it indexes. Each condition moves one knob from the E1 regime and replays the same 24 cells (the six diagonal pairs, and every strategy against neutral and against competing in both seats, all as rules) on 20 shared instances: 480 games per condition, 4800 in all, gpt-6-luna on both sides, $2.50, no errors; not labeled. Per condition: `results/bargain_e2_<condition>_{rows.csv,summary.json,chart.svg,matrix.svg}`; the map: [results/bargain_e2_map.json](../results/bargain_e2_map.json) and [results/bargain_e2_map.svg](../results/bargain_e2_map.svg). Three readings per condition, seats pooled: *symmetric* (both sides use the strategy), *vs neutral* (one side adopts it against the default agent) and *vs competing* (against a hard bargainer); the net payoff is points − λ·tokens/1000 for λ from 0 to 20. Each comparison with neutral pairs 40 games by instance and seat (exact sign test); with 20 instances a 7-point gain is at the edge of significance, so most entries below are directions.

| condition (knob) | symmetric best | vs neutral best | vs competing best | vs neutral: best switches at λ → to |
|---|---|---|---|---|
| base (E1 regime) | accommodating 65.7 (+3.1) | collaborating 69.6 (+7.0, 22/14, p=.24) | collaborating 53.6 (+3.3) | 10 → neutral |
| urgent (4 moves) | accommodating 64.8 (+8.7) | accommodating 57.9 (+1.8) | collaborating 42.7 (+2.7) | 20 → neutral |
| time_cost (3 points per move) | accommodating 55.5 (+8.2) | collaborating 49.9 (+2.5) | collaborating 35.5 (+4.3) | 5 → avoiding |
| tight_budget (250 reply tokens) | accommodating 66.2 (+4.6) | competing 68.4 (+6.8, 24/15, p=.20) | collaborating 54.9 (+7.1) | 20 → neutral |
| distributive (asymmetry 0) | accommodating 60.7 (+0.5) | competing 64.3 (+4.1) | neutral 47.6 | 5 → neutral |
| integrative (asymmetry 1) | collaborating 75.5 (+6.4) | collaborating 76.0 (+6.9) | collaborating 64.6 (+5.2) | 20 → neutral |
| all_contested (opposition 1) | neutral 54.3 (accommodating 54.3) | collaborating 56.6 (+2.3) | neutral 37.9 | 2 → accommodating |
| low_stakes_p2 (stakes 1 / 0.3) | accommodating 43.5 (+4.6) | collaborating 44.6 (+5.7) | collaborating 38.3 (+4.6) | 10 → avoiding |
| power_p1 (outside 45 / 15) | collaborating 68.4 (+7.1) | competing 67.7 (+6.4, 24/13, p=.10) | collaborating 54.4 (+4.2) | 5 → neutral |
| open_info (weights visible) | collaborating 67.6 (+3.8) | competing 73.2 (+9.4, 25/12, p=.047) | collaborating 57.1 (+6.8, 23/10, p=.035) | 5 → avoiding |

Own payoff in points at λ = 0; in brackets the gain over neutral in the same reading (symmetric: over the neutral pair; vs competing: over neutral's payoff against competing), with wins/losses and the sign test where it is near significance. In power_p1 collaborating against neutral is the one entry significant in its own right (+4.3, 25/11, p = .029) although competing's mean is higher.

- **Both sides soft is the efficient pair almost everywhere.** Accommodating pairs are the best symmetric cell in 7 of 10 conditions at λ = 0 and in 9 of 10 at λ = 20; collaborating pairs lead where there is something to find (integrative 75.5, power_p1, open_info) and tie with neutral when every decision is contested. Competing pairs are the worst cell in every condition (11 to 26 points below the neutral pair, p ≤ .04 in 9 of 10; under urgency and under full opposition they reach 0 agreements in 20 and both sides take their outside options), and avoiding pairs lose 4 to 22 points with 4 to 17 agreements in 20. No soft pair's gain over the neutral pair is significant at 40 pairs (best p = .13).
- **Adopted alone against the default agent, collaborating or competing pays, never compromising, accommodating or avoiding.** Collaborating is the best unilateral strategy in five conditions (base, time_cost, integrative, all_contested, low_stakes_p2; +2 to +7), competing in four (tight_budget, distributive, power_p1, open_info; +4 to +9), accommodating only under urgency (+1.8, when there is no time to ask and trade and collaborating loses 3.4). Competing pays by taking from the partner (open_info: 73.3 against the partner's 46.1 with 18 agreements in 20; tight_budget: 69.0 against 46.7 with 17), and it pays exactly where the default partner does not push back: when it can see the competing side's scores, when its reply budget is too short to counter, when there is nothing to trade, and when the competing side has the better outside option. In the base regime competing gains nothing (+1.1), replicating E1 (+2.7). Compromising loses 4 to 12 points against neutral in every condition but open_info (p = .006 in power_p1), accommodating 1 to 8, and avoiding 9 to 20 (p < .05 in 8 of 10).
- **Against a hard bargainer, collaborate; do not match.** Collaborating is the best reply to competing in 8 of 10 conditions (+3 to +7 over answering with neutral; p = .035 in open_info); neutral is best when there is nothing to trade (distributive) or everything is contested. Answering competing with competing never pays (−1 to −10; 0 of 21 decided pairs under urgency, 3 of 19 under full opposition), and avoiding against competing is the worst reply everywhere (−8 to −23, p < .01 in 9 of 10).
- **Cost orders the strategies the same way in every condition**, tokens per agent and game in symmetric play: competing 3,500 to 4,650 (1,450 under urgency, where the game ends in 4 moves), collaborating 1,700 to 3,500, neutral 1,200 to 2,400, compromising 1,100 to 1,950, accommodating 1,100 to 1,700, avoiding 800 to 950. Against neutral, the best strategy by net payoff leaves collaborating or competing for a cheap strategy at λ = 2 to 10 in six conditions and only at λ = 20 under urgency, tight budget and integrative potential; against competing, collaborating survives to λ = 5 to 10 in base, integrative and power_p1 and at every λ with open information, but gives way at λ = 1 to 2 under time cost and urgency. In symmetric play accommodating is best at every λ in six conditions. At λ = 20 the cheap strategies (accommodating, neutral, avoiding) win every reading but open_info against competing.
- **Seats (20 games per cell and seat; directions).** With stakes 1 against 0.3, the high-stakes side does best by collaborating against neutral (68.1 against 57.4) and in accommodating pairs (67.1): the low-stakes side gives way on what it values little. The low-stakes side's best unilateral strategy is competing (23.1 against 20.5 of 30) and its worst are accommodating and avoiding (17.1, 17.0): in payoff terms the side the issue matters less to gains nothing by accommodating; the joint gain goes to the other side. With outside options 45 against 15, the strong side's best unilateral strategy is competing (68.9 against 61.5), as predicted, but so is the weak side's (66.5 against 61.0): the neutral agent does not use its better outside option. Power shows through the rule-based strategies that key on the outside option: in avoiding pairs the strong side gets 77.6 and the weak side 27.8 (the weak side accepts the strong side's one proposal, the strong side does not accept the weak side's), in accommodating pairs 73.8 against 58.2, while collaborating and compromising pairs give the weak side more (70.6 against 66.2, 64.5 against 56.0).
- **Frozen predictions against results.** Right: integrative (collaborating beats compromising by the largest margin, 12.2 points against neutral); urgent for accommodating and collaborating, wrong for competing (it does not beat neutral, −0.4, and competing pairs never agree); time_cost for competing pairs paying most (−24.8) and accommodating gaining, wrong for avoiding (−13.5 against neutral); distributive (collaborating's edge over neutral falls from +7.0 to +0.5; competing becomes the best response to neutral); all_contested for impasses under competing (0 of 20), wrong for compromising (−6.5). Wrong: tight_budget (avoiding is the worst strategy there, not the best net one; competing becomes best against neutral by points), low_stakes_p2 (every prediction reversed, see seats) and open_info (competing's exploitation of neutral grows to the largest in the map instead of shrinking; collaborating's margin over compromising does shrink, 14.7 to 5.5). Open information also makes the default pair the most efficient of all conditions: 3.2 moves, 1,200 tokens, gap 12.3.

**Reading for the paper.** In TKI terms the map says: the situational conditions matter, but not as the model's prescriptions say. Collaborating is the robust unilateral strategy for an LLM agent (never worse than neutral, best against a hard bargainer in 8 of 10 conditions); competing pays only against a partner that cannot or will not push back, and pays by transfer; accommodating is efficient only when both sides do it and is otherwise exploited; compromising and avoiding have no condition of their own in this environment, including the ones the model assigns them (nothing to trade; confrontation costs more than it yields). The cost axis moves the answer toward the cheap strategies long before λ reaches the price of a token in points. Caveats: one model, 20 instances per cell, pooled seats, unlabeled, λ swept rather than measured.

### Strategy payoff study: grounded instances (2026-11-20)

Config: [configs/bargain_grounded.json](../configs/bargain_grounded.json), E2 base's 24 cells on the 85 instances extracted from ParallACT's contradictory pairs (two to four decisions each, 152 of 225 contested; outside option 30, 10 moves, weights hidden, the ticket context as the framing): 2040 games, gpt-6-luna on both sides, $0.86, no errors, not labeled. Results: [results/bargain_grounded_rows.csv](../results/bargain_grounded_rows.csv), [summary](../results/bargain_grounded_summary.json), [matrix](../results/bargain_grounded_matrix.svg). Comparisons with neutral pair 170 games by instance and seat (exact sign test). Own payoff in points, with the E2 base value in brackets and the gain over neutral in the same reading:

| strategy | both sides use it | adopted alone against neutral | against a competing partner |
|---|---|---|---|
| neutral | 74.3 (62.6) | 74.3 (62.6) | 67.0 (50.4) |
| competing | 64.1 (46.9), −10.2, p < .001 | 75.0 (63.7), +0.7 | 64.1 (46.9), −2.9 |
| collaborating | 74.7 (65.6), +0.4 | 74.8 (69.6), +0.5 | 69.1 (53.6), +2.1 |
| compromising | 69.4 (58.4), −4.9 | 69.6 (54.9), −4.7, p = .008 | 64.0 (42.6), −3.1 |
| accommodating | 73.9 (65.7), −0.4 | 70.4 (60.8), −3.9, p = .004 | 66.9 (50.0), −0.1 |
| avoiding | 63.6 (52.1), −10.6, p = .002 | 59.1 (47.8), −15.2, p < .001 | 53.9 (36.3), −13.1, p < .001 |

- **The default agent is already efficient here.** Neutral pairs agree in 85 of 85 games, in 4.0 moves, and end on the frontier in 77 (joint 148.6 of a maximal 154.6, gap 6.0); in the E2 base the same pair ends on the frontier in 8 of 20 with a gap of 14.6. The grounded instances have more joint value (maximal joint 155 against 140; integrative gain 21.4 against 17.9) because 55 of 85 contain a decision both sides want the same way, and fewer decisions (two in 34, three in 47).
- **So the map flattens, in the direction the synthetic core predicts for a partner that cannot be exploited.** Nothing beats neutral when adopted alone: collaborating's +7.0 of the base becomes +0.5 and competing's +1.1 becomes +0.7; the soft strategies still lose (compromising −4.7, accommodating −3.9, both significant; avoiding −15.2); competing pairs still destroy value (−10.2; 59 of 85 agreements) and so do avoiding pairs (−10.6; 62 of 85). Against a competing partner collaborating is still the best reply (+2.1) and matching still costs (−2.9), with avoiding the worst reply (−13.1). The 30 instances contested on every decision show the same picture (collaborating against neutral +0.7, competing pairs −17.3, avoiding pairs −23.5).
- **Cost keeps its order but shrinks**, tokens per agent and game in symmetric play: competing 2,490 (4,080 in the base; games end in 5.2 moves instead of 8.7), collaborating 1,820, neutral 1,670, compromising 1,540, accommodating 1,260, avoiding 890.
- **Rule compliance is lower, partly for structural reasons.** Collaborating by rule is complied with in 33 of 85 games against neutral, 4 of 33 on instances with one contested decision (the rule asks every proposal to give each side its top option on a contested decision, impossible with one) and 29 of 52 with two or more; competing 69 of 85, accommodating 55 of 85, compromising 72 of 85, avoiding 84 of 85. The grounded manipulation of collaborating is therefore weaker than in the core; a rule written in shares of contested decisions, or a compliance check that skips infeasible rules, is needed before the grounded cells are compared strategy by strategy.

**Reading.** Two things differ from the synthetic core at once: the structure (fewer decisions, shared decisions, a middle option that is a genuine compromise) and the semantics (each side is told its own ticket and the other's, and the option names say which ticket each serves, so the partner's needs are legible as under open information). Either can explain why the default agent finds the frontier and the strategies have little to add. The cheap control is to replay the same 85 structures under the abstract skin (Decision 1, Option A; about $0.9): if collaborating's edge returns, it is the semantics; if not, the structure. Until then the grounded layer says that on real-conflict structures the conflict-handling strategy matters less than in the generated regime, and that the ranking of what not to do (avoid, match a hard bargainer, pair competing with competing, concede unilaterally) is unchanged.

### Strategy payoff study: grounded control, structure against semantics (2026-11-20)

Config, frozen before the run: [configs/bargain_grounded_abstract.json](../configs/bargain_grounded_abstract.json): the same 85 structures, option letters, seats and 24 cells as the grounded run, rendered under the abstract skin (Developer 1 and 2, Decision 1, Option A; no ticket context, no roles). 2040 games, $0.82, no errors. Results: `results/bargain_grounded_abstract_{rows.csv,summary.json,chart.svg,matrix.svg}`. Own payoff as control / grounded / E2 base:

| strategy | both sides use it | adopted alone against neutral | against a competing partner |
|---|---|---|---|
| neutral | 68.7 / 74.3 / 62.6 | 68.7 / 74.3 / 62.6 | 63.1 / 67.0 / 50.4 |
| competing | 58.6 / 64.1 / 46.9 | 73.9 / 75.0 / 63.7 | 58.6 / 64.1 / 46.9 |
| collaborating | 73.0 / 74.7 / 65.6 | 75.4 / 74.8 / 69.6 | 66.5 / 69.1 / 53.6 |
| compromising | 59.2 / 69.4 / 58.4 | 62.8 / 69.6 / 54.9 | 55.5 / 64.0 / 42.6 |
| accommodating | 74.6 / 73.9 / 65.7 | 62.8 / 70.4 / 60.8 | 59.3 / 66.9 / 50.0 |
| avoiding | 59.5 / 63.6 / 52.1 | 55.2 / 59.1 / 47.8 | 50.8 / 53.9 / 36.3 |

- **The map comes back.** On the same structures without the ticket context, collaborating adopted alone beats neutral by 6.7 points (69 wins to 41, p = .010), against +0.5 with the context and +7.0 in the E2 base; competing alone gains 5.2 (not significant), compromising and accommodating alone lose 5.9 each (p < .01), avoiding 13.5. Accommodating pairs are again the most efficient cell (+5.9 over the neutral pair, p = .067; gap 5.4, 82 of 85 on the frontier) with collaborating pairs next (+4.3); competing pairs lose 10.1 and avoiding pairs 9.2. Against a competing partner collaborating is again the best reply (+3.4), matching costs 4.5 (p = .001) and avoiding 12.3. The best responses are E1's: collaborating to neutral, to competing and to collaborating.
- **The default pair leaves the frontier.** Neutral pairs end on the frontier in 61 of 85 games (77 with the context) with a gap of 17.2 (6.0); paired by instance and seat the first seat loses 6.7 points (p = .044) and the second 4.4. Their talk changes with the framing: without it the neutral agents state their scores in 53 of 85 games and ask in 38 (11 and 24 with the context); with the tickets they negotiate in terms of what each side's change needs, without them in numbers.
- **So the grounded flattening is semantic, not structural.** Fewer decisions and shared decisions are held fixed here and the strategy effects return, so what removed them was the ticket context: it makes the partner's needs legible, as open information did in E2, and the default agent then finds the frontier on its own. Tokens and compliance are as in the grounded run (neutral 1,470, competing 2,460, collaborating 1,790, accommodating 980, avoiding 730 per agent and game; collaborating by rule complied with in 38 of 85 games, for the structural reason noted above).

**Reading for the paper.** The strategy map of the synthetic core is a property of bargaining under hidden interests. A realistic framing that reveals what the other side needs takes most of the strategic room away from the default agent, and what survives the framing is the list of what not to do: avoid, concede alone, match a hard bargainer, pair competing with competing. This is the argument for keeping both layers in the environment: the core measures the strategies where interests are hidden, the grounded layer measures how much of that a realistic framing removes, and the pair of runs on identical structures is the manipulation check for the framing itself.

### Strategy payoff study: the collaborating rule, revised (2026-11-20)

The round-3 collaborating rule (used in E1, E2 and both grounded runs) asks every proposal to give each side its preferred option on at least one contested decision, which cannot be done on an instance with a single contested decision: 33 of the 85 grounded instances, where it was complied with in 4 of 33 games against 29 of 52 with two or more. The revised text keeps the trade clause for two or more contested decisions and adds one for a single contested decision: propose its middle option, and give the other developer their preferred option on it if it matters more to them than to you. The checker (`collaborative_proposal`) tests the clause that applies: a trade with two or more contested decisions, any proposal that does not insist on one's own top option with one, and nothing with none. The old text is archived in [configs/bargain_dev.json](../configs/bargain_dev.json) as `rules_round3`; the frozen E1, E2 and grounded configs are unchanged, so their compliance figures are those of the old rule. Two replay configs are frozen for the user's go, [configs/bargain_grounded_collab.json](../configs/bargain_grounded_collab.json) and [configs/bargain_grounded_abstract_collab.json](../configs/bargain_grounded_abstract_collab.json): the five collaborating cells with the revised rule and the three neutral and competing reference cells, 680 games each (about $0.3), with the prediction that compliance rises on one-contested-decision instances while collaborating's payoff against neutral stays near the earlier run's.

### Strategy payoff study: the collaborating cells replayed with the revised rule (2026-11-20)

Configs as frozen above; 680 games per rendering ($0.33 with the ticket context, $0.32 under the abstract skin), no errors. Results: `results/bargain_grounded_collab_*` and `results/bargain_grounded_abstract_collab_*`. The checker gained one refinement after the first reading of the replays: with a single contested decision, accepting a proposal of the partner's that takes that decision away from the agent counts as the concession the clause asks for (the stored compliance of the 1360 replay games was recomputed from their moves; 145 records changed).

Compliance of collaborating by rule, all five collaborating cells, on instances with one contested decision and with two or more (the round-3 rule and check in brackets): with the ticket context 104 of 165 and 159 of 260 (21 of 165 and 166 of 260); under the abstract skin 95 of 165 and 163 of 260 (29 of 165 and 149 of 260). The structural gap is closed: compliance no longer depends on how many decisions are contested, and sits near 60 percent in both renderings. The remaining non-compliant one-contested games are those in which the partner accepted the agent's opening proposal, which carried the agent's own top option on the contested decision in 27 of 31 openings with the context and 21 of 22 without: the protocol requires a proposal with the first message, before the agent has asked which decision the partner cares about, so the clause "propose its middle option" is rarely followed at the opening and the agent only concedes if the game goes on.

Payoffs are where the earlier runs left them, as predicted. With the ticket context, collaborating alone against neutral +0.9 (35 to 35; +0.5 before), collaborating pairs +1.8 over the neutral pair (+0.4), against a competing partner +2.1 (+2.1). Under the abstract skin +4.8 against neutral (67 to 43, p = .028; +6.7 before), +5.9 in pairs (p = .090; +4.3), +3.3 against competing (+3.4). The reference cells reproduce (neutral pairs: joint 147.8 against 148.6 with the context, 136.6 against 137.4 without; competing against neutral 74.4 against 73.7 and 71.3 against 71.8). The revised rule changes what the agent proposes a little (the share of contested decisions set to the middle option in agreements against neutral rises from 0.10 to 0.12 under the abstract skin and from 0.09 to 0.16 with the ticket context; corrected 2026-11-20, the figures first reported here, 0.34 and 0.42, counted option index 1, see the E4 section) and what it earns not at all; the grounded conclusions stand with a rule the agent can follow on every instance.

### Strategy payoff study: E4 scenario skins, the map under three framings (2026-11-20)

Config, frozen before the run: [configs/bargain_e4.json](../configs/bargain_e4.json) (the index) with one config per skin ([software_design](../configs/bargain_e4_software_design.json), [team_plan](../configs/bargain_e4_team_plan.json), [release_plan](../configs/bargain_e4_release_plan.json)). E1's 40 instances (same seed; the structures, weights and seats are identical, checked instance by instance) rendered as the design of a shared module between two developers, a quarterly plan between two team leads and a joint release plan between two product owners; the rule diagonal (six pairs) plus competing-neutral and collaborating-neutral, 8 cells; strategies and rule texts byte-identical to E1 (the round-3 collaborating text, whose one-contested clause never applies with three contested decisions), so E1 is the abstract rendering of the same games and every E4 game has a twin. 960 games, $0.56, 15 minutes, no errors, no invalid replies. Results: `results/bargain_e4_{software_design,team_plan,release_plan}_*`, the cross-rendering chart [results/bargain_e4_renderings.svg](../results/bargain_e4_renderings.svg), the paired statistics [results/bargain_e4_paired.json](../results/bargain_e4_paired.json), and the condition map with the skins as conditions and E1 as the abstract one (`results/bargain_e4_map.{json,svg}`).

**The map does not depend on the framing.**

- *Diagonal order.* By joint payoff, under every skin: collaborating > accommodating > neutral > compromising > competing > avoiding. E1's abstract order swaps the two top pairs and the two bottom pairs (accommodating > collaborating, avoiding > competing); both swaps are within noise in both directions. Against the neutral pair within each rendering (joint, paired by instance): collaborating pairs +4.8 (p = .047), +6.3 (p < .001) and +12.3 (p < .001) under the three skins, +7.9 (p = .050) abstract; accommodating +4.6, +5.4 (p = .020), +5.8 (p = .020), +8.7 abstract; compromising −9.1, −8.8, −4.1, −12.7 abstract; competing −28.3 (p = .003), −15.2, −13.0, −25.0 abstract; avoiding −32.1 (p = .008), −22.1, −30.3 (p = .011), −14.8 abstract.
- *Paired with the abstract twin* (same instance, same cell), the joint payoff moves by less than 5 points in 17 of the 24 skin-by-cell comparisons, and only two comparisons reach .05: avoiding pairs under release_plan (−16.5, p = .02; −14.6, p = .09 under software_design) and the first seat's own payoff in competing pairs under team_plan and release_plan (+7.0, p = .05; +7.3, p = .03). Both are the knife-edge cells. Avoiding pairs accept only at or above their outside option, and their single opening is worth 28 points to the responder under the skins against 32 abstract, so walks rise from 8 of 40 games to 12, 12 and 17. Competing pairs walk in 9 to 16 of 40 games depending on the rendering with the same openings (worth 25 to the responder) and the same concessions (29 to 30 points), which is binomial noise around a 30 percent impasse rate.
- *Condition map.* The symmetric reading is identical in all four renderings: collaborating at λ = 0, accommodating once tokens cost 5 points per thousand or more. Against neutral, collaborating is best at λ = 0 and 5 in all four (the skin configs hold only competing and collaborating against neutral, so that reading compares the two deviations with staying neutral).
- *Unilateral deviations.* Competing against a neutral partner earns the deviator nothing in any rendering (+2.7, −1.2, +3.7, −0.2 over its payoff in the neutral pair; none significant) and costs the partner 7 to 13 points. Collaborating against neutral keeps its joint gain (+6.7 abstract; +5.8, +8.3, +8.0 under the skins), but under the skins the gain goes to the partner rather than the deviator: own +7.4 (p = .017) abstract against +0.7, +3.8 and +5.3 (none significant); partner −0.7 abstract against +5.1, +4.4 and +2.7. Paired with its abstract twin, the collaborating agent earns 4.6 to 6.5 points less (p = .08 to .26) and its neutral partner correspondingly more. This is the grounded finding in a weaker form, without any ticket context: legible option names let the neutral partner hold its own.
- *Costs and compliance.* Skinned prompts are longer, so neutral pairs use 14 to 39 percent more tokens per game (4300 to 5200 against 3760); collaborating pairs use fewer (4160 to 4730 against 5390) because they settle in fewer moves (4.4 to 4.8 against 5.6) and reach the frontier more often (27 to 31 of 40 against 25). Compliance is unchanged: competing 35 to 38 of 40 (39 abstract), collaborating 23 to 31 (24), compromising 34 to 39 (36), accommodating 35 to 39 (36), avoiding 40 (40).

**Option names carry a prior.** Pooled over the eight cells, the agreed option on a contested decision is the second-listed option B ("the existing SQL tables", "one senior and two junior engineers", "Tuesday to Thursday", "every two weeks", "end of April") in 43 to 46 percent of cases under the skins against 37 percent abstract, while the share set to the value-middle option (the one Developer 1 ranks second, which is the compromise for both sides where they clash) does not rise: 19 to 24 percent against 25. The skins' B options read as the moderate choice, and the agents drift to them regardless of their scores; on decisions the sides agree on, B's share is unchanged (33 to 36 percent against 32). The drift has no visible cost (the neutral pair's joint payoff is within 3 points of abstract in every skin) because the option that sounds moderate is the value middle on only a third of the decisions and an agreement still has to pass both acceptance thresholds. Skins whose option names form a three-step scale invite this; further skins should avoid ordinal option names, and the paper should report the drift as a property of the framing rather than hide it.

**A correction found on the way.** `middle_share` and the frontier's compromise benchmark took option index 1 to be the middle option. That is true of the grounded layer's canonical order before the letters are shuffled and of nothing else: the generator draws the rankings, so index 1 is the middle option on a third of the issues. Both now use the option Developer 1 ranks second (`bargain.middle_option`), which is the middle for both sides on a contested decision; all 13,148 stored records were recomputed and every summary regenerated (payoffs, compliance and the matrices are untouched). The two sentences in this document that quoted the share are corrected in place (E0's "a third of the contested decisions" and the replay's 0.34 to 0.42, which were the shares of a random letter); no other reported number used it.

**Reading.** The framing manipulation check is passed in the sense that matters for the paper: the order of the symmetric pairs, the sign of every pair's difference from the neutral pair, the best strategy in the condition map and the verdict on competing as a unilateral deviation are the same under four renderings of the same games. What the framing changes is who captures collaborating's unilateral gain: the deviator in the abstract rendering, the partner once the options have names. With the grounded control that gives a consistent account, in which the semantic legibility of the options, not the structure of the conflict, is what flattens the strategy's edge; the main map can be reported on the synthetic core with the skins and the grounded layer as its robustness and boundary conditions.

### Strategy payoff study: E3 models, design and smoke (2026-11-21)

Access to other models arrived through the University of Arizona AI Assistant, an OpenAI-compatible LiteLLM proxy with a $200 monthly budget (see [DEVELOPMENT.md](DEVELOPMENT.md), model providers): the configs name models with a provider prefix (`ai2s/claude-sonnet-5-5`), `agent_coordination/models.py` routes the call, and the cost of every call is the proxy's own figure from its response header. Two smoke games per Claude model and three probe calls settled the sampling question: the proxy's Claude models accept temperature 1 only, and `gpt-6.1-sol` rejects any other temperature as well (it reports its reasoning as active even with `reasoning_effort` none), so every E3 arm runs at the model's default temperature, and a `gpt-6-luna` arm at the default temperature is added as the control for the difference from E1's 0.7 / top-p 0.8. The Claude models also think by default through the proxy (about 170 hidden tokens per reply, billed as completion tokens, invisible to `reasoning_effort`); in a first start of the haiku arm the thinking ran into E1's 700-token reply cap and truncated the JSON in a quarter of the calls (65 rejected replies and 12 lost moves in 46 games), so the Claude arms run with `thinking: disabled`, which the proxy accepts and which matches the reasoning-off setting of the gpt arms; the 46 games were discarded. Game records now keep every rejected reply with its error (`rejected`). In the smoke games both Claude models reached the frontier in 3 to 4 moves, the collaborating agent opened by naming its priority and asking for the partner's, and the replies were long: 400 to 900 completion tokens per agent and game against about 200 for `gpt-6-luna`. Cost per game from the proxy's figures: `claude-haiku-5-5` $0.0012, `claude-sonnet-5-5` $0.016; `gpt-6.1-sol` is priced like Sonnet ($2 and $10 per million tokens).

Design, frozen in [configs/bargain_e3.json](../configs/bargain_e3.json) and one config per arm (`configs/bargain_e3_{claude_haiku_5_5,claude_sonnet_5_5,gpt_6_1_sol,gpt_6_luna_t1}.json`): E1's eight reading cells (the rule diagonal, competing-neutral and collaborating-neutral) on E1's 40 instances with E1's rule texts, so that every game has a twin in E1; 320 games per arm, 1,280 in all, about $11 by the smoke prices (Sonnet and sol about $5 each). Predictions are in the configs: E1's order of the symmetric pairs on every model; collaborating alone beats neutral and competing alone does not, everywhere; the Claude models follow the collaborating trade clause more often; the token order holds with a lower switch price for the verbose models; the luna control reproduces E1 within 3 points. The two conditions that move the map most (open information and unequal stakes) follow on the models that keep the base map.

### Strategy payoff study: E3 models, results (2026-11-21)

Configs as frozen above, with two changes made before or during the run and recorded in them: the Claude arms run with `thinking: disabled` (the proxy's Claude models think by default, about 170 hidden tokens per reply billed as completion tokens, and with E1's 700-token reply cap the thinking truncated the JSON in a quarter of the calls of a first start of the haiku arm, 46 games, discarded), and the luna control was replayed directly against OpenAI (through the proxy `reasoning_effort` none is not honoured for `gpt-6-luna` either: 284 of 1,761 calls returned 700 completion tokens with an empty or truncated reply and 57 moves were lost; that run is kept under `runs/bargain/e3_gpt_6_luna_t1_proxy` and not reported). Reported arms: `claude-haiku-5-5` 320 games $0.39, `claude-sonnet-5-5` $8.21, `gpt-6.1-sol` $6.17, `gpt-6-luna` at the default temperature $0.18; no errors; rejected replies 14, 49, 3 and 2 of about 1,600 calls (sonnet's are long replies that hit the 700-token cap; 5 moves lost in 320 games), against 0 in E1. Results: `results/bargain_e3_<arm>_{rows.csv,summary.json,chart.svg,matrix.svg}`, the model map `results/bargain_e3_map.{json,svg}`, the paired statistics [results/bargain_e3_paired.json](../results/bargain_e3_paired.json) and the chart [results/bargain_e3_models.svg](../results/bargain_e3_models.svg). Every comparison is paired by instance with E1's cell on the same 40 instances (exact sign tests).

**The control first.** `gpt-6-luna` at the default temperature reproduces E1 within noise in every cell (neutral pair joint 130.0 against 127.6, p = .49; collaborating pairs 136.8 against 135.5; accommodating pairs 136.3 against 136.3; compromising 114.4 against 114.9; competing pairs 94.1 against 102.5, p = .12; collaborating alone against neutral +5.7 own, against +7.4). The sampling difference between E1 and the proxy arms does not drive what follows.

Joint payoff of the neutral pair and the five symmetric pairs' difference from it (sign p), own payoff of the unilateral deviation against neutral minus neutral's own payoff in the neutral pair, and the default pair's frontier rate:

| arm | neutral pair joint (frontier /40) | competing | collaborating | compromising | accommodating | avoiding | competing alone (partner) | collaborating alone (partner) |
|---|---|---|---|---|---|---|---|---|
| gpt-6-luna 0.7 (E1) | 127.6 (20) | −25.0 (.003) | +7.9 (.050) | −12.7 (.011) | +8.7 (.005) | −14.8 (.058) | +2.7 (−11.6) | +7.4, p = .017 (−0.7) |
| gpt-6-luna default | 130.0 (16) | −35.3 (<.001) | +6.0 (.029) | −15.0 (.001) | +6.8 (.050) | −22.7 (.029) | +1.3 (−15.1) | +5.7 (+1.6) |
| claude-haiku-5-5 | 135.0 (29) | −17.9 (<.001) | +5.3 (.013) | −8.6 (.047) | +1.1 (.86) | −29.1 (<.001) | +2.3 (−3.7) | +0.9 (+1.7) |
| claude-sonnet-5-5 | 138.1 (28) | −34.4 (<.001) | +2.5 (.001) | −6.1 (<.001) | −0.2 (.85) | −12.5 (.15) | −0.2 (+0.5) | −0.8 (+2.6) |
| gpt-6.1-sol | 138.8 (34) | −28.7 (<.001) | +1.7 (.008) | −8.2 (<.001) | −3.8 (.23) | −10.9 (.011) | +1.1 (−3.3) | −3.1, p = .011 (+3.8) |

- **The default agent is much stronger on the three other models.** Neutral pairs reach 135.0, 138.1 and 138.8 of a maximal 141 (gaps 6.6, 3.5 and 2.8 against 14.0; on the frontier in 29, 28 and 34 of 40 games against 20), +7.5 to +11.3 over their E1 twins (p ≤ .02). They get there by asking rather than telling: the neutral agents ask about the partner's priorities in 69, 80 and 80 of 80 games (37 for luna) and state their own scores in 37, 8 and 30 (51 for luna); sol's messages are twice as long (886 characters against 444).
- **The sign structure of the map holds on every model.** Collaborating pairs are the best symmetric cell on all four arms and above the neutral pair in each (+5.3, +2.5, +1.7, p ≤ .013), competing pairs the worst everywhere (−18 to −34, p < .001; sol loses on 40 of 40 instances, sonnet on 38 of 39; agreement 32, 25 and 29 of 40), compromising pairs below the neutral pair on all (−6 to −9, p ≤ .047), avoiding pairs too (−11 to −29). Competing adopted alone earns the deviator nothing on any model (+2.3, −0.2, +1.1, none significant) and matching a hard bargainer is destructive on all. The best response among the eight cells is collaborating to neutral in joint terms on every arm.
- **What changes is who gains from the soft strategies.** Two accommodators are no longer better than two default agents (+1.1, −0.2, −3.8, none significant; +8.7 for luna): when the default pair already finds the frontier, taking the partner's priorities adds nothing. And collaborating adopted alone no longer pays the deviator: own +0.9 (haiku), −0.8 (sonnet) and −3.1 (sol, p = .011, 8 wins to 24), while the partner gains +1.7, +2.6 and +3.8 and the joint payoff still rises (+2.6, +1.8, +0.7). This is the third time the same thing appears: under the skins and under the ticket context collaborating's gain went to the partner, and here it goes to a stronger partner. The rule creates value; whether its user keeps it depends on whether the other side can find the frontier without it.
- **The default agent is also harder to exploit.** Against a competing partner, luna's neutral agent kept 51.1 (E1); haiku's keeps 62.2, sonnet's 69.8 and sol's 67.0, and the competing side gains no more than against its own default (71.3, 68.7, 69.6 against 69.1, 68.9, 68.6); joint payoff in that cell is 133.6 to 138.4 against 118.7 (p ≤ .01 paired with E1). Power to extract comes from the partner's weakness, not from the rule.
- **Compliance** is higher on the stronger models for the rule that needs a constructed package, as predicted: the collaborating trade clause is met in 52, 71 and 64 of 80 games (45 for luna, 45 at the default temperature); competing, compromising, accommodating and avoiding are followed in 35 to 40 of 40 by sonnet and sol. The exception is haiku, which follows the competing rule in 46 of 80 games (it changes more than one decision per proposal) and accordingly loses less in competing pairs (−17.9, 32 agreements).
- **Cost.** The token order by strategy (competing > collaborating > neutral > compromising > accommodating > avoiding) holds on every arm. Per game and pair the stronger models talk more: neutral pairs 6,100 to 8,700 tokens against 3,760, competing pairs 8,800 to 13,400 against 8,000; sol is the exception in collaborating pairs (3,200 against 5,400), where it settles in 3.5 moves. The model map (seats pooled) therefore moves to the cheap strategies at a lower price of tokens: symmetric play is collaborating at λ = 0 and accommodating at λ = 5 on haiku and sonnet (collaborating survives to λ = 5 on sol); against neutral the best strategy is competing or neutral at λ = 0 by a fraction of a point on haiku, sonnet and sol and collaborating from λ = 5 on haiku and sol, where its shorter games pay.

**Reading for the paper.** The map is a property of the game in its signs and of the model in its margins. What transfers across four models: collaborating pairs on top, competing pairs at the bottom, compromising and avoiding pairs below the default, competing alone worthless, matching a hard bargainer ruinous. What does not: the room above the default agent, which is 14 points of joint value for `gpt-6-luna` and 3 to 7 for the others, and with it the payoff of adopting a soft strategy alone, which goes to the partner on the stronger models. The result turns the E1 headline ("collaborating by rule dominates") into the more careful one the grounded control already suggested: a collaborating rule raises the joint payoff with every partner, and its user keeps the gain only against a partner who cannot find the frontier alone. Two practical lessons for the proxy: the Claude models must be run with thinking disabled and OpenAI's models with reasoning off at the source, or hidden reasoning silently eats the reply budget; and the proxy's cost header, not litellm's price list, is the figure to record.

### Strategy payoff study: E3 follow-up, two conditions on two models (2026-11-21)

Configs, frozen before the run: [configs/bargain_e3_conditions.json](../configs/bargain_e3_conditions.json) (the index, which also lists E2's luna runs of the same conditions) and one config per arm, `configs/bargain_e3_{open_info,low_stakes_p2}_{claude_sonnet_5_5,gpt_6_1_sol}.json`: E2's open-information and unequal-stakes conditions on E2's 20 instances, with 16 of E2's 24 cells (the diagonal and every strategy against neutral in both seats; the eight cells against competing were left out to fit the month's proxy budget), the models set as in their E3 base arms. 1,280 reported games, $26.46 (sol $11.07, sonnet $15.39), no errors; with the discarded first run of sonnet's open-information arm (below) the follow-up cost $37.50, and the proxy key's spend stands at $160 of $200. Results: `results/bargain_e3_<condition>_<model>_*`, the map `results/bargain_e3_conditions_map.{json,svg}` and the paired statistics [results/bargain_e3_conditions_paired.json](../results/bargain_e3_conditions_paired.json). Comparisons are paired by instance (and seat where pooled) with the arm's own neutral pair and with the E2 twin.

**One arm was replayed.** Under open information sonnet's replies run long (it reasons about both score tables in its message): in the first run of that arm 342 of 1,620 calls hit E1's 700-token reply cap with the JSON cut off, 344 replies were rejected and 78 moves lost. The arm was replayed the same day with a 1,500-token cap (`max_tokens_note` in its config; the first run is kept under `runs/bargain/e3_open_info_claude_sonnet_5_5_cap700` and not reported): 1 rejected reply in 1,275 calls, no lost move, about 580 completion tokens per reply, $8.51. Every number below is from the replay; the direction of every result was the same in the truncated run. The sol arms (1 rejected reply in 2,900 calls) and sonnet's unequal-stakes arm (45 rejected, 5 lost moves) ran under the 700-token cap without trouble.

**Open information.** On gpt-6-luna this condition made competing the best unilateral strategy (+9.4, p = .047, by exploiting a default partner that could see its scores) and left the default pair 12 points short of the frontier. On the stronger models the default pair is on the frontier in 19 and 20 of 20 games (joint 138 of 140, gap 1.8) and nothing is left for a strategy to add: competing alone gains nothing (sonnet −0.9, sol +0.2; the partner's payoff does not move), collaborating pairs tie with the neutral pair (+0.6, +0.8), and every soft strategy adopted alone is a pure transfer to the partner, who finds the frontier anyway: collaborating −2.3 (p = .078) and −2.8 (p < .001; partner +1.6, +3.0), compromising −8.9 and −10.4, accommodating −13.6 and −15.2, avoiding −10.7 and −9.8 (all p < .001, at most 1 win in 32 to 40 pairs). Competing pairs remain the worst cell (−33.8 and −30.8, 1 win in 20 and 0 in 20), compromising pairs lose 6.8 and 10.6; avoiding pairs on sonnet reach 136.5 with 16 of 20 on the frontier in 2.2 moves, since with everything visible one proposal suffices. Compliance is near perfect on both models (59 to 60 of 60 for every rule, the collaborating trade clause included, against 53 of 60 for luna; the exception is accommodating on sonnet, 43 of 60).

**Unequal stakes** (the second seat's points and outside option scaled by 0.3). On gpt-6-luna the high-stakes side did best by collaborating against neutral (68.1 against 57.4) and the low-stakes side by competing (23.1 against 20.5 of 30), worst by accommodating and avoiding (17.1, 17.0). On the stronger models the high-stakes side's collaborating edge is gone (sonnet 68.9 against 70.5 in the neutral pair, sol 67.0 against 67.5), because the default partner already concedes the low-stakes issues (the neutral pair gives the high-stakes side 70.5 and 67.5 against luna's 57.4, gap 17 against 29); its best unilateral strategy is competing by a small margin (+1.0, +3.2). The low-stakes side's lesson holds on every model: accommodating is its worst strategy (17.5 and 15.1 of 30 against 19.4 and 21.4 in the neutral pair; pooled over seats accommodating alone loses 6.8 and 10.6, p < .001, and the partner gains 6.7 and 9.4), avoiding next (15.3, 16.1), competing its best (21.8 on sonnet; 21.3 on sol, a tie). Pooled over seats, competing alone is the one strategy with a positive gain on sonnet (+1.7, 24 wins to 8, p = .007). Symmetric play: accommodating pairs lead on sonnet (+3.6, not significant) and tie on sol, collaborating pairs +1.0 and +0.9, competing pairs −26.1 and −21.1 (0 of 20 and 2 of 18 wins), avoiding pairs −9.4 and −0.2 (on sol, 19 of 20 agree; on luna 11). Compliance: sonnet and sol follow every rule in 59 to 60 of 60 games except collaborating (47 and 54; luna 39).

**Reading.** The two conditions that moved luna's map most move the stronger models' maps less, for the reason E3 found: their default agent is already at or near the frontier, so a condition that changes what there is to find changes little, and a strategy adopted alone can only move the split. What survives on every model and in both conditions is the list of what not to do (pair competing with competing, compromise, avoid, and, for the side the issue matters less to, accommodate), the sign of collaborating pairs against the neutral pair (never negative), and the finding that competing alone does not pay once the partner cannot be exploited. The one textbook prescription that fails on every model is the one for unequal stakes: the side that cares less does worst in payoff by accommodating, and the gain goes to the other side. The paper's condition map should be presented as luna's, with the stronger models as the limit in which the default agent closes the gap and the conditions stop mattering; the seat result under unequal stakes is model-independent and can be stated as such.

### Strategy payoff study: two external benchmarks, design (2026-11-21)

The question a reviewer will ask of the map is whether it holds outside our generator. Two published multi-issue negotiation benchmarks are reduced to the environment's instance format, so that the same strategy library, checker and measures run on them unchanged ([benchmarks.py](../src/agent_coordination/benchmarks.py); the code now takes a per-issue number of options). **CaSiNo** (Chawla et al. 2021, CC BY 4.0): two campers split three packages each of food, water and firewood; a camper's packages of its high-, medium- and low-priority item are worth 5, 4 and 3 points (36 at most) and a walk-away pays 5 to both. The 36 ordered pairs of priority orders are the instances (the full factorial of the benchmark's structures; six are distributive, six fully reversed), each item a decision with four options (how many packages go to Camper 1). **LLM-Deliberation** (Abdelnabi et al. 2024, MIT): six parties negotiate five issues with three to five options each, every party with a score table and an acceptance threshold. The reduction keeps two parties and their score tables over all five issues (a decision is contested when their top options differ and both sides' scores vary on it; a side that scores every option alike is indifferent), takes every pair with a contested decision in the four games (57 pairs; one to five contested decisions each), sets the outside options at 30 percent of each party's maximum as in the core and keeps the original thresholds for a secondary check; each party is told the game's own scenario paragraph and both parties' names. Neither reduction uses dialogue text.

Configs, frozen before the runs ([bargain_casino.json](../configs/bargain_casino.json), [bargain_deliberation.json](../configs/bargain_deliberation.json)): the current rules (with the revised collaborating clause), E1's model settings on `gpt-6-luna`, 16 cells (the rule diagonal and every strategy against neutral in both seats), 576 and 912 games, about $1.5 together. Predictions are in the configs: E1's order of the symmetric pairs on both; on CaSiNo collaborating alone pays where priorities differ and not on the six distributive pairs, the accommodating rule (which accepts at 8.6 of 36 with so low a walk-away) loses most of any strategy adopted alone, and the neutral pair is close to the frontier because the trades are obvious; on LLM-Deliberation the neutral pair finds the frontier in more than half of the games thanks to the indifferent decisions, collaborating's unilateral gain is under 5 points, and fewer than half of the agreements clear both parties' original thresholds. In two smoke games each the agents argued in the benchmarks' terms ("giving you all the firewood is a fair trade for my larger shares of food and water"; "I can accept the water-based mix and balanced ecological option, but...").

### Strategy payoff study: two external benchmarks, results (2026-11-21)

Configs as frozen above. CaSiNo: 576 games, $0.33, no errors, no rejected reply. LLM-Deliberation: 912 games, $0.46, no errors; 84 rejected replies in 3,357 calls (81 named an option in words the parser did not recognize, 0 carried no JSON; none hit the reply cap) and 35 lost moves (collaborating-collaborating 5, accommodating-neutral 4, competing-competing 4, compromising-compromising 3), about one percent of the calls, noted and not corrected. Results: `results/bargain_casino_*`, `results/bargain_deliberation_*` and the paired statistics [results/bargain_benchmarks_paired.json](../results/bargain_benchmarks_paired.json). Both ran on `gpt-6-luna` with E1's settings, so every difference from the core is the game's, not the model's.

**CaSiNo.** The default pair is close to efficient: 36 of 36 agreements, joint 37.7 of a maximal 40.0 (gap 2.3), on the frontier in 22 of 36 games, in 4.1 moves; the agents state their scores in 57 of 72 cases and ask in 13. With the walk-away worth 5 of 36, the default agent also accepts poor deals: the lower side averages 13.3 and five agreements give one side under 10. On this structure the map compresses to the picture of E3's stronger models and of the grounded layer, with one exception. Symmetric play: collaborating pairs 36.9 (−0.8 against the neutral pair, not significant; 33 of 36 agreements), accommodating pairs 37.3 (−0.4), avoiding pairs 36.3 (−1.4), compromising pairs 32.2 (−5.4, p = .036: 2-1 splits of every item), and competing pairs do not agree once in 36 games (joint 10.0, both at their walk-away, −27.7, 0 wins): with four options per item the competing rule's one-step concessions from a take-everything opening cannot meet in ten moves. The exception is the unilateral deviation: **competing alone beats the default agent by 5.6 points of 36 (52 wins to 11, p < .001) and takes 7.6 from it**, on the identical-priority pairs (+7.2, p < .001) and the mixed ones (+5.7, p < .001) alike; collaborating alone gains nothing (0.0; +2.2 on identical, −1.2 on reversed, −0.2 on mixed), compromising +0.4, accommodating −1.1 (partner +1.9), avoiding −2.6 (p = .004). This is the condition E2 found for competing on luna (distributive, tight budget, open information): a default partner that does not push back, here because its outside option is too small to refuse anything. Compliance is high (competing 92 of 108, collaborating 90, compromising 84, accommodating 76, avoiding 108), the collaborating trade clause at 83 percent against 56 in E1, because the trade is obvious (whole items). Against the frozen predictions: right on competing pairs (the most walk-aways: all of them), compromising pairs below the neutral pair, the small gap of the default pair and compliance; wrong on collaborating pairs leading (they tie), on collaborating alone paying where priorities differ (it does not), on competing alone not paying (it is the one strategy that does) and on accommodating losing most alone (avoiding does).

**LLM-Deliberation.** The default pair agrees in 57 of 57 games with joint 158.9 of a maximal 171.9 (gap 13.0, 8 percent), on the frontier in 22 of 57, in 4.5 moves, asking in 55 of 114 cases and disclosing in 24; 53 of its 57 agreements clear both parties' original acceptance thresholds, as do 739 of the 883 agreements of all cells (84 percent; the thresholds were set for six-party deals and bind little when two parties split 172 points with outside options of 30). Symmetric play: collaborating pairs 160.4 (+1.4, not significant), compromising 154.4 (−4.6), accommodating 155.1 (−3.9, p = .015), competing 143.3 (−15.6, 45 agreements; −34.5 on the 13 pairs contested on every decision), avoiding 138.9 (−20.0, p < .001; −41.6 with five contested decisions). Adopted alone against the default agent nothing pays: competing +0.4 (seat 1 +4.2, seat 2 −3.4), collaborating −1.9 (partner +3.8; −8.4, p = .01, on the all-contested pairs), compromising −6.8, accommodating −10.9 and avoiding −11.9 (all p < .001; the partner gains 3.7 to 8.8). Compliance: competing 163 of 171, collaborating 110, compromising 142, accommodating 150, avoiding 171. Against the predictions: right on competing alone not paying, on the soft strategies losing alone and on collaborating pairs at the top in joint terms; wrong on collaborating alone gaining anything, on the default pair's frontier rate (39 percent, not more than half) and on the thresholds (84 percent of agreements clear them, not fewer than half).

**Reading.** On two human-designed multi-issue games played by E1's model, the default agent is within 6 to 8 percent of the frontier, and the strategy map looks as it does on real-conflict structures and on the stronger models rather than as in E1: collaborating pairs are at the top but not above the default pair, competing pairs and avoiding pairs are the worst cells, compromising pairs lose, and every soft strategy adopted alone transfers points to the partner. Competing alone pays on CaSiNo and nowhere else here, and for the reason the condition map gave: its default partner cannot refuse. The claims that transfer across the core, the grounded layer, four models and two external benchmarks are the negative ones (do not pair competing with competing, compromise, avoid, or concede alone) and the sign of collaborating pairs against the default pair (never below it, above it only where the default agent leaves value on the table). The positive claim of E1, that a collaborating rule pays its user, is a property of bargaining under hidden interests between agents that do not find the frontier on their own, and the paper should say so with these two benchmarks as the evidence.

### Strategy payoff study: the last runs of the month, design (2026-11-21)

Four runs close the experimental programme, all frozen in their configs before any game was played and all on the proxy budget that was left ($40 of $200) plus the OpenAI key.

**Cross-model pairs** ([configs/bargain_e3_cross.json](../configs/bargain_e3_cross.json), one config per arm). E3 left a question open: collaborating adopted alone pays its user on `gpt-6-luna` (+5.7 to +7.4) and not on `claude-sonnet-5-5` (−0.8, the partner +2.6). Our account is the partner's strength (a default partner that finds the frontier alone leaves nothing for the rule to add); the alternative is the adopter's own model. A luna agent and a sonnet agent now play each other on E1's 40 instances with the 16 cells of the E3 follow-up, in two arms with the seats' models swapped, so that each model adopts each strategy against the other's default agent in 80 games when both arms are played. `bargain.py` gained per-seat model settings for this (`seats` in a config: model, sampling and reply cap per seat; the record stores the models and the cost per seat): luna as in E3's control (OpenAI directly, reasoning off, default temperature), sonnet as in its E3 arm with the 1,500-token cap of its open-information replay. Predictions (in the configs): sonnet's default agent takes at least 5 points more than luna's in the mixed default pair, whose joint payoff lies between the two same-model pairs (130.0, 138.1); luna's collaborating rule does not pay luna against sonnet while sonnet's pays sonnet against luna (+3 or more), the opposite of what the adopter's-model account predicts; sonnet competing takes at least 8 from luna's default agent and luna competing nothing from sonnet's; collaborating pairs best and competing pairs worst in both arms; the soft strategies lose for whoever adopts them, more against sonnet. Two smoke games (luna collaborating against sonnet neutral: 69 against 79 and 70 against 73.5, both on the frontier) cost $0.0066 and $0.0098, almost all of it sonnet's seat.

**The eight cells against competing on the two conditions.** The E3 follow-up played 16 of E2's 24 cells on open information and unequal stakes for `claude-sonnet-5-5` and `gpt-6.1-sol`; the eight cells in which a strategy faces a competing partner are added to the same four configs (`cells_note` in each; the 16 played cells are untouched) so that the *vs competing* reading of the condition map exists for the stronger models. On luna under open information collaborating was the best reply to competing (+6.8 against the neutral reply, p = .035). Prediction: on the stronger models no reply beats the neutral reply by more than 3 points and collaborating and neutral are within 3 of each other; accommodating and avoiding are the worst replies (at least 10 below neutral) and the competing side does best against an accommodator; competing against collaborating or accommodating agrees in at least 15 of 20 games and beats the competing pair's joint payoff; under unequal stakes the low-stakes side facing competing does best by competing or neutral and worst by accommodating, the high-stakes side by collaborating or neutral. 640 games.

**The grounded layer on a second model** ([configs/bargain_grounded_claude_haiku_5_5.json](../configs/bargain_grounded_claude_haiku_5_5.json)): the 85 ParallACT-derived instances with the ticket context, the 16 cells above, `claude-haiku-5-5` with thinking disabled (the one proxy model cheap enough for 1,360 games with what the budget has left: $0.0012 a game in a six-game smoke, 240 to 290 completion tokens a call, no rejected reply, so E1's 700-token cap stands). Prediction: the default pair agrees in at least 80 of 85 games and is on the frontier at least as often as luna's (77); collaborating and competing adopted alone within 3 points of neutral; compromising and accommodating alone lose at least 3, avoiding at least 10; collaborating pairs within 3 of the default pair, competing and avoiding pairs the two worst cells, competing pairs less destructive than on sonnet and sol because haiku follows the competing rule in about half of its games; the collaborating trade clause met in fewer than half of the games for the structural reason found on luna.

**Style labels** for E2's base condition, the three E4 skins, the grounded run and the four E3 arms: E1's labeler (`gpt-6-luna`, low reasoning, the public transcript only, [docs/B_BARGAIN_STYLE_CODEBOOK.md](B_BARGAIN_STYLE_CODEBOOK.md)) added to those configs after the fact (`labeler_note`), about 4,800 games at E1's $0.0004 a game. The labels are the secondary measure: they say how each model's default agent reads in TKI terms and whether the rule-following agents are recognized as their style under the skins and the ticket context, where the programmatic compliance check is weaker.

### Strategy payoff study: the cells against competing on the two conditions, results (2026-11-21)

640 games, $14.08 (sonnet $8.39, sol $5.70), no errors; rejected replies 1, 0, 36 and 0 of about 720 to 850 calls per arm (sonnet's unequal-stakes arm again ran into the 700-token cap, 34 of its 36, 6 moves lost; the open-information arm ran under its 1,500-token cap). The four arms' results files now carry 24 cells (`results/bargain_e3_<condition>_<model>_*`), the condition map has its *vs competing* reading for the stronger models (`results/bargain_e3_conditions_map.{json,svg}`) and the paired statistics are under `vs_competing` in [results/bargain_e3_conditions_paired.json](../results/bargain_e3_conditions_paired.json): each reply to competing is paired by instance and seat with the neutral reply in the same seat (40 pairs), each cell with its E2 twin on luna.

Own payoff of the replying strategy against a competing partner, seats pooled, with the competing side's payoff in brackets (luna from E2 / sonnet / sol):

| reply | open information | unequal stakes (high side of 100, low side of 30) |
|---|---|---|
| neutral | 50.2 (73.2) / 69.2 (68.1) / 68.9 (69.2) | high 52.1 / 63.9 / 65.0; low 15.4 / 19.3 / 19.1 |
| collaborating | 57.1 (73.9) / 68.4 (69.1) / 68.0 (69.8) | high 59.4 / 68.1 / 66.5; low 17.3 / 19.4 / 19.8 |
| compromising | 43.7 (72.4) / 59.5 (70.0) / 58.4 (66.5) | high 48.9 / 62.0 / 55.8; low 15.1 / 17.1 / 16.2 |
| accommodating | 51.8 (79.2) / 58.5 (77.6) / 57.0 (79.2) | high 50.5 / 54.0 / 46.6; low 13.7 / 15.3 / 13.7 |
| avoiding | 37.4 (68.1) / 44.3 (64.5) / 35.8 (66.9) | high 32.2 / 38.1 / 35.8; low 9.6 / 11.4 / 10.5 |
| competing (the pair) | 49.6 / 52.1 / 53.6 | high 42.1 / 49.1 / 52.1; low 13.2 / 14.7 / 15.7 |

- **Under open information the stronger models' default agent is not exploited by a competing partner at all**: the neutral reply keeps 69.2 and 68.9 against 68.1 and 69.2 for the competing side, where luna's kept 50.2 against 73.2. With nothing to recover, collaborating is no longer the best reply (−0.8 against the neutral reply on sonnet, 6 wins to 17, p = .035; −0.9 on sol, 1 to 6 with 33 ties; +6.8, p = .035, on luna), as predicted; the two are within a point. Every other reply loses: compromising −9.8 and −10.6, accommodating −10.8 and −11.9 (the competing side gains 9.5 and 10.0; p < .001 throughout), avoiding −24.9 and −33.1, and matching competing −17.1 and −15.3 (on luna matching cost nothing, −0.6, because its default agent was already giving 23 points away). Competing against collaborating or accommodating agrees in 40 of 40 games on both models and beats the competing pair's joint payoff by 29 to 33 points; competing against avoiding agrees in 24 to 26 of 40, as rarely as the competing pair.
- **Under unequal stakes collaborating is the best reply to competing on every model, by a small margin**: pooled over seats +4.6 on luna (not significant), +2.2 on sonnet (18 wins to 8, p = .076), +1.0 on sol (18 to 6, p = .023), the joint payoff up by 2.0 to 4.3. The high-stakes side facing a hard bargainer does best by collaborating on all three (59.4, 68.1, 66.5 against 52.1, 63.9, 65.0 for neutral) and worst by avoiding and by matching. The low-stakes side facing competing does best by collaborating or neutral (19.4 and 19.8 of 30 against 19.3 and 19.1), and worst by avoiding (11.4, 10.5), accommodating (15.3, 13.7) and matching (14.7, 15.7): the prediction that it would do best by competing was wrong, since two hard bargainers agree in only 24 to 26 of 40 games. Accommodating facing competing is a transfer on every model (the competing side gains 11.7, 6.3 and 9.7; the accommodating high-stakes side gets 54.0 and 46.6 and the competing partner 81.6 and 85.7).
- **Compliance is near perfect** on sonnet and sol in these cells (39 to 40 of 40 for every rule, the competing rule included, against 30 to 40 for luna), so the replies measure the rules, not their execution. Every cell against competing has a higher joint payoff on the stronger models than its E2 twin on luna (by 3 to 20 points; significant in 6 of the 16 cells).
- **The map's vs-competing reading** (seats pooled, net payoff): under open information neutral is the best reply at λ = 0 on both models (collaborating at λ = 5 on sol; luna's was collaborating throughout); under unequal stakes collaborating at λ = 0 on both (neutral from λ = 5 on sonnet).

**Reading.** The follow-up's picture is completed: on models whose default agent cannot be pushed around, the best reply to a hard bargainer is the default agent itself or collaborating, which differ by a point; what a competing partner punishes is the same list everywhere (match it, concede to it, avoid it), and it punishes it harder on the stronger models because the default reply gives up so little. The one place collaborating still earns something against competing is unequal stakes, where it is the best reply on all three models for both sides, which is also the one condition in which the stronger models' default pair leaves value on the table (gap 17 against 2 under open information).

### Strategy payoff study: cross-model pairs, results (2026-11-21)

Arm 1 (seat 1 `gpt-6-luna`, seat 2 `claude-sonnet-5-5`) was played in full: 640 games, $8.17, no errors, 4 rejected replies, no lost move; its first pass stopped at the arm's $6 cap after 419 games because a game with sonnet's seat under the 1,500-token cap costs about $0.014 rather than the $0.008 estimated (sonnet writes 930 to 1,280 completion tokens a game), and the cap was raised to $10 with nothing else changed. Arm 2 (the seats swapped) was played for its five central cells, the mixed default pair and competing and collaborating against neutral in both seats (200 games, $3.38), which gives those two strategies 80 pairs per direction; the soft strategies and the symmetric pairs rest on arm 1's 40. Results: `results/bargain_e3_cross_{luna_sonnet,sonnet_luna}_*` and the paired statistics [results/bargain_e3_cross_paired.json](../results/bargain_e3_cross_paired.json); every comparison is paired by instance with the arm's own mixed default pair, and the same-model cells come from E3's luna control and sonnet arms on the same 40 instances. The proxy key's spend stands at $186 of $200 after these and the runs below.

Own gain of a strategy adopted alone against the other side's default agent, by the adopter's model (rows) and the partner's model (columns); the partner's change in brackets:

| adopter | partner luna | partner sonnet |
|---|---|---|
| luna, competing | +1.3 (−15.1, p = .020) | +2.8, p = .072 (−2.7) |
| sonnet, competing | +3.1, p = .091 (−12.2, p < .001) | −0.2 (+0.5) |
| luna, collaborating | +5.7 (+1.6) | −2.8, p = .009 (+4.8, p = .005) |
| sonnet, collaborating | +2.0 (−0.8) | −0.8 (+2.6) |

- **The partner's model decides, not the adopter's.** Collaborating by rule pays against a luna partner whichever model adopts it (+5.7 for luna, +2.0 for sonnet) and costs against a sonnet partner whichever model adopts it (−2.8 for luna, 19 wins to 40; −0.8 for sonnet), with the joint payoff up in every cell (+1.2 to +2.6) and the gain going to the sonnet partner (+4.8, p = .005). The adopter's-model account predicted the opposite diagonal (luna gaining everywhere as in E1, sonnet nowhere as in E3) and is ruled out; the partner-strength account's second prediction, that sonnet's rule would pay sonnet by 3 or more against luna, is met in direction only (+2.0, 33 to 26). The same holds for competing: whoever competes against luna's default agent takes 12 to 15 points from it and keeps little of it (+1.3, +3.1), and whoever competes against sonnet's takes under 3. What a rule earns its user is a property of the other side.
- **The mixed default pair is near the frontier, and the weaker model takes the larger share.** Joint 135.3 and 136.4 (on the frontier in 30 and 27 of 40, gap 6.3 and 5.2), 6 to 7 points above luna's own default pair (p ≤ .017) and 2 to 3 below sonnet's (not significant): one sonnet agent is enough to find the trades. Against the prediction that sonnet would take 5 points more, luna's agent takes more in both arms (71.1 against 64.2, 27 wins to 13, p = .038; 70.1 against 66.3): sonnet's default agent asks for the partner's priorities and gives them, and luna's states a price. Whether the sonnet agent moves first or second changes little (luna 71.7 and 70.4).
- **Symmetric mixed pairs** are the E3 picture: accommodating and collaborating pairs 137.0 and 136.5 (+1.7, +1.2 against the mixed default pair, not significant; the mixed collaborating pair matches luna's own, +1.1, and falls short of sonnet's, −4.1, p = .012), compromising pairs 127.8 (−7.5, p < .001), avoiding 119.8 (−15.4, p = .004) and competing 103.4 (−31.9, 24 agreements). The soft strategies adopted alone cost the weaker adopter far more against the stronger partner than the reverse: luna loses 12.7, 13.5 and 22.9 by compromising, accommodating and avoiding against sonnet (sonnet gains 10.0, 14.6 and 6.1), sonnet loses 3.0, 5.7 and 22.7 against luna.
- **Compliance and cost.** Sonnet follows the competing rule in 120 of 120 games and the collaborating trade clause in 93 of 120; luna 107 and 76. Per game sonnet's seat writes 930 completion tokens as the default agent, 1,200 to 1,280 under the competing and collaborating rules and 350 to 570 under avoiding and accommodating ($0.006 to $0.019 a game); luna's 80 to 230 ($0.0001 to $0.0004).

**Reading.** The cross-model pairs settle the question E3 left: the value of a conflict-handling rule to the agent that follows it is set by the partner. Against a default agent that cannot find the frontier alone (luna), a collaborating rule creates value and its user keeps some of it, and a competing rule extracts value; against one that can (sonnet), collaborating only moves the split toward the partner and competing extracts nothing. For the paper this is the mechanism behind the E1-to-E3 change, measured directly rather than inferred, and it turns the practical advice into a conditional one: configure collaborating when the other side is weaker or interests are hidden, and expect nothing from any rule, except losses from the soft ones, when the other side is a capable default agent.

## Next steps

1. The paper (ICML 2027: abstracts 14 January, papers 22 January 2027) has its environment, design and results sections written in `paper/` (2026-11-21); the introduction, related work and discussion are outlined as notes, and the body is about two pages over the limit. Paused in favour of the experiments.
2. The two external benchmarks are played and reported (2026-11-21); they go into the paper's external-validity section beside the grounded layer. Label a sample of E2, of the grounded games and of E4; have a teammate spot-check ten extractions against the tickets.
3. E3 and its follow-up are played and clean (2026-11-21; proxy spend $160 of $200 this month, resets 1 December). Open: cross-model pairs (a luna agent against a sonnet agent, with and without a strategy) to separate the partner's strength from the rule; the eight cells against competing on the two conditions if budget allows. Lesson for every further run on a verbose model: set the reply cap from a smoke run, not from E1's 700.
4. Reduce LLM-Deliberation to two parties (E5, with a teammate); optionally re-run 80 ParallACT instances on a second model.
5. Outline the paper: the TKI map as the main result, the coding studies as the binary boundary case, the negotiation pilot as the zero-sum pole.
6. Earlier CooperBench follow-ups (same-harness scoring of all pairs, the other models' runs, S2 trajectories) stay on the list but behind the study above.
