# =========================================
# V43: Pre-Global-Route Hook — Bidirectional Clock Layer Assignment
# =========================================
# Sourced via PRE_GLOBAL_ROUTE_TCL before global_route.
# Reads LP-based subnet classification (written by CTS step)
# and applies per-net layer ranges for bidirectional routing:
#   "hold"  -> M5-M7 (low R -> less delay -> protect hold)
#   "setup" -> M2-M3 (high R -> more delay -> deliver useful skew)

if {[info exists ::env(CTS_ENABLE_LAYER_ASSIGNMENT)] && $::env(CTS_ENABLE_LAYER_ASSIGNMENT)} {
  # fix: Skip layer assignment for heterogeneous 3D designs (ASAP7 upper + NG45 lower).
  # Root cause of GRT-0183 on ng45 designs:
  #   hold-critical nets get min_layer=M5 (M5-M7). ASAP7 cell pins are at M1-M3.
  #   3D maze router cannot navigate from M1-M3 pins to M5 in the combined
  #   ASAP7+NG45 layer graph -> priority queue exhausted -> heap underflow.
  #   M2-M3 setup nets are unaffected (min_layer=M2 reachable from M1 pins).
  #   Disabling layer assignment for ng45 avoids GRT-0183 with no CTS change.
  if {[info exists ::env(HETEROGENEOUS_3D)] && $::env(HETEROGENEOUS_3D) == 1} {
    puts "layer assignment: SKIP for heterogeneous 3D (HETEROGENEOUS_3D=1)"
    puts "  Reason: GRT-0183 -- M5-M7 hold nets fail pin access in NG45+ASAP7 layer stack."
    puts "  Workaround: no per-net layer restriction; GRT uses default layer assignment."
  } else {
    # Source the layer assignment procs
    source $::env(OPENROAD_SCRIPTS_DIR)/clock_layer_assignment_v43.tcl

    # Build path to the subnet classification file
    set subnet_file "$::env(RESULTS_DIR)/v43_layer_assignment.txt"

    # Apply per-net layer ranges
    apply_lp_based_layers $subnet_file
  }
} else {
  puts "layer assignment disabled (CTS_ENABLE_LAYER_ASSIGNMENT not set)"
}
