# dpl_cover_pass2.tcl — Sub-OpenROAD script for COVER-based DPL Pass 2
# DPL upper tier with bottom=COVER.
#
# Called by run_cover_dpl proc in cts_3d.tcl.
# Reads DEF from Pass 1 (upper=COVER session) with bottom.cover LEFs,
# runs detailed_placement (bottom=COVER → only upper cells legalized),
# writes updated upper-cell positions to a text file.
#
# Required env vars:
#   DPL_INPUT_DEF        — DEF from Pass 1
#   DPL_OUTPUT_POSITIONS — output file: "name x y" per line (upper cells only)
#   PLATFORM_DIR         — platform directory (for LEF paths)
#   DPL_TECH_LEF         — tech LEF path
#   DPL_BOTTOM_COVER_LEF — bottom.cover.lef path
#   DPL_UPPER_LEFS       — space-separated list of upper LEF paths

# Read LEFs (bottom=COVER, upper=CORE)
read_lef $::env(DPL_TECH_LEF)
read_lef $::env(DPL_BOTTOM_COVER_LEF)
foreach _lef $::env(DPL_UPPER_LEFS) {
  read_lef $_lef
}

# Read DEF from Pass 1
read_def $::env(DPL_INPUT_DEF)

# Record pre-DPL positions of upper cells
set _block [[[ord::get_db] getChip] getBlock]
set _pre_pos [dict create]
foreach _inst [$_block getInsts] {
  set _mname [[$_inst getMaster] getName]
  if {[string match "*upper*" $_mname]} {
    set _name [$_inst getName]
    dict set _pre_pos $_name [list [lindex [$_inst getOrigin] 0] [lindex [$_inst getOrigin] 1]]
  }
}
puts ">>> DPL Pass 2: [dict size $_pre_pos] upper cells before DPL"

# Run DPL (bottom=COVER → invisible to DPL, upper=CORE → legalized)
if {[catch {detailed_placement} err]} {
  puts "WARNING: DPL Pass 2 failed: $err"
  puts "INFO: Upper cells remain at Pass 1 positions."
}

# Write only CHANGED upper cell positions
set _changed 0
set _total 0
set fp [open $::env(DPL_OUTPUT_POSITIONS) w]
foreach _inst [$_block getInsts] {
  set _mname [[$_inst getMaster] getName]
  if {![string match "*upper*" $_mname]} { continue }
  set _name [$_inst getName]
  set _nx [lindex [$_inst getOrigin] 0]
  set _ny [lindex [$_inst getOrigin] 1]
  incr _total
  if {[dict exists $_pre_pos $_name]} {
    set _old [dict get $_pre_pos $_name]
    if {$_nx != [lindex $_old 0] || $_ny != [lindex $_old 1]} {
      puts $fp "$_name $_nx $_ny"
      incr _changed
    }
  } else {
    # New cell not in pre-snapshot — write it
    puts $fp "$_name $_nx $_ny"
    incr _changed
  }
}
close $fp
puts ">>> DPL Pass 2: $_total upper cells, $_changed positions changed"
puts ">>> DPL Pass 2: output written to $::env(DPL_OUTPUT_POSITIONS)"
