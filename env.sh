#!/usr/bin/env bash

function __setpaths() {
  DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
  if [[ -z "${FLOW_ENV_QUIET:-}" ]]; then
    echo "Setting FLOW_HOME to $DIR"
  fi
  export FLOW_HOME="$DIR"
}
__setpaths

# ------------------------------------------------------------------------------
# HPC module loading
# ------------------------------------------------------------------------------
if command -v module &>/dev/null 2>&1; then
  module load tcl/8.6.6 yaml-cpp/0.8.0 gcc/12.2.0 2>/dev/null || true
  module load innovus/21.1 2>/dev/null || true
  module load genus/21.1 2>/dev/null || true
  # Ensure gcc 12 libstdc++ takes priority over older versions
  export LD_LIBRARY_PATH="$(dirname $(which gcc))/../lib64:$(dirname $(which gcc))/../lib:${LD_LIBRARY_PATH}"
fi

# ------------------------------------------------------------------------------
# Toolchain paths
# ------------------------------------------------------------------------------
# Set these to your local build paths (see README.md for build instructions)
export OPENROAD_EXE="${OPENROAD_EXE:-<path-to-patched-openroad>/build/bin/openroad}"
export YOSYS_EXE="${YOSYS_EXE:-<path-to-yosys>/yosys}"
export STA_EXE="${STA_EXE:-<path-to-openroad>/build/src/sta}"
export NUM_CORES="${NUM_CORES:-16}"
export OPENROAD_CMD_DOCKER="${OPENROAD_CMD_DOCKER:-${OPENROAD_EXE} -threads ${NUM_CORES}}"

# Disable Qt GUI for headless SSH environments
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

# ------------------------------------------------------------------------------
# Cadence flow: TritonPart partitioning via OpenROAD (local or docker)
# ------------------------------------------------------------------------------
export CDS_PARTITION_MODE="${CDS_PARTITION_MODE:-local}"
if [[ "${CDS_PARTITION_MODE}" == "docker" ]]; then
  export CDS_USE_OPENROADDOCKER=1
  export CDS_PARTITION_TARGET="cds-docker-partition"
else
  export CDS_USE_OPENROADDOCKER=0
  export CDS_PARTITION_TARGET="cds-tier-partition"
fi

# Docker settings (used when CDS_PARTITION_MODE=docker)
export DOCKER="${DOCKER:-docker}"
export CONTAINER="${CONTAINER:-openroad}"
export CONTAINER_USER="${CONTAINER_USER:-user}"
export INNER_DIR="${INNER_DIR:-/flow}"

# ------------------------------------------------------------------------------
# Remote eval (optional, for multi-server setups)
# ------------------------------------------------------------------------------
export ORD_EVAL_MODE="${ORD_EVAL_MODE:-local}"
export ORD_EVAL_REMOTE_USER="${ORD_EVAL_REMOTE_USER:-${USER}}"
export ORD_EVAL_REMOTE_HOST="${ORD_EVAL_REMOTE_HOST:-${HOSTNAME:-localhost}}"
export ORD_EVAL_REMOTE_PROJECT_DIR="${ORD_EVAL_REMOTE_PROJECT_DIR:-${FLOW_HOME}}"
export ORD_EVAL_SSH_OPTS="${ORD_EVAL_SSH_OPTS:-}"
