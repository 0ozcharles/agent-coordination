# Development

## Everyday commands

These use only the Python 3.9+ standard library:

```bash
python3 scripts/project.py census                # failure causes in M0 runs
python3 scripts/project.py census --export       # re-export results/m0_runs.csv from local M0 data
python3 -m unittest discover -s tests            # tests do not run Docker or call model APIs
```

## Setup

You need Git, uv, Python 3.12, and Docker. CooperBench is pinned to this commit:

```bash
git clone https://github.com/cooperbench/CooperBench.git experiments/m0/CooperBench
git -C experiments/m0/CooperBench checkout 63b9d44d9f39a02fccf5bf0052db48a917a011fd
cd experiments/m0/CooperBench
uv sync --locked --python 3.12
```

Experiments run with `experiments/m0/CooperBench/.venv/bin/python`.

Put the API key in `.env` at the repository root (it is gitignored):

```text
OPENAI_API_KEY=...
```

Never put keys in configs, scripts, or commits.

## Running S1

1. Start Redis (coop agents use it to message each other):

   ```bash
   docker run -d --name cooperbench-redis -p 6379:6379 redis:7
   ```

2. Preview: shows the job count, how many are done, and the command. Nothing runs.

   ```bash
   python3 scripts/project.py s1 configs/s1_main.json
   ```

3. Run (calls a paid API). `--workers` sets parallelism; 4 is a good choice with 8 GB of Docker memory.

   ```bash
   python3 scripts/project.py s1 configs/s1_main.json --execute --workers 4
   ```

4. Re-tally results without running Docker:

   ```bash
   python3 scripts/project.py s1 configs/s1_main.json --report
   ```

Docker images (`akhatua/cooperbench-<repo>:task<id>`, 1–3 GB each) are pulled automatically on first use.

Results go to the config's `output` directory (`runs/` is gitignored): raw CooperBench logs, per-agent scores, and the aggregated `rows.csv` and `summary.json`. Finished jobs are skipped, so an interrupted run can simply be restarted. Jobs that failed because of API errors are retried.

After screening, build the main task list with the rule written in the screening config:

```bash
python3 scripts/project.py s1 configs/s1_screen.json --freeze configs/s1_main.json
```

If the computer sleeps during a long run, jobs hang. On macOS, wrap the command in `caffeinate -is` and keep the machine plugged in.

## Offline merge test

Re-merges the patch pairs of S1 coop runs whose `git merge` conflicted, then tests the result against both features. No agent is re-run. The `llm` method calls gpt-6-luna once per conflicted file, so a full set costs cents. `--set dev` (20 screening runs) is for checking the pipeline and the prompt. `--set test` (all 88 conflicted main-run runs) is run once.

```bash
python3 scripts/project.py merge configs/merge_test.json --set dev --execute --workers 4
python3 scripts/project.py merge configs/merge_test.json --set test --report
```

Each run directory under `runs/merge_test/<set>/` keeps the conflicted files with diff3 markers, the merged patch per method, the model's prompts and replies, and `merge.json` with test results.

## Running S2

S2 runs every arm in `configs/s2.json` on the S1 main tasks. Each arm is an S1-style config: setting, agent config, and git flag. Results go to `runs/s2/<arm>/`. After each coop run whose `git merge` conflicts, the runner re-merges the two patches with the merge test's frozen llm method and saves `merge.json` in the run directory. The report labels runs on the re-merged result and also prints CooperBench's official score.

```bash
python3 scripts/project.py s2 configs/s2.json                          # preview
python3 scripts/project.py s2 configs/s2.json --execute --workers 4    # run (paid API)
python3 scripts/project.py s2 configs/s2.json --report                 # re-tally, no Docker
```

The own-feature arm's prompt, `configs/agents/luna_mini_swe_own_feature.yaml`, is CooperBench's coop prompt with the "Your Situation" block and two submission notes changed. CooperBench only logs an agent config it cannot load and then falls back to its defaults, so the S2 runner checks every arm's config before starting.

## Offline check on the paper's runs

`configs/upstream_check.json` points at the coop and solo runs behind the CooperBench paper on Hugging Face, pinned to a dataset revision. Files are downloaded on first use and cached under `runs/upstream/published/`. Three sets:

- `calibration` re-tests a random sample of clean-merge pairs with our harness and compares with the recorded results.
- `remerge` re-merges every pair that conflicted and failed the paper's pipeline, with the merge test's llm method (cents).
- `own` tests each agent's patch alone on the same pairs.

