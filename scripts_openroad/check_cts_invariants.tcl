# =============================================================================
# check_cts_invariants.tcl  (V73)
#
# Post-CTS clock-connectivity invariants.  Sourced right after CTS, on the
# in-memory ODB, with the clock propagated.  The C++ side (TritonCTS
# checkClockNetInvariants, CTS-0725/0726) covers (1) and (2) structurally; this
# script re-checks them from Tcl and adds (3), which only STA can answer.
#
#   (1) clock buffers with an unconnected input pin  == 0
#   (2) every macro CLK pin traces to a clock source (BTerm) through drivers
#   (3) every macro CLK pin has an STA-propagated clock
#
# Why (3) is not redundant with (2): a pin can be structurally connected and still
# have no clock in STA (e.g. the arc is disabled, or the source is not an SDC clock).
# STA clock absence is what silently removes setup/hold checks on those macros and
# collapses their vectorless internal power.
#
# Fatal unless CTS_STRICT_CLK_INVARIANTS=0.
# =============================================================================
proc check_cts_invariants {} {
  set strict 1
  if {[info exists ::env(CTS_STRICT_CLK_INVARIANTS)]} {
    set strict $::env(CTS_STRICT_CLK_INVARIANTS)
  }
  set db [ord::get_db]
  set block [[$db getChip] getBlock]

  # ---- build net -> driver-instance map once ----
  array unset drv
  foreach net [$block getNets] {
    foreach it [$net getITerms] {
      if {[[$it getMTerm] getIoType] eq "OUTPUT"} {
        set drv([$net getName]) [$it getInst]
        break
      }
    }
  }
  # nets that reach a top-level port
  array unset hasbterm
  foreach net [$block getNets] {
    if {[llength [$net getBTerms]] > 0} { set hasbterm([$net getName]) 1 }
  }

  # ---- (1) clock buffers with unconnected input ----
  set dangling {}
  foreach inst [$block getInsts] {
    set nm [$inst getName]
    # CTS-created clock cells: clkbuf_*, and per-tier prefixed t<N>_clkbuf_*
    if {![regexp {^(t\d+_)?clkbuf} $nm]} { continue }
    set driven 0
    foreach it [$inst getITerms] {
      if {[[$it getMTerm] getIoType] eq "INPUT" && [$it getNet] ne "NULL"} {
        set driven 1; break
      }
    }
    if {!$driven} { lappend dangling $nm }
  }

  # ---- (2)+(3) macro CLK pins ----
  set macro_pins 0
  set unrooted {}
  set noclock {}
  foreach inst [$block getInsts] {
    set mname [[$inst getMaster] getName]
    if {![$inst isBlock] && ![string match -nocase "*fakeram*" $mname] \
        && ![string match -nocase "sram_*" $mname]} { continue }
    foreach it [$inst getITerms] {
      set mt [$it getMTerm]
      if {[$mt getSigType] ne "CLOCK"} { continue }
      incr macro_pins
      set pinname "[$inst getName]/[$mt getName]"

      # (2) structural trace up through drivers to a BTerm
      set net [$it getNet]
      set rooted 0
      for {set hop 0} {$hop < 64} {incr hop} {
        if {$net eq "NULL" || $net eq ""} { break }
        set nn [$net getName]
        if {[info exists hasbterm($nn)]} { set rooted 1; break }
        if {![info exists drv($nn)]} { break }
        set d $drv($nn)
        set up "NULL"
        foreach dit [$d getITerms] {
          if {[[$dit getMTerm] getIoType] eq "INPUT" && [$dit getNet] ne "NULL"} {
            set up [$dit getNet]; break
          }
        }
        set net $up
      }
      if {!$rooted} { lappend unrooted $pinname }

      # (3) STA: is a clock propagated to this pin?
      set p [get_pin -quiet $pinname]
      if {$p eq ""} { lappend noclock "$pinname (no STA pin)" ; continue }
      set clks ""
      catch {set clks [get_property $p clocks]}
      if {[llength $clks] == 0} { lappend noclock $pinname }
    }
  }

  set nd [llength $dangling]; set nu [llength $unrooted]; set nc [llength $noclock]
  puts [format "INVARIANT clock_buffers_unconnected_input = %d" $nd]
  puts [format "INVARIANT macro_clk_pins = %d  untraceable_to_source = %d" $macro_pins $nu]
  puts [format "INVARIANT macro_clk_pins_without_sta_clock = %d" $nc]

  if {$nd || $nu || $nc} {
    foreach x [lrange $dangling 0 9] { puts "  VIOLATION dangling clock buffer: $x" }
    foreach x [lrange $unrooted 0 9] { puts "  VIOLATION macro CLK pin not traceable to source: $x" }
    foreach x [lrange $noclock  0 9] { puts "  VIOLATION macro CLK pin has no STA clock: $x" }
    if {$strict} {
      error "CTS invariant check FAILED: dangling=$nd untraceable=$nu no_sta_clock=$nc \
(set CTS_STRICT_CLK_INVARIANTS=0 to downgrade to a warning)"
    } else {
      puts "WARNING: CTS invariant check failed (non-strict mode), continuing."
    }
  } else {
    puts "INVARIANT check PASSED (all 3 invariants hold)."
  }
}
