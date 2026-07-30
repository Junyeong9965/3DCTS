set place_density [calculate_placement_density]
# puts "Running global placement with density: $place_density"
# set global_placement_args ""
# log_cmd global_placement -density $place_density \
#         -pad_left $::env(CELL_PAD_IN_SITES_GLOBAL_PLACEMENT) \
#         -pad_right $::env(CELL_PAD_IN_SITES_GLOBAL_PLACEMENT) \
#         {*}$global_placement_args
if { [find_macros] != "" } {
  if { ![env_var_exists_and_non_empty RTLMP_RPT_DIR] } {
    set ::env(RTLMP_RPT_DIR) "$::env(OBJECTS_DIR)/rtlmp"
  }

  lassign $::env(MACRO_PLACE_HALO) halo_x halo_y
  set halo_max [expr max($halo_x, $halo_y)]

  set additional_rtlmp_args ""
  append_env_var additional_rtlmp_args RTLMP_MAX_LEVEL -max_num_level 1
  append_env_var additional_rtlmp_args RTLMP_MAX_INST -max_num_inst 1
  append_env_var additional_rtlmp_args RTLMP_MIN_INST -min_num_inst 1
  append_env_var additional_rtlmp_args RTLMP_MAX_MACRO -max_num_macro 1
  append_env_var additional_rtlmp_args RTLMP_MIN_MACRO -min_num_macro 1
  append additional_rtlmp_args " -halo_width $halo_x"
  append additional_rtlmp_args " -halo_height $halo_y"
#   append_env_var additional_rtlmp_args RTLMP_MIN_AR -min_ar 1
#   append_env_var additional_rtlmp_args RTLMP_AREA_WT -area_weight 1
#   append_env_var additional_rtlmp_args RTLMP_WIRELENGTH_WT -wirelength_weight 1
#   append_env_var additional_rtlmp_args RTLMP_OUTLINE_WT -outline_weight 1
#   append_env_var additional_rtlmp_args RTLMP_BOUNDARY_WT -boundary_weight 1
#   append_env_var additional_rtlmp_args RTLMP_NOTCH_WT -notch_weight 1
  append_env_var additional_rtlmp_args RTLMP_RPT_DIR -report_directory 1
#   append_env_var additional_rtlmp_args RTLMP_FENCE_LX -fence_lx 1
#   append_env_var additional_rtlmp_args RTLMP_FENCE_LY -fence_ly 1
#   append_env_var additional_rtlmp_args RTLMP_FENCE_UX -fence_ux 1
#   append_env_var additional_rtlmp_args RTLMP_FENCE_UY -fence_uy 1

  append additional_rtlmp_args " -target_util $place_density"

  # if { $::env(RTLMP_DATA_FLOW_DRIVEN) } {
  #   append additional_rtlmp_args " -data_flow_driven"
  # }

  set all_args $additional_rtlmp_args

  if { [env_var_exists_and_non_empty RTLMP_ARGS] } {
    set all_args $::env(RTLMP_ARGS)
  }

  log_cmd rtl_macro_placer {*}$all_args
  set block [ord::get_db_block]
  foreach inst [$block getInsts] {
    if { [[$inst getMaster] getType] == "BLOCK" } {
      $inst setPlacementStatus "FIRM"
    }
  }
} else {
  puts "No macros found: Skipping macro_placement"
}