```bash
python3 scripts/project.py upstream configs/upstream_check.json --set remerge                        # preview
python3 scripts/project.py upstream configs/upstream_check.json --set remerge --execute --workers 4  # Docker
python3 scripts/project.py upstream configs/upstream_check.json --report
```

The failure census (`configs/paper_census.json`) labels every failed GPT-5 and Claude pair. It reuses the upstream check's job directories and adds own-patch tests and a merged re-test for clean pairs. A full run takes a few hours with 4 workers. Pairs are split across workers in fixed shards, so the last worker can finish well after the others.

```bash
python3 scripts/project.py paper-census configs/paper_census.json                                       # preview
caffeinate -is python3 scripts/project.py paper-census configs/paper_census.json --execute --workers 4  # Docker
python3 scripts/test_watchdog.py runs/upstream/census/timeouts.log                                      # alongside, after the workers start
python3 scripts/project.py paper-census configs/paper_census.json --report
```

The re-scoring (`configs/paper_rescore.json`) downloads each pair's published merge report, re-scores every test log, and re-labels the census. It needs no Docker:

```bash
python3 scripts/project.py rescore configs/paper_rescore.json
```

The merged-patch check (`configs/paper_merge_check.json`) tests the paper's published naive-merge patches in our harness (Docker, about an hour with 4 workers):

```bash
caffeinate -is python3 scripts/project.py merge-check configs/paper_merge_check.json --execute --workers 4
python3 scripts/test_watchdog.py runs/upstream/merge_check/timeouts.log agent_coordination.merge_check   # alongside
python3 scripts/project.py merge-check configs/paper_merge_check.json --report
```

The merge audit (`configs/paper_merge_audit.json`) counts which agents' code each tested merged patch holds, and re-merges the failed "clean" pairs whose merge conflicts in our harness with the upstream check's llm method (Docker plus a few cents of API, about 2.5 hours with 4 workers). Its report also re-labels the census on our own merges:

```bash
caffeinate -is python3 scripts/project.py merge-audit configs/paper_merge_audit.json --execute --workers 4
python3 scripts/test_watchdog.py runs/upstream/merge_audit/timeouts.log agent_coordination.merge_audit   # alongside
python3 scripts/project.py merge-audit configs/paper_merge_audit.json --report
```

The sensitivity check (`configs/paper_sensitivity.json`) re-merges every failed pair whose real merge conflicts with git's union merge, and on 150 random pairs per model scores coop and solo in our harness (Docker, plus cents of API; about 8 hours with 4 workers):

```bash
caffeinate -is python3 scripts/project.py sensitivity configs/paper_sensitivity.json --execute --workers 4
python3 scripts/test_watchdog.py runs/upstream/sensitivity/timeouts.log agent_coordination.sensitivity   # alongside
python3 scripts/project.py sensitivity configs/paper_sensitivity.json --report
```

## Coordination conflict states

`configs/c_conflict_states.json` finds the first coordination conflict in every S1 and S2 coop run that has message logs: the first message after which both agents claim the same file, and the trajectory step at which each agent reaches it (the state a branch would resume from). A model labeler then reads only the messages and labels conflict types and each agent's handling style with [the codebook](C_CONFLICT_CODEBOOK.md). Building the state table needs no API; labeling costs well under a dollar for all sets.

```bash
python3 scripts/project.py conflict-states configs/c_conflict_states.json --set all --report              # state table and summary, no API
python3 scripts/project.py conflict-states configs/c_conflict_states.json --set library --execute --workers 8
python3 scripts/project.py conflict-states configs/c_conflict_states.json --set library --execute --repeat  # second labels for 40 runs
```

Sets: `dev` (S1 screening, where the codebook was revised), `library` (S1 main and the S2 baseline, default prompt), `conditions` (the two S2 interventions). A label is used only while the codebook text it was made with is unchanged, so editing the codebook makes every label stale until the set is labeled again. About 16% of runs have an agent whose trajectory was compacted into a summary; their messages are ordered by sender only and their conflict step is unknown.

## Branching from conflict states

`configs/c_branch_pilot.json` restarts library runs at their first conflict and continues them (`src/agent_coordination/branch.py`). The sender of the conflict message keeps its history up to the step that sent it. The receiver keeps its history up to just before it receives the message. Messages already sent but not yet received go back into the receiver's inbox. Each agent starts in a fresh container from the task image, so only runs whose agents wrote no file before the cut are eligible.

Before resuming, the runner checks that the prompt still renders as in the original run, apart from the host OS line CooperBench adds. It also holds both agents until both inboxes are filled. Needs Docker, Redis and the API key, like S1. Each branch costs well under a cent. Scoring is the same as in S2: CooperBench's merge, plus the llm re-merge when the merge conflicts.

