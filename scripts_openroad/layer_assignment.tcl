# =========================================
# Clock Layer Assignment for 3D CTS (V17)
# =========================================
#
# This script assigns clock nets to optimal metal layers based on criticality.
# Approach (- Simplified working version):
#   - Extract timing slack from STA for all clock endpoints
#   - If majority are critical → use higher clock layers (M3-M5)
#   - If majority are non-critical → use lower clock layers (M2-M4)
#   - OpenROAD limitation: Can't set per-net layer, only global clock layer range
#
# Usage: Called from cts_3d.tcl when ENABLE_LAYER_ASSIGNMENT=1

namespace eval layer_assign {
    variable script_dir [file dirname [info script]]
}

# Extract clock endpoint slack from STA
proc extract_clock_endpoint_slack {output_csv} {
    puts ">>> Extracting clock endpoint slack from STA..."

    set fp [open $output_csv w]
    puts $fp "endpoint,slack_setup_ps,slack_hold_ps,is_critical"

    set endpoint_count 0
    set critical_count 0
    set slack_threshold -50.0  ;# Critical if slack < -50ps

    # Get all clock endpoints (FFs)
    set db [ord::get_db]
    set block [[$db getChip] getBlock]

    foreach inst [$block getInsts] {
        set master [$inst getMaster]
        if {![$master isSequential]} {
            continue  ;# Skip non-FF instances
        }

        set inst_name [$inst getName]

        # Find clock pin
        foreach iterm [$inst getITerms] {
            set mterm [$iterm getMTerm]
            set pin_name [$mterm getName]

            # Check if this is a clock pin
            if {[string match "*CLK*" [string toupper $pin_name]] || \
                [string match "*CK" [string toupper $pin_name]]} {

                # Get slack from STA
                set slack_setup 0.0
                set slack_hold 0.0

                if {[catch {
                    set pin_path "${inst_name}/${pin_name}"
                    set pin_obj [get_pins $pin_path]
                    if {$pin_obj ne ""} {
                        set sta_pin [sta::get_pin $pin_obj]
                        set vertex [sta::pin_vertex $sta_pin]

                        # Get setup slack (max path)
                        set slack_max_sec [sta::Vertex_slack $vertex "max"]
                        set slack_setup [expr {$slack_max_sec * 1e12}]  ;# Convert to ps

                        # Get hold slack (min path)
                        set slack_min_sec [sta::Vertex_slack $vertex "min"]
                        set slack_hold [expr {$slack_min_sec * 1e12}]  ;# Convert to ps
                    }
                }]} {
                    # If STA query fails, use default
                    set slack_setup 0.0
                    set slack_hold 0.0
                }

                # Classify as critical or not
                set is_critical 0
                if {$slack_setup < $slack_threshold} {
                    set is_critical 1
                    incr critical_count
                }

                puts $fp "${inst_name}/${pin_name},$slack_setup,$slack_hold,$is_critical"
                incr endpoint_count
                break  ;# Only one clock pin per FF
            }
        }
    }

    close $fp
    puts "  Extracted $endpoint_count clock endpoints"
    puts "  Critical endpoints (slack < ${slack_threshold} ps): $critical_count"

    return [list $endpoint_count $critical_count]
}

# Apply global clock layer assignment
proc apply_clock_layer_assignment {endpoint_count critical_count} {
    puts ">>> Applying clock layer assignment..."

    if {$endpoint_count == 0} {
        puts "  WARNING: No clock endpoints found, skipping layer assignment"
        return
    }

    # Calculate criticality ratio
    set critical_ratio [expr {double($critical_count) / double($endpoint_count)}]
    puts "  Criticality ratio: [format "%.1f%%" [expr {$critical_ratio * 100}]] ($critical_count / $endpoint_count)"

    # Determine clock layer range based on criticality
    # High criticality → use higher layers (lower RC)
    # Low criticality → use lower layers (save area)

    set min_clk_layer 2  ;# M2 (default)
    set max_clk_layer 5  ;# M5 (default)

    if {$critical_ratio > 0.5} {
        # Majority critical → prefer higher layers
        set min_clk_layer 3  ;# M3
        set max_clk_layer 5  ;# M5
        puts "  Strategy: HIGH CRITICALITY - Using layers M3-M5 for clock routing"
    } elseif {$critical_ratio > 0.2} {
        # Moderate criticality → balanced
        set min_clk_layer 2  ;# M2
        set max_clk_layer 4  ;# M4
        puts "  Strategy: MODERATE CRITICALITY - Using layers M2-M4 for clock routing"
    } else {
        # Low criticality → allow lower layers
        set min_clk_layer 2  ;# M2
        set max_clk_layer 3  ;# M3
        puts "  Strategy: LOW CRITICALITY - Using layers M2-M3 for clock routing"
    }

    # Apply to global router (OpenROAD API)
    # Note: This sets global clock layer range, not per-net
    if {[catch {
        set_routing_layers -min_layer [get_routing_layer_name $min_clk_layer] \
                          -max_layer [get_routing_layer_name $max_clk_layer] \
                          -clock
        puts "  ✓ Clock layer range set: M${min_clk_layer}-M${max_clk_layer}"
    } err]} {
        # If OpenROAD API fails, try alternative (GRT-specific)
        puts "  WARNING: set_routing_layers failed, trying GRT API: $err"
        if {[catch {
            # Use GRT (Global Router) API directly
            grt::set_min_layer_for_clock $min_clk_layer
            grt::set_max_layer_for_clock $max_clk_layer
            puts "  ✓ Clock layer range set via GRT API: M${min_clk_layer}-M${max_clk_layer}"
        } err2]} {
            puts "  ERROR: Failed to set clock layer range: $err2"
            puts "  NOTE: Layer assignment may not be applied during routing"
        }
    }
}

# Helper: Get routing layer name from index
proc get_routing_layer_name {layer_idx} {
    # Map layer index to layer name
    # This is platform-specific - adjust for your tech
    set layer_names {
        "invalid"
        "M1"
        "M2"
        "M3"
        "M4"
        "M5"
        "M6"
        "M7"
        "M8"
        "M9"
        "M10"
    }

    if {$layer_idx >= 0 && $layer_idx < [llength $layer_names]} {
        return [lindex $layer_names $layer_idx]
    }
    return "M${layer_idx}"
}

# Main entry point
proc run_layer_assignment {} {
    global env

    puts "\n=========================================="
    puts "CLOCK LAYER ASSIGNMENT (V17)"
    puts "=========================================="

    set results_dir $env(RESULTS_DIR)
    set slack_file "$results_dir/clock_endpoint_slack.csv"

    # Step 1: Extract clock endpoint slack
    set stats [extract_clock_endpoint_slack $slack_file]
    set endpoint_count [lindex $stats 0]
    set critical_count [lindex $stats 1]

    # Step 2: Apply global clock layer assignment
    apply_clock_layer_assignment $endpoint_count $critical_count

    puts "=========================================="
    puts "LAYER ASSIGNMENT COMPLETE"
    puts "=========================================="
}

# Check if layer assignment is enabled
if {[info exists env(ENABLE_LAYER_ASSIGNMENT)] && $env(ENABLE_LAYER_ASSIGNMENT)} {
    run_layer_assignment
} else {
    puts "Layer assignment disabled (set ENABLE_LAYER_ASSIGNMENT=1 to enable)"
}
