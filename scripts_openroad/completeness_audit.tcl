# COMPLETENESS AUDIT: dump STA per-register-endpoint WORST setup slack (balanced + with a_i),
# to compare against the extracted graph. NOT timing analysis — edge-coverage verification.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "completeness audit"
repair_clock_inverters
set cts_args [list -sink_clustering_enable -repair_clock_nets -root_buf $::env(CTS_BUF_CELL) -buf_list "$::env(CTS_BUF_UPPER)"]
append_env_var cts_args CTS_BUF_DISTANCE -distance_between_buffers 1
clock_tree_synthesis {*}$cts_args
estimate_parasitics -placement
set_propagated_clock [all_clocks]
proc inst_of {pin} { set fn [get_full_name $pin]; set i [string last "/" $fn]; return [string range $fn 0 [expr {$i-1}]] }
# apply a_i as clock latency
array set LB {}; set f [open $::env(BAL_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set LB([lindex $c 0]) [lindex $c 1] }; close $f
array set A {}; set f [open $::env(SKEW_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set A([lindex $c 0]) [lindex $c 1] }; close $f
if {![info exists ::env(AUDIT_NOAI)]} {
foreach ff [array names LB] { set pin [get_pins -quiet "${ff}/CLK"]; if {$pin eq ""} continue
  set v [expr {$LB($ff)/1000.0}]; if {[info exists A($ff)]} {set v [expr {$v+$A($ff)}]}; set_clock_latency $v $pin } }
# per-endpoint worst (unique paths to endpoint) WITH a_i
set paths [find_timing_paths -path_delay max -unique_paths_to_endpoint -endpoint_path_count 1 -group_path_count 10000000 -slack_max 1e9]
set fh [open $::env(RESULTS_DIR)/$::env(AUDIT_OUT) w]
puts $fh "endpoint,worst_slack_ps"
set n 0
foreach p $paths { set ep [inst_of [get_property $p endpoint]]; puts $fh "$ep,[expr {[get_property $p slack]/1000.0*1000}]"; incr n }
close $fh
puts ">>> dumped $n endpoint worst-slacks to $::env(AUDIT_OUT)"
