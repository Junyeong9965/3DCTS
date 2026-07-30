source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "LP-solution eval (balanced basis, no tree)"
estimate_parasitics -placement
proc tns_ps {} { return [expr {[sta::total_negative_slack_cmd "max"]*1e12}] }
# load L_bal (ps) and a_i (ns)
array set LB {}; set f [open $::env(BAL_CSV) r]; gets $f
while {[gets $f line]>=0} { set c [split $line ,]; set LB([lindex $c 0]) [lindex $c 1] }
close $f
array set A {}; set f [open $::env(SKEW_CSV) r]; gets $f
while {[gets $f line]>=0} { set c [split $line ,]; set A([lindex $c 0]) [lindex $c 1] }
close $f
# BASELINE: set latency = L_bal (ns) for every FF that has one
set nb 0
foreach ff [array names LB] {
  set pin [get_pins -quiet "${ff}/CLK"]; if {$pin eq ""} continue
  set_clock_latency [expr {$LB($ff)/1000.0}] $pin ;# ps->ns
  incr nb
}
set base [tns_ps]
puts ">>> BASELINE (L_bal only, $nb pins) setup TNS = [format %.1f $base] ps"
# ADD a_i on top: latency = L_bal + a_i
set n 0
foreach ff [array names A] {
  if {$A($ff) <= 0.0000001} continue
  set pin [get_pins -quiet "${ff}/CLK"]; if {$pin eq ""} continue
  set lb 0.0; if {[info exists LB($ff)]} { set lb [expr {$LB($ff)/1000.0}] }
  set_clock_latency [expr {$lb + $A($ff)}] $pin
  incr n
}
set skew [tns_ps]
puts ">>> WITH L_bal + LP a_i ($n skewed) setup TNS = [format %.1f $skew] ps"
puts ">>> DELTA = [format %.1f [expr {$skew-$base}]] ps  (POSITIVE = LP solution IMPROVES real TNS on balanced basis)"
puts ">>> For contrast: LP's OWN model claimed 28914 -> 19451 (subset endpoint-TNS)."
