#!/bin/bash
# Batch runner for the F2F 3D useful-skew CTS flow.
# Runs the full pipeline (synthesis -> 3D placement -> CTS -> route -> final)
# for the 10-design matrix. Each design writes to its own results/logs/reports.
#
# Usage:
#   ./run_benchmarks.sh                          # all 10 designs
#   PLATFORMS="asap7_3D" ./run_benchmarks.sh     # one platform
#   DESIGNS="aes ibex"  ./run_benchmarks.sh      # subset of designs
#   PARALLEL=1 ./run_benchmarks.sh               # background all designs at once
set -uo pipefail

FLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$FLOW_ROOT"

PLATFORMS="${PLATFORMS:-asap7_3D asap7_nangate45_3D}"
DESIGNS="${DESIGNS:-aes ibex jpeg swerv_wrapper ariane133}"
PARALLEL="${PARALLEL:-0}"

run_one() {
  local p="$1" d="$2"
  local rs="test/$p/$d/ord/run.sh"
  local log="$FLOW_ROOT/run_${p}_${d}.console"
  [[ -f "$rs" ]] || { echo "SKIP (no run.sh): $p/$d"; return; }
  echo ">>> START $p/$d  ($(date +%H:%M:%S))  -> $log"
  ( cd "$FLOW_ROOT" && bash "$rs" ) >"$log" 2>&1
  echo ">>> DONE  $p/$d  exit=$? ($(date +%H:%M:%S))"
}

for p in $PLATFORMS; do
  for d in $DESIGNS; do
    if [[ "$PARALLEL" == "1" ]]; then run_one "$p" "$d" & else run_one "$p" "$d"; fi
  done
done
[[ "$PARALLEL" == "1" ]] && wait
echo ">>> ALL DONE"
