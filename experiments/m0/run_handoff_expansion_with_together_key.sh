#!/bin/bash
set -euo pipefail
CB_CROSS_ROOT="$(cd "$(dirname "$0")" && pwd)"
CB_CROSS_PYTHON="$CB_CROSS_ROOT/CooperBench/.venv/bin/python"
CB_CROSS_SUITE="${1:-$CB_CROSS_ROOT/artifacts/handoff_expansion_runs/cross_handoff_v1}"
"$CB_CROSS_PYTHON" "$CB_CROSS_ROOT/run_handoff_expansion.py" --verify-preflight
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_CROSS_KEY
printf '\n'
if [ -z "$CB_CROSS_KEY" ]; then exit 1; fi
export TOGETHER_API_KEY="$CB_CROSS_KEY"
unset CB_CROSS_KEY
trap 'unset TOGETHER_API_KEY' EXIT
while true; do
  if "$CB_CROSS_PYTHON" -u "$CB_CROSS_ROOT/run_handoff_expansion.py" --suite-dir "$CB_CROSS_SUITE"; then break; fi
  printf '\nBatch paused. Completed models and validated scores will not repeat.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r CB_CROSS_REPLY
  if [ -n "$CB_CROSS_REPLY" ]; then break; fi
done
