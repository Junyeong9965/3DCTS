#!/usr/bin/env python3
"""
Pre-CTS Skew Target LP Solver (Sequential-Graph-Driven CTS)
Based on: Fishburn, "Clock Skew Optimization", IEEE Trans. Computers, 1990.

Changes from V31:
  1. LP variable bounds: a_i in [0, t_max_i]  (non-negative, physically realizable)
       - Union of all three leaf-buffer cases (professor's bottom-*):
           Case 1: single bottom-tier leaf  -> top FFs delayed by t_via through HB
           Case 2: single top-tier leaf     -> bottom FFs delayed by t_via through HB
           Case 3: per-tier leaf buffers    -> each tier independently controlled
       - t_max_i for bottom FF = max_skew  (direct path)
       - t_max_i for top    FF = max_skew + t_via_ns  (HB adds baseline delay)
       - a_i >= 0 ensures only delay insertion (no clock advancement required)
  2. sigma_hb REMOVED: HB via delay is already deterministic RC in STA parasitics;
       only sigma_local remains for same-tier clock uncertainty
  3. z_i variables REMOVED: a_i >= 0 means |a_i| = a_i; no linearization needed
       n_vars = n_ffs + 1  (down from 2*n_ffs + 1 in V31)
  4. --bounds-csv: per-FF physical achievability from ClockLatencyEstimator C++ class
       format: ff_name,tier,t_min_ns,t_max_ns
       If not provided, falls back to --t-via argument (global HB via delay)
  5. Regularization uses sum(a_i) directly (not sum(z_i) as in V31)
  6. Pruning threshold: t_max_i + t_max_j per edge (was 2*max_skew global)

LP-SAFETY formulation (V32):
  Variables:
    a_i in [0, t_max_i]    per-FF clock arrival offset (ns), add-only delay  (n_ffs)
    M   (unbounded)         minimum margin across all timing paths              (1)

  Clock uncertainty (Fishburn Sec. VII, sigma_hb removed):
    sigma_i = sigma_local   (same for all FFs in V32)
    eff_slack_setup_ij = slack_setup_ij - sigma_i - sigma_j
    eff_slack_hold_ij  = slack_hold_ij  - sigma_i - sigma_j

  Constraints (LP-SAFETY mode):
    Setup: a_i - a_j + M <= eff_slack_setup_ij   for all i->j setup edges
    Hold:  a_j - a_i + M <= eff_slack_hold_ij    for all i->j hold edges
    (M coefficient = +1 gives upper bound on M, making the problem bounded)

  Objective: minimize  -M + lambda_reg * sum(a_i)
    = maximize minimum margin while minimizing unnecessary delay insertion

Usage:
  python cts_skew_lp.py <ff_timing_graph.csv> <output.csv> [options]
  python cts_skew_lp.py in.csv out.csv --lp-mode safety --bounds-csv bounds.csv
  python cts_skew_lp.py in.csv out.csv --lp-mode safety --t-via 0.015
"""

import argparse
import csv
import json
import os
import signal
import sys
import time

# Ignore SIGHUP so that SSH disconnects do not kill the LP solver process.
# When run via `exec` from OpenROAD (Tcl), this child process inherits the
# default SIGHUP handler (terminate).  The parent run_all*.sh uses
# `trap '' HUP` but that does NOT propagate to grandchild processes.
# Without this, an SSH disconnect during Phase 2 LP causes "child killed:
# hangup" and the flow falls back to zero skew targets.  (V50-DPL-C fix)
if hasattr(signal, 'SIGHUP'):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

try:
    import numpy as np
    from scipy.optimize import linprog
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    print("WARNING: scipy not available, LP solver will not work")


