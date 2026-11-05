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
