# M0 Code Map

This page describes `experiments/m0/`, for reference or for resuming past batches. Current code lives in `src/agent_coordination/`: `census.py` tallies M0 results and `s1.py` is the S1 experiment runner.

## M0 pipeline

```text
Task (two features)
        ↓
agent1 + agent2: separate workspaces, messaging, shared Git
        ↓
Checkpoint: code, conversation history, tool and messaging state
        ↓
Switch organization, hand off information
        ↓
Continue → submitted code → official tests → result and cost
```

M0 code grew batch by batch, so some functionality exists in several versions. The files are left as they are so past batches stay reproducible.

## Call graph of the last batch

Paths are relative to `experiments/m0/`.

| Layer | File | Role |
| --- | --- | --- |
| Orchestration | `run_luna_early_roles.py` | Tasks, order, starting point, budget, execution, scoring |
| Roles | `early_role_runtime.py` | StrongShare / Integrator instructions and submission rules |
| Agent loop | `luna_integrator_worker.py` | Agent loop and tool execution |
| Containers and messaging | `baseline_runtime.py` | Workspaces, shared Git, messages, snapshot and restore |
| Handoff | `cross_task_packet.py` | Builds the code diff and handoff information from a checkpoint |
| Model | `luna_model.py` | API calls and error handling |
| Cost | `luna_accounting.py` | Usage and cost records |
| Submission | `publication_guard.py` | Reminds agents to submit |
| Scoring | `early_role_evaluator.py` | Runs the official tests on agent1's submitted code |

## Result files

- `summary.json`: batch summary
- `result.json`: result for one condition
- `evaluation_summary.json`: official test results
- `usage_ledger.json`: request ledger, including failed requests and requests with unknown usage
- `checkpoint.json` / `shared_git.tar`: state for restoring; also needs the matching Docker snapshot
- `source_snapshot/` / `protocol.json`: code and config used by the batch

Count each request once when computing cost; ledger copies under `failed_attempts/` must not be added again.

## Limitations

Past batches depend on local `artifacts/` and Docker snapshots, and some scripts hard-code the macOS Docker socket, so they cannot be reproduced directly on another machine.
