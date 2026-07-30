# CTS-only 2D timing extraction.
# Loads 3_place DB → runs standard CTS → reports setup+hold TNS/WNS.
# No routing — CTS-stage timing only (fair comparison with 3D CTS).
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl

# Load from place DB (3_place, not 2_2_floorplan)
load_design 3_place.def 3_place.sdc "2D CTS timing extraction"
source $::env(OPENROAD_SCRIPTS_DIR)/placement_utils.tcl

repair_clock_inverters

# Standard CTS (same as openroad_2d_flow.tcl)
set cts_args [list \
  -sink_clustering_enable \
  -repair_clock_nets \
  -root_buf $::env(CTS_BUF_CELL) \
  -buf_list $::env(CTS_BUF_CELL)
  ]

append_env_var cts_args CTS_BUF_DISTANCE -distance_between_buffers 1
append_env_var cts_args CTS_CLUSTER_SIZE -sink_clustering_size 1
append_env_var cts_args CTS_CLUSTER_DIAMETER -sink_clustering_max_diameter 1
append_env_var cts_args CTS_BUF_LIST -buf_list 1
append_env_var cts_args CTS_LIB_NAME -library 1

if { [env_var_exists_and_non_empty CTS_ARGS] } {
  set cts_args $::env(CTS_ARGS)
}

log_cmd clock_tree_synthesis {*}$cts_args

# Post-CTS timing (propagated clock, placement parasitics)
estimate_parasitics -placement
set_propagated_clock [all_clocks]

set setup_tns [sta::total_negative_slack_cmd max]
set setup_wns [sta::worst_slack_cmd max]
set hold_tns [sta::total_negative_slack_cmd min]
set hold_wns [sta::worst_slack_cmd min]

# Convert to ps
set setup_tns_ps [expr {$setup_tns * 1e12}]
set setup_wns_ps [expr {$setup_wns * 1e12}]
set hold_tns_ps [expr {$hold_tns * 1e12}]
set hold_wns_ps [expr {$hold_wns * 1e12}]

puts "=========================================\n"
puts "2D Pin3D CTS Timing Report"
puts "=========================================\n"
puts [format "setup_tns %12.2f ps" $setup_tns_ps]
puts [format "setup_wns %12.2f ps" $setup_wns_ps]
puts [format "hold_tns  %12.2f ps" $hold_tns_ps]
puts [format "hold_wns  %12.2f ps" $hold_wns_ps]
puts "========================================="

# Also write to report file
set rpt_dir $::env(REPORTS_DIR)
file mkdir $rpt_dir
set f [open "${rpt_dir}/4_cts_timing.rpt" w]
puts $f "========================================="
puts $f "timing report_tns"
puts $f "-----------------------------------------"
puts $f [format "tns max %.4f" $setup_tns_ps]
puts $f [format "tns min %.4f" $hold_tns_ps]
puts $f ""
puts $f "========================================="
puts $f "timing report_wns"
puts $f "-----------------------------------------"
puts $f [format "wns max %.4f" $setup_wns_ps]
puts $f [format "wns min %.4f" $hold_wns_ps]
puts $f ""
puts $f "========================================="
puts $f "finish hold timing"
puts $f "-----------------------------------------"
puts $f [format "setup_tns %.4f ps" $setup_tns_ps]
puts $f [format "setup_wns %.4f ps" $setup_wns_ps]
puts $f [format "hold_tns  %.4f ps" $hold_tns_ps]
puts $f [format "hold_wns  %.4f ps" $hold_wns_ps]
puts $f "========================================="
close $f
puts "Report: ${rpt_dir}/4_cts_timing.rpt"
