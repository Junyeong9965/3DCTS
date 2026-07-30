# Post-TARP final-CTS FF timing graph extraction (for stale-target + data-arrival analysis).
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 4_cts.def 4_cts.sdc "post-TARP final CTS extraction"
set_propagated_clock [all_clocks]
estimate_parasitics -placement
set out $::env(RESULTS_DIR)/post_cts_ff_timing_graph.csv
if {[catch {cts::extract_ff_timing_graph_odb $out} err]} { puts "EXTRACT_FAIL: $err" } else { puts "WROTE $out" }