# Parse IO timing edges CSV (PI→FF, FF→PO)
def parse_io_timing_edges(io_csv, ff_to_idx):
    """Parse IO timing edges from extract_io_timing_edges Tcl output.

    CSV format: edge_type,port_name,ff_name,slack_setup_ns,slack_hold_ns
    edge_type = PI_TO_FF or FF_TO_PO

    Returns list of dicts:
      type: 'pi_setup' | 'pi_hold' | 'po_setup' | 'po_hold'
      ff_idx: index into ff_list (from ff_to_idx)
      slack: slack value in ns
      port: port name (for logging)

    Only FFs present in ff_to_idx are included (missing FFs silently skipped).
    """
    io_edges = []
    n_skip = 0

    with open(io_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            edge_type = row.get('edge_type', '').strip()
            ff_name   = row.get('ff_name', '').strip()
            port      = row.get('port_name', '').strip()

            if ff_name not in ff_to_idx:
                n_skip += 1
                continue

            ff_idx = ff_to_idx[ff_name]

            # Parse setup slack
            # Skip inf/nan slacks from unconstrained paths (same pattern as
            # ghost edge filter in parse_ff_timing_graph). C++ exhaustive IO
            # extraction writes inf for paths where STA has no timing constraint.
            # float('inf') parses without ValueError but crashes scipy linprog
            # with "b_ub must not contain values inf, nan, or None".
            s_setup = row.get('slack_setup_ns', '').strip()
            if s_setup:
                try:
                    slack_setup = float(s_setup)
                    if slack_setup != slack_setup or abs(slack_setup) == float('inf'):
                        continue  # unconstrained path (inf/nan) — no valid IO edge
                    if edge_type == 'PI_TO_FF':
                        io_edges.append({'type': 'pi_setup', 'ff_idx': ff_idx,
                                         'slack': slack_setup, 'port': port})
                    elif edge_type == 'FF_TO_PO':
                        io_edges.append({'type': 'po_setup', 'ff_idx': ff_idx,
                                         'slack': slack_setup, 'port': port})
                except ValueError:
                    pass

            # Parse hold slack
            s_hold = row.get('slack_hold_ns', '').strip()
            if s_hold:
                try:
                    slack_hold = float(s_hold)
                    if slack_hold != slack_hold or abs(slack_hold) == float('inf'):
                        continue  # unconstrained path (inf/nan) — no valid IO edge
                    if edge_type == 'PI_TO_FF':
                        io_edges.append({'type': 'pi_hold', 'ff_idx': ff_idx,
                                         'slack': slack_hold, 'port': port})
                    elif edge_type == 'FF_TO_PO':
                        io_edges.append({'type': 'po_hold', 'ff_idx': ff_idx,
                                         'slack': slack_hold, 'port': port})
                except ValueError:
                    pass

    # Summary
    counts = {}
    for e in io_edges:
        counts[e['type']] = counts.get(e['type'], 0) + 1
    print(f"  IO edges parsed: {len(io_edges)} total ({n_skip} FFs skipped)")
    for t in ['pi_setup', 'pi_hold', 'po_setup', 'po_hold']:
        print(f"    {t}: {counts.get(t, 0)}")

    return io_edges


def parse_ff_timing_graph(csv_file):
    """Parse FF timing graph CSV file.
    Expected columns: from_ff, to_ff, slack_max_ns, slack_min_ns, from_tier, to_tier
    Returns (edges, sorted_ff_list, ff_tiers_dict)
    """
    edges = []
    ff_set = set()
    ff_tiers = {}

    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_ff = row.get('from_ff', '')
            to_ff   = row.get('to_ff', '')

            try:
                slack_max = float(row.get('slack_max_ns', 0))
            except (ValueError, TypeError):
                slack_max = 0.0

            try:
                slack_min = float(row.get('slack_min_ns', 0))
            except (ValueError, TypeError):
                slack_min = 0.0

            from_tier = row.get('from_tier', '0')
            to_tier   = row.get('to_tier', '0')
            try:
                from_tier = int(from_tier) if from_tier else 0
            except ValueError:
                from_tier = 0
            try:
                to_tier = int(to_tier) if to_tier else 0
            except ValueError:
                to_tier = 0

            if from_ff and to_ff:
                # (ported to V50): Skip ghost edges where both
                # setup and hold slack are exactly 0.0 AND all arrival/required times are 0.0.
                # Root cause: extract_ff_timing_graph_verilog writes 0.0 for FF pairs
                # where STA cannot find a valid timing path (false/multi-cycle paths,
                # or paths not yet constrained in pre-CTS state).
                # In nangate45_3D (timing fully met), 643 such edges form 3,551 hard-hold
                # constraint cycles after sigma correction (eff_hold = 0 - 10ps = -10ps),
                # causing LP INFEASIBLE. Ghost edges have arrival_max=0 AND arrival_min=0
                # (real paths always have non-zero clock arrival time from the H-tree).
                try:
                    arr_max = float(row.get('arrival_max_ns', 1.0))
                    arr_min = float(row.get('arrival_min_ns', 1.0))
                except (ValueError, TypeError):
                    arr_max, arr_min = 1.0, 1.0

                if (slack_max == 0.0 and slack_min == 0.0
                        and arr_max == 0.0 and arr_min == 0.0):
                    continue  # ghost edge: no real STA timing constraint found

                edges.append({
                    'from_ff':    from_ff,
                    'to_ff':      to_ff,
                    'slack_setup': slack_max,
                    'slack_hold':  slack_min,
                    'from_tier':  from_tier,
                    'to_tier':    to_tier
                })
                ff_set.add(from_ff)
                ff_set.add(to_ff)
                ff_tiers[from_ff] = from_tier
                ff_tiers[to_ff]   = to_tier

    return edges, sorted(ff_set), ff_tiers


def parse_bounds_csv(bounds_csv, ff_list, ff_tiers, max_skew, t_via_global):
    """Parse per-FF physical achievability bounds from ClockLatencyEstimator output.

    Expected CSV columns: ff_name, tier, t_min_ns, t_max_ns
    Returns dict: ff_name -> t_max_ns  (t_min_ns is always 0 in V32)

    If bounds_csv is None or a FF is missing, falls back to:
      bottom-tier FF: t_max = max_skew
      upper-tier  FF: t_max = max_skew + t_via_global
    """
    per_ff_tmax = {}

    if bounds_csv is not None:
        try:
            with open(bounds_csv, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ff   = row.get('ff_name', '').strip()
                    tmax = row.get('t_max_ns', None)
                    if ff and tmax is not None:
                        try:
                            per_ff_tmax[ff] = float(tmax)
                        except (ValueError, TypeError):
                            pass
            print(f"  Loaded bounds for {len(per_ff_tmax)} FFs from {bounds_csv}")
        except Exception as e:
            print(f"  WARNING: cannot read bounds CSV '{bounds_csv}': {e}; using fallback")

    # Fill in any missing FFs with tier-based fallback
    n_fallback = 0
    for ff in ff_list:
        if ff not in per_ff_tmax:
            tier = ff_tiers.get(ff, 0)
            per_ff_tmax[ff] = max_skew + (t_via_global if tier == 1 else 0.0)
            n_fallback += 1
    if n_fallback > 0:
        print(f"  Fallback bounds for {n_fallback} FFs "
              f"(bottom: {max_skew*1e3:.0f}ps, upper: {(max_skew+t_via_global)*1e3:.0f}ps)")

    return per_ff_tmax


# ---------------------------------------------------------------------------
# LP-SAFETY solver V32
# ---------------------------------------------------------------------------

def solve_lp_safety(edges, ff_list, ff_tiers, per_ff_tmax,
                    sigma_local=0.005,
                    lambda_reg=0.01):
    """
    Fishburn LP-SAFETY V32: maximize minimum margin M across all setup and hold
    constraints, with non-negative per-FF arrival offsets bounded by physical
    achievability (professor's unified three-case estimator).

    simplifications vs V31:
      - a_i in [0, t_max_i]  (non-negative; only add delay)
      - z_i variables removed (|a_i| = a_i since a_i >= 0)
      - sigma_hb removed (via RC is in STA; only sigma_local remains)
      - n_vars = n_ffs + 1

    Variable layout:
      [0 .. n_ffs-1] : a_i  (clock arrival offset in ns, non-negative)
      [n_ffs]        : M    (minimum safety margin, unbounded)
    """
    if not HAS_SCIPY:
        print("ERROR: scipy not available")
        return {}

    n_ffs = len(ff_list)
    if n_ffs == 0:
        return {}

    ff_to_idx = {ff: i for i, ff in enumerate(ff_list)}

    # Per-FF sigma: same for all FFs in (sigma_hb removed)
    sigma = np.full(n_ffs, sigma_local)

    # Collect effective slacks per edge
    setup_edges = []   # (i, j, eff_slack)
    hold_edges  = []   # (i, j, eff_slack)
    n_cross = 0

    for edge in edges:
        i = ff_to_idx.get(edge['from_ff'])
        j = ff_to_idx.get(edge['to_ff'])
        if i is None or j is None:
            continue

        # V32: no extra delta_hb (via RC already in STA parasitics)
        eff_setup = edge['slack_setup'] - sigma[i] - sigma[j]
        eff_hold  = edge['slack_hold']  - sigma[i] - sigma[j]

        setup_edges.append((i, j, eff_setup))
        hold_edges.append((i, j, eff_hold))
        if edge['from_tier'] != edge['to_tier']:
            n_cross += 1

    n_neg_setup = sum(1 for _, _, s in setup_edges if s < 0)
    n_neg_hold  = sum(1 for _, _, s in hold_edges  if s < 0)

    print(f"  FFs: {n_ffs} (sigma_local={sigma_local*1e3:.1f}ps; sigma_hb removed in V32)")
    print(f"  Timing edges: {len(setup_edges)} ({n_cross} cross-tier)")
    print(f"  Negative setup (after sigma): {n_neg_setup}")
    print(f"  Negative hold  (after sigma): {n_neg_hold}")

    if n_neg_setup == 0 and n_neg_hold == 0:
        print("  All slacks positive after sigma — targets set to 0.0")
        return {ff: 0.0 for ff in ff_list}

    # Build tmax array for pruning and bounds
    t_max_arr = np.array([per_ff_tmax.get(ff, 0.1) for ff in ff_list])

    # Variable layout: a_i (n_ffs), M (1)
    n_vars = n_ffs + 1
    M_idx  = n_ffs

    # Objective: minimize -M + lambda_reg * sum(a_i)
    #   = maximize M while minimizing unnecessary delay insertion
    c = np.zeros(n_vars)
    c[M_idx] = -1.0                     # -M (maximize M)
    for i in range(n_ffs):
        c[i] = lambda_reg               # penalize delay spread

    constraints_A = []
    constraints_b = []

    # Setup constraints: a_i - a_j + M <= eff_slack_setup_ij
    # Derivation: setup_margin = slack_setup + (a_j - a_i) >= M
    #   -> a_i - a_j + M <= slack_setup  (M is UPPER-bounded -> problem bounded)
    # Prune if eff_slack > t_max_i + t_max_j (constraint can never be active)
    n_setup_ct = 0
    for i, j, eff_slack in setup_edges:
        if eff_slack > t_max_arr[i] + t_max_arr[j]:
            continue
        row = np.zeros(n_vars)
        row[i]     =  1.0    # +a_i (launching FF: later launch hurts setup)
        row[j]     = -1.0    # -a_j (capturing FF: later capture helps setup)
        row[M_idx] = +1.0    # +M   (upper bound on M -> bounded maximization)
        constraints_A.append(row)
        constraints_b.append(eff_slack)
        n_setup_ct += 1

    # Hold constraints: a_j - a_i + M <= eff_slack_hold_ij
    # Derivation: hold_margin = slack_hold + (a_i - a_j) >= M
    #   -> a_j - a_i + M <= slack_hold  (M is UPPER-bounded -> problem bounded)
    n_hold_ct = 0
    for i, j, eff_slack in hold_edges:
        if eff_slack > t_max_arr[i] + t_max_arr[j]:
            continue
        row = np.zeros(n_vars)
        row[j]     =  1.0    # +a_j (capturing FF: later capture hurts hold)
        row[i]     = -1.0    # -a_i (launching FF: later launch helps hold)
        row[M_idx] = +1.0    # +M   (upper bound on M -> bounded maximization)
        constraints_A.append(row)
        constraints_b.append(eff_slack)
        n_hold_ct += 1

    total_ct = len(constraints_A)
    print(f"  LP vars: {n_vars} (a:{n_ffs} M:1)  [z_i removed vs V31]")
    print(f"  Setup ct: {n_setup_ct}, Hold ct: {n_hold_ct}")
    print(f"  Total constraints: {total_ct}")

    # Bounds: a_i in [0, t_max_i] (non-negative, physically achievable)
    #         M:  unbounded below (can be negative when infeasible)
    bounds = (
        [(0.0, float(t_max_arr[i])) for i in range(n_ffs)] +
        [(None, None)]      # M: no bounds
    )

    A_ub = np.array(constraints_A) if constraints_A else np.zeros((0, n_vars))
    b_ub = np.array(constraints_b) if constraints_b else np.zeros(0)

    import scipy
    _sv = tuple(int(x) for x in scipy.__version__.split('.')[:2])
    method = 'highs' if _sv >= (1, 9) else 'interior-point'
    print(f"\n  Solving LP-SAFETY ({n_vars} vars, {total_ct} constraints, {method})...")

    try:
        result = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method=method)

        if not result.success:
            print(f"  LP-SAFETY failed: {result.message}")
            return {ff: 0.0 for ff in ff_list}

        a_vals = result.x[:n_ffs]
        M_val  = result.x[M_idx]

        print(f"  LP-SAFETY solved! (M = {M_val*1e3:+.2f} ps, obj = {result.fun:.6f})")
        if M_val >= 0:
            print(f"  => All paths have >= {M_val*1e3:.2f} ps margin (fully feasible region)")
        else:
            print(f"  => Worst path margin: {M_val*1e3:.2f} ps (violations remain, balanced)")

        _print_stats(a_vals, M_val, setup_edges, n_ffs, ff_list, ff_tiers, t_max_arr)
        return {ff: a_vals[i] for i, ff in enumerate(ff_list)}

    except Exception as e:
        print(f"  LP-SAFETY solver error: {e}")
        import traceback
        traceback.print_exc()
        return {ff: 0.0 for ff in ff_list}


# ---------------------------------------------------------------------------
# LP-TNS solver V43: minimize total violation + WNS minimax (sparse matrix)
# ---------------------------------------------------------------------------

def solve_lp_tns(edges, ff_list, ff_tiers, per_ff_tmax,
                 hold_margin=0.0,
                 sigma_local=0.005,
                 lambda_reg=0.001,
                 io_edges=None,
                 weight_io=1.0,
                 gamma_wns=0.0,
                 hard_pi_hold=False,
                 base_latency_ns=None,
                 fishburn=False):
    """
    LP-TNS V44: minimize total setup violation + WNS minimax penalty.
    adds base_latency_ns to correct hard PI->FF hold constraint.

    changes from V38:
      1. Sparse matrix (COO -> CSC) for memory/speed improvement
      2. WNS minimax variable W: min ... + gamma_wns * W, W >= v_k for all k
      3. Hard PI->FF hold: positive-slack edges -> hard constraint (no penalty var)

    change (2-pass CTS):
      4. Hard PI->FF hold corrected: a_j <= max(0, slack - base_latency_j)
         base_latency_j from Phase 1a balanced CTS (propagated clock).
         Without this, used ideal-clock slack (77.5ps) but actual clock
         already has base H-tree latency (~22ps), so relay had no room.

    Variable layout:
      [0 .. n_ffs-1]                    : a_i in [0, t_max_i]   (per-FF clock delay, ns)
      [n_ffs .. n_ffs+K-1]              : v_k >= 0              (FF-FF setup violation, K paths)
      [n_ffs+K .. n_ffs+K+M_soft-1]     : w_m >= 0              (IO soft violation, M_soft edges)
      [n_ffs+K+M_soft]                  : W >= 0                (WNS minimax, if gamma_wns>0)

    Formulation:
      FF-FF setup: a_i - a_j - v_k <= eff_setup_k
      FF-FF hold: a_j - a_i <= eff_hold_k - hold_margin
      IO soft: a_j - w_m <= slack (various signs per type)
      PI->FF hold HARD (V43/V44): a_j <= corrected_slack (positive slack only, no w_m)
        V43: corrected_slack = slack (ideal clock)
        V44: corrected_slack = max(0, slack - base_latency_j) (propagated clock)
      WNS minimax: v_k - W <= 0 for all k

      Objective: lambda_reg*sum(a_i) + sum(v_k) + weight_io*sum(w_m) + gamma_wns*W

    Pruning: skip if eff_slack > t_max_i + t_max_j (same as LP-SAFETY).
    PI->FF hold with positive corrected_slack uses hard constraint.
    PI->FF hold with negative corrected_slack stays soft (lesson: hard -> INFEASIBLE).
    """
    if not HAS_SCIPY:
        print("ERROR: scipy not available")
        # V47-fix: Return 2-tuple; caller: arrivals, lp_stats = solve_lp_tns(...)
        return {}, {"lp_mode": "tns", "status": "no_scipy"}

    n_ffs = len(ff_list)
    if n_ffs == 0:
        # V47-fix: Return 2-tuple; caller: arrivals, lp_stats = solve_lp_tns(...)
        return {}, {"lp_mode": "tns", "status": "no_ffs"}

    ff_to_idx = {ff: i for i, ff in enumerate(ff_list)}

    # Same sigma model as LP-SAFETY: tighten effective slacks by local uncertainty
    sigma = np.full(n_ffs, sigma_local)

    # Collect effective slacks per edge
    setup_edges = []   # (i, j, eff_slack)
    hold_edges  = []   # (i, j, eff_slack)
    n_cross = 0

    for edge in edges:
        i = ff_to_idx.get(edge['from_ff'])
        j = ff_to_idx.get(edge['to_ff'])
        if i is None or j is None:
            continue

        eff_setup = edge['slack_setup'] - sigma[i] - sigma[j]
        eff_hold  = edge['slack_hold']  - sigma[i] - sigma[j]

        setup_edges.append((i, j, eff_setup))
        hold_edges.append((i, j, eff_hold))
        if edge['from_tier'] != edge['to_tier']:
            n_cross += 1

    n_neg_setup = sum(1 for _, _, s in setup_edges if s < 0)
    print(f"  FFs: {n_ffs} (sigma_local={sigma_local*1e3:.1f}ps)")
    print(f"  Timing edges: {len(setup_edges)} ({n_cross} cross-tier)")
    print(f"  Violating setup (after sigma): {n_neg_setup}")

    if n_neg_setup == 0:
        print("  No setup violations after sigma — targets set to 0.0")
        # V47-fix: Must return (dict, lp_stats) tuple.
        # Caller does: arrivals, lp_stats = solve_lp_tns(...)
        # Returning a bare dict caused "too many values to unpack (expected 2)"
        # (Python tried to unpack the dict keys instead of the tuple).
        _zero_stats = {"lp_mode": "tns", "status": "no_violations", "n_ffs": n_ffs,
                       "n_edges": len(setup_edges), "targets": {"non_zero": 0}}
        return {ff: 0.0 for ff in ff_list}, _zero_stats

    t_max_arr = np.array([per_ff_tmax.get(ff, 0.1) for ff in ff_list])

    # --- Build variable index for v_k (one per pruned setup path) ---
    # Include path k if eff_setup_k < t_max_i + t_max_j
    # (same pruning as LP-SAFETY; paths with huge slack can never be violated)
    pruned_setup = []
    for i, j, eff_slack in setup_edges:
        if eff_slack <= t_max_arr[i] + t_max_arr[j]:
            pruned_setup.append((i, j, eff_slack))

    n_viol_vars = len(pruned_setup)

    # Separate hard vs soft IO constraints
    # Hard PI->FF hold (positive corrected_slack): a_j <= corrected_slack, no w_m
    # Soft IO (everything else): a_j - w_m <= slack, penalty variable w_m >= 0
    #
    # Base latency correction for hard PI->FF hold.
    # V43: corrected_slack = slack (ideal clock, pre-CTS STA)
    # V44: corrected_slack = max(0, slack - base_latency_j)
    #   base_latency_j = actual H-tree clock delay (from Phase 1a balanced CTS).
    #   Rationale: relay adds a_j more clock delay on top of existing base_latency.
    #   Total clock latency = base_latency_j + a_j.
    #   Hold condition: data_delay >= (base_latency_j + a_j) + hold_time
    #   => a_j <= data_delay - hold_time - base_latency_j = slack - base_latency_j
    if io_edges is None:
        io_edges = []
    if base_latency_ns is None:
        base_latency_ns = {}

    hard_io = []   # (ff_idx, corrected_slack, etype) -- hard PI->FF hold, no w_m
    soft_io = []   # (ff_idx, slack, etype) -- soft, gets w_m
    n_base_corrected = 0
    for io_edge in io_edges:
        ff_idx = io_edge['ff_idx']
        slack  = io_edge['slack']
        etype  = io_edge['type']
        if etype == 'pi_hold' and hard_pi_hold:
            # V44: Apply base latency correction if available
            ff_name = ff_list[ff_idx] if ff_idx < len(ff_list) else ""
            base_ns = base_latency_ns.get(ff_name, 0.0)
            corrected_slack = max(0.0, slack - base_ns)
            if base_ns > 0:
                n_base_corrected += 1
            # Hard only if corrected slack is positive (lesson: hard -> infeasible)
            if corrected_slack > 0:
                hard_io.append((ff_idx, corrected_slack, etype))
            else:
                # Corrected slack <= 0: keep soft (already violated or fully consumed)
                soft_io.append((ff_idx, slack, etype))
        else:
            soft_io.append((ff_idx, slack, etype))

    n_io_hard = len(hard_io)
    n_io_soft = len(soft_io)

    # V43: Variable layout with optional WNS minimax variable W
    io_var_base = n_ffs + n_viol_vars
    n_vars = n_ffs + n_viol_vars + n_io_soft
    W_idx = -1
    if gamma_wns > 0:
        W_idx = n_vars
        n_vars += 1

    # Objective: lambda_reg*sum(a_i) + sum(v_k) + weight_io*sum(w_m) + gamma_wns*W
    c = np.zeros(n_vars)
    for i in range(n_ffs):
        c[i] = lambda_reg
    for k in range(n_viol_vars):
        c[n_ffs + k] = 1.0
    for m in range(n_io_soft):
        c[io_var_base + m] = weight_io
    if gamma_wns > 0:
        c[W_idx] = gamma_wns

    # V43: Build constraint matrix using COO sparse format
    from scipy.sparse import coo_matrix
    sp_rows, sp_cols, sp_vals = [], [], []
    sp_b = []
    ct = 0  # constraint row counter
    n_setup_ct = 0
    n_hold_ct  = 0

    # Setup constraints: a_i - a_j - v_k <= eff_setup_k
    for k, (i, j, eff_slack) in enumerate(pruned_setup):
        sp_rows.extend([ct, ct, ct])
        sp_cols.extend([i, j, n_ffs + k])
        sp_vals.extend([1.0, -1.0, -1.0])
        sp_b.append(eff_slack)
        ct += 1
        n_setup_ct += 1

    # Hold constraints: a_j - a_i <= eff_hold - hold_margin
    for i, j, eff_slack in hold_edges:
        if eff_slack - hold_margin > t_max_arr[i] + t_max_arr[j]:
            continue  # constraint always satisfied, prune
        sp_rows.extend([ct, ct])
        sp_cols.extend([j, i])
        sp_vals.extend([1.0, -1.0])
        sp_b.append(eff_slack - hold_margin)
        ct += 1
        n_hold_ct += 1

    # V43: Hard PI->FF hold constraints: a_j <= slack (no w_m variable)
    n_hard_ct = 0
    for ff_idx, slack, etype in hard_io:
        sp_rows.append(ct)
        sp_cols.append(ff_idx)
        sp_vals.append(1.0)
        sp_b.append(slack)
        ct += 1
        n_hard_ct += 1

    # IO soft constraints (with penalty variable w_m)
    n_io_ct = 0
    for m, (ff_idx, slack, etype) in enumerate(soft_io):
        w_idx = io_var_base + m

        if etype == 'pi_hold':
            # PI->FF hold (soft, negative slack): a_j - w_m <= slack
            sp_rows.extend([ct, ct])
            sp_cols.extend([ff_idx, w_idx])
            sp_vals.extend([1.0, -1.0])
        elif etype == 'pi_setup':
            # PI->FF setup: -a_j - w_m <= slack
            sp_rows.extend([ct, ct])
            sp_cols.extend([ff_idx, w_idx])
            sp_vals.extend([-1.0, -1.0])
        elif etype == 'po_setup':
            # FF->PO setup: a_i - w_m <= slack
            sp_rows.extend([ct, ct])
            sp_cols.extend([ff_idx, w_idx])
            sp_vals.extend([1.0, -1.0])
        elif etype == 'po_hold':
            # FF->PO hold: -a_i - w_m <= slack
            sp_rows.extend([ct, ct])
            sp_cols.extend([ff_idx, w_idx])
            sp_vals.extend([-1.0, -1.0])
        else:
            continue

        sp_b.append(slack)
        ct += 1
        n_io_ct += 1

    # V43: WNS minimax constraints: v_k - W <= 0 for all k
    n_wns_ct = 0
    if gamma_wns > 0:
        for k in range(n_viol_vars):
            sp_rows.extend([ct, ct])
            sp_cols.extend([n_ffs + k, W_idx])
            sp_vals.extend([1.0, -1.0])
            sp_b.append(0.0)
            ct += 1
            n_wns_ct += 1

    total_ct = ct
    print(f"  LP vars: {n_vars} (a:{n_ffs}, v:{n_viol_vars}, w_io:{n_io_soft}"
          + (f", W:1" if gamma_wns > 0 else "") + ")")
    print(f"  Setup ct: {n_setup_ct}, Hold ct: {n_hold_ct}", end="")
    if n_hard_ct > 0:
        print(f", Hard PI hold: {n_hard_ct}", end="")
    if n_io_ct > 0:
        print(f", IO soft: {n_io_ct}", end="")
    if n_wns_ct > 0:
        print(f", WNS minimax: {n_wns_ct}", end="")
    print(f", Total: {total_ct}")

    # Bounds: a_i in [0, t_max_i] (ours) or [-t_max_i, t_max_i] (Fishburn)
    # v_k in [0, inf), w_m in [0, inf), W in [0, inf)
    if fishburn:
        a_bounds = [(-float(t_max_arr[i]), float(t_max_arr[i])) for i in range(n_ffs)]
    else:
        a_bounds = [(0.0, float(t_max_arr[i])) for i in range(n_ffs)]
    bounds = (
        a_bounds +
        [(0.0, None) for _ in range(n_viol_vars)] +
        [(0.0, None) for _ in range(n_io_soft)]
    )
    if gamma_wns > 0:
        bounds.append((0.0, None))  # W >= 0

    # V43: Build sparse matrix (COO -> CSC, massive memory/speed improvement)
    A_ub = coo_matrix((sp_vals, (sp_rows, sp_cols)), shape=(total_ct, n_vars)).tocsc()
    b_ub = np.array(sp_b)

    import scipy
    _sv = tuple(int(x) for x in scipy.__version__.split('.')[:2])
    method = 'highs' if _sv >= (1, 9) else 'interior-point'
    lp_label = "LP-TNS (Fishburn a_i∈R)" if fishburn else "LP-TNS V43"

    # Report sparse matrix stats
    nnz = len(sp_vals)
    dense_bytes = total_ct * n_vars * 8
    sparse_bytes = nnz * (8 + 4 + 4)  # val + row_idx + col_idx
    print(f"\n  Solving {lp_label} ({n_vars} vars, {total_ct} constraints, {method}, sparse)")
    print(f"  Matrix: {total_ct}x{n_vars}, nnz={nnz}, "
          f"dense={dense_bytes/1e6:.1f}MB, sparse={sparse_bytes/1e6:.2f}MB")

    # Build stats dict to be written as JSON (V43/stats export)
    lp_stats = {
        "lp_mode":   "tns",
        "method":    method,
        "status":    "failed",
        "objective": None,
        "solve_time_s": None,
        "n_ff":      n_ffs,
        "vars":      {"total": n_vars, "a": n_ffs, "v": n_viol_vars,
                      "w_io": n_io_soft, "W": 1 if gamma_wns > 0 else 0},
        "constraints": {"total": total_ct, "setup": n_setup_ct, "hold": n_hold_ct,
                        "hard_pi_hold": n_hard_ct, "io_soft": n_io_ct,
                        "wns_minimax": n_wns_ct},
        "matrix":    {"rows": total_ct, "cols": n_vars, "nnz": nnz,
                      "dense_mb": round(dense_bytes / 1e6, 1),
                      "sparse_mb": round(sparse_bytes / 1e6, 3)},
        "targets":   {},
        "wns_minimax": {},
        "hard_pi_hold_report": {},
    }

    try:
        t0 = time.time()
        result = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method=method)
        lp_stats["solve_time_s"] = round(time.time() - t0, 2)

        if not result.success:
            print(f"  {lp_label} failed: {result.message}")
            lp_stats["status"] = result.message
            return {ff: 0.0 for ff in ff_list}, lp_stats

        a_vals = result.x[:n_ffs]
        v_vals = result.x[n_ffs:n_ffs + n_viol_vars]

        total_viol_before = sum(max(0.0, -s) for _, _, s in pruned_setup)
        total_viol_after  = float(np.sum(v_vals))
        tns_reduction_pct = (1.0 - total_viol_after / max(total_viol_before, 1e-9)) * 100.0

        print(f"  {lp_label} solved! (obj = {result.fun:.4f})")
        print(f"  Total setup violation: {total_viol_before*1e3:.1f}ps -> "
              f"{total_viol_after*1e3:.1f}ps  ({tns_reduction_pct:.1f}% reduction)")

        # Populate stats
        lp_stats["status"]    = "optimal"
        lp_stats["objective"] = round(float(result.fun), 6)
        lp_stats["targets"]   = {
            "tns_before_ps": round(total_viol_before * 1e3, 1),
            "tns_after_ps":  round(total_viol_after  * 1e3, 1),
            "tns_reduction_pct": round(tns_reduction_pct, 1),
        }

        # V43: WNS minimax report
        if gamma_wns > 0:
            W_val = result.x[W_idx]
            worst_vk = float(np.max(v_vals)) if n_viol_vars > 0 else 0.0
            print(f"  Worst violation W: {W_val*1e3:.1f}ps "
                  f"(gamma_wns={gamma_wns}, worst v_k={worst_vk*1e3:.1f}ps)")
            lp_stats["wns_minimax"] = {
                "W_val_ps":   round(W_val * 1e3, 1),
                "worst_vk_ps": round(worst_vk * 1e3, 1),
                "gamma_wns":  gamma_wns,
            }

        # IO soft violation summary
        if n_io_soft > 0:
            w_vals = result.x[io_var_base:io_var_base + n_io_soft]
            total_io_viol = float(np.sum(w_vals))
            n_io_active = int(np.sum(w_vals > 1e-6))
            print(f"  IO soft violations: {n_io_active}/{n_io_soft} active, "
                  f"total penalty: {total_io_viol*1e3:.1f}ps")
            # Breakdown by type
            io_type_viol = {}
            for m, (_, _, etype) in enumerate(soft_io):
                if w_vals[m] > 1e-6:
                    io_type_viol[etype] = io_type_viol.get(etype, 0) + 1
            for etype in ['pi_hold', 'pi_setup', 'po_setup', 'po_hold']:
                if etype in io_type_viol:
                    print(f"    {etype}: {io_type_viol[etype]} active violations")

        # V43/V44: Hard PI->FF hold report
        if n_io_hard > 0:
            affected_ffs = set(ff_idx for ff_idx, _, _ in hard_io)
            n_clipped = 0
            for ff_idx in affected_ffs:
                min_slack = min(s for fi, s, _ in hard_io if fi == ff_idx)
                if a_vals[ff_idx] >= min_slack - 1e-6:
                    n_clipped += 1
            base_note = (f", {n_base_corrected} base-latency-corrected"
                         if n_base_corrected > 0 else "")
            print(f"  Hard PI->FF hold: {n_io_hard} constraints on "
                  f"{len(affected_ffs)} FFs, {n_clipped} FFs clipped{base_note}")
            lp_stats["hard_pi_hold_report"] = {
                "n_constraints": n_io_hard,
                "n_ffs":         len(affected_ffs),
                "n_clipped":     n_clipped,
            }

        n_nonzero = sum(1 for a in a_vals if a > 1e-6)
        a_max     = float(np.max(a_vals)) if n_nonzero > 0 else 0.0
        a_mean_nz = float(np.mean([a for a in a_vals if a > 1e-6])) if n_nonzero > 0 else 0.0
        print(f"  FFs with non-zero target: {n_nonzero} / {n_ffs}")
        print(f"  Max target: {a_max*1e3:.1f}ps")
        lp_stats["targets"].update({
            "non_zero":   n_nonzero,
            "mean_nz_ps": round(a_mean_nz * 1e3, 2),
            "max_ps":     round(a_max      * 1e3, 2),
        })

        return {ff: float(a_vals[i]) for i, ff in enumerate(ff_list)}, lp_stats

    except Exception as e:
        print(f"  {lp_label} solver error: {e}")
        import traceback
        traceback.print_exc()
        lp_stats["status"] = f"exception: {e}"
        return {ff: 0.0 for ff in ff_list}, lp_stats


