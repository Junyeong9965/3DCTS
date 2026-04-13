# This script was written and developed by Zhiyu Zheng at Fudan University; however, the underlying
# commands and reports are copyrighted by Cadence. We thank Cadence for
# granting permission to share our research to help promote and foster the next
# generation of innovators.
# ============== Final and extract metrics (restoreDesign → extract_report -postRoute) ==============
# ---- Common setup ----
source $::env(CADENCE_SCRIPTS_DIR)/utils.tcl
source $::env(CADENCE_SCRIPTS_DIR)/lib_setup.tcl
source $::env(CADENCE_SCRIPTS_DIR)/design_setup.tcl

set LOG_DIR       [_get LOG_DIR]
set RESULTS_DIR   [_get RESULTS_DIR]
set REPORTS_DIR   [_get REPORTS_DIR]
set OBJECTS_DIR   [_get OBJECTS_DIR]

# ---- Use ONLY 5_route.v / 5_route.sdc for init globals (no fallback) ----
set V_IN   [file join $RESULTS_DIR "5_route.v"]
set sdc  [file join $RESULTS_DIR "5_route.sdc"]
set DEF_IN [file join $RESULTS_DIR "5_route.def"]

if {![file exists $V_IN]}  { puts "ERROR: Missing netlist: $V_IN";  exit 1 }
if {![file exists $sdc]} { puts "ERROR: Missing SDC:     $sdc";  exit 1 }

# SDC unit fix: ASAP7 Liberty uses time_unit=1ps → OpenROAD writes SDC in ps.
# Innovus reads SDC as ns. Convert ps→ns for PROCESS=7 (ASAP7) platforms.
set _process [_get PROCESS 45]
if {$_process == 7} {
  set _sdc_orig $sdc
  set _sdc_ns [file join $RESULTS_DIR "5_route_ns.sdc"]
  puts "INFO: ASAP7 detected (PROCESS=7). Converting SDC from ps to ns: $_sdc_ns"
  set _fin [open $_sdc_orig r]
  set _fout [open $_sdc_ns w]
  while {[gets $_fin _line] >= 0} {
    if {[regexp {^(create_clock|set_input_delay|set_output_delay|set_clock_uncertainty|set_clock_latency|set_max_delay|set_min_delay)} $_line]} {
      # Escape existing Tcl brackets so subst only evaluates the new [format ...] expressions
      set _line [string map {[ \\[ ] \\]} $_line]
      regsub -all {([-]?[0-9]+\.?[0-9]*)} $_line {[format "%.4f" [expr {\1 / 1000.0}]]} _line
      set _line [subst $_line]
    }
    puts $_fout $_line
  }
  close $_fin
  close $_fout
  set sdc $_sdc_ns
}

# init_* globals required by some Innovus builds even for restoreDesign
if {![info exists lefs] || $lefs eq ""} {
  puts "WARN: 'lefs' was not exported by lib_setup.tcl; continue with netlist/SDC only."
}

source $::env(CADENCE_SCRIPTS_DIR)/mmmc_setup.tcl

set init_lef_file $lefs
set init_mmmc_file ""
set init_design_settop 1
set init_top_cell $DESIGN
set init_verilog   $V_IN
set init_design_netlisttype "Verilog"

# ---- Restore routed DB (no DEF fallback) ----
# set ENC_PRIMARY   [file join $OBJECTS_DIR "_postRoute.enc"]
# set ENC_FILE [file join $OBJECTS_DIR "${DESIGN}_postRoute.enc.dat"]
# if {[file exists $ENC_FILE]} {
#   puts "INFO: restoreDesign $ENC_FILE $DESIGN"
#   restoreDesign $ENC_FILE $DESIGN
# } else {
#   puts "Missing routed ENC file: $ENC_FILE"
puts "INFO: restoreDesign $DESIGN from def, verilog"
init_design -setup {WC_VIEW} -hold {BC_VIEW}

setAnalysisMode -reset
set_power_analysis_mode -leakage_power_view WC_VIEW -dynamic_power_view WC_VIEW
set_interactive_constraint_modes {CON}
setAnalysisMode -analysisType onChipVariation -cppr both
set_analysis_view -setup {WC_VIEW} -hold {BC_VIEW} -leakage WC_VIEW -dynamic WC_VIEW

setOptMode -powerEffort low -leakageToDynamicRatio 0.5
setGenerateViaMode -auto true
generateVias

# basic path groups
createBasicPathGroups -expanded

defIn $DEF_IN
# }
puts "READ DEF: $DEF_IN"
set_default_switching_activity -seq_activity 0.2

# Analysis knobs

setMultiCpuUsage -localCpu [_get NUM_CORES 16]
fit
dumpToGIF $LOG_DIR/6_final.png
# Newer Voltus API hint (do not error if views absent)

# Run unified extractor directly into LOG_DIR
file mkdir [file join $LOG_DIR timingReports]

set EXTRACT_TCL [file join $::env(CADENCE_SCRIPTS_DIR) extract_report.tcl]
if {![file exists $EXTRACT_TCL]} { puts "ERROR: Cannot find $EXTRACT_TCL"; exit 1 }
source $EXTRACT_TCL

set CSV_PATH [file join $LOG_DIR "final_metrics.csv"]
set SUMMARY  [file join $LOG_DIR "final_summary.txt"]

set csv_line [extract_report -postRoute \
                            -outdir $LOG_DIR \
                            -write_csv $CSV_PATH \
                            -write_summary $SUMMARY]

catch { file mkdir [file join $LOG_DIR final] }
catch { dumpPictures -dir [file join $LOG_DIR final] -prefix final }

puts "INFO: Final metrics CSV -> $CSV_PATH"
puts "INFO: Final summary     -> $SUMMARY"
puts "INFO: timingReports/, power_Final.rpt, drc.rpt, fep.rpt are under $LOG_DIR."

set VISUALIZE_FINAL [_get VISUALIZE_FINAL "0"]
if {$VISUALIZE_FINAL eq "1"} {
  puts "INFO: Pausing for Final Design Visualization. Type 'resume' to continue or exit manually."
  win
  suspend
}

exit
