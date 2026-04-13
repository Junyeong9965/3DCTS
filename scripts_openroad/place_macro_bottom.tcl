source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 2_5_floorplan_upper.v 1_synth.sdc "Starting macro placement"

source $::env(OPENROAD_SCRIPTS_DIR)/placement_utils.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/floorplan_utils.tcl

apply_tier_policy bottom -cts_safe 1

source $::env(OPENROAD_SCRIPTS_DIR)/place_macro_util.tcl

write_def     $env(RESULTS_DIR)/2_5_floorplan_macro.def
write_verilog $env(RESULTS_DIR)/2_5_floorplan_macro.v

save_image -resolution 0.1 $::env(LOG_DIR)/2_place_macro_bottom.webp 

exit