# ---------------------------------------------------------------------------
# LP-SPEED solver (legacy from V31, kept for backward compatibility)
# ---------------------------------------------------------------------------

def solve_lp_speed(edges, ff_list, ff_tiers,
                   max_skew=0.100,
                   hold_margin=0.010,
                   lambda_reg=0.01):
    """
    Original LP-SPEED formulation: maximize total setup slack improvement.
    Kept from for backward compatibility; default is LP-SAFETY.
    NOTE: This still uses symmetric a_i bounds [-max_skew, +max_skew] (behavior).
    """
    if not HAS_SCIPY:
        print("ERROR: scipy not available")
        return {}

    n_ffs = len(ff_list)
    if n_ffs == 0:
        return {}

    ff_to_idx = {ff: i for i, ff in enumerate(ff_list)}

    setup_edges = []
    hold_edges  = []
    n_cross = 0

    for edge in edges:
        i = ff_to_idx.get(edge['from_ff'])
        j = ff_to_idx.get(edge['to_ff'])
        if i is None or j is None:
            continue

        setup_edges.append((i, j, edge['slack_setup'], edge['from_tier'] != edge['to_tier']))
        hold_edges.append( (i, j, edge['slack_hold'],  edge['from_tier'] != edge['to_tier']))
        if edge['from_tier'] != edge['to_tier']:
            n_cross += 1

    n_neg_setup = sum(1 for _, _, s, _ in setup_edges if s < 0)
    n_neg_hold  = sum(1 for _, _, s, _ in hold_edges  if s < hold_margin)

    print(f"  FFs: {n_ffs}")
    print(f"  Timing edges: {len(setup_edges)} ({n_cross} cross-tier)")
    print(f"  Negative setup slack: {n_neg_setup}")
    print(f"  Tight hold (< {hold_margin*1e3:.0f}ps): {n_neg_hold}")

    if n_neg_setup == 0:
        print("  No negative setup slack — targets set to 0.0")
        return {ff: 0.0 for ff in ff_list}

    neg_setup_indices = [idx for idx, (_, _, s, _) in enumerate(setup_edges) if s < 0]
    n_ve   = len(neg_setup_indices)
    n_vars = 2 * n_ffs + n_ve

    c = np.zeros(n_vars)
    for k in range(n_ve):
        c[2 * n_ffs + k] = -1.0
    for i in range(n_ffs):
        c[n_ffs + i] = lambda_reg

    ve_idx_map = {edge_idx: k for k, edge_idx in enumerate(neg_setup_indices)}

    constraints_A = []
    constraints_b = []

    for idx, (i, j, slack, _) in enumerate(setup_edges):
        if slack > 2 * max_skew:
            continue
        row = np.zeros(n_vars)
        row[j] =  1.0
        row[i] = -1.0
        if idx in ve_idx_map:
            row[2 * n_ffs + ve_idx_map[idx]] = 1.0
        constraints_A.append(row)
        constraints_b.append(slack)

    for i, j, slack, _ in hold_edges:
        hold_rhs = slack - hold_margin
        if hold_rhs > 2 * max_skew:
            continue
        row = np.zeros(n_vars)
        row[i] =  1.0
        row[j] = -1.0
        constraints_A.append(row)
        constraints_b.append(hold_rhs)

    for i in range(n_ffs):
        row  = np.zeros(n_vars); row[i]  =  1.0; row[n_ffs + i] = -1.0
        row2 = np.zeros(n_vars); row2[i] = -1.0; row2[n_ffs + i] = -1.0
        constraints_A.append(row);  constraints_b.append(0.0)
        constraints_A.append(row2); constraints_b.append(0.0)

    bounds = (
        [(-max_skew, max_skew)] * n_ffs +
        [(0, max_skew)] * n_ffs +
        [(None, 0)] * n_ve
    )

    A_ub = np.array(constraints_A) if constraints_A else np.zeros((0, n_vars))
    b_ub = np.array(constraints_b) if constraints_b else np.zeros(0)

    import scipy
    _sv = tuple(int(x) for x in scipy.__version__.split('.')[:2])
    method = 'highs' if _sv >= (1, 9) else 'interior-point'
    print(f"\n  Solving LP-SPEED ({n_vars} vars, {len(constraints_A)} constraints, {method})...")

    try:
        result = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method=method)

        if not result.success:
            print(f"  LP-SPEED failed: {result.message}")
            return {ff: 0.0 for ff in ff_list}

        print(f"  LP-SPEED solved! (obj = {result.fun:.6f})")
        a_vals = result.x[:n_ffs]

        v_vals = result.x[2*n_ffs:] if n_ve > 0 else np.array([])
        if n_ve > 0:
            n_violated = int(np.sum(np.abs(v_vals) > 1e-6))
            print(f"  Setup violations remaining: {n_violated}/{n_ve}")
            print(f"  Total violation: {np.sum(np.abs(v_vals))*1e3:.1f} ps")

        t_max_arr = np.full(n_ffs, max_skew)
        _print_stats(a_vals, None, setup_edges, n_ffs, ff_list, ff_tiers, t_max_arr)
        return {ff: a_vals[i] for i, ff in enumerate(ff_list)}

    except Exception as e:
        print(f"  LP-SPEED solver error: {e}")
        import traceback
        traceback.print_exc()
        return {ff: 0.0 for ff in ff_list}


