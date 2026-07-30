source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "eval vec"
estimate_parasitics -placement
proc tns {mm} { return [expr {[sta::total_negative_slack_cmd $mm]*1e12}] }
set f [open $::env(LAT_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set ff [lindex $c 0]; if {$ff eq ""} continue
  set p [get_pins -quiet "${ff}/CLK"]; if {$p ne ""} {set_clock_latency [lindex $c 1] $p} }
close $f
puts "VECRESULT $::env(VECNAME) setup=[format %.1f [tns max]] hold=[format %.1f [tns min]]"
