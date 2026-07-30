# Re-check the V73 invariants on the FINAL post-CTS DB (after repair_clock_nets + DPL +
# buffer sizing), not just at Phase 3 -- those stages can also break clock connectivity.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 4_cts.odb 4_cts.sdc "post-CTS invariant re-check"
estimate_parasitics -placement
set_propagated_clock [all_clocks]
source $::env(OPENROAD_SCRIPTS_DIR)/check_cts_invariants.tcl
catch {check_cts_invariants} e
puts "POST setup_tns_ps [expr {[sta::total_negative_slack_cmd max]*1e12}]"
puts "POST hold_tns_ps  [expr {[sta::total_negative_slack_cmd min]*1e12}]"
puts "POST hold_wns_ps  [expr {[sta::worst_slack_cmd min]*1e12}]"
# how many FF/macro endpoints actually have a clock?
set nff 0; set noclk 0
set block [[[ord::get_db] getChip] getBlock]
foreach inst [$block getInsts] {
  foreach it [$inst getITerms] {
    if {[[$it getMTerm] getSigType] ne "CLOCK"} continue
    incr nff
    set p [get_pin -quiet "[$inst getName]/[[$it getMTerm] getName]"]
    if {$p eq ""} { incr noclk; continue }
    set c ""; catch {set c [get_property $p clocks]}
    if {[llength $c]==0} { incr noclk }
  }
}
puts "POST clock_pins $nff  without_sta_clock $noclk"
puts "POSTCHECK_DONE"
