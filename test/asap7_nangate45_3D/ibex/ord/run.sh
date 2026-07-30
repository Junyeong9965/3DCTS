#!/bin/bash
# Full F2F 3D flow for asap7_nangate45_3D/ibex:
#   2D synthesis -> tier partitioning -> 3D floorplan/PDN/placement/legalization
#   -> useful-skew 3D CTS -> route -> final timing report.
# All method knobs are centralized in configs/cts_params.env.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOW_ROOT="${SCRIPT_DIR}"
while [[ "${FLOW_ROOT}" != "/" && ! -f "${FLOW_ROOT}/env.sh" ]]; do
  FLOW_ROOT="$(dirname "${FLOW_ROOT}")"
done
source "${FLOW_ROOT}/env.sh"
source "${FLOW_ROOT}/configs/cts_params.env"

export DESIGN_NICKNAME="ibex"
export PLATFORM="asap7_nangate45_3D"
# Heterogeneous stack (ASAP7 top tier + Nangate45 bottom tier)
export HETEROGENEOUS_3D=1

export LOG_DIR=./logs/${PLATFORM}/${DESIGN_NICKNAME}/openroad
export OBJECTS_DIR=./objects/${PLATFORM}/${DESIGN_NICKNAME}/openroad
export REPORTS_DIR=./reports/${PLATFORM}/${DESIGN_NICKNAME}/openroad
export RESULTS_DIR=./results/${PLATFORM}/${DESIGN_NICKNAME}/openroad

CFG_2D="designs/${PLATFORM}/${DESIGN_NICKNAME}/config2d.mk"
CFG_3D="designs/${PLATFORM}/${DESIGN_NICKNAME}/config.mk"
CFG_BOTTOM="designs/${PLATFORM}/${DESIGN_NICKNAME}/config_bottom_cover.mk"
CFG_CTS="${CFG_BOTTOM}"

echo "=== F2F 3D useful-skew CTS: ${PLATFORM}/${DESIGN_NICKNAME} ==="

# 1) 2D synthesis and timing-driven tier partitioning
make DESIGN_CONFIG=${CFG_2D} clean_all
make DESIGN_CONFIG=${CFG_3D} clean_all
make DESIGN_CONFIG=${CFG_2D} ord-synth
make DESIGN_CONFIG=${CFG_2D} ord-preplace
make DESIGN_CONFIG=${CFG_2D} ord-tier-partition

# 2) 3D floorplan, PDN, placement and legalization on both tiers
make DESIGN_CONFIG=${CFG_3D} ord-pre
make DESIGN_CONFIG=${CFG_3D} ord-3d-pdn
make DESIGN_CONFIG=${CFG_3D} ord-place-init
make DESIGN_CONFIG=${CFG_3D} ord-place-init-upper
make DESIGN_CONFIG=${CFG_3D} ord-place-init-bottom
make DESIGN_CONFIG=${CFG_3D} ord-place-upper
make DESIGN_CONFIG=${CFG_3D} ord-place-bottom
make DESIGN_CONFIG=${CFG_3D} ord-pre-opt
make DESIGN_CONFIG=${CFG_3D} ord-legalize-bottom
make DESIGN_CONFIG=${CFG_3D} ord-legalize-upper

# 3) Useful-skew 3D clock tree synthesis (this work)
make DESIGN_CONFIG=${CFG_CTS} ord-cts-3d

# 4) Route and final report in OpenROAD
make DESIGN_CONFIG=${CFG_3D} ord-route
make DESIGN_CONFIG=${CFG_3D} ord-final
