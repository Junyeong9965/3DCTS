# Useful-Skew Clock Tree Synthesis for F2F 3D ICs

LP-based useful-skew CTS framework for Face-to-Face (F2F) 3D ICs with hybrid bonding,
built on [OpenROAD-Research](https://github.com/ieee-ceda-datc/OpenROAD-Research).

## Overview

This framework achieves 36-97% post-CTS setup TNS reduction over the Pin-3D
single-tier baseline across five designs and two F2F platforms.

Four phases address the realizability gap between LP-optimal skew and physical delivery:

1. **Phase 1 — Per-tier calibration**: balanced CTS + propagated-clock STA extraction
2. **Phase 2 — Delivery-constrained LP**: endpoint-TNS objective with 2-phase WNS guarantee (OR-Tools GLOP)
3. **Phase 3 — TARP clustering + cascaded TAP delivery**: timing-affinity FM min-cut clustering with Hilbert initialization; cascaded delay-tap chains for per-cluster useful-skew delivery
4. **Phase 4 — Skew-aware buffer sizing**: Liberty-based LP buffer sizing

Additional 3D-specific features:
- **3D-aware detailed placement**: cross-tier overlap handling for F2F tier assignment
- **Per-tier independent H-tree**: separate CTS per tier with shared root buffer

## Prerequisites

### Required
- **OS**: Linux (tested on RHEL 8)
- **Compiler**: GCC 12+ with C++17 support
- **CMake** 3.16+
- **Tcl** 8.6+
- **Python** 3.8+ with `scipy`, `numpy`
- **yaml-cpp** 0.8+
- **Yosys** 0.61 (`77005b69a`) for synthesis (see [Yosys build guide](https://github.com/YosysHQ/yosys#building-from-source))
  - **yosys-slang** (`64b4461`)
- OR-Tools is built automatically as part of OpenROAD

### Optional
- **Cadence Innovus** 21.1+ (only for ariane133 post-route stage due to a Pin-3D environment setup issue; post-CTS timing is fully verifiable with OpenROAD alone)
- **Cadence Genus** 21.1+ (synthesis alternative, not required for CTS evaluation)

### HPC Environment (module-based)

If your system uses environment modules:
```bash
module load tcl/8.6.6 yaml-cpp/0.8.0 gcc/12.2.0
```
Otherwise, ensure these libraries are installed and visible in your `PATH`/`LD_LIBRARY_PATH`.

## Setup

### 1. Build Patched OpenROAD

```bash
# Clone the base OpenROAD-Research
git clone https://github.com/ieee-ceda-datc/OpenROAD-Research.git
cd OpenROAD-Research

# Checkout the tested base commit
git checkout 2c85b9db45c109d48e1f68ef0806746d74e0d6b4

# Apply the 3D CTS patch (from this repository)
patch -p1 < ../3DCTS/openroad_3dcts.patch

# Build
mkdir -p build && cd build
cmake .. -DCMAKE_CXX_STANDARD=17 -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=ON -DENABLE_TESTS=OFF
make -j$(nproc)
```

### 2. Build Yosys

Follow the [Yosys build instructions](https://github.com/YosysHQ/yosys#building-from-source),
or use the version bundled in OpenROAD-Research:
```bash
cd OpenROAD-Research/tools/yosys
make -j$(nproc)
```

### 3. Configure Environment

Edit `env.sh` and set the tool paths:

```bash
export OPENROAD_EXE=/path/to/OpenROAD-Research/build/bin/openroad
export YOSYS_EXE=/path/to/yosys
export STA_EXE=/path/to/OpenROAD-Research/build/src/sta
```

Then source it:

```bash
source env.sh
```

## Clock Period Settings

Target clock periods (TCPs) are scaled from nominal values inherited from Pin-3D.
Three settings are evaluated: tight, medium, and relaxed.

**asap7_3D (homogeneous)**

| Design | Nominal TCP | Tight (P70) | Medium (P75) | Relaxed (P80) |
|--------|------------|-------------|--------------|----------------|
| AES | 380 ps | 266 ps | 285 ps | 304 ps |
| IBEX | 1000 ps | 700 ps | 750 ps | 800 ps |
| JPEG | 680 ps | 476 ps | 510 ps | 544 ps |
| SWERV | 1600 ps | 1120 ps | 1200 ps | 1280 ps |
| ARIANE133 | 900 ps | 630 ps | 675 ps | 720 ps |

**asap7_nangate45_3D (heterogeneous)**

| Design | Nominal TCP | Tight | Medium | Relaxed |
|--------|------------|-------|--------|---------|
| AES | 0.82 ns | 0.246 ns (P30) | 0.287 ns (P35) | 0.328 ns (P40) |
| IBEX | 2.2 ns | 1.54 ns (P70) | 1.65 ns (P75) | 1.76 ns (P80) |
| JPEG | 1.2 ns | 0.36 ns (P30) | 0.42 ns (P35) | 0.48 ns (P40) |
| SWERV | 2.0 ns | 1.4 ns (P70) | 1.5 ns (P75) | 1.6 ns (P80) |
| ARIANE133 | 3.0 ns | 2.1 ns (P70) | 2.25 ns (P75) | 2.4 ns (P80) |

> **Note**: asap7_nangate45_3D AES and JPEG use P30/P35/P40 scaling
> (instead of P70/P75/P80) because Pin-3D shows only marginal timing
> violations at the standard scaling, limiting the observable CTS improvement.

## Running the Flow

To reproduce the reported results, use the pre-placed design databases from the Pin-3D
flow (synthesis through legalization), available in our artifact, and run 3D CTS from
the `ord-cts-3d` stage onward. Full-flow re-execution may yield slightly different
placements due to non-determinism in the open-source tool chain, but CTS improvements
over the respective Pin-3D baseline remain consistent.

### Smoke Test (single design)

```bash
bash test/asap7_3D/aes/ord/run.sh
```

### Run All Benchmarks

```bash
# All platforms x all designs in parallel
./run_benchmarks.sh

# Specific platform/design
PLATFORMS="asap7_3D" DESIGNS="aes ibex" ./run_benchmarks.sh

# Report only (regenerate HTML from existing results)
REPORT_ONLY=1 ./run_benchmarks.sh
```

### Expected Output

Results: `results/<platform>/<design>/openroad/`
Reports: `reports/<platform>/<design>/openroad/`

Key output files:
- `4_cts_timing.rpt` — post-CTS timing report
- `cts_debug_per_ff.csv` — per-FF clock latency and LP targets
- `cts_debug_report.html` — visual debug report with LP correlation

## Supported Platforms and Designs

| Platform | Description | Designs |
|----------|-------------|---------|
| asap7_3D | Homogeneous ASAP7 F2F | aes, ibex, jpeg, ariane133, swerv_wrapper |
| asap7_nangate45_3D | Heterogeneous ASAP7/NG45 F2F | aes, ibex, jpeg, ariane133, swerv_wrapper |

## Patch Contents

`openroad_3dcts.patch` modifies 36 files across 3 OpenROAD modules:

| Module | Files | Key Changes |
|--------|-------|-------------|
| **src/cts/** | 27 | LP solver (CtsSkewLpSolver), TARP clustering (TarpClustering), H-tree 3D extensions (HTreeBuilder), buffer sizing LP (BufSizingLpSolver), FF timing graph extraction (VerilogFFExtractor), 3D tier database (Cts3DDatabase) |
| **src/dpl/** | 7 | 3D-aware detailed placement: cross-tier overlap skip, tier-aware swap/shift |
| **src/odb/** | 1 | Parasitic extraction robustness (tmg_conn) |

Tested against: `ieee-ceda-datc/OpenROAD-Research` @ [`2c85b9db45`](https://github.com/ieee-ceda-datc/OpenROAD-Research/commit/2c85b9db45c109d48e1f68ef0806746d74e0d6b4) (2026-02-03)

## Directory Structure

```
├── openroad_3dcts.patch     # OpenROAD modifications (36 files)
├── scripts_openroad/        # Tcl/Python CTS flow scripts
│   ├── cts_3d.tcl           # Main 3D CTS orchestrator (4 phases)
│   ├── cts_phase1a_extract.tcl  # Propagated-clock timing extraction
│   ├── buffer_sizing_iterative.tcl  # Iterative buffer sizing
│   └── ...
├── configs/                 # CTS parameter configuration
│   └── cts_params.env       # All tunable parameters with defaults
├── platforms/               # 3D PDK (per-tier LEF/LIB/RC)
├── designs/                 # Design configurations and constraints
├── scripts_cadence/         # Cadence integration (partitioning, routing)
├── test/                    # Per-design run scripts
├── run_benchmarks.sh        # Batch runner for all experiments
├── Makefile                 # ORFS-based flow automation
└── env.sh                   # Environment setup
```

## License

BSD 3-Clause License.
