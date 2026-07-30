# Open a pre-built layout in the OpenROAD GUI. Viewing only -- no timing.
#
# Reads the F2F-merged LEFs named by the design config plus the chosen DEF
# (post-CTS 4_cts, or routed 6_final). Liberty and SDC are not read: cell
# geometry and routing come from the LEF/DEF alone.
#
# Invoked by the `ord-open` make target; the wrapper is `open.sh`, which
# decompresses the DEF and points OPEN_DEF at it.

foreach lef $::env(LEF_FILES) {
  read_lef $lef
}
read_def $::env(OPEN_DEF)

puts "==== layout loaded: $::env(PLATFORM)/$::env(DESIGN_NICKNAME) ($::env(OPEN_DEF)) ===="
puts ">>> close the viewer window to exit"

gui::show
