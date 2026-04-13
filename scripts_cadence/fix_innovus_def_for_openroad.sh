#!/bin/bash
# fix_innovus_def_for_openroad.sh
# Post-process Innovus DEF for OpenROAD compatibility (ODB-0421 fix).
#
# Fix 1: Remove NanoRoute generated vias from NONDEFAULTRULES.
#   Root cause: Innovus CTS NDR references generated vias (NR_VIA*, M2_M1_VH,
#   TECH_RULE_*, M2_m_M1_m_*, M3add_M2add_*) not in tech LEF.
#   OpenROAD definNonDefaultRule::via() -> findVia() fails -> _errors++ -> ODB-0421.
#
# Fix 2: Remove VIRTUAL keyword from NETS routing.
#   Root cause: Innovus NanoRoute uses VIRTUAL for zero-length routing segments.
#   OpenROAD ODB-0126: "VIRTUAL in net's routing is unsupported" -> DEFPARS-6010 -> ODB-0421.
#
# Usage: bash fix_innovus_def_for_openroad.sh <input.def> [output.def]
#        If output omitted, writes to <input>.mod.def

set -euo pipefail

INPUT="$1"
OUTPUT="${2:-${INPUT%.def}.mod.def}"

if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: $INPUT not found" >&2
    exit 1
fi

awk '
# Fix 1: Whitelist approach — keep only tech LEF vias in NONDEFAULTRULES.
# Tech LEF vias: VIA12, VIA56_m, VIA_M1m_M2add, hb_layer_0, etc.
# All other "+ VIA ..." lines are NanoRoute generated (Vx_, M2add_, NR_VIA, TECH_RULE_, etc.)
/^NONDEFAULTRULES/,/^END NONDEFAULTRULES/ {
    if ($0 ~ /\+ VIA / && $0 !~ /\+ VIA (VIA[0-9]|VIA_M|hb_layer|via[0-9])/) next
}
# Fix 2: Remove VIRTUAL keyword from routing (replace "VIRTUAL" with empty)
{ gsub(/ VIRTUAL /, " "); print }
' "$INPUT" > "$OUTPUT"

# Report
REMOVED=$(( $(wc -l < "$INPUT") - $(wc -l < "$OUTPUT") ))
VIRTUAL_ORIG=$(grep -c "VIRTUAL" "$INPUT" || true)
VIRTUAL_LEFT=$(grep -c "VIRTUAL" "$OUTPUT" || true)
VIRTUAL_FIXED=$(( VIRTUAL_ORIG - VIRTUAL_LEFT ))
echo "Removed $REMOVED NDR via lines, fixed $VIRTUAL_FIXED/$VIRTUAL_ORIG VIRTUAL keywords"
echo "Output: $OUTPUT"
