#!/bin/bash
# One hidden key entry runs the common prefix and both baselines with evaluation.
set -euo pipefail
CB_BASELINE_ROOT="$(cd "$(dirname "$0")" && pwd)"
CB_BASELINE_PYTHON="$CB_BASELINE_ROOT/CooperBench/.venv/bin/python"
"$CB_BASELINE_PYTHON" -c 'import json,sys; assert json.load(open(sys.argv[1]))["passed"]' \
  "$CB_BASELINE_ROOT/artifacts/baseline_offline_validation.json"
CB_BASELINE_DIR="${1:-$CB_BASELINE_ROOT/artifacts/baseline_runs/baseline_smoke_$(date -u +%Y%m%dT%H%M%SZ)}"
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_BASELINE_KEY
printf '\n'
if [ -z "$CB_BASELINE_KEY" ]; then
  printf 'No key entered; no API request made.\n'
  exit 1
fi
export TOGETHER_API_KEY="$CB_BASELINE_KEY"
unset CB_BASELINE_KEY
while true; do
  if "$CB_BASELINE_PYTHON" -u "$CB_BASELINE_ROOT/run_baselines.py" --run-dir "$CB_BASELINE_DIR"; then
    break
  fi
  printf '\nRun paused. The checkpoint and completed phases are saved.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r CB_BASELINE_REPLY
  if [ -n "$CB_BASELINE_REPLY" ]; then break; fi
done
unset TOGETHER_API_KEY
