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

**What the logs show instead (post hoc, not yet verified).** We read the paper's failing output for the 67 unreproduced features:

- Most fail because the feature's code is not in the program under test: an unexpected keyword argument, a missing attribute, a failed import. In 3, the merged patch was empty.
- The paper also published its naive-merge patch. In 45 of the 67, that patch lacks at least half of the lines the agent's own patch adds. Among non-empty reproduced failures, this happens in 30 of 113.
- Example: GPT-5, tiktoken, features 1 and 4. The published merged patch is byte-identical to agent 4's patch, and the report calls the merge clean. Feature 1's test fails because `max_tokens` does not exist.

So the unreproduced failures look like code lost in the paper's merge or patch step, not agents' code that does not fit together. Two things keep this from being a conclusion:

- Published merged patches that lack one side's lines also occur in 68 of the 239 clean pairs the paper recorded as passing.
- In the tiktoken example, feature 4 also failed for a missing argument that agent 4's patch adds.

So the published patch may not be exactly the code the paper tested. Testing the published merged patches in our harness would settle it.

## Next steps

1. Test the paper's published naive-merge patches in our harness for the unreproduced failures and a control sample. If they fail where the paper failed, the non-reproduction comes from the paper's merged code. Then check how much of the paper's coop–solo gap rests on such pairs.
2. Run the same census on the paper's no-communication runs, which conflict far more often, to see how messaging changes the mix of causes.
3. Ablations the advisor asked for: number of agents, team composition (different, stronger and weaker, commercial and open models together) and the communication channel.
4. Read the shared-git trajectories to find out why capability failures rose in S2.
5. Choose what replaces S3. Candidates: a repair round after the merge, where an agent sees the merged code and the failing tests; and three-agent tasks with CooperBench's team mode.