# ---------------------------------------------------------------------------
# Fishburn comparison: clamp-induced hold violation check
# ---------------------------------------------------------------------------

def _check_clamp_hold_violations(fish_arrivals, ff_list, edges, sigma_local, hold_margin):
    """Check if clamping Fishburn a_i to max(0, a_i) introduces hold violations.

    Uses edges already filtered by parse_ff_timing_graph() ghost edge filter.
    Only checks FF->FF hold constraints (IO hold checked separately).
    Returns list of violation dicts sorted by magnitude (worst first).
    """
    clamped = {ff: max(0.0, a) for ff, a in fish_arrivals.items()}

    violations = []
    for edge in edges:
        i_ff = edge['from_ff']
        j_ff = edge['to_ff']
        eff_hold = edge['slack_hold'] - 2 * sigma_local

        a_i = clamped.get(i_ff, 0.0)
        a_j = clamped.get(j_ff, 0.0)

        # Hold constraint: a_j - a_i <= eff_hold - hold_margin
        violation = (a_j - a_i) - (eff_hold - hold_margin)
        if violation > 1e-6:
            violations.append({
                'from_ff': i_ff, 'to_ff': j_ff,
                'violation_ns': violation,
                'eff_hold_ns': eff_hold
            })

    violations.sort(key=lambda v: -v['violation_ns'])
    return violations


