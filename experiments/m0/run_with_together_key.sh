#!/bin/bash
# Paste only the Together key. Input is hidden and is never written to disk.
set -euo pipefail
CB_RUN_ROOT="$(cd "$(dirname "$0")" && pwd)"
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_RUN_KEY
printf '\n'
if [ -z "$CB_RUN_KEY" ]; then
  printf 'No key entered; no API request made.\n'
  exit 1
fi
export TOGETHER_API_KEY="$CB_RUN_KEY"
unset CB_RUN_KEY
while true; do
  if "$CB_RUN_ROOT/CooperBench/.venv/bin/python" -u "$CB_RUN_ROOT/run_solo_smoke.py"; then
    break
  fi
  printf '\nRun stopped. The key remains only in this terminal process memory.\n'
  printf 'After reviewing the error, press Enter to retry; type q and Enter to quit: '
  IFS= read -r CB_RUN_REPLY
  if [ -n "$CB_RUN_REPLY" ]; then break; fi
done
unset TOGETHER_API_KEY
