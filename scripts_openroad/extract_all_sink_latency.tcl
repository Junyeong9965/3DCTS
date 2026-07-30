# Complete per-sink delivered clock latency extraction (ALL endpoints, no 5000-path cap).
# Loads the DELIVERED post-CTS DB (4_cts.odb) and reports, for every register capture
# endpoint, its propagated clock latency + setup slack. Uses -unique_paths_to_endpoint so
# every endpoint is covered exactly once (group_path_count > #registers). Also reports the
# aggregate STA setup/hold TNS/WNS as the ground-truth physical result. Coverage (missing=0)
# is verified against all_registers.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl

load_design 4_cts.odb 4_cts.sdc "all-sink latency extraction (delivered)"
estimate_parasitics -placement
set_propagated_clock [all_clocks]

# ground-truth aggregate (should match 4_cts_timing.rpt)
puts "GROUNDTRUTH setup_tns [expr {[sta::total_negative_slack_cmd max]*1e12}] ps"
puts "GROUNDTRUTH setup_wns [expr {[sta::worst_slack_cmd max]*1e12}] ps"

set nreg [llength [all_registers]]
set ncap [expr {$nreg + 10000}]
puts "enumerating up to $ncap unique setup endpoints (registers=$nreg) ..."

set out [open $::env(RESULTS_DIR)/all_sink_latency.csv w]
puts $out "ff_name,clk_latency_ps,setup_slack_ps"
array set seen {}
set path_ends [find_timing_paths -path_delay max -sort_by_slack \
                 -group_path_count $ncap -unique_paths_to_endpoint]
set n 0
foreach pe $path_ends {
  set pin [get_full_name [$pe pin]]
  set ff  [regsub {/[^/]+$} $pin ""]
  if {[info exists seen($ff)]} continue
  set seen($ff) 1
  set lat [expr {[$pe target_clk_delay]*1e12}]
  set slk [expr {[$pe slack]*1e12}]
  puts $out "$ff,$lat,$slk"
  incr n
}
close $out
puts "COVERAGE registers=$nreg covered=[array size seen] endpoints_dumped=$n"
puts "DONE all_sink_latency.csv"
exit
