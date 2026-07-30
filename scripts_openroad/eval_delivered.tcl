# Apply the tree's DELIVERED per-FF latency (clk_latency_ps) as exact ideal clock latency (no tree).
# Isolates: do the delivered LATENCY VALUES explain the -28,889, or does the physical tree add damage?
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "eval delivered latency"
estimate_parasitics -placement
proc tns_ps {mm} { return [expr {[sta::total_negative_slack_cmd $mm]*1e12}] }
# read cts_debug_per_ff.csv: ff_name, ..., clk_latency_ps(col3)
set f [open $::env(DBG_CSV) r]; gets $f hdr
set cols [split $hdr ,]; set ci [lsearch $cols clk_latency_ps]
set n 0
while {[gets $f l]>=0} { set c [split $l ,]; set ff [lindex $c 0]; if {$ff eq ""} continue
  set lat [lindex $c $ci]; set p [get_pins -quiet "${ff}/CLK"]
  if {$p ne "" && $lat ne ""} { set_clock_latency $lat $p; incr n } }
close $f
puts "RESULT eval_delivered: applied $n delivered latencies -> setup TNS = [format %.1f [tns_ps max]]  hold = [format %.1f [tns_ps min]]"
