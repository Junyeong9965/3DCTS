# =====================================================================
# eval_sweep.tcl  (EXPERIMENT  — slack-window extraction study)
#
# Out-of-model validation: full-STA re-evaluation of the balanced schedule
# perturbed by each window's LP skew, i.e. set_clock_latency = L_bal + a_i(W),
# then total_negative_slack over ALL paths. Loads the design ONCE and sweeps
# all W in-process. This is the decision metric (ideal-STA TNS), independent
# of the LP's own endpoint-TNS on the (possibly incomplete) extracted graph.
#
# CRITICAL: uses the IDEAL-clock mechanism (set_clock_latency) on the L_bal
# basis. Does NOT set_propagated_clock (which would make set_clock_latency a
# no-op). Design time unit is ps (verified: report_units). a_i is in ns,
# L_bal in ps.
#
# Env: EXP_DIR (has L_bal.csv + out/ai_<W>.csv), WLIST.
# =====================================================================
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "eval_sweep (L_bal + a_i(W))"
estimate_parasitics -placement
proc tns {mm} { return [expr {[sta::total_negative_slack_cmd $mm]*1e12}] }

# ---- UNIT TRAP GUARD (memory: ns-vs-ps mix cost days, a_i applied 1/1000) ----
# set_clock_latency takes the value in the DESIGN time unit. Empirically probe it:
# inject a known latency on one FF under ideal clock and measure the slack shift (ps).
# PS_PER_UNIT = ps of slack shift per 1.0 passed to set_clock_latency. For a ps-unit
# design PS_PER_UNIT≈1 (pass ps numbers); for ns-unit ≈1000 (pass ns numbers).
puts ">>> report_units:"; catch { report_units }
set expd  $::env(EXP_DIR)
set wlist $::env(WLIST)

# L_bal (ps)
array set LB {}
set f [open "$expd/L_bal.csv" r]; gets $f
while {[gets $f line]>=0} { set c [split $line ,]
  if {[lindex $c 0] ne ""} { set LB([lindex $c 0]) [lindex $c 1] } }
close $f

# Deterministic unit detection via STA (report_units uses this same suffix).
# LATU = design-time-units per 1 ps. ps-design => 1.0 ; ns-design => 0.001.
set _tsuf "1ns"
catch { set _tsuf [sta::unit_scale_suffix "time"] }   ;# e.g. "1ps" (aes) or "1ns" (ng45/ibex)
if {[string match "*fs" $_tsuf]}     { set _uscale 1e-15 } \
elseif {[string match "*ps" $_tsuf]} { set _uscale 1e-12 } \
elseif {[string match "*ns" $_tsuf]} { set _uscale 1e-9  } \
elseif {[string match "*us" $_tsuf]} { set _uscale 1e-6  } \
else                                 { set _uscale 1e-9  }
set LATU [expr {1e-12 / $_uscale}]
puts ">>> TIME_UNIT=$_tsuf  LATU(units per ps)=$LATU  (L_bal in ps, a_i in ns)"
# Sanity probe: inject 100 ps on one FF, expect ~100 ps TNS shift magnitude.
set _probe_ff ""; foreach ff [array names LB] { set p [get_pins -quiet "${ff}/CLK"]; if {$p ne ""} { set _probe_ff $ff; break } }
set _pp [get_pins -quiet "${_probe_ff}/CLK"]
set_clock_latency 0.0 $_pp; set _s0 [tns max]
set_clock_latency [expr {100.0*$LATU}] $_pp; set _s1 [tns max]
puts [format ">>> UNIT_SANITY: +100ps on %s -> TNS shift %.2f ps (want |shift|<=100)" $_probe_ff [expr {$_s1-$_s0}]]
set_clock_latency 0.0 $_pp

# Reset every FF's clock latency to its balanced value (full reset between W).
# All latencies expressed in ps then scaled by LATU into the design time unit.
proc set_baseline {} {
  upvar LB LB; upvar LATU LATU
  foreach ff [array names LB] {
    set pin [get_pins -quiet "${ff}/CLK"]
    if {$pin ne ""} { set_clock_latency [expr {$LB($ff) * $LATU}] $pin }
  }
}

set_baseline
set base [tns max]
set baseh [tns min]
puts ">>> EVAL_BASELINE Lbal-only setup=[format %.1f $base] hold=[format %.1f $baseh]"

foreach w $wlist {
  set skv "$expd/out/ai_${w}.csv"
  if {![file exists $skv]} { puts ">>> EVAL_W=$w MISSING $skv"; continue }
  set_baseline    ;# reset all FFs to L_bal
  set n 0
  set f [open $skv r]; gets $f
  while {[gets $f line]>=0} {
    set c [split $line ,]; set ff [lindex $c 0]; if {$ff eq ""} continue
    set a [lindex $c 1]
    if {$a > 0.0000001 || $a < -0.0000001} {
      set pin [get_pins -quiet "${ff}/CLK"]; if {$pin eq ""} continue
      set lb 0.0; if {[info exists LB($ff)]} { set lb $LB($ff) }
      # latency in ps = L_bal(ps) + a_i(ns)*1000 ; then scale into design unit via LATU
      set_clock_latency [expr {($lb + $a*1000.0) * $LATU}] $pin; incr n
    }
  }
  close $f
  set s [tns max]
  puts [format ">>> EVAL_W=%s skewed=%d setup=%.1f hold=%.1f  DELTA_vs_base=%.1f" \
        $w $n $s [tns min] [expr {$s - $base}]]
}
puts ">>> EVAL_ALL_DONE"
