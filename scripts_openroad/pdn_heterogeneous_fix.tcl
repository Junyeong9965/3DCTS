# Heterogeneous 3D PDN Fix
# Problem: initialize_floorplan with single PLACE_SITE creates rows for only one tier
# Solution: Use smallest common site height or skip row creation in initialize_floorplan

# Check if this is heterogeneous 3D
set is_heterogeneous 0
if {[info exists ::env(UPPER_SITE)] && [info exists ::env(BOTTOM_SITE)]} {
  if {$::env(UPPER_SITE) ne $::env(BOTTOM_SITE)} {
    set is_heterogeneous 1
    puts "INFO: Detected heterogeneous 3D platform (UPPER_SITE=$::env(UPPER_SITE), BOTTOM_SITE=$::env(BOTTOM_SITE))"
  }
}

if {$is_heterogeneous} {
  # For heterogeneous 3D, use the SMALLER site (ASAP7 0.27um < NanGate45 1.4um)
  # This creates finer-grained rows that both tiers can use
  puts "INFO: Using UPPER_SITE ($::env(UPPER_SITE)) as base site for heterogeneous 3D"
  set ::env(PLACE_SITE) $::env(UPPER_SITE)
} else {
  # Homogeneous or single-tier: keep existing logic
  if {![info exists ::env(PLACE_SITE)] || $::env(PLACE_SITE) eq ""} {
    if {[info exists ::env(BOTTOM_SITE)]} {
      set ::env(PLACE_SITE) $::env(BOTTOM_SITE)
    }
  }
}

puts "INFO: PDN will use PLACE_SITE=$::env(PLACE_SITE)"
