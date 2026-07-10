#!/usr/bin/env bash
# Keep re-invoking enrich_workday (bounded to 300 candidates/run) until the
# eligible backlog is drained, instead of the operator re-running it by hand
# every ~15-20 min. Each iteration is still the same self-contained, already-
# tested worker -- this just automates hitting "run again".
set -euo pipefail
cd "$(dirname "$0")/.."

LOG="${1:-logs/enrich_workday_loop.log}"
SLEEP_SECS="${SLEEP_SECS:-30}"
mkdir -p "$(dirname "$LOG")"

# Keep the system awake (idle + display sleep) for as long as this script is
# alive. `-w $$` ties caffeinate's lifetime to our own PID, so it exits on its
# own when the loop below finishes -- no separate process to remember to kill.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -d -w $$ &
fi

echo "$(date '+%F %T') starting continuous enrich_workday loop (log: $LOG)" | tee -a "$LOG"

while true; do
  echo "=== $(date '+%F %T') ===" | tee -a "$LOG"
  output=$(uv run python -m jobmaxxing.enrich_workday 2>&1 | tee -a "$LOG")
  candidates=$(echo "$output" | grep -oE "'candidates': [0-9]+" | grep -oE '[0-9]+' || echo "1")
  if [ "$candidates" = "0" ]; then
    echo "$(date '+%F %T') backlog drained -- stopping" | tee -a "$LOG"
    break
  fi
  sleep "$SLEEP_SECS"
done
