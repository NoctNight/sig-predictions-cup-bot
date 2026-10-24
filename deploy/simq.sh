#!/bin/bash
# Run a heavy job (a sim, a replay, a study script) only when one of SIMQ_SLOTS slots is free, at nice 5.
#   deploy/simq.sh python3 tests/sim_pipeline.py --churn day
# Every session shares the same slots (/tmp/sigbot-simq), so the Mac never runs more than SIMQ_SLOTS heavy jobs at once.
# A slot whose owner process has died is reclaimed. botctl's precheck does not queue (deploys go first).
SLOTS=${SIMQ_SLOTS:-3}
DIR=/tmp/sigbot-simq
mkdir -p "$DIR"
while :; do
  for i in $(seq 1 "$SLOTS"); do
    s="$DIR/slot$i"
    if mkdir "$s" 2>/dev/null; then
      echo $$ > "$s/pid"
      trap 'rm -rf "$s"' EXIT INT TERM
      nice -n 5 "$@"
      exit $?
    fi
    pid=$(cat "$s/pid" 2>/dev/null)
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then rm -rf "$s"; fi
  done
  sleep 15
done
