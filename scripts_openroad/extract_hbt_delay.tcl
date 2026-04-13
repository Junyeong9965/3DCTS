# =========================================
# HBT Delay Extraction (RC-based)
# =========================================
#
# Extract actual cross-tier connection delays from STA after RC extraction
# Usage: Called after estimate_parasitics, before useful skew optimization
#
# Returns: Average HBT delay in nanoseconds

proc extract_cross_tier_delays {output_csv} {
    puts ">>> Extracting cross-tier HBT delays from STA..."

    set db [ord::get_db]
    set block [[$db getChip] getBlock]

    set fp [open $output_csv w]
    puts $fp "driver_inst,sink_inst,net_name,wire_delay_ps,is_cross_tier,driver_tier,sink_tier"

    set cross_tier_count 0
    set cross_tier_delays [list]
    set same_tier_delays [list]

    # Iterate through clock nets
    foreach net [$block getNets] {
        set net_name [$net getName]

        # Filter for clock-related nets
        if {![string match "*clk*" $net_name] && ![string match "*CLK*" $net_name]} {
            continue
        }

        # Skip power/ground
        if {[$net getSigType] eq "POWER" || [$net getSigType] eq "GROUND"} {
            continue
        }

        # Find driver
        set driver_pin ""
        set driver_inst ""
        set driver_tier -1

        foreach iterm [$net getITerms] {
            set mterm [$iterm getMTerm]
            if {[$mterm getIoType] eq "OUTPUT"} {
                set driver_pin $iterm
                set driver_inst [$iterm getInst]
                set driver_tier [get_inst_tier $driver_inst]
                break
            }
        }

        if {$driver_inst eq ""} continue

        set driver_name [$driver_inst getName]

        # Process each sink
        foreach iterm [$net getITerms] {
            set mterm [$iterm getMTerm]
            if {[$mterm getIoType] eq "OUTPUT"} continue

            set sink_inst [$iterm getInst]
            set sink_name [$sink_inst getName]
            set sink_tier [get_inst_tier $sink_inst]

            set is_cross_tier [expr {$driver_tier != $sink_tier}]

            # Get actual wire delay from STA
            set wire_delay_ps [get_net_wire_delay $net_name $driver_name $sink_name]

            puts $fp "$driver_name,$sink_name,$net_name,$wire_delay_ps,$is_cross_tier,$driver_tier,$sink_tier"

            if {$is_cross_tier && $wire_delay_ps > 0} {
                lappend cross_tier_delays $wire_delay_ps
                incr cross_tier_count
            } elseif {!$is_cross_tier && $wire_delay_ps > 0} {
                lappend same_tier_delays $wire_delay_ps
            }
        }
    }

    close $fp

    # Calculate statistics
    if {$cross_tier_count > 0} {
        set sum 0.0
        foreach d $cross_tier_delays {
            set sum [expr {$sum + $d}]
        }
        set avg_cross_tier [expr {$sum / $cross_tier_count}]

        # Also calculate same-tier average for comparison
        set avg_same_tier 0.0
        if {[llength $same_tier_delays] > 0} {
            set sum2 0.0
            foreach d $same_tier_delays {
                set sum2 [expr {$sum2 + $d}]
            }
            set avg_same_tier [expr {$sum2 / [llength $same_tier_delays]}]
        }

        # HBT overhead = cross-tier delay - same-tier delay
        set hbt_overhead [expr {$avg_cross_tier - $avg_same_tier}]
        if {$hbt_overhead < 0} {
            set hbt_overhead 0.0
        }

        puts "  Cross-tier connections: $cross_tier_count"
        puts "  Avg cross-tier wire delay: [format %.2f $avg_cross_tier] ps"
        puts "  Avg same-tier wire delay: [format %.2f $avg_same_tier] ps"
        puts "  Estimated HBT overhead: [format %.2f $hbt_overhead] ps"

        # Return HBT overhead in ns (minimum 10ps to be conservative)
        set result_ns [expr {max(0.010, $hbt_overhead / 1000.0)}]
        puts "  Using HBT delay: [format %.4f $result_ns] ns"
        return $result_ns
    }

    puts "  No cross-tier connections found, using default 0.02 ns"
    return 0.02
}

# Helper: Get tier from instance (0=bottom, 1=upper)
proc get_inst_tier {inst} {
    # Try native getTier() first
    set tier 0
    catch {set tier [$inst getTier]}

    # Fallback to master name suffix
    if {$tier == 0} {
        set master [$inst getMaster]
        set master_name [$master getName]
        if {[string match "*_upper*" $master_name] || [string match "*_UPPER*" $master_name]} {
            set tier 1
        }
    }
    return $tier
}

# Helper: Get wire delay from STA for a specific driver->sink path
proc get_net_wire_delay {net_name driver_name sink_name} {
    # Try to get delay using STA timing arcs
    set delay_ps 0.0

    # Method 1: Use report_net delay (if available)
    catch {
        # Get the net object from STA
        set sta_net [sta::find_net $net_name]
        if {$sta_net ne ""} {
            # Get pin delays
            set delays [sta::net_pin_delays $sta_net]
            if {[llength $delays] > 0} {
                # Average of all pin delays on this net
                set sum 0.0
                set count 0
                foreach d $delays {
                    if {[string is double $d] && $d > 0} {
                        set sum [expr {$sum + $d}]
                        incr count
                    }
                }
                if {$count > 0} {
                    # Convert to ps (STA returns ns)
                    set delay_ps [expr {($sum / $count) * 1000.0}]
                }
            }
        }
    }

    # Method 2: Fallback - estimate from net bounding box
    if {$delay_ps <= 0} {
        catch {
            set db [ord::get_db]
            set block [[$db getChip] getBlock]
            set net [$block findNet $net_name]

            if {$net ne ""} {
                set bbox [$net getBBox]
                if {$bbox ne ""} {
                    set width [expr {[$bbox xMax] - [$bbox xMin]}]
                    set height [expr {[$bbox yMax] - [$bbox yMin]}]
                    # Manhattan distance in DBU
                    set dist [expr {$width + $height}]
                    # Convert to um (assuming 1000 DBU per um for ASAP7)
                    set dist_um [expr {$dist / 1000.0}]
                    # Rough estimate: ~1ps per um for clock nets
                    set delay_ps [expr {$dist_um * 1.0}]
                }
            }
        }
    }

    # Method 3: Last fallback - use reasonable default
    if {$delay_ps <= 0} {
        set delay_ps 15.0  ;# 15ps default for short clock segments
    }

    return $delay_ps
}

# Debug helper: Print cross-tier net statistics
proc debug_cross_tier_nets {} {
    puts "\n=== Cross-tier Net Debug ==="

    set db [ord::get_db]
    set block [[$db getChip] getBlock]

    set count 0
    foreach net [$block getNets] {
        set net_name [$net getName]
        if {![string match "*clk*" $net_name]} continue

        set has_upper 0
        set has_bottom 0

        foreach iterm [$net getITerms] {
            set inst [$iterm getInst]
            set tier [get_inst_tier $inst]
            if {$tier == 1} {
                set has_upper 1
            } else {
                set has_bottom 1
            }
        }

        if {$has_upper && $has_bottom} {
            incr count
            if {$count <= 5} {
                puts "  Cross-tier net: $net_name"
            }
        }
    }
    puts "  Total cross-tier clock nets: $count"
    puts "=========================\n"
}
