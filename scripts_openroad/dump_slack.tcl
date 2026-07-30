source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "dump per-FF slack"
estimate_parasitics -placement
proc inst_of {pin} { set fn [get_full_name $pin]; set i [string last "/" $fn]; return [string range $fn 0 [expr {$i-1}]] }
set f [open $::env(LAT_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set ff [lindex $c 0]; if {$ff eq ""} continue
  set p [get_pins -quiet "${ff}/CLK"]; if {$p ne ""} {set_clock_latency [lindex $c 1] $p} }
close $f
set paths [find_timing_paths -path_delay max -unique_paths_to_endpoint -endpoint_path_count 1 -group_path_count 10000000 -slack_max 1e9]
set fh [open $::env(SLACK_OUT) w]; puts $fh "ff,slack_ps"
foreach p $paths { puts $fh "[inst_of [get_property $p endpoint]],[get_property $p slack]" }
close $fh
puts ">>> dumped [llength $paths] endpoint slacks to $::env(SLACK_OUT)"
