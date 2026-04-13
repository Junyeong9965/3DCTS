#!/bin/bash
# ================================================================
# 3D CTS Benchmark Runner
#   - Launches all run.sh (CTS -> Route -> Final) in parallel
#   - Generates debug HTML reports as each CTS completes
#
# Usage:
#   # Run all defaults
#   nohup ./run_benchmarks.sh > run_benchmarks.log 2>&1 &
#
#   # Select specific platforms/designs
#   PLATFORMS="asap7_3D" DESIGNS="aes ibex" ./run_benchmarks.sh
#
#   # Report only (skip CTS, regenerate HTML from existing CSVs)
#   REPORT_ONLY=1 ./run_benchmarks.sh
# ================================================================

trap '' HUP

FLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${FLOW_ROOT}/run_logs"
mkdir -p "${LOG_DIR}"

# ---- Configurable: platforms and designs ----
DESIGNS=(${DESIGNS:-aes ibex jpeg ariane133 swerv_wrapper})
PLATFORMS=(${PLATFORMS:-asap7_3D asap7_nangate45_3D})
REPORT_ONLY="${REPORT_ONLY:-0}"

# Clean 4*/5*/6* stage logs before run (prevent append across runs)
if [[ "${REPORT_ONLY}" != "1" ]]; then
  for _p in "${PLATFORMS[@]}"; do
    for _d in "${DESIGNS[@]}"; do
      _ldir="${FLOW_ROOT}/logs/${_p}/${_d}/openroad"
      [[ -d "${_ldir}" ]] && find "${_ldir}" -maxdepth 1 \
        \( -name "4*.log" -o -name "5*.log" -o -name "6*.log" -o -name "4*.webp" \) -delete
    done
  done
fi

