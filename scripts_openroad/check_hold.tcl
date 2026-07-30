source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design $::env(RH_ODB) $::env(RH_SDC) "checkhold"
if {[file exists $::env(RH_SPEF)]} { read_spef $::env(RH_SPEF) } else { estimate_parasitics -placement }
set_propagated_clock [all_clocks]
proc tns {mm} { return [expr {[sta::total_negative_slack_cmd $mm]*1e12}] }
# hold TNS + unconstrained endpoints
set unconst [sta::endpoints_without_paths]
puts "CHKHOLD holdTNS=[format %.1f [tns min]] setupTNS=[format %.1f [tns max]] n_unconstrained_endpoints=[llength $unconst]"
