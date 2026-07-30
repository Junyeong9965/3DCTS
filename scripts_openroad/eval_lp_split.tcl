source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "ideal-eval split"
estimate_parasitics -placement
proc tns_ps {} { return [expr {[sta::total_negative_slack_cmd "max"]*1e12}] }
array set LB {}; set f [open $::env(BAL_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set LB([lindex $c 0]) [lindex $c 1] }; close $f
array set A {}; set f [open $::env(SKEW_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set A([lindex $c 0]) [lindex $c 1] }; close $f
proc apply {useai} {
  upvar #0 LB LB A A
  foreach ff [array names LB] { set pin [get_pins -quiet "${ff}/CLK"]; if {$pin eq ""} continue
    set v [expr {$LB($ff)/1000.0}]; if {$useai && [info exists A($ff)]} { set v [expr {$v+$A($ff)}] }
    set_clock_latency $v $pin }
}
apply 0; set full0 [tns_ps]; apply 1; set full1 [tns_ps]
puts ">>> FULL (all endpoints):     bal=[format %.0f $full0]  a_i=[format %.0f $full1]  delta=[format %+.0f [expr {$full0-$full1}]]"
# register-only: false-path all outputs
apply 0
set_false_path -to [all_outputs]
apply 0; set r0 [tns_ps]; apply 1; set r1 [tns_ps]
puts ">>> REGISTER endpoints only:  bal=[format %.0f $r0]  a_i=[format %.0f $r1]  delta=[format %+.0f [expr {$r0-$r1}]]"
puts ">>> (LP complete-graph model claimed 28857 -> 22497 on register endpoint-TNS)"
puts ">>> OUTPUT degradation = FULL_delta - REG_delta = [format %+.0f [expr {($full0-$full1)-($r0-$r1)}]] ps"
