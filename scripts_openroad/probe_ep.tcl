source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "probe"
repair_clock_inverters
set cts_args [list -sink_clustering_enable -repair_clock_nets -root_buf $::env(CTS_BUF_CELL) -buf_list "$::env(CTS_BUF_UPPER)"]
append_env_var cts_args CTS_BUF_DISTANCE -distance_between_buffers 1
clock_tree_synthesis {*}$cts_args
estimate_parasitics -placement
set_propagated_clock [all_clocks]
proc inst_of {pin} { set fn [get_full_name $pin]; set i [string last "/" $fn]; return [string range $fn 0 [expr {$i-1}]] }
array set LB {}; set f [open $::env(BAL_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set LB([lindex $c 0]) [lindex $c 1] }; close $f
array set A {}; set f [open $::env(SKEW_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set A([lindex $c 0]) [lindex $c 1] }; close $f
foreach ff [array names LB] { set pin [get_pins -quiet "${ff}/CLK"]; if {$pin eq ""} continue
  set v [expr {$LB($ff)/1000.0}]; if {[info exists A($ff)]} {set v [expr {$v+$A($ff)}]}; set_clock_latency $v $pin }
foreach ep {_16371__upper _16650__bottom _16173__upper} {
  set paths [find_timing_paths -to [get_pins -quiet ${ep}/D] -path_delay max -endpoint_path_count 3 -group_path_count 100]
  puts "=== worst incoming to $ep (a_i applied) ==="
  foreach p $paths {
    set sp [inst_of [get_property $p startpoint]]
    set spt [get_property [get_property $p startpoint] direction]
    puts "   startpoint=$sp  dir=$spt  slack=[format %.1f [expr {[get_property $p slack]/1000.0*1000}]]ps"
  }
}
