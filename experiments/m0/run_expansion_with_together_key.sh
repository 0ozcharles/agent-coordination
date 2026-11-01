#!/bin/bash
# One hidden entry runs the two-task expansion, then clears the key.
set -euo pipefail
CB_EXP_ROOT="$(cd "$(dirname "$0")" && pwd)"
CB_EXP_PYTHON="$CB_EXP_ROOT/CooperBench/.venv/bin/python"
CB_EXP_DIR="${1:-$CB_EXP_ROOT/artifacts/expansion_runs/expansion_wip_v1}"
"$CB_EXP_PYTHON" "$CB_EXP_ROOT/run_task_expansion.py" --verify-preflight
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_EXP_KEY
printf '\n'
if [ -z "$CB_EXP_KEY" ]; then exit 1; fi
export TOGETHER_API_KEY="$CB_EXP_KEY"
unset CB_EXP_KEY
trap 'unset TOGETHER_API_KEY' EXIT
while true; do
  if "$CB_EXP_PYTHON" -u "$CB_EXP_ROOT/run_task_expansion.py" --suite-dir "$CB_EXP_DIR"; then break; fi
  printf '\nBatch paused. Saved model runs will not repeat for evaluator failures.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r CB_EXP_REPLY
  if [ -n "$CB_EXP_REPLY" ]; then break; fi
done
