source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl

load_design 2_3_floorplan_3d.v 1_synth.sdc "Starting IO assignment"

source $::env(OPENROAD_SCRIPTS_DIR)/placement_utils.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/floorplan_utils.tcl

source $::env(OPENROAD_SCRIPTS_DIR)/io_place.tcl

write_def     $env(RESULTS_DIR)/2_4_floorplan_io.def
write_verilog $env(RESULTS_DIR)/2_4_floorplan_io.v
exit
