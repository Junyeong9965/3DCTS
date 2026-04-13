#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOW_ROOT="${SCRIPT_DIR}"
while [[ "${FLOW_ROOT}" != "/" && ! -f "${FLOW_ROOT}/env.sh" ]]; do
  FLOW_ROOT="$(dirname "${FLOW_ROOT}")"
done
source "${FLOW_ROOT}/env.sh"
source "${FLOW_ROOT}/configs/cts_params.env"

export HETEROGENEOUS_3D=1
export CTS_NO_INSERTION_DELAY=1          # LatencyBalancer OFF

export DESIGN_NICKNAME="ariane133"
export PLATFORM="asap7_nangate45_3D"

export LOG_DIR=./logs/${PLATFORM}/${DESIGN_NICKNAME}/openroad
export OBJECTS_DIR=./objects/${PLATFORM}/${DESIGN_NICKNAME}/openroad
export REPORTS_DIR=./reports/${PLATFORM}/${DESIGN_NICKNAME}/openroad
export RESULTS_DIR=./results/${PLATFORM}/${DESIGN_NICKNAME}/openroad

CFG_2D="designs/${PLATFORM}/${DESIGN_NICKNAME}/config2d.mk"
CFG_3D="designs/${PLATFORM}/${DESIGN_NICKNAME}/config.mk"
CFG_BOTTOM="designs/${PLATFORM}/${DESIGN_NICKNAME}/config_bottom_cover.mk"

echo "=== 3D CTS: ${PLATFORM}/${DESIGN_NICKNAME} ==="

make DESIGN_CONFIG=${CFG_2D} clean_all
make DESIGN_CONFIG=${CFG_3D} clean_all

make DESIGN_CONFIG=${CFG_2D} ord-synth
make DESIGN_CONFIG=${CFG_2D} ord-preplace
make DESIGN_CONFIG=${CFG_2D} ord-tier-partition

make DESIGN_CONFIG=${CFG_3D} ord-pre
make DESIGN_CONFIG=${CFG_3D} ord-3d-floorplan
make DESIGN_CONFIG=${CFG_3D} ord-3d-io
make DESIGN_CONFIG=${CFG_3D} ord-place-macro-upper
make DESIGN_CONFIG=${CFG_3D} ord-place-macro-bottom
make DESIGN_CONFIG=${CFG_3D} ord-3d-pdn-only
make DESIGN_CONFIG=${CFG_3D} ord-place-init
make DESIGN_CONFIG=${CFG_3D} ord-place-init-upper
make DESIGN_CONFIG=${CFG_3D} ord-place-init-bottom

iteration=1
for ((i=1;i<=iteration;i++)); do
  echo "Iteration: $i"
  make DESIGN_CONFIG=${CFG_3D} ord-place-upper
  make DESIGN_CONFIG=${CFG_3D} ord-place-bottom
done

make DESIGN_CONFIG=${CFG_3D} ord-pre-opt
make DESIGN_CONFIG=${CFG_3D} ord-legalize-bottom
make DESIGN_CONFIG=${CFG_3D} ord-legalize-upper

make DESIGN_CONFIG=${CFG_BOTTOM} ord-cts-3d
make DESIGN_CONFIG=${CFG_3D} cds-route
make DESIGN_CONFIG=${CFG_3D} cds-restore
make DESIGN_CONFIG=${CFG_3D} ord-final
