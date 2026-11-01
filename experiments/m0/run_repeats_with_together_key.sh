#!/bin/bash
# One hidden key entry runs/resumes this fixed six-job batch, then clears the key.
set -euo pipefail
CB_REPEAT_ROOT="$(cd "$(dirname "$0")" && pwd)"
CB_REPEAT_PYTHON="$CB_REPEAT_ROOT/CooperBench/.venv/bin/python"
CB_REPEAT_DIR="${1:-$CB_REPEAT_ROOT/artifacts/repeat_runs/repeat_smoke_jinja1621_v1}"
"$CB_REPEAT_PYTHON" "$CB_REPEAT_ROOT/run_repeats.py" --verify-preflight
if "$CB_REPEAT_PYTHON" - "$CB_REPEAT_DIR" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])/'status.json'
raise SystemExit(0 if p.exists() and json.loads(p.read_text()).get('status') == 'completed' else 1)
PY
then
  "$CB_REPEAT_PYTHON" -u "$CB_REPEAT_ROOT/run_repeats.py" --suite-dir "$CB_REPEAT_DIR"
  exit $?
fi
printf 'Paste Together API key (hidden), then press Enter: '
IFS= read -r -s CB_REPEAT_KEY
printf '\n'
if [ -z "$CB_REPEAT_KEY" ]; then
  printf 'No key entered; no API request made.\n'
  exit 1
fi
export TOGETHER_API_KEY="$CB_REPEAT_KEY"
unset CB_REPEAT_KEY
trap 'unset TOGETHER_API_KEY' EXIT
while true; do
  if "$CB_REPEAT_PYTHON" -u "$CB_REPEAT_ROOT/run_repeats.py" --suite-dir "$CB_REPEAT_DIR"; then
    break
  fi
  printf '\nBatch paused. Completed model runs and evaluations are saved.\n'
  printf 'After reviewing the error, press Enter to resume; type q and Enter to quit: '
  IFS= read -r CB_REPEAT_REPLY
  if [ -n "$CB_REPEAT_REPLY" ]; then break; fi
done
