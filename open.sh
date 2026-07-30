#!/bin/bash
# View a pre-built 3D layout in the OpenROAD GUI. Viewing only -- no timing.
#
#   bash open.sh <platform> <design> [final|cts]
#
#     platform  : asap7_3D | asap7_nangate45_3D
#     design    : aes | ibex | jpeg | swerv_wrapper | ariane133
#     final|cts : final (default) = routed layout (6_final);
#                 cts = post-CTS clock tree before routing (4_cts).
#
# Needs an X display (SSH X-forwarding is enough). The layouts are shipped
# gzipped under prebuilt/<platform>/<design>/; this script decompresses one and
# opens it with the F2F-merged LEFs named by designs/<platform>/<design>/config.mk.
# Any OpenROAD build can open them -- the CTS patch is not required to view a layout.
set -e

PLATFORM="$1"
DESIGN="$2"
STAGE="${3:-final}"

if [[ -z "$PLATFORM" || -z "$DESIGN" ]]; then
  echo "usage: bash open.sh <platform> <design> [final|cts]" >&2
  echo "  platform: asap7_3D | asap7_nangate45_3D" >&2
  echo "  design  : aes | ibex | jpeg | swerv_wrapper | ariane133" >&2
  exit 1
fi

case "$STAGE" in
  final) STEM="6_final" ;;
  cts)   STEM="4_cts" ;;
  *) echo "error: stage must be 'final' or 'cts' (got '$STAGE')" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEFGZ="prebuilt/${PLATFORM}/${DESIGN}/${STEM}.def.gz"
if [[ ! -f "$DEFGZ" ]]; then
  echo "error: $DEFGZ not found" >&2
  exit 1
fi

source ./env.sh
source ./configs/cts_params.env

export PLATFORM
export DESIGN_NICKNAME="$DESIGN"
export RESULTS_DIR="./results/${PLATFORM}/${DESIGN}/openroad"
export LOG_DIR="./logs/${PLATFORM}/${DESIGN}/openroad"
export REPORTS_DIR="./reports/${PLATFORM}/${DESIGN}/openroad"
export OBJECTS_DIR="./objects/${PLATFORM}/${DESIGN}/openroad"

# Decompress the gzipped DEF into a scratch dir (git-ignored) and point the
# viewer script at it.
mkdir -p "$RESULTS_DIR"
gunzip -c "$DEFGZ" > "$RESULTS_DIR/${STEM}.def"
export OPEN_DEF="$RESULTS_DIR/${STEM}.def"

# Heterogeneous stack selects the ASAP7-top / Nangate45-bottom cell LEFs.
if [[ "$PLATFORM" == "asap7_nangate45_3D" ]]; then
  export HETEROGENEOUS_3D=1
fi

# env.sh forces offscreen Qt for headless runs; the viewer needs a real display.
export QT_QPA_PLATFORM=xcb

make DESIGN_CONFIG="designs/${PLATFORM}/${DESIGN}/config.mk" ord-open
