#!/bin/bash
# Run four fresh-solo controls with a key held only in this terminal's environment.
set -euo pipefail
RSI_SOLO_ROOT="$(cd "$(dirname "$0")" && pwd)"
RSI_SOLO_PYTHON="$RSI_SOLO_ROOT/CooperBench/.venv/bin/python"
RSI_SOLO_DIR="${1:-$RSI_SOLO_ROOT/artifacts/solo_expansion_runs/solo_controls_v1}"
"$RSI_SOLO_PYTHON" "$RSI_SOLO_ROOT/run_solo_expansion.py" --verify-preflight
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s RSI_SOLO_KEY
printf '\n'
if [ -z "$RSI_SOLO_KEY" ]; then exit 1; fi
export TOGETHER_API_KEY="$RSI_SOLO_KEY"
unset RSI_SOLO_KEY
trap 'unset TOGETHER_API_KEY' EXIT
while true; do
  if "$RSI_SOLO_PYTHON" -u "$RSI_SOLO_ROOT/run_solo_expansion.py" --suite-dir "$RSI_SOLO_DIR"; then break; fi
  printf '\nBatch paused. Completed model executions will not repeat for scoring failures.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r RSI_SOLO_REPLY
  if [ -n "$RSI_SOLO_REPLY" ]; then break; fi
done
