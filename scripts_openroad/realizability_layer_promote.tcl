# Realizability layer promotion — general, cap-driven, no design/net hardcoding.
#
# MEASURED root cause (swerv): a clock leaf net that must span far, high-cap sinks (SRAM clk pins,
# 25fF each, 65um apart) gets routed 68% on M2/M3 (0.036-0.046 ohm/um) -> ~5kohm wire R -> post-route
# clock latency/slew blowup (Elmore R*C). The C-isolation guard alone can't fix R (span is fixed by
# where the sinks sit). The general, low-risk lever is to route these HIGH-R*C clock nets on LOW-R
# upper metal (M5/M6: 0.012-0.019 ohm/um, 3-4x lower) so the same span costs 3-4x less R.
#
# This is realizability-aware, NOT macro-aware: the trigger is the net's own predicted implementation
# risk = (total sink cap) x (geometric span), estimated purely from the clock netlist + placement.
# Sinks may be SRAMs, IO pads, clock-gate clusters, analog blocks — anything heavy. FF-only clock nets
# never trip the threshold -> no-op.
#
# Hook: PRE_GLOBAL_ROUTE_TCL (runs just before global_route). Env gate CTS_ENABLE_RLAYER (default 1).

proc realizability_layer_promote {} {
  if {[info exists ::env(CTS_ENABLE_RLAYER)] && $::env(CTS_ENABLE_RLAYER) == 0} {
    puts "\[RLAYER\] disabled (CTS_ENABLE_RLAYER=0)"
    return
  }
  set db [ord::get_db]
  set block [[$db getChip] getBlock]
  set dbu [$block getDbUnitsPerMicron]

  # Thresholds (relative + physical floor; overridable, not per-design).
  #   cap:  net drives >= this total pin cap -> heavy. 20fF ~ a couple SRAM pins.
  #   span: net half-perimeter >= this (um). Short heavy nets are already low-R.
  set cap_thresh_ff 20.0
  set span_thresh_um 15.0
  if {[info exists ::env(RLAYER_CAP_FF)]}  { set cap_thresh_ff  $::env(RLAYER_CAP_FF) }
  if {[info exists ::env(RLAYER_SPAN_UM)]} { set span_thresh_um $::env(RLAYER_SPAN_UM) }
  set min_layer "M5"
  set max_layer "M7"
  if {[info exists ::env(RLAYER_MIN)]} { set min_layer $::env(RLAYER_MIN) }
  if {[info exists ::env(RLAYER_MAX)]} { set max_layer $::env(RLAYER_MAX) }

  # Verify the target layers exist (heterogeneous/thin stacks may lack M7).
  set tech [$db getTech]
  if {[$tech findLayer $min_layer] == "NULL" || $tech eq ""} {
    puts "\[RLAYER\] layer $min_layer not in tech; skipping"
    return
  }

  set n_promoted 0
  set n_clock 0
  foreach net [$block getNets] {
    set sig [$net getSigType]
    if {$sig ne "CLOCK"} { continue }
    incr n_clock
    # total input-pin capacitance + bbox span over the net's sink iterms
    set total_cap 0.0
    set xmin 1e18; set ymin 1e18; set xmax -1e18; set ymax -1e18
    set nsink 0
    foreach iterm [$net getITerms] {
      if {![$iterm isInputSignal]} { continue }
      # pin cap via liberty
      set mt [$iterm getMTerm]
      set inst [$iterm getInst]
      # geometric location
      set bbox [$iterm getBBox]
      set cx [expr {([$bbox xMin] + [$bbox xMax]) / 2.0}]
      set cy [expr {([$bbox yMin] + [$bbox yMax]) / 2.0}]
      if {$cx < $xmin} {set xmin $cx}; if {$cx > $xmax} {set xmax $cx}
      if {$cy < $ymin} {set ymin $cy}; if {$cy > $ymax} {set ymax $cy}
      incr nsink
      # cap from STA
      if {[catch {
        set pin [sta::get_pins -quiet [get_full_name $iterm]]
      }]} { set pin "" }
    }
    if {$nsink < 1} { continue }
    set span_um [expr {(($xmax - $xmin) + ($ymax - $ymin)) / double($dbu)}]

    # Heavy-sink detection: a clock net whose input pins include block macros
    # (isBlock: SRAMs, hard IP) carries the high pin cap (~25fF each vs ~1.5fF FF)
    # that drives the R*C blowup. isBlock is the robust, general, unit-free proxy
    # for "high-cap clock sink" (SRAM/IP); FF-only nets have zero block pins ->
    # no-op. (Pin-cap-in-Farads via Liberty was tried but the per-net STA lookups
    # are fragile/slow; isBlock is the same signal without the fragility.)
    set n_heavy 0
    foreach iterm [$net getITerms] {
      if {![$iterm isInputSignal]} { continue }
      if {[[[$iterm getInst] getMaster] isBlock]} { incr n_heavy }
    }
    # cap proxy: 25fF per heavy pin (SRAM clk pin scale).
    set cap_ff [expr {$n_heavy * 25.0}]

    if {$cap_ff >= $cap_thresh_ff && $span_um >= $span_thresh_um} {
      if {![catch {
        set_clock_subnet_layers -net [$net getName] -min_layer $min_layer -max_layer $max_layer
      } err]} {
        incr n_promoted
        if {$n_promoted <= 20} {
          puts [format "\[RLAYER\] promote %s -> %s-%s (cap=%.1ffF span=%.1fum sinks=%d)" \
                [$net getName] $min_layer $max_layer $cap_ff $span_um $nsink]
        }
      }
    }
  }
  puts "\[RLAYER\] scanned $n_clock clock nets, promoted $n_promoted high-R*C nets to $min_layer-$max_layer (cap>=${cap_thresh_ff}fF, span>=${span_thresh_um}um)"
}

realizability_layer_promote