TOTAL_JOBS=$(( ${#DESIGNS[@]} * ${#PLATFORMS[@]} ))

echo "================================================================"
echo "3D CTS Benchmark Runner"
echo "  Designs:     ${DESIGNS[*]}"
echo "  Platforms:   ${PLATFORMS[*]}"
echo "  Total:       ${TOTAL_JOBS} jobs"
echo "  Report only: ${REPORT_ONLY}"
echo "  Log dir:     ${LOG_DIR}/"
echo "  Started:     $(date)"
echo "================================================================"
echo ""

# --- Check placement results ---
if [[ "${REPORT_ONLY}" != "1" ]]; then
  MISSING=0
  for PLATFORM in "${PLATFORMS[@]}"; do
    for DESIGN in "${DESIGNS[@]}"; do
      PLACE_V="${FLOW_ROOT}/results/${PLATFORM}/${DESIGN}/openroad/3_place.v"
      if [[ ! -f "${PLACE_V}" ]]; then
        echo "[WARNING] Missing placement: ${PLATFORM}/${DESIGN} (${PLACE_V})"
        ((MISSING++))
      fi
    done
  done

  if [[ ${MISSING} -gt 0 ]]; then
    echo ""
    echo "[WARNING] ${MISSING} design(s) missing placement results."
    echo "  These jobs will fail at CTS. Run placement first."
    echo ""
  fi
fi

# ================================================================
# Function: generate debug HTML report for a completed CTS run
# ================================================================
generate_debug_report() {
  local platform="$1"
  local design="$2"
  local results_dir="${FLOW_ROOT}/results/${platform}/${design}/openroad"
  local report_dir="${FLOW_ROOT}/reports/${platform}/${design}"
  mkdir -p "${report_dir}"

  local per_ff="${results_dir}/cts_debug_per_ff.csv"
  local per_edge="${results_dir}/cts_debug_per_edge.csv"
  local paths="${results_dir}/cts_debug_clock_paths.csv"
  local buffers="${results_dir}/cts_debug_buffers.csv"
  local lp_targets="${results_dir}/pre_cts_skew_targets.csv"
  local lp_stats="${results_dir}/pre_cts_lp_stats.json"

  # Check required CSVs exist
  local missing_csv=0
  for f in "${per_ff}" "${per_edge}" "${paths}" "${buffers}" "${lp_targets}"; do
    if [[ ! -f "$f" ]]; then
      echo "  [REPORT SKIP] Missing: $f"
      ((missing_csv++))
    fi
  done

  if [[ ${missing_csv} -gt 0 ]]; then
    echo "  [REPORT SKIP] ${platform}/${design}: ${missing_csv} required CSV(s) missing"
    return 1
  fi

  local output_html="${report_dir}/cts_debug_report.html"
  local report_args=""
  report_args+=" --per-ff ${per_ff}"
  report_args+=" --per-edge ${per_edge}"
  report_args+=" --paths ${paths}"
  report_args+=" --buffers ${buffers}"
  report_args+=" --lp-targets ${lp_targets}"
  if [[ -f "${lp_stats}" ]]; then
    report_args+=" --lp-stats ${lp_stats}"
  fi
  report_args+=" --output ${output_html}"

  echo "  [REPORT] Generating ${platform}/${design} -> ${output_html}"
  if python3 "${FLOW_ROOT}/scripts_openroad/cts_debug_report.py" ${report_args} 2>&1; then
    echo "  [REPORT] OK: ${output_html}"
  else
    echo "  [REPORT] FAIL: ${platform}/${design}"
    return 1
  fi
  return 0
}

# ================================================================
# Phase 1: CTS runs (parallel) + CTS-done poller
# ================================================================
if [[ "${REPORT_ONLY}" != "1" ]]; then
  PIDS=()
  JOBS=()

  # Delete pre-existing cts_debug_per_ff.csv so the poller detects fresh ones
  for PLATFORM in "${PLATFORMS[@]}"; do
    for DESIGN in "${DESIGNS[@]}"; do
      rm -f "${FLOW_ROOT}/results/${PLATFORM}/${DESIGN}/openroad/cts_debug_per_ff.csv"
    done
  done

  # Launch all jobs
  for PLATFORM in "${PLATFORMS[@]}"; do
    for DESIGN in "${DESIGNS[@]}"; do
      RUN_SH="${FLOW_ROOT}/test/${PLATFORM}/${DESIGN}/ord/run.sh"
      JOB_NAME="${PLATFORM}/${DESIGN}"
      JOB_LOG="${LOG_DIR}/${PLATFORM}_${DESIGN}.log"

      if [[ ! -f "${RUN_SH}" ]]; then
        echo "[SKIP] ${JOB_NAME}: run.sh not found (${RUN_SH})"
        continue
      fi

      echo "[$(date '+%H:%M:%S')] Launching ${JOB_NAME} -> ${JOB_LOG}"
      (cd "${FLOW_ROOT}" && bash "${RUN_SH}") > "${JOB_LOG}" 2>&1 &
      PIDS+=($!)
      JOBS+=("${JOB_NAME}")
    done
  done

  echo ""
  echo "Launched ${#PIDS[@]} jobs. Polling for CTS completion..."
  echo ""

  # Background poller: generate HTML as soon as cts_debug_per_ff.csv appears
  declare -A _REPORTED
  _all_pids_done() {
    for pid in "${PIDS[@]}"; do kill -0 "$pid" 2>/dev/null && return 1; done
    return 0
  }
  while ! _all_pids_done; do
    for PLATFORM in "${PLATFORMS[@]}"; do
      for DESIGN in "${DESIGNS[@]}"; do
        _key="${PLATFORM}/${DESIGN}"
        if [[ -z "${_REPORTED[$_key]}" ]]; then
          _csv="${FLOW_ROOT}/results/${PLATFORM}/${DESIGN}/openroad/cts_debug_per_ff.csv"
          if [[ -f "${_csv}" ]]; then
            echo "[$(date '+%H:%M:%S')] CTS done: ${_key} — generating HTML..."
            generate_debug_report "${PLATFORM}" "${DESIGN}" \
              && echo "[$(date '+%H:%M:%S')] HTML OK: ${_key}" \
              || echo "[$(date '+%H:%M:%S')] HTML FAIL: ${_key} (non-fatal)"
            _REPORTED[$_key]=1
          fi
        fi
      done
    done
    sleep 10
  done

  # Wait for all jobs and collect status
  PASS=0
  FAIL=0
  for i in "${!PIDS[@]}"; do
    wait "${PIDS[$i]}"
    RC=$?
    if [[ ${RC} -eq 0 ]]; then STATUS="PASS"; ((PASS++))
    else STATUS="FAIL (rc=${RC})"; ((FAIL++)); fi
    echo "[$(date '+%H:%M:%S')] ${JOBS[$i]}: ${STATUS}"
  done

  echo ""
  echo "--- All jobs done. Running fallback report pass... ---"
  echo ""
fi

# ================================================================
# Phase 2: Fallback report pass
# ================================================================
REPORT_PASS=0
REPORT_FAIL=0
for PLATFORM in "${PLATFORMS[@]}"; do
  for DESIGN in "${DESIGNS[@]}"; do
    if generate_debug_report "${PLATFORM}" "${DESIGN}"; then
      ((REPORT_PASS++))
    else
      ((REPORT_FAIL++))
    fi
  done
done

# ================================================================
# Summary
# ================================================================
echo ""
echo "================================================================"
echo "BATCH COMPLETE"
echo "================================================================"

if [[ "${REPORT_ONLY}" != "1" ]]; then
  printf "%-35s  %-10s  %s\n" "Design" "CTS" "Log"
  echo "-----------------------------------  ----------  ---"
  for i in "${!JOBS[@]}"; do
    JOB_LOG="${LOG_DIR}/$(echo "${JOBS[$i]}" | tr '/' '_').log"
    STATUS="FAIL"
    # Check if final stage completed
    if [[ -f "${FLOW_ROOT}/results/$(echo "${JOBS[$i]}" | tr '/' '/')/openroad/6_final.def" ]]; then
      STATUS="PASS"
    fi
    printf "%-35s  %-10s  %s\n" "${JOBS[$i]}" "${STATUS}" "${JOB_LOG}"
  done
  echo ""
  echo "CTS:    PASS=${PASS}  FAIL=${FAIL}  TOTAL=${#PIDS[@]}"
fi

echo "Report: PASS=${REPORT_PASS}  FAIL=${REPORT_FAIL}  TOTAL=$((REPORT_PASS + REPORT_FAIL))"
echo "HTML:   reports/<platform>/<design>/cts_debug_report.html"
echo "Finished: $(date)"
echo "================================================================"
