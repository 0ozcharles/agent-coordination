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

Offline check on the paper's own runs: we took the published GPT-5 and Claude Sonnet 4.5 coop runs and re-merged every pair that failed the paper's pipeline after a conflict. No agent was re-run.

- Coop success rises from 28.2% to 32.9% for GPT-5 and from 22.6% to 26.0% for Claude. That closes 20% and 3% of the gap to solo (about 46% for both), short of the pre-registered third.
- The re-merge recovers 12 of 16 and 18 of 20 pairs where both agents' own patches pass, about as well as in our merge test. But such pairs are only about 4% of each model's failures; 70–79% of the failed pairs merged cleanly and still failed.
- So the large merge effect in S1 and S2 is specific to our setup. In the paper's runs the coordination gap is real and mostly comes from code that merges cleanly but does not work. See the [research plan](docs/RESEARCH_PLAN.md#offline-check-on-the-cooperbench-papers-runs-2026-11-03).

Failure census on the paper's runs: we labeled all 880 failed GPT-5 and Claude pairs, using each agent's patch tested alone and the published solo runs. This needed Docker only.

- Take the pairs where solo passes and coop fails. Integration (each own patch passes, the merged code fails) is 47% [28%, 66%] of them for GPT-5 and 67% [56%, 75%] for Claude. The pre-registered rule therefore gives a split verdict.
- The integration labels do not hold up. When we merge the same two patches ourselves and test them, most of these failures disappear: 8 of 19 reproduce for GPT-5 and 3 of 13 for Claude, against 34 of 37 and 16 of 25 for context labels. Counting only reproduced failures, integration falls to 33% and 41%.
- Our harness and the paper's disagree most exactly where the integration label is decided. So the way merged code is evaluated is a large source of uncertainty in failure attribution. See the [research plan](docs/RESEARCH_PLAN.md#failure-census-on-the-papers-runs-2026-11-04).

Re-scoring the paper's test logs: CooperBench counts a test run as failed if its output contains "failed" anywhere, warnings included.

- The paper used this check: re-implemented, it reproduces all 2,211 recorded results.
- It rarely matters. It flips 6 results and explains only 2 of the 67 unreproduced failures.
- In the paper's own logs, most of those failures come from tests that ran on code missing the agent's changes. See the [research plan](docs/RESEARCH_PLAN.md#re-scoring-the-papers-test-logs-2026-11-04).

The paper's merged patches tested in our harness:

- 62 of the 64 unreproduced failures also fail when we test the merged patch the paper published (97% [90%, 100%]). In 55 of 56 comparable cases the first error line matches the paper's.
- Control pairs the paper recorded as passing pass in our harness too.
- So these failures come from the paper's merge step, not from the agents. In 27 of 48 pairs, its merged patch is simply one agent's patch, with the other agent's work missing. Our `git merge` of the same two patches passes.
- Correcting these pairs, together with the earlier re-merge of conflicted pairs, closes at least 43% of GPT-5's coop–solo gap and 19% of Claude's. In the census, integration falls to a third (GPT-5) and two fifths (Claude) of the pairs solo passes. See the [research plan](docs/RESEARCH_PLAN.md#the-papers-merged-patches-tested-in-our-harness-2026-11-04).

Per-run data: [S1 main](results/s1_main_rows.csv), [S1 screening](results/s1_screen_rows.csv), [merge test](results/merge_test_rows.csv), [S2](results/s2_rows.csv), [paper runs](results/upstream_rows.csv), [paper census](results/paper_census_rows.csv), [re-scored logs](results/paper_rescore_rows.csv), [merged-patch check](results/paper_merge_check_rows.csv).

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
