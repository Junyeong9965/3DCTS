# CAPTURE-ONLY ORACLE (correct ps units). Ideal-clock env (no CTS, no propagated).
# L_bal (cts_base_latency.csv) is in PS -> pass directly. a_i (skew_targets) is in NS -> x1000.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "capture-only oracle"
estimate_parasitics -placement
proc tns_ps {mm} { return [expr {[sta::total_negative_slack_cmd $mm]*1e12}] }
proc inst_of {pin} { set fn [get_full_name $pin]; set i [string last "/" $fn]; return [string range $fn 0 [expr {$i-1}]] }
array set LB {}; set f [open $::env(BAL_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; if {[lindex $c 0] ne ""} {set LB([lindex $c 0]) [lindex $c 1]} }; close $f
array set A {}; set f [open $::env(SKEW_CSV) r]; gets $f
while {[gets $f l]>=0} { set c [split $l ,]; if {[lindex $c 0] ne ""} {set A([lindex $c 0]) [lindex $c 1]} }; close $f
proc set_all_lbal {} { global LB; foreach ff [array names LB] { set p [get_pins -quiet "${ff}/CLK"]; if {$p ne ""} {set_clock_latency $LB($ff) $p} } }

# ---- BASELINE (balanced L_bal, PS) ----
set_all_lbal
set Sbase [tns_ps max]; set Hbase [tns_ps min]
puts "RESULT BASELINE      setup=[format %.1f $Sbase]  hold=[format %.1f $Hbase]"

# ---- REF: current LP a_i applied CORRECTLY (x1000) ----
set nlp 0
foreach ff [array names A] { if {$A($ff)>1e-9} { set p [get_pins -quiet "${ff}/CLK"]; if {$p ne ""} {set_clock_latency [expr {$LB($ff)+$A($ff)*1000.0}] $p; incr nlp} } }
set Slp [tns_ps max]; set Hlp [tns_ps min]
puts "RESULT LP_ai_correct setup=[format %.1f $Slp]  hold=[format %.1f $Hlp]  dSetup=[format %+.1f [expr {$Sbase-$Slp}]]  nFF=$nlp"

# ---- CAPTURE-ONLY ORACLE ----
set_all_lbal
# per-register worst incoming setup slack (deficit) at balanced
set paths [find_timing_paths -path_delay max -unique_paths_to_endpoint -endpoint_path_count 1 -group_path_count 10000000 -slack_max 1e9]
array set DEF {}
foreach p $paths { set j [inst_of [get_property $p endpoint]]
  if {[info exists LB($j)]} { set s [get_property $p slack]; if {$s<0 && (![info exists DEF($j)] || -$s>$DEF($j))} {set DEF($j) [expr {-$s}]} } }
# apply a_j = min(100, deficit)  (PS)
set norc 0; set suma 0
foreach j [array names DEF] { set aj [expr {$DEF($j)<100.0 ? $DEF($j) : 100.0}]
  set p [get_pins -quiet "${j}/CLK"]; if {$p ne ""} {set_clock_latency [expr {$LB($j)+$aj}] $p; incr norc; set suma [expr {$suma+$aj}]} }
set Sorc [tns_ps max]; set Horc [tns_ps min]
puts "RESULT CAPTURE_ORACLE setup=[format %.1f $Sorc]  hold=[format %.1f $Horc]  dSetup=[format %+.1f [expr {$Sbase-$Sorc}]]  nFF=$norc  avg_aj=[format %.1f [expr {$suma/$norc}]]ps"
puts "DECISION: dSetup(oracle) >> 0 => AES HAS headroom (LP/objective wrong); ~0 => structurally zero-sum"
