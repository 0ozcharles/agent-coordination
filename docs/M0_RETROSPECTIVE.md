# M0 Retrospective

M0 was the exploration phase from 2026-10-29 to 11-01: 13 batches and 115 scored runs. The data is in [results/m0_runs.csv](../results/m0_runs.csv) and can be re-tallied with `python3 scripts/project.py census`.

## Conclusion

The main cause of failure was integration: the two patches conflicted, or both agents submitted the same patch. When a single agent owned the final code (Consolidate, Integrator), these failures disappeared, and what remained was mostly capability.

This rests on only 10 model × task combinations, of which only 6 distinguish between conditions. It is not enough to draw a conclusion.

## Failure causes

| Outcome | Meaning | Consolidate | Integrator | Keep | Solo | StrongShare |
|---|---|---|---|---|---|---|
| Passed | Both features pass | 20 | 16 | 0 | 16 | 31 |
| Merge conflict | The two patches cannot be merged | 0 | 0 | 2 | 0 | 6 |
| Duplicate patch | Both agents submitted the same patch, and it fails | 0 | 0 | 0 | 0 | 2 |
| Capability gap | The failed feature never passed in any run with the same model and task | 2 | 2 | 2 | 4 | 3 |
| Recoverable | The failed feature passed in some other run | 4 | 0 | 0 | 4 | 1 |

M0 did not test each agent's patch separately, so it cannot identify context failures. S1 adds this.

## What went wrong

1. Each condition ran only twice; 2/2 and 1/2 are statistically indistinguishable.
2. Many tasks always passed or always failed, so they could not separate conditions.
3. The scoring rule changed four times, so the same condition name measured different things in different batches.
4. The model changed mid-study, from Qwen3.5-9B to gpt-6-luna.
5. Conditions were added along the way (Integrator, submission reminders, automatic strategy selection).
6. Interventions were tested before the causes of failure were understood.

Cost was not the bottleneck: a gpt-6-luna run costs about $0.004–0.03. What slowed progress was that every experiment depended on saved checkpoints and Docker snapshots.

## Batches

| Batch | Model | Question | Result |
|---|---|---|---|
| baseline / consolidate smoke | Qwen | Does writing separately and merging work? | Keep hit a merge conflict; Consolidate passed (n=1) |
| expansion_wip_v1 | Qwen | Three conditions on new tasks | Keep 0/4; click2800 failed under every condition |
| solo_controls_v1 | Qwen | Solo reference | click2800 0/2, dirty_equals 2/2 |
| handoff_dirty_v1 | Qwen | StrongShare vs. Consolidate after a code handoff | 4/4 passed; no difference |
| cross_handoff_v1 | Qwen | StrongShare / Consolidate / Solo on 3 tasks | All StrongShare failures were merge conflicts or duplicate patches |
| luna_solo_v1 | luna | Solo after switching models | 4/4 |
| luna_collaboration_v1 | luna | StrongShare vs. Consolidate | 7/8; the only failure was a merge conflict |
| luna_timing_v1 | luna | Early vs. late intervention | One failing cell, again a merge conflict |
| luna_integrator_v1 | luna | One agent owns the merge | Integrator 8/8, StrongShare 6/8 |
| luna_selector_v1 | luna | Choose a strategy by where changes land | dspy failed everywhere; nothing to compare |
| luna_conflict_v1 | luna | Intervene when a conflict appears | No conflicts occurred; no data |
| luna_publication_v1 | luna | Remind agents to submit | 6/6 |
| luna_early_roles_v1 | luna | StrongShare vs. Integrator under the same submission rule | 8/8 passed |

## What we keep

- Checkpoint branching, cost accounting, and the official scoring integration, which S3 will reuse.
- The code in `experiments/m0/` stays unchanged so past batches remain reproducible.
