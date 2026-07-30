# Δslack = Δclock GATE. Ideal-clock env (NO CTS tree, NO set_propagated_clock).
# Perturb ONE FF by +100ps; measure whether ITS worst slack moves by exactly +/-100ps.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "perturb-one Δslack=Δclock gate"
estimate_parasitics -placement
puts "ENV: sta time_unit scale = [sta::unit_scale time] (1e-9=ns basis, 1e-12=ps basis for get_property)"
# report environment facts (rule out confounds)
puts "ENV: propagated_clocks = [get_property [lindex [all_clocks] 0] is_propagated]"
proc inst_of {pin} { set fn [get_full_name $pin]; set i [string last "/" $fn]; return [string range $fn 0 [expr {$i-1}]] }
# establish L_bal basis for ALL ffs (same as eval_lp_solution.tcl)
array set LB {}; set f [open $::env(BAL_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; set LB([lindex $c 0]) [lindex $c 1] }; close $f
foreach ff [array names LB] { set p [get_pins -quiet "${ff}/CLK"]; if {$p ne ""} { set_clock_latency [expr {$LB($ff)/1000.0}] $p } }
proc worst_to {ff} { set ps [find_timing_paths -to [get_pins -quiet ${ff}/D] -path_delay max -endpoint_path_count 1 -group_path_count 50]
  if {[llength $ps]==0} {return "NA"}; return [get_property [lindex $ps 0] slack] }
proc worst_from {ff} { set ps [find_timing_paths -from [get_cells -quiet $ff] -path_delay max -endpoint_path_count 1 -group_path_count 50]
  if {[llength $ps]==0} {return "NA"}; return [get_property [lindex $ps 0] slack] }
puts "\n=== CAPTURE-side test: delay FF by +100ps, expect worst INCOMING slack +100ps ==="
foreach ff {_16371__upper _16405__upper _16373__upper} {
  set s0 [worst_to $ff]
  set_clock_latency [expr {$LB($ff)/1000.0 + 0.1}] [get_pins ${ff}/CLK]
  set s1 [worst_to $ff]
  set_clock_latency [expr {$LB($ff)/1000.0}] [get_pins ${ff}/CLK]
  set d [expr {$s1-$s0}]; puts [format "  %-20s rawS0=%.6e rawS1=%.6e  dSlack=%.6e   ratio(d/0.1)=%.4f  (expect +1.0)" $ff $s0 $s1 $d [expr {$d/0.1}]]
}
puts "\n=== LAUNCH-side test: delay FF by +100ps, expect worst OUTGOING slack -100ps ==="
foreach ff {_16362__bottom _16323__upper _16296__upper} {
  set s0 [worst_from $ff]
  set_clock_latency [expr {$LB($ff)/1000.0 + 0.1}] [get_pins ${ff}/CLK]
  set s1 [worst_from $ff]
  set_clock_latency [expr {$LB($ff)/1000.0}] [get_pins ${ff}/CLK]
  set d [expr {$s1-$s0}]; puts [format "  %-20s rawS0=%.6e rawS1=%.6e  dSlack=%.6e   ratio(d/0.1)=%.4f  (expect -1.0)" $ff $s0 $s1 $d [expr {$d/0.1}]]
}
