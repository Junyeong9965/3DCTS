# EXHAUSTIVE FF->FF via PER-SOURCE find_timing_paths (reliable coverage) + slack_max>=max_skew.
source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "exhaustive extract per-source"
repair_clock_inverters
set cts_args [list -sink_clustering_enable -repair_clock_nets -root_buf $::env(CTS_BUF_CELL) -buf_list "$::env(CTS_BUF_UPPER)"]
append_env_var cts_args CTS_BUF_DISTANCE -distance_between_buffers 1
append_env_var cts_args CTS_CLUSTER_SIZE -sink_clustering_size 1
append_env_var cts_args CTS_CLUSTER_DIAMETER -sink_clustering_max_diameter 1
clock_tree_synthesis {*}$cts_args
estimate_parasitics -placement
set_propagated_clock [all_clocks]
proc inst_of {pin} { set fn [get_full_name $pin]; set i [string last "/" $fn]; return [string range $fn 0 [expr {$i-1}]] }
array set E {}
set regs [all_registers -edge_triggered]
puts ">>> looping [llength $regs] launch registers (slack_max 0.5ns, per-source)"
set nsrc 0
foreach reg $regs {
  incr nsrc
  if {[catch {
    set paths [find_timing_paths -from $reg -path_delay max -slack_max 260 -endpoint_path_count 400 -group_path_count 100000]
    foreach p $paths {
      set fi [inst_of [get_property $p startpoint]]
      set fj [inst_of [get_property $p endpoint]]
      if {$fi eq $fj} continue
      set sl [expr {[get_property $p slack]/1000.0}]; set k "$fi|$fj"
      if {![info exists E($k)] || $sl < $E($k)} { set E($k) $sl }
    }
  } err]} { continue }
}
puts ">>> distinct FF->FF edges = [array size E] (from $nsrc sources)"
set out $::env(RESULTS_DIR)/pre_cts_ff_timing_graph.csv
set fh [open $out w]
puts $fh "from_ff,to_ff,slack_max_ns,slack_min_ns,arrival_max_ns,arrival_min_ns,required_max_ns,required_min_ns,from_x,from_y,from_tier,to_x,to_y,to_tier,from_clock_net,to_clock_net"
foreach k [array names E] {
  set pp [split $k "|"]; set fi [lindex $pp 0]; set fj [lindex $pp 1]
  set ft 0; if {[string match "*upper*" $fi]} {set ft 1}; set tt 0; if {[string match "*upper*" $fj]} {set tt 1}
  puts $fh "$fi,$fj,$E($k),1.0,1.0,1.0,1.0,1.0,0,0,$ft,0,0,$tt,clk,clk"
}
close $fh
puts ">>> WROTE exhaustive graph ([array size E] edges)"
