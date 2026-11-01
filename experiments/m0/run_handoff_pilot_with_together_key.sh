#!/bin/bash
# Check frozen offline proof before one hidden credential entry; preserve completed work on resume.
set -euo pipefail
CB_HANDOFF_ROOT="$(cd "$(dirname "$0")" && pwd)"
CB_HANDOFF_PYTHON="$CB_HANDOFF_ROOT/CooperBench/.venv/bin/python"
CB_HANDOFF_SUITE="${1:-$CB_HANDOFF_ROOT/artifacts/expansion_runs/handoff_dirty_v1}"
"$CB_HANDOFF_PYTHON" "$CB_HANDOFF_ROOT/run_handoff_pilot.py" --verify-preflight
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_HANDOFF_KEY
printf '\n'
if [ -z "$CB_HANDOFF_KEY" ]; then exit 1; fi
export TOGETHER_API_KEY="$CB_HANDOFF_KEY"
unset CB_HANDOFF_KEY
trap 'unset TOGETHER_API_KEY' EXIT
while true; do
  if "$CB_HANDOFF_PYTHON" -u "$CB_HANDOFF_ROOT/run_handoff_pilot.py" --suite-dir "$CB_HANDOFF_SUITE"; then break; fi
  printf '\nPilot paused. Completed model runs and validated scores will not repeat.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r CB_HANDOFF_REPLY
  if [ -n "$CB_HANDOFF_REPLY" ]; then break; fi
done
