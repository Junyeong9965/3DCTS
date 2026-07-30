# =====================================================================
# lp_sweep.tcl  (EXPERIMENT  — slack-window extraction study)
#
# Runs cts::solve_skew_lp on a SET of pre-filtered FF->FF timing graphs
# (one per slack window W) inside a SINGLE OpenROAD process, so the design
# is loaded exactly once. Purpose: isolate the *modeling* question —
# does dropping FF->FF edges whose (setup AND hold) slack >= W change the
# LP solution vs the complete (batch=1) graph? — WITHOUT any re-extraction
# and WITHOUT the endpoint-truncation confound of a real windowed extract.
#
# The theory under test: for add-only bounded skew (0 <= a_i <= D=max_skew),
# any edge with pre-CTS slack >= D + margin can never become active, so
# filtering it must leave the LP feasible region and objective unchanged.
#
# Env:
#   EXP_DIR  : dir holding graphs/ff_<W>.csv (pre-filtered) + io.csv
#   WLIST    : space-separated W labels (e.g. "inf 260 200 150 120 110")
#   PRE_CTS_* / max_skew : LP params (defaults = batch=1 values)
# Output: EXP_DIR/out/ai_<W>.csv per W. LP objective printed via CTS-0604.
# =====================================================================
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "lp_sweep (windowed graphs)"
estimate_parasitics -placement
catch { set_propagated_clock [all_clocks] }

set expd  $::env(EXP_DIR)
set wlist $::env(WLIST)
set io_csv "$expd/io.csv"

# LP params — default to the batch=1 values (matched to results_batch1 cts.log)
proc _env {n d} { return [expr {[info exists ::env($n)] ? $::env($n) : $d}] }
set sigma_local  [_env PRE_CTS_SIGMA_LOCAL 0.001]
set sigma_pi     [_env PRE_CTS_SIGMA_PI    0.005]
set lambda_reg   [_env PRE_CTS_LAMBDA_REG  0.01]
set hold_margin  [_env PRE_CTS_HOLD_MARGIN 0.003]
set gamma_wns    [_env PRE_CTS_GAMMA_WNS   10.0]
set weight_io    [_env PRE_CTS_WEIGHT_IO   1.0]
set hard_pi_hold [_env PRE_CTS_HARD_PI_HOLD 0]
set max_skew     [_env PRE_CTS_MAX_SKEW    0.100]

set vfile ""
catch { set vfile [lindex [glob -nocomplain $::env(RESULTS_DIR)/3_*.v] 0] }

file mkdir "$expd/out"
set ::env(CTS_IO_TIMING_EDGES_CSV) $io_csv
foreach w $wlist {
  set g   "$expd/graphs/ff_${w}.csv"
  set out "$expd/out/ai_${w}.csv"
  if {![file exists $g]} { puts ">>> SWEEP_W=$w MISSING_GRAPH $g"; continue }
  set ::env(CTS_FF_TIMING_GRAPH_CSV) $g
  set ::env(CTS_SKEW_TARGETS_CSV)    $out
  puts "=================================================================="
  puts ">>> SWEEP_W=$w  graph=[file tail $g]  edges=[expr {[exec wc -l < $g]-1}]"
  puts "=================================================================="
  if {[catch { cts::solve_skew_lp $vfile $sigma_local $sigma_pi $lambda_reg \
                 $hold_margin $gamma_wns $weight_io $hard_pi_hold $max_skew } err]} {
    puts ">>> SWEEP_W=$w LP_FAILED: $err"
  } else {
    puts ">>> SWEEP_W=$w DONE -> [file tail $out]"
  }
}
puts ">>> SWEEP_ALL_DONE"