```bash
python3 scripts/project.py branch configs/c_branch_pilot.json                                     # preview
caffeinate -is python3 scripts/project.py branch configs/c_branch_pilot.json --execute --workers 4
python3 scripts/project.py branch configs/c_branch_pilot.json --label --execute                   # label branches (cents)
python3 scripts/project.py branch configs/c_branch_pilot.json --report                            # no Docker or API
```

`configs/c_style_interventions.json` uses the same runner with several arms. Each arm's `instruction` is added as a user message just before each agent's first new model call, after the messages it receives at that step. `instruction_agent1` and `instruction_agent2` override it for one agent. Workers share one queue through `claims/` in the output directory; the CLI clears it before starting workers. With `score_agents: false` each agent's own patch is not tested during the run. Backfill those tests for chosen arms with Docker only:

```bash
caffeinate -is python3 scripts/project.py branch configs/c_style_interventions.json --execute --workers 6
python3 scripts/project.py branch configs/c_style_interventions.json --score-agents neutral,compromising --execute --workers 5
python3 scripts/project.py branch configs/c_style_interventions.json --label --execute
python3 scripts/project.py branch configs/c_style_interventions.json --report
```

## ParallACT tasks

[ParallACT](https://github.com/rm206/ParallACT_data_and_code) (arXiv 2611.00314) gives two agents separate requests on one repository. In its contradictory instances the two requests change the same code in incompatible ways, and a reconciled implementation that satisfies both exists. Clone it at the pinned commit (`external/` is gitignored; the repository states no license, so its data is not copied into this one):

```bash
git clone https://github.com/rm206/ParallACT_data_and_code external/ParallACT
git -C external/ParallACT checkout c212deb174fbc8980212cc1bde5b3f047941f332
```

`src/agent_coordination/parallact.py` turns each instance into a two-feature CooperBench task (feature 1 is request A, feature 2 is request B) and runs CooperBench's coop agents on it, with CooperBench's coop prompt. The agents work in the instance's SWE-Bench Pro image (`jefzda/sweap-images`, about 2 GB each, linux/amd64, so Docker emulates it on Apple silicon); the repository at `/app` is reset to the base commit and linked to `/workspace/repo`. Scoring follows ParallACT's grader, loaded from the checkout: each agent's patch against its own request's tests, then a git merge of the two patches in the image (conflicted files resolved with the merge test's llm method) against the combined tests.

```bash
python3 scripts/project.py parallact configs/parallact_pilot.json --gold --execute --workers 4   # reference patches, Docker only
python3 scripts/project.py parallact configs/parallact_pilot.json --execute --workers 3          # coop runs (paid API)
python3 scripts/project.py parallact configs/parallact_pilot.json --report
```

The reference check confirms, per instance, that the base commit fails the combined tests, each reference patch passes its own tests, the reconciled patch passes the combined tests, and a plain git merge of the two reference patches does not.

## Notes

- CooperBench's 600 s test timeout does not work with the Docker backend: `exec_run` runs inside a `with ThreadPoolExecutor` block, which waits for the command after the timeout. A test that loops forever hangs its worker. `scripts/test_watchdog.py` kills tests that run longer than 600 s and logs each kill; the feature then fails, as it would under the intended timeout.

- gpt-6-luna call parameters are in `configs/agents/luna_mini_swe.yaml` and match M0. `reasoning_effort` must be passed through `extra_body`; otherwise litellm drops it and the API rejects requests that include tools.
- CooperBench issue: when a coop agent finishes without opening a PR, it is told to keep going and submit, but the `exit` message left in its history makes the next API call fail. The S1 runner fixes that message before each request (`s1.api_messages`) without modifying CooperBench.
- CooperBench runs agent commands with `bash -lc`. The login shell resets PATH and drops Go and cargo in the go_chi and typst images, so agents there could not build or test. The S1 runner switches agents to `bash -c` (`s1.keep_image_path`); other images get the same environment either way. The fix applies from the S1 main run on; screening ran without it.
- Code in `experiments/m0/` is tied to completed batches; do not modify it. New code goes in `src/agent_coordination/`.

## Resuming M0 batches

M0 batches depend on local artifacts and Docker snapshots, so they can only be resumed on the original machine. By default the commands only print what they would do:

```bash
python3 scripts/project.py show early-roles
python3 scripts/project.py run early-roles
```

Add `--execute` to continue an interrupted batch. This runs only the unfinished parts and never adds new repeats. Add `--mode offline` to restore state and score without calling a model API.
