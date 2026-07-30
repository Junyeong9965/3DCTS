# Post-route setup+hold reporter: load routed ODB (+SPEF if present), report TNS max/min.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
set odb $::env(RH_ODB)
set sdc $::env(RH_SDC)
load_design $odb $sdc "reporthold"
if {[info exists ::env(RH_SPEF)] && [file exists $::env(RH_SPEF)]} {
  puts "reading SPEF $::env(RH_SPEF)"
  read_spef $::env(RH_SPEF)
} else {
  puts "no SPEF -> estimate_parasitics -placement"
  estimate_parasitics -placement
}
set_propagated_clock [all_clocks]
proc tns {mm} { return [expr {[sta::total_negative_slack_cmd $mm]*1e12}] }
set nreg [llength [all_registers]]
set nend [llength [sta::endpoints]]
puts "COVERAGE n_registers=$nreg n_endpoints=$nend"
puts "THRESULT $::env(RH_NAME) setup=[format %.1f [tns max]] hold=[format %.1f [tns min]]"
