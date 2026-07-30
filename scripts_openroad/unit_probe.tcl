source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
load_design 3_place.def 3_place.sdc "unit probe"
estimate_parasitics -placement
report_units
puts "PERIOD: [get_property [lindex [all_clocks] 0] period]"
proc worst_to {ff} { set ps [find_timing_paths -to [get_pins -quiet ${ff}/D] -path_delay max -endpoint_path_count 1 -group_path_count 50]
  if {[llength $ps]==0} {return NA}; return [get_property [lindex $ps 0] slack] }
set ff _16405__upper
set s0 [worst_to $ff]
set_clock_latency 100 [get_pins ${ff}/CLK]  ;# literal 100 — if arg=ps -> +100 slack; if arg=ns -> +100000
set s1 [worst_to $ff]
puts "PROBE arg=100: rawS0=$s0 rawS1=$s1 dSlack=[expr {$s1-$s0}]  (if ~+100 => arg is PS; if ~+100000 => arg is NS)"
set_clock_latency 0 [get_pins ${ff}/CLK]
set_clock_latency 0.1 [get_pins ${ff}/CLK]
set s2 [worst_to $ff]
puts "PROBE arg=0.1: dSlack_from_s0=[expr {$s2-$s0}]  (if ~+0.1 => PS; if ~+100 => NS)"
