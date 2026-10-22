#!/bin/bash
# Pre-deploy gate: every change to the live bot must pass this before it is restarted (deploy/mac/botctl.sh deploy).
#   1. every module compiles   2. guard regression tests   3. self-tuner tests   4. fake-venue simulation
#   5. one dry cycle with no traceback
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
fail() { echo "PRECHECK FAILED: $1"; exit 1; }
python3 -m py_compile sigbot/*.py sigbot/proposals/*.py || fail "compile"
python3 tests/test_guards.py || fail "guard tests"
python3 tests/test_autotune.py "$ROOT" > /tmp/sigbot_autotune.txt 2>&1 || { tail -5 /tmp/sigbot_autotune.txt; fail "tuner tests"; }
# 4. the whole pipeline on the fake venue (tests/sim_pipeline.py): every sleeve against invariants, all scenarios but the
#    24-h replay (4 min; run it by hand with: python3 tests/sim_pipeline.py day)
python3 tests/sim_pipeline.py $(python3 tests/sim_pipeline.py --list | awk '$1!="day"{print $1}') > /tmp/sigbot_sim.txt 2>&1 \
  || { grep -E "^FAIL|^\[" -A4 /tmp/sigbot_sim.txt | head -30; fail "fake-venue simulation"; }
out=$(python3 -I -c "
import faulthandler, sys, runpy, os
sys.path.insert(0, os.getcwd()); faulthandler.dump_traceback_later(400, exit=True)
sys.argv=['run','--once']; runpy.run_module('sigbot.run', run_name='__main__')" 2>&1)
echo "$out" | grep -q "cycle [0-9]* done" || { echo "$out" | tail -15; fail "dry run did not complete a cycle"; }
echo "$out" | grep -qE "Traceback|Timeout \(" && { echo "$out" | grep -A8 -E "Traceback|Timeout" | head -20; fail "dry run raised"; }
echo "PRECHECK PASSED"
