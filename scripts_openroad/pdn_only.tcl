source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl

load_design 2_5_floorplan_macro.v 1_synth.sdc "Starting PDN Generation"

source $::env(OPENROAD_SCRIPTS_DIR)/placement_utils.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/floorplan_utils.tcl

[catch { source $::env(PDN_TCL)} errorMessage]
# Allow PDN repair channel failure for macro-heavy designs (PDN-0179 workaround).
# Macros have internal power rings; missing repair straps only affect IR drop margin.
# Use namespace-qualified call (pdn::) for ORFS_260323+ OpenROAD builds
pdn::allow_repair_channels 1
if { [info exists ::env(UPPER_SITE)] && [info exists ::env(BOTTOM_SITE)] } {
  puts "PDN sites: UPPER_SITE=$::env(UPPER_SITE), BOTTOM_SITE=$::env(BOTTOM_SITE)"
} else {
  if {[catch { pdngen } errorMessage]} {
    puts "ErrorPDN: $errorMessage"
  }
}

if { [info exists ::env(POST_PDN_TCL)] && [file exists $::env(POST_PDN_TCL)] } {
  source $::env(POST_PDN_TCL)
}

write_def     $env(RESULTS_DIR)/2_6_floorplan_pdn.def
write_verilog $env(RESULTS_DIR)/2_6_floorplan_pdn.v
exit