def print_fishburn_comparison(ours_arrivals, ours_stats, fish_arrivals, fish_stats,
                              ff_list, ff_tiers, edges, sigma_local, hold_margin,
                              fishburn_csv=''):
    """Print 4-section Fishburn vs Ours comparison report.

    Sections:
      (a) Objective comparison
      (b) Negative arrival statistics
      (c) Clamp-induced hold violation analysis
      (d) Per-FF comparison table (top-20)
    """
    n_ffs = len(ff_list)

    # --- (a) Objective comparison ---
    ours_obj = ours_stats.get("objective", 0.0) or 0.0
    fish_obj = fish_stats.get("objective", 0.0) or 0.0
    ours_tns = ours_stats.get("targets", {}).get("tns_after_ps", 0.0) or 0.0
    fish_tns = fish_stats.get("targets", {}).get("tns_after_ps", 0.0) or 0.0

    print("\n--- (a) LP Objective Comparison ---")
    print(f"  Ours     (a_i >= 0):  obj = {ours_obj:.6f},  Setup TNS after LP = {ours_tns:.1f} ps")
    print(f"  Fishburn (a_i in R):  obj = {fish_obj:.6f},  Setup TNS after LP = {fish_tns:.1f} ps")
    if ours_tns > 0:
        gap_pct = (1.0 - fish_tns / ours_tns) * 100.0
        print(f"  Gap: Fishburn {gap_pct:.1f}% better TNS (theoretical, unrealizable)")
    elif fish_tns == 0 and ours_tns == 0:
        print(f"  Gap: Both achieve zero violation (no setup timing pressure)")
    else:
        print(f"  Gap: Fishburn TNS = {fish_tns:.1f} ps vs Ours TNS = {ours_tns:.1f} ps")

    # --- (b) Negative arrival statistics ---
    neg_ffs = [(ff, fish_arrivals.get(ff, 0.0)) for ff in ff_list
               if fish_arrivals.get(ff, 0.0) < -1e-6]
    n_neg = len(neg_ffs)
    neg_ffs.sort(key=lambda x: x[1])  # most negative first

    print(f"\n--- (b) Negative Arrival Statistics ---")
    print(f"  Negative arrivals: {n_neg} / {n_ffs} FFs ({n_neg/max(n_ffs,1)*100:.1f}%)")
    if n_neg > 0:
        mean_neg = sum(a for _, a in neg_ffs) / n_neg
        min_neg_ff, min_neg_val = neg_ffs[0]
        print(f"  Mean negative a_i: {mean_neg*1e3:.1f} ps")
        print(f"  Min  negative a_i: {min_neg_val*1e3:.1f} ps (FF: {min_neg_ff})")
        print(f"  Top-5 most negative:")
        for ff, val in neg_ffs[:5]:
            tier = ff_tiers.get(ff, 0)
            print(f"    {ff}: {val*1e3:.1f} ps (tier {tier})")
    else:
        print("  No negative arrivals — Fishburn and Ours produce identical bounds for this design")

    # --- (c) Clamp-induced hold violation analysis ---
    violations = _check_clamp_hold_violations(
        fish_arrivals, ff_list, edges, sigma_local, hold_margin)

    print(f"\n--- (c) Clamp-Induced Hold Violation Analysis ---")
    print(f"  (Fishburn a_i clamped to max(0, a_i), then hold constraints checked)")
    if violations:
        total_hold_tns = sum(v['violation_ns'] for v in violations)
        worst = violations[0]
        print(f"  Hold violations introduced: {len(violations)} edges")
        print(f"  Worst hold violation: {worst['violation_ns']*1e3:.1f} ps "
              f"({worst['from_ff']} -> {worst['to_ff']})")
        print(f"  Total hold TNS from clamp: {total_hold_tns*1e3:.1f} ps")
    else:
        print(f"  No hold violations introduced by clamping")
        if n_neg > 0:
            print(f"  (Fishburn has negative arrivals but clamping is hold-safe for this design)")

    # --- (d) Per-FF comparison table (top-20) ---
    diffs = []
    for ff in ff_list:
        ours_a = ours_arrivals.get(ff, 0.0)
        fish_a = fish_arrivals.get(ff, 0.0)
        diffs.append((ff, ours_a, fish_a, fish_a - ours_a))
    diffs.sort(key=lambda x: -abs(x[3]))

    print(f"\n--- (d) Per-FF Comparison (top-20 by |delta|) ---")
    print(f"  {'FF Name':<40s} {'Ours(ps)':>9s} {'Fish(ps)':>9s} {'Delta(ps)':>10s} {'Tier':>4s}")
    print(f"  {'-'*40} {'-'*9} {'-'*9} {'-'*10} {'-'*4}")
    for ff, ours_a, fish_a, delta in diffs[:20]:
        tier = ff_tiers.get(ff, 0)
        print(f"  {ff:<40s} {ours_a*1e3:>+9.1f} {fish_a*1e3:>+9.1f} {delta*1e3:>+10.1f} {tier:>4d}")

    # --- Optional CSV output ---
    if fishburn_csv:
        import csv as csv_mod
        with open(fishburn_csv, 'w', newline='') as f:
            writer = csv_mod.DictWriter(f, fieldnames=[
                'ff_name', 'ours_a_ns', 'fishburn_a_ns', 'is_negative',
                'clamp_delta_ns', 'tier'])
            writer.writeheader()
            for ff in ff_list:
                ours_a = ours_arrivals.get(ff, 0.0)
                fish_a = fish_arrivals.get(ff, 0.0)
                writer.writerow({
                    'ff_name': ff,
                    'ours_a_ns': f"{ours_a:.6f}",
                    'fishburn_a_ns': f"{fish_a:.6f}",
                    'is_negative': 1 if fish_a < -1e-6 else 0,
                    'clamp_delta_ns': f"{max(0.0, fish_a) - fish_a:.6f}",
                    'tier': ff_tiers.get(ff, 0)
                })
        print(f"\n  Fishburn comparison CSV written: {fishburn_csv}")


