#!/bin/bash
# Reuse the original checkpoint, run one Consolidate branch, then score both features.
set -euo pipefail
CB_CONSOLIDATE_ROOT="$(cd "$(dirname "$0")" && pwd)"
CB_CONSOLIDATE_PYTHON="$CB_CONSOLIDATE_ROOT/CooperBench/.venv/bin/python"
"$CB_CONSOLIDATE_PYTHON" "$CB_CONSOLIDATE_ROOT/run_consolidate.py" --verify-preflight
CB_CONSOLIDATE_DIR="${1:-$CB_CONSOLIDATE_ROOT/artifacts/consolidate_runs/consolidate_smoke_$(date -u +%Y%m%dT%H%M%SZ)}"
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_CONSOLIDATE_KEY
printf '\n'
if [ -z "$CB_CONSOLIDATE_KEY" ]; then
  printf 'No key entered; no API request made.\n'
  exit 1
fi
export TOGETHER_API_KEY="$CB_CONSOLIDATE_KEY"
unset CB_CONSOLIDATE_KEY
trap 'unset TOGETHER_API_KEY' EXIT
while true; do
  if "$CB_CONSOLIDATE_PYTHON" -u "$CB_CONSOLIDATE_ROOT/run_consolidate.py" --run-dir "$CB_CONSOLIDATE_DIR"; then
    break
  fi
  printf '\nRun paused. The original checkpoint and completed execution are saved.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r CB_CONSOLIDATE_REPLY
  if [ -n "$CB_CONSOLIDATE_REPLY" ]; then break; fi
done
