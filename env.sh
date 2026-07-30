#!/usr/bin/env bash
# Environment for the F2F 3D useful-skew CTS flow.
# Point the three *_EXE variables at your local tool builds (see README.md),
# or export them before sourcing this file.

function __setpaths() {
  DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
  if [[ -z "${FLOW_ENV_QUIET:-}" ]]; then
    echo "Setting FLOW_HOME to $DIR"
  fi
  export FLOW_HOME="$DIR"
}
__setpaths

# ------------------------------------------------------------------------------
# Optional environment modules (no-op if `module` is unavailable).
# ortools + spdlog are runtime dependencies of the OR-Tools-linked CTS binary.
# ------------------------------------------------------------------------------
if command -v module &>/dev/null 2>&1; then
  module load tcl/8.6.6 yaml-cpp/0.8.0 gcc/12.2.0 2>/dev/null || true
  module load ortools/9.15 spdlog/1.15.1 2>/dev/null || true
  # Cadence tools are only needed for the ariane133 route stage (see README).
  module load innovus/21.1 genus/21.1 2>/dev/null || true
  # Ensure gcc 12 libstdc++ takes priority over older versions.
  if command -v gcc &>/dev/null; then
    export LD_LIBRARY_PATH="$(dirname "$(command -v gcc)")/../lib64:$(dirname "$(command -v gcc)")/../lib:${LD_LIBRARY_PATH}"
  fi
fi

# ------------------------------------------------------------------------------
# Toolchain executables — override these to match your local builds.
#   OPENROAD_EXE : OpenROAD built from OpenROAD-Research + openroad_3dcts.patch
#   YOSYS_EXE    : Yosys used for synthesis
#   STA_EXE      : OpenSTA (bundled with the OpenROAD build)
# Defaults assume the executables are on $PATH.
# ------------------------------------------------------------------------------
export OPENROAD_EXE="${OPENROAD_EXE:-openroad}"
export YOSYS_EXE="${YOSYS_EXE:-yosys}"
export STA_EXE="${STA_EXE:-sta}"
export NUM_CORES="${NUM_CORES:-16}"
export OPENROAD_CMD_DOCKER="${OPENROAD_CMD_DOCKER:-${OPENROAD_EXE} -threads ${NUM_CORES}}"

# Disable Qt GUI for headless environments.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

# ------------------------------------------------------------------------------
# Tier partitioning is performed by OpenROAD's TritonPart (local by default).
# ------------------------------------------------------------------------------
export CDS_PARTITION_MODE="${CDS_PARTITION_MODE:-local}"
if [[ "${CDS_PARTITION_MODE}" == "docker" ]]; then
  export CDS_USE_OPENROADDOCKER=1
  export CDS_PARTITION_TARGET="cds-docker-partition"
else
  export CDS_USE_OPENROADDOCKER=0
  export CDS_PARTITION_TARGET="cds-tier-partition"
fi

# Docker settings (used only when CDS_PARTITION_MODE=docker).
export DOCKER="${DOCKER:-docker}"
export CONTAINER="${CONTAINER:-openroad}"
export CONTAINER_USER="${CONTAINER_USER:-user}"
export INNER_DIR="${INNER_DIR:-/flow}"
