# Agent Coordination

Why do teams of LLM agents fail at coding tasks, and which fix is most cost-effective for each cause of failure?

## Overview

Two agents each implement one feature, and their code is merged at the end. A run can fail for four reasons:

- **Capability**: the task is beyond what the model can do.
- **Context**: after the task is split, an agent lacks information it needs for its own part.
- **Coordination**: agents duplicate work or don't know what the other is changing.
- **Integration**: each agent's code is correct on its own, but the two don't merge.

Each cause calls for a different fix. We first measure how often each cause occurs, then compare targeted interventions for the main causes, and finally ask whether the cause can be predicted while a run is still in progress. Tasks and tests come from [CooperBench](https://github.com/cooperbench/CooperBench).

| Stage | Question | Status |
|---|---|---|
| M0 Exploration | Build the experiment pipeline | Done, see [M0 retrospective](docs/M0_RETROSPECTIVE.md) |
| S1 Failure census | Which causes produce failures, and how often? | In progress: screening done, main run pending |
| S2 Targeted interventions | Does a fix matched to the cause beat other fixes? | After S1 |
| S3 Online diagnosis | Can we detect the cause mid-run and pick a fix? | After S2 |

The full design is in the [research plan](docs/RESEARCH_PLAN.md).

## Findings so far

S1 screening: gpt-6-luna, 58 tasks, 2 coop and 2 solo runs each, 232 runs in total, $1.93.

- Coop passed 32 / 116. Of the 84 failures, 58 were integration failures, and 55 of those were git merge conflicts: both agents wrote correct code but edited the same lines.
- Capability caused 15 failures and context caused 10. Reading the traces showed that 12 of these 25 were handoffs: to avoid merge conflicts, an agent left part of its feature to the partner, and the combined work failed.
- Solo officially passed only 25 / 116, but another 44 runs wrote correct code and never opened a PR. CooperBench only grades submitted code.

Each task ran only twice, so these numbers are preliminary until the main run. Per-run data is in [results/s1_screen_rows.csv](results/s1_screen_rows.csv). M0 pointed in the same direction, but its sample was too small to draw conclusions.

## Quick start

These commands need only Python 3.9+, with no Docker or API key:

```bash
python3 scripts/project.py census                               # failure causes in M0 runs
python3 scripts/project.py s1 configs/s1_screen.json --report   # S1 screening results (needs local run logs)
python3 -m unittest discover -s tests
```

Running S1 experiments requires Docker, Redis, and an OpenAI API key. See [Development](docs/DEVELOPMENT.md).

## Repository layout

```text
src/agent_coordination/   current code: CLI, M0 census, S1 runner
configs/                  experiment configs
results/                  shareable result data
experiments/m0/           M0 code (frozen)
docs/                     research plan, M0 retrospective, development guide
tests/
```

## Documentation

- [Research plan](docs/RESEARCH_PLAN.md): question, how failure causes are labeled, stage designs, progress
- [M0 retrospective](docs/M0_RETROSPECTIVE.md): what the exploration phase did and learned
- [Development](docs/DEVELOPMENT.md): setup, running S1, resuming M0 batches
- [M0 code map](docs/CODEBASE.md): structure of `experiments/m0/`