# ---------------------------------------------------------------------------
# Shared statistics printer
# ---------------------------------------------------------------------------

def _print_stats(a_vals, M_val, setup_edges, n_ffs, ff_list, ff_tiers, t_max_arr):
    """Print solution statistics and top FFs by target magnitude."""
    n_nonzero  = int(np.sum(np.abs(a_vals) > 1e-6))
    n_positive = int(np.sum(a_vals > 1e-6))
    # In V32, a_i >= 0 always; n_negative expected to be 0
    n_negative = int(np.sum(a_vals < -1e-6))
    a_range    = float(np.max(a_vals) - np.min(a_vals)) if n_ffs > 0 else 0.0

    print(f"\n  Results:")
    print(f"    FFs with non-zero target: {n_nonzero} / {n_ffs}")
    print(f"    Non-zero arrivals (delay added): {n_positive}")
    if n_negative:
        print(f"    WARNING: {n_negative} negative arrivals (should be 0 in V32)")
    print(f"    Arrival range: [{float(np.min(a_vals))*1e3:.1f}, {float(np.max(a_vals))*1e3:.1f}] ps")
    print(f"    Total spread: {a_range*1e3:.1f} ps")

    if M_val is not None:
        if M_val >= 0:
            print(f"    Minimum margin M: +{M_val*1e3:.2f} ps (all paths feasible)")
        else:
            print(f"    Minimum margin M: {M_val*1e3:.2f} ps (violations balanced)")

    # Utilization of per-FF budget
    if t_max_arr is not None:
        util = a_vals / (t_max_arr + 1e-12)  # avoid div-by-zero
        mean_util = float(np.mean(util[t_max_arr > 1e-9]))
        print(f"    Mean budget utilization: {mean_util*100:.1f}%")

    # Top 10 FFs by target value
    sorted_by_val = sorted(
        [(ff, float(a_vals[i])) for i, ff in enumerate(ff_list)],
        key=lambda x: -x[1]
    )[:10]
    print(f"\n  Top 10 FFs by target arrival:")
    for ff, val in sorted_by_val:
        tier = ff_tiers.get(ff, 0)
        tmax = float(t_max_arr[ff_list.index(ff)]) if ff_list.index(ff) < len(t_max_arr) else 0.0
        print(f"    {ff}: {val*1e3:+.1f}ps / {tmax*1e3:.0f}ps budget (tier {tier})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Pre-CTS Skew Target LP Solver (Fishburn 1990 + 3D extension)')
    parser.add_argument('input_csv',  help='FF timing graph CSV')
    parser.add_argument('output_csv', help='Output per-FF skew targets CSV')

    parser.add_argument('--lp-mode', choices=['speed', 'safety', 'tns'], default='tns',
                        help='LP formulation: tns=LP-TNS minimize total violation (default), '
                             'safety=LP-SAFETY maximin V32, '
                             'speed=maximize total slack improvement (legacy)')
    parser.add_argument('--max-skew', type=float, default=0.100,
                        help='Max arrival offset per FF in ns (default: 100ps)')

    # V32: bounds from ClockLatencyEstimator
    parser.add_argument('--bounds-csv', type=str, default=None,
                        help='Per-FF physical bounds CSV from cts::estimate_leaf_latencies '
                             '(ff_name,tier,t_min_ns,t_max_ns). '
                             'If not provided, uses --t-via fallback.')
    parser.add_argument('--t-via', type=float, default=0.0,
                        help='HB via delay in ns for fallback bounds '
                             '(used when --bounds-csv is absent). '
                             'upper-tier FF t_max = max_skew + t_via. (default: 0)')

    # LP-SAFETY specific
    parser.add_argument('--sigma-local', type=float, default=0.005,
                        help='[safety] Same-tier clock uncertainty in ns (default: 5ps)')
    # PI->FF hold safety margin for per_ff_tmax clipping.
    # Any FF with PI->FF hold slack S is clipped: per_ff_tmax = max(0, S - sigma_pi).
    # Prevents H-tree from delivering LP target that exceeds PI->FF hold budget.
    parser.add_argument('--sigma-pi', type=float, default=0.005,
                        help='PI->FF hold safety margin in ns for tmax clipping (default: 5ps)')

    # LP-SPEED specific (legacy)
    parser.add_argument('--hold-margin', type=float, default=0.010,
                        help='[speed] Hold safety guard band in ns (default: 10ps)')

    # V38: IO timing edges for PI→FF hold / FF→PO setup constraints
    parser.add_argument('--io-csv', type=str, default='',
                        help='IO timing edges CSV (from extract_io_timing_edges Tcl)')
    parser.add_argument('--weight-io', type=float, default=1.0,
                        help='Weight for IO violation penalty in LP objective (default: 1.0)')

    # V43: WNS minimax and hard PI->FF hold
    parser.add_argument('--gamma-wns', type=float, default=0.0,
                        help='WNS minimax penalty weight (0=disabled, default: 0)')
    parser.add_argument('--hard-pi-hold', action='store_true', default=False,
                        help='Make PI->FF hold constraints hard for positive-slack edges (V43)')

    # V44: Base latency CSV for corrected PI->FF hold constraint (2-pass CTS)
    parser.add_argument('--base-latency-csv', type=str, default='',
                        help='Per-FF base clock latency CSV from Phase 1a balanced CTS '
                             '(ff_name,base_latency_ps). Used to correct hard PI->FF hold: '
                             'a_j <= max(0, slack - base_latency_j). (V44)')

    # Common
    parser.add_argument('--lambda-reg', type=float, default=0.01,
                        help='Regularization weight for arrival spread (default: 0.01)')
    # V43: Write LP solver stats JSON for HTML debug reports
    parser.add_argument('--stats-json', type=str, default='',
                        help='Optional path to write LP solver statistics as JSON')

    # Fishburn comparison experiment (paper evidence)
    parser.add_argument('--fishburn', action='store_true', default=False,
                        help='Run Fishburn LP (a_i in [-t_max,+t_max]) alongside ours for comparison')
    parser.add_argument('--fishburn-csv', type=str, default='',
                        help='Optional CSV output path for Fishburn per-FF comparison data')

    # V51-impact: Impact-based cutoff replaces flat MIN_DELTA threshold.
    # impact = target × |worst_setup_slack| for each FF.
    # FFs with impact < threshold have their target zeroed (depth=0 in C++).
    # This prioritizes buffer insertion on critical-path FFs over non-critical ones.
    parser.add_argument('--impact-cutoff', type=float, default=0.0,
                        help='Impact threshold (ps^2). impact = target_ps * |worst_slack_ps|. '
                             'FFs with impact < cutoff get target=0. 0=disabled (default)')

    args = parser.parse_args()

    mode_label = {
        'tns':   'LP-TNS (minimize total setup violation, a_i >= 0)',
        'safety': 'LP-SAFETY (Fishburn 1990 maximin, a_i >= 0)',
        'speed':  'LP-SPEED (legacy formulation)',
    }.get(args.lp_mode, args.lp_mode)
    print("=" * 60)
    print("Pre-CTS Skew Target LP Solver (SG-CTS)")
    print(f"  Mode: {mode_label}")
    print("=" * 60)

    if not HAS_SCIPY:
        print("\nERROR: scipy required. Install: pip install scipy numpy")
        return 1

    print(f"\nReading: {args.input_csv}")
    edges, ff_list, ff_tiers = parse_ff_timing_graph(args.input_csv)
    print(f"  {len(ff_list)} FFs, {len(edges)} timing edges")

    if not edges:
        print("\nERROR: No timing edges found")
        return 1

    # V38: Parse IO timing edges if provided
    ff_to_idx = {ff: i for i, ff in enumerate(ff_list)}
    io_edges = []
    if args.io_csv and os.path.exists(args.io_csv):
        print(f"\nReading IO edges: {args.io_csv}")
        io_edges = parse_io_timing_edges(args.io_csv, ff_to_idx)
    else:
        print(f"\n  No IO edges CSV (--io-csv not provided or file missing)")

    # Parse per-FF base clock latency from Phase 1a balanced CTS.
    # Used to correct hard PI->FF hold constraint: a_j <= max(0, slack - base_latency_j).
    base_latency_ns = {}
    if args.base_latency_csv and os.path.exists(args.base_latency_csv):
        print(f"\nReading base latency: {args.base_latency_csv}")
        with open(args.base_latency_csv) as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or line.startswith('ff_name'):
                    continue
                parts = line.split(',')
                if len(parts) >= 2:
                    ff_name = parts[0].strip()
                    try:
                        base_ps = float(parts[1])
                        base_latency_ns[ff_name] = base_ps * 1e-3  # ps -> ns
                    except ValueError:
                        pass
        if base_latency_ns:
            base_vals = list(base_latency_ns.values())
            mean_ps = sum(base_vals) / len(base_vals) * 1e3
            print(f"  {len(base_latency_ns)} FFs loaded, mean base latency: {mean_ps:.1f}ps")
        else:
            print(f"  WARNING: No valid entries in base latency CSV")
    else:
        if args.base_latency_csv:
            print(f"\n  Base latency CSV not found: {args.base_latency_csv} (skipping correction)")

    print(f"\nSolving...")
    print(f"  max_skew: {args.max_skew*1e3:.0f} ps")

    # Parse per-FF physical achievability bounds (shared by tns and safety modes)
    per_ff_tmax = parse_bounds_csv(
        args.bounds_csv, ff_list, ff_tiers,
        args.max_skew, args.t_via
    )

    # Fishburn comparison: save bounds before PI-clip.
    # Fishburn LP uses original (unclipped) bounds — PI-clip is our innovation.
    per_ff_tmax_before_piclip = dict(per_ff_tmax)

    # V50-2PASS: PI-hold-zero — zero per_ff_tmax for slack<0 FFs.
    # Extended to PI-hold-clip — also clip tmax for slack>=0 FFs.
    # Root cause of hold regression (V50-Legal): LP assigns a_j>0 to a capture FF whose
    # PI->FF hold slack is positive but small (e.g. +10ps). H-tree then delivers +25ps
    # clock latency to that FF. PI->FF hold: 10-25 = -15ps -> violated.
    # V50-2PASS pi_hold_zero only protected slack<0 FFs; left slack>0 FFs unguarded.
    # fix: for every FF with any PI->FF hold edge, clip:
    #   per_ff_tmax[ff] = max(0, min(existing_tmax, pi_slack_min - sigma_pi))
    # where sigma_pi is a per-path safety margin (default 5ps, same as sigma_local).
    # This ensures LP never assigns a target that would push PI->FF hold into violation.
    # FFs with no PI->FF paths are unaffected (safe for all designs).
    n_pi0_zeroed = 0   # zeroed (slack < 0)
    n_pi1_clipped = 0  # clipped (0 <= slack < existing tmax)
    sigma_pi = args.sigma_pi  # PI->FF hold safety margin (ns)
    if io_edges:
        # Build per-FF minimum PI->FF hold slack (most restrictive path per FF)
        pi_hold_slack_map = {}  # ff_name -> min pi_hold_slack across all PI->FF paths
        for e in io_edges:
            if e.get('type') == 'pi_hold':
                ff = ff_list[e['ff_idx']]
                s = e.get('slack', 0.0)
                if ff not in pi_hold_slack_map or s < pi_hold_slack_map[ff]:
                    pi_hold_slack_map[ff] = s
        # Clip per_ff_tmax for every FF that has a PI->FF hold constraint
        for ff, pi_slack in pi_hold_slack_map.items():
            safe_max = max(0.0, pi_slack - sigma_pi)
            current_tmax = per_ff_tmax.get(ff, args.max_skew)
            if safe_max < current_tmax:
                per_ff_tmax[ff] = safe_max
                if pi_slack < 0.0:
                    n_pi0_zeroed += 1
                else:
                    n_pi1_clipped += 1
        print(f"\n[PI-clip] {len(pi_hold_slack_map)} FFs have PI->FF hold paths "
              f"(sigma_pi={sigma_pi*1e3:.0f}ps): "
              f"{n_pi0_zeroed} zeroed (slack<0), "
              f"{n_pi1_clipped} clipped (0<=slack<tmax)")

    if args.lp_mode == 'tns':
        # LP-TNS — minimize total setup violation
        # + IO soft constraints (PI→FF hold, FF→PO setup)
        # + base latency correction for hard PI->FF hold
        # V50-2PASS: + PI-hold-zero unconditional (per_ff_tmax=0 for PI->FF hold-critical FFs)
        print(f"  sigma_local: {args.sigma_local*1e3:.1f} ps")
        print(f"  hold_margin: {args.hold_margin*1e3:.1f} ps")
        print(f"  lambda_reg:  {args.lambda_reg}")
        if io_edges:
            print(f"  weight_io:   {args.weight_io}")
        if args.gamma_wns > 0:
            print(f"  gamma_wns:   {args.gamma_wns}")
        if args.hard_pi_hold:
            print(f"  hard_pi_hold: enabled")
        print(f"  pi_hold_clip: zeroed={n_pi0_zeroed}, clipped={n_pi1_clipped} (sigma_pi={sigma_pi*1e3:.0f}ps)")
        if base_latency_ns:
            print(f"  base_latency: {len(base_latency_ns)} FFs (2-pass CTS correction)")
        arrivals, lp_stats = solve_lp_tns(
            edges, ff_list, ff_tiers, per_ff_tmax,
            hold_margin=args.hold_margin,
            sigma_local=args.sigma_local,
            lambda_reg=args.lambda_reg,
            io_edges=io_edges,
            weight_io=args.weight_io,
            gamma_wns=args.gamma_wns,
            hard_pi_hold=args.hard_pi_hold,
            base_latency_ns=base_latency_ns
        )
    elif args.lp_mode == 'safety':
        print(f"  sigma_local: {args.sigma_local*1e3:.1f} ps")
        print(f"  t_via (fallback): {args.t_via*1e3:.1f} ps")
        print(f"  lambda_reg:  {args.lambda_reg}")
        arrivals = solve_lp_safety(
            edges, ff_list, ff_tiers, per_ff_tmax,
            sigma_local=args.sigma_local,
            lambda_reg=args.lambda_reg
        )
        lp_stats = {"lp_mode": "safety", "status": "no_stats"}
    else:
        print(f"  [LP-SPEED legacy] hold_margin: {args.hold_margin*1e3:.1f} ps")
        print(f"  lambda_reg: {args.lambda_reg}")
        arrivals = solve_lp_speed(
            edges, ff_list, ff_tiers,
            max_skew=args.max_skew,
            hold_margin=args.hold_margin,
            lambda_reg=args.lambda_reg
        )
        lp_stats = {"lp_mode": "speed", "status": "no_stats"}

    if not arrivals:
        print("\nERROR: LP returned no results")
        return 1

    # V51-impact: Impact-based cutoff — zero out low-impact FF targets.
    # impact = target_ps × |worst_setup_slack_ps| for each FF.
    # Prioritizes buffer insertion on critical-path FFs.
    impact_cutoff = args.impact_cutoff  # ps^2 units
    n_impact_zeroed = 0
    if impact_cutoff > 0:
        # Build per-FF worst setup slack from timing edges
        ff_worst_setup_slack = {}  # ff_name -> worst (most negative) setup slack in ps
        for e in edges:
            from_ff = e['from_ff']
            to_ff   = e['to_ff']
            slack_ps = e['slack_setup'] * 1000.0  # ns -> ps
            # Both launch and capture FF contribute to setup criticality
            for ff in [from_ff, to_ff]:
                if ff in ff_worst_setup_slack:
                    ff_worst_setup_slack[ff] = min(ff_worst_setup_slack[ff], slack_ps)
                else:
                    ff_worst_setup_slack[ff] = slack_ps

        # Compute impact and zero low-impact targets
        impact_stats = []
        for ff in ff_list:
            target = max(0.0, arrivals.get(ff, 0.0))
            if target < 1e-9:
                continue
            target_ps = target * 1000.0
            worst_slack_ps = ff_worst_setup_slack.get(ff, 0.0)
            impact = target_ps * abs(worst_slack_ps)
            impact_stats.append((ff, target_ps, worst_slack_ps, impact))
            if impact < impact_cutoff:
                arrivals[ff] = 0.0
                n_impact_zeroed += 1

        # Sort by impact for reporting
        impact_stats.sort(key=lambda x: x[3], reverse=True)
        print(f"\n[Impact cutoff] threshold={impact_cutoff:.0f} ps^2, "
              f"zeroed={n_impact_zeroed}/{len(impact_stats)} FFs")
        if impact_stats:
            print(f"  Top-5 impact FFs:")
            for ff, tgt, slk, imp in impact_stats[:5]:
                print(f"    {ff}: target={tgt:.1f}ps × |slack|={abs(slk):.1f}ps = impact={imp:.0f}")
            if n_impact_zeroed > 0:
                kept = [x for x in impact_stats if x[3] >= impact_cutoff]
                zeroed = [x for x in impact_stats if x[3] < impact_cutoff]
                print(f"  Kept: {len(kept)} FFs (impact >= {impact_cutoff:.0f})")
                print(f"  Zeroed: {len(zeroed)} FFs (impact < {impact_cutoff:.0f})")
                if zeroed:
                    print(f"  Zeroed range: impact {zeroed[-1][3]:.0f} - {zeroed[0][3]:.0f} ps^2")

    # Write output: ALL FFs get a target (always >= 0.0 in LP-SAFETY mode)
    print(f"\nWriting: {args.output_csv}")
    n_nonzero = 0
    with open(args.output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['ff_name', 'target_arrival_ns', 'tier'])
        writer.writeheader()
        for ff in ff_list:
            target = max(0.0, arrivals.get(ff, 0.0))  # clamp to non-negative
            tier   = ff_tiers.get(ff, 0)
            writer.writerow({
                'ff_name':           ff,
                'target_arrival_ns': f"{target:.6f}",
                'tier':              tier
            })
            if target > 1e-6:
                n_nonzero += 1

    print(f"  Written {len(ff_list)} FF targets ({n_nonzero} non-zero, all >= 0)"
          + (f", {n_impact_zeroed} impact-zeroed" if n_impact_zeroed > 0 else ""))

    # V43: Write LP solver stats JSON for HTML debug reports
    if args.stats_json:
        import json
        with open(args.stats_json, 'w') as f:
            json.dump(lp_stats, f, indent=2)
        print(f"LP stats written: {args.stats_json}")

    print("\n" + "=" * 60)
    print("Pre-CTS skew target LP complete")
    print("=" * 60)

    # Fishburn LP comparison experiment
    # Runs Fishburn LP (a_i in [-t_max_i, +t_max_i]) and compares with our LP (a_i >= 0).
    # Uses original (pre-PI-clip) bounds for Fishburn — PI-clip is our innovation.
    if args.fishburn and args.lp_mode == 'tns':
        print("\n" + "=" * 60)
        print("Fishburn LP Comparison (a_i in [-t_max_i, +t_max_i])")
        print("=" * 60)

        fishburn_arrivals, fishburn_stats = solve_lp_tns(
            edges, ff_list, ff_tiers, per_ff_tmax_before_piclip,
            hold_margin=args.hold_margin,
            sigma_local=args.sigma_local,
            lambda_reg=args.lambda_reg,
            io_edges=io_edges,
            weight_io=args.weight_io,
            gamma_wns=args.gamma_wns,
            hard_pi_hold=args.hard_pi_hold,
            base_latency_ns=base_latency_ns,
            fishburn=True
        )

        if fishburn_stats.get("status") == "optimal":
            print_fishburn_comparison(
                ours_arrivals=arrivals, ours_stats=lp_stats,
                fish_arrivals=fishburn_arrivals, fish_stats=fishburn_stats,
                ff_list=ff_list, ff_tiers=ff_tiers,
                edges=edges, sigma_local=args.sigma_local,
                hold_margin=args.hold_margin,
                fishburn_csv=args.fishburn_csv
            )
        else:
            print(f"  Fishburn LP failed: {fishburn_stats.get('status', 'unknown')}")
            print("  Comparison skipped (Fishburn LP did not converge)")

    return 0


if __name__ == '__main__':
    sys.exit(main())
