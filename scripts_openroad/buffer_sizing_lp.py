#!/usr/bin/env python3
"""
LP-Based Pareto Buffer Sizing for 3D CTS (V35)

Instead of greedy one-step net-benefit, uses LP with Pareto-optimal buffer sizes
to find globally optimal sizing for all clock buffers simultaneously.

Key idea:
  - For each clock buffer, enumerate available sizes and their delays
  - Build Pareto frontier: (delay, drive_strength) non-dominated set
  - LP variable: d_k = delay of buffer k (continuous, convex combination of Pareto points)
  - Objective: maximize setup improvement + hold protection (same as buffer_insertion_lp.py)
  - Constraints: setup/hold slack preservation per timing edge
  - Post-LP: map continuous delay to nearest discrete buffer size

addition (--skew-targets):
  - Load post-CTS LP per-FF arrival targets (from cts_skew_lp.py run on post-CTS graph)
  - Aggregate to per-buffer target: target_k = mean(a_i for FFs driven by buffer k)
  - Add soft constraint to objective: skew_weight * |d_k - (d_k_current + target_k)|
  - This guides buffer sizing toward the globally-optimal skew solution

Usage:
  python buffer_sizing_lp.py <timing_edges.csv> <buffer_info.csv> <output.csv> \
    --buf-masters <masters.csv> --buf-delays <delays.csv> [--skew-targets <targets.csv>]
"""

import argparse
import csv
import sys
import numpy as np
from collections import defaultdict

try:
    from scipy.optimize import linprog
except ImportError:
    print("ERROR: scipy not found. Install with: pip install scipy")
    sys.exit(1)


# =========================================
# Buffer master loading (same as buffer_sizing.py)
# =========================================

MASTER_MAP = {}
BOTTOM_OPTIONS = []
UPPER_OPTIONS = []


def _dedup_by_strength(entries):
    by_strength = {}
    for strength, name in entries:
        if strength not in by_strength:
            by_strength[strength] = name
        else:
            existing = by_strength[strength]
            existing_is_f = 'f_' in existing or existing.split('_')[0].endswith('f')
            new_is_f = 'f_' in name or name.split('_')[0].endswith('f')
            if existing_is_f and not new_is_f:
                by_strength[strength] = name
    return sorted(by_strength.items())


def load_buffer_masters(csv_file):
    global MASTER_MAP, BOTTOM_OPTIONS, UPPER_OPTIONS
    bottom = []
    upper = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get('master_name', '').strip()
            tier = row.get('tier', '').strip()
            strength = int(float(row.get('drive_strength', 0)))
            if not name or strength <= 0:
                continue
            MASTER_MAP[name] = strength
            if tier == 'bottom':
                bottom.append((strength, name))
            elif tier == 'upper':
                upper.append((strength, name))

    bottom.sort()
    upper.sort()
    bottom = _dedup_by_strength(bottom)
    upper = _dedup_by_strength(upper)
    BOTTOM_OPTIONS = [name for _, name in bottom]
    UPPER_OPTIONS = [name for _, name in upper]
    # Fix: MASTER_MAP values are int (drive strength), cast to str for display
    bottom_info = [str(MASTER_MAP[n]) + "(" + n.split("_")[0] + ")" for n in BOTTOM_OPTIONS]
    upper_info  = [str(MASTER_MAP[n]) + "(" + n.split("_")[0] + ")" for n in UPPER_OPTIONS]
    print(f"  Bottom sizes: {bottom_info}")
    print(f"  Upper sizes:  {upper_info}")


def get_drive_strength(cell_name):
    if cell_name in MASTER_MAP:
        return MASTER_MAP[cell_name]
    return 0


def get_size_options(cell_name):
    if '_bottom' in cell_name:
        return BOTTOM_OPTIONS
    elif '_upper' in cell_name:
        return UPPER_OPTIONS
    return []


# =========================================
# Buffer delay model
# =========================================

def load_buffer_delays(csv_file):
    """Load measured buffer delays from CSV.
    Returns dict: cell_name -> delay_ns
    """
    delays = {}
    if csv_file is None:
        return delays
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get('buf_cell', '').strip()
                delay = float(row.get('delay_ns', 0))
                if name and delay > 0:
                    delays[name] = delay
    except FileNotFoundError:
        pass
    return delays


def estimate_delay(cell_name, measured_delays):
    """Get delay for a buffer cell. Use measured if available, else estimate."""
    if cell_name in measured_delays:
        return measured_delays[cell_name]
    strength = get_drive_strength(cell_name)
    if strength <= 0:
        strength = 4
    # Model: delay ≈ k / sqrt(strength), calibrated to BUFx4=12ps=0.012ns
    return 0.024 / (strength ** 0.5)


# =========================================
# CSV parsers (compatible with buffer_sizing.py)
# =========================================

def parse_timing_edges(csv_file):
    edges = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            edge = {
                'launch_ff': row.get('launch_ff', ''),
                'capture_ff': row.get('capture_ff', ''),
                'slack_setup': float(row.get('slack_setup_ps', 0)),
                'slack_hold': float(row.get('slack_hold_ps', 0)),
            }
            if edge['launch_ff'] and edge['capture_ff']:
                edges.append(edge)
    return edges


def parse_buffer_info(csv_file):
    buffers = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            driven_ffs_str = row.get('driven_ffs', '')
            driven_ffs = [ff.strip() for ff in driven_ffs_str.split(';') if ff.strip()]
            buf = {
                'inst': row.get('buffer_inst', ''),
                'cell': row.get('buffer_cell', ''),
                'driven_ffs': driven_ffs,
            }
            if buf['inst'] and buf['cell']:
                buffers.append(buf)
    return buffers


# =========================================
# V35: Post-CTS LP skew target loading
# =========================================

def load_skew_targets(csv_file):
    """Load per-FF skew targets from post-CTS LP solve.
    Expected columns: ff_name, target_arrival_ns, tier
    Returns dict: ff_name -> target_arrival_ps (converted to ps)
    """
    targets = {}
    if csv_file is None:
        return targets
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ff = row.get('ff_name', '').strip()
                target = float(row.get('target_arrival_ns', 0))
                if ff and abs(target) > 1e-9:
                    targets[ff] = target * 1e3  # ns -> ps
        print(f"  Loaded {len(targets)} non-zero skew targets from {csv_file}")
    except FileNotFoundError:
        print(f"  WARNING: skew targets file not found: {csv_file}")
    except Exception as e:
        print(f"  WARNING: failed to read skew targets: {e}")
    return targets


def aggregate_targets_to_buffers(buffers, ff_targets):
    """Aggregate per-FF LP targets to per-buffer targets.
    For buffer k driving FFs {i1, i2, ...}:
      target_k = mean(a_i for i in driven_ffs that have targets)
    Returns dict: buffer_index -> target_delay_change_ps
    """
    buf_targets = {}
    for k, buf in enumerate(buffers):
        ff_vals = [ff_targets[ff] for ff in buf['driven_ffs'] if ff in ff_targets]
        if ff_vals:
            buf_targets[k] = sum(ff_vals) / len(ff_vals)
    n_nonzero = sum(1 for v in buf_targets.values() if abs(v) > 0.1)
    if buf_targets:
        vals = list(buf_targets.values())
        print(f"  Aggregated targets for {len(buf_targets)} buffers "
              f"({n_nonzero} non-zero, range: {min(vals):.1f} - {max(vals):.1f} ps)")
    return buf_targets


# =========================================
# Pareto frontier computation
# =========================================

def compute_pareto_sizes(cell_name, measured_delays):
    """For a given buffer cell, compute Pareto-optimal sizes.

    Each size option: (cell_name, drive_strength, delay_ns)
    Pareto frontier: maximize strength (minimize delay) — all points are Pareto-optimal
    since larger strength always means lower delay.

    Returns list of (cell_name, delay_ns) sorted by delay (descending = smallest first).
    """
    options = get_size_options(cell_name)
    if not options:
        return []

    pareto = []
    for opt in options:
        delay = estimate_delay(opt, measured_delays)
        pareto.append((opt, delay))

    # Sort by delay descending (smallest buf = highest delay first)
    pareto.sort(key=lambda x: -x[1])
    return pareto


# =========================================
# LP Formulation
# =========================================

def solve_buffer_sizing_lp(buffers, edges, measured_delays,
                           hold_margin=0.0, setup_margin=0.0,
                           hold_weight=0.5, reg_weight=0.01,
                           buf_skew_targets=None, skew_weight=0.5):
    """
    LP-based buffer sizing using Pareto-optimal buffer sizes.

    Variables:
      d_k = delay of clock buffer k (one per buffer)

    The delay is constrained to lie within [min_delay, max_delay] of available sizes.

    Objective:
      Maximize: setup_weight * Σ (d_capture_buf - d_launch_buf) for setup-critical edges
              + hold_weight  * Σ (d_launch_buf - d_capture_buf) for hold-critical edges
              - reg_weight   * Σ |d_k - d_k_current|
      V35: + skew_weight * Σ |d_k - (d_k_current + target_k)|  (for buffers with LP targets)

    Constraints:
      For each setup edge (launch_ff → capture_ff):
        d_launch_buf - d_capture_buf ≤ setup_slack - setup_margin
        (sizing change must not create new setup violations)

      For each hold edge (launch_ff → capture_ff):
        d_capture_buf - d_launch_buf ≤ hold_slack - hold_margin
        (hold slack must stay >= +hold_margin after sizing)

    All delays and slacks in ps.
    """
    n_bufs = len(buffers)
    if n_bufs == 0:
        print("  No buffers to optimize.")
        return {}

    # Build FF -> buffer index mapping
    ff_to_buf_idx = {}
    for k, buf in enumerate(buffers):
        for ff in buf['driven_ffs']:
            ff_to_buf_idx[ff] = k

    # Compute current delay and bounds for each buffer
    current_delays = np.zeros(n_bufs)
    delay_bounds = []
    pareto_info = []  # For post-LP mapping

    for k, buf in enumerate(buffers):
        cell = buf['cell']
        current_delays[k] = estimate_delay(cell, measured_delays) * 1e3  # ns -> ps

        pareto = compute_pareto_sizes(cell, measured_delays)
        if pareto:
            delays_ps = [d * 1e3 for _, d in pareto]  # ns -> ps
            d_min = min(delays_ps)
            d_max = max(delays_ps)
            delay_bounds.append((d_min, d_max))
            pareto_info.append([(name, d * 1e3) for name, d in pareto])
        else:
            # No sizing options — fix at current delay
            cd = current_delays[k]
            delay_bounds.append((cd, cd))
            pareto_info.append([(cell, cd)])

    print(f"  Buffers: {n_bufs}")
    print(f"  Current delay range: {current_delays.min():.1f} - {current_delays.max():.1f} ps")

    # Count how many buffers actually have sizing freedom
    flexible = sum(1 for lo, hi in delay_bounds if hi - lo > 0.1)
    print(f"  Flexible buffers (>1 size option): {flexible}")

    if flexible == 0:
        print("  No buffers with sizing options. Nothing to optimize.")
        return {}

    # ===== Build LP =====
    # Variables: d_k for each buffer (delay in ps)
    # We also need auxiliary variables for |d_k - d_k_current| linearization:
    #   d_k - d_k_current = p_k - n_k, p_k >= 0, n_k >= 0
    # V35: For skew targets, additional variables for |d_k - d_k_target|:
    #   d_k - d_k_target = s_m - t_m, s_m >= 0, t_m >= 0
    # where m indexes only buffers with non-zero targets

    # V35: Prepare skew target variables
    if buf_skew_targets is None:
        buf_skew_targets = {}
    # List of (buffer_idx, target_delay_ps) for buffers with targets
    target_list = []
    for k in range(n_bufs):
        if k in buf_skew_targets and abs(buf_skew_targets[k]) > 0.1:
            # Target delay = current + LP offset, clamped to delay bounds
            target_delay = current_delays[k] + buf_skew_targets[k]
            target_delay = max(delay_bounds[k][0], min(delay_bounds[k][1], target_delay))
            target_list.append((k, target_delay))
    n_targets = len(target_list)

    # Total variables: d_k(n_bufs) + p_k(n_bufs) + n_k(n_bufs) + s_m(n_targets) + t_m(n_targets)
    n_vars = 3 * n_bufs + 2 * n_targets

    # Objective: minimize (scipy convention)
    c = np.zeros(n_vars)

    weight_setup = 1.0

    # Classify edges
    setup_edges = []
    hold_edges = []

    for edge in edges:
        launch_ff = edge['launch_ff']
        capture_ff = edge['capture_ff']
        slack_setup = edge['slack_setup']  # ps
        slack_hold = edge['slack_hold']    # ps

        ki = ff_to_buf_idx.get(launch_ff)
        kj = ff_to_buf_idx.get(capture_ff)

        if ki is None or kj is None:
            continue

        # Setup-critical: slack_setup < 0 means violation
        if slack_setup < 0:
            weight = abs(slack_setup)
            # Want to maximize (d_j - d_i) for setup improvement
            # In minimize form: minimize -(d_j - d_i) * weight
            c[ki] += weight_setup * weight   # d_i coefficient (penalty for launch)
            c[kj] -= weight_setup * weight   # d_j coefficient (reward for capture)
            setup_edges.append((ki, kj, slack_setup))

        # Hold-critical: slack_hold < 0 means violation
        if slack_hold < hold_margin:
            weight = abs(slack_hold) if slack_hold < 0 else (hold_margin - slack_hold)
            # Want to maximize (d_i - d_j) for hold improvement
            # In minimize form: minimize -(d_i - d_j) * weight
            c[ki] -= hold_weight * weight   # d_i coefficient (reward for launch)
            c[kj] += hold_weight * weight   # d_j coefficient (penalty for capture)
            hold_edges.append((ki, kj, slack_hold))

    # Regularization: minimize |d_k - d_k_current|
    # = minimize (p_k + n_k) for all k
    for k in range(n_bufs):
        c[n_bufs + k] = reg_weight      # p_k coefficient
        c[2 * n_bufs + k] = reg_weight  # n_k coefficient

    # V35: Skew target guidance — minimize |d_k - target_k| for targeted buffers
    # s_m, t_m variables start at index 3*n_bufs
    skew_base = 3 * n_bufs
    for m, (k, target_delay) in enumerate(target_list):
        c[skew_base + m] = skew_weight               # s_m coefficient
        c[skew_base + n_targets + m] = skew_weight    # t_m coefficient

    print(f"  Setup-critical edges: {len(setup_edges)}")
    print(f"  Hold-critical edges:  {len(hold_edges)}")
    if n_targets > 0:
        print(f"  Skew-targeted buffers: {n_targets} (weight={skew_weight})")
    print(f"  Objective weights: setup={weight_setup}, hold={hold_weight}, "
          f"reg={reg_weight}, skew={skew_weight if n_targets > 0 else 'N/A'}")

    # ===== Constraints =====
    A_rows = []
    b_rows = []

    # 1. Setup constraints:
    #    new_setup_slack = old_slack + (d_j - current_d_j) - (d_i - current_d_i)
    #    Want: new_setup_slack >= -setup_margin
    #    => d_i - d_j <= old_slack + setup_margin + current_d_i - current_d_j
    #    fix: current_delays[ki]/[kj] were swapped
    for edge in edges:
        launch_ff = edge['launch_ff']
        capture_ff = edge['capture_ff']
        slack_setup = edge['slack_setup']

        ki = ff_to_buf_idx.get(launch_ff)
        kj = ff_to_buf_idx.get(capture_ff)

        if ki is None or kj is None:
            continue
        if ki == kj:
            continue  # Same buffer drives both — sizing doesn't affect this edge

        # d_i - d_j <= slack_setup + setup_margin + current_d_i - current_d_j
        rhs = slack_setup + setup_margin + current_delays[ki] - current_delays[kj]

        # Only add if constraining (prune loose constraints)
        max_possible = delay_bounds[ki][1] - delay_bounds[kj][0]
        if rhs >= max_possible:
            continue

        row = np.zeros(n_vars)
        row[ki] = 1.0
        row[kj] = -1.0
        A_rows.append(row)
        b_rows.append(rhs)

    # 2. Hold constraints:
    #    new_hold_slack = old_hold + (d_i - current_d_i) - (d_j - current_d_j)
    #    Want: new_hold >= +hold_margin (hold must stay positive with margin)
    #    => d_j - d_i <= old_hold - hold_margin + current_d_j - current_d_i
    #    fix: (1) +hold_margin → -hold_margin, (2) current_delays[ki]/[kj] swapped
    for edge in edges:
        launch_ff = edge['launch_ff']
        capture_ff = edge['capture_ff']
        slack_hold = edge['slack_hold']

        ki = ff_to_buf_idx.get(launch_ff)
        kj = ff_to_buf_idx.get(capture_ff)

        if ki is None or kj is None:
            continue
        if ki == kj:
            continue

        rhs = slack_hold - hold_margin + current_delays[kj] - current_delays[ki]

        max_possible = delay_bounds[kj][1] - delay_bounds[ki][0]
        if rhs >= max_possible:
            continue

        row = np.zeros(n_vars)
        row[kj] = 1.0
        row[ki] = -1.0
        A_rows.append(row)
        b_rows.append(rhs)

    # 3. Linearization of |d_k - current_d_k|:
    #    d_k - current_d_k = p_k - n_k
    #    => d_k - p_k + n_k <= current_d_k   (upper bound)
    #    => -d_k + p_k - n_k <= -current_d_k (lower bound)
    for k in range(n_bufs):
        # d_k - p_k + n_k <= current_d_k
        row1 = np.zeros(n_vars)
        row1[k] = 1.0
        row1[n_bufs + k] = -1.0
        row1[2 * n_bufs + k] = 1.0
        A_rows.append(row1)
        b_rows.append(current_delays[k])

        # -d_k + p_k - n_k <= -current_d_k
        row2 = np.zeros(n_vars)
        row2[k] = -1.0
        row2[n_bufs + k] = 1.0
        row2[2 * n_bufs + k] = -1.0
        A_rows.append(row2)
        b_rows.append(-current_delays[k])

    # V35: Skew target linearization — |d_k - target_k| = s_m - t_m
    for m, (k, target_delay) in enumerate(target_list):
        # d_k - s_m + t_m <= target_delay
        row1 = np.zeros(n_vars)
        row1[k] = 1.0
        row1[skew_base + m] = -1.0
        row1[skew_base + n_targets + m] = 1.0
        A_rows.append(row1)
        b_rows.append(target_delay)

        # -d_k + s_m - t_m <= -target_delay
        row2 = np.zeros(n_vars)
        row2[k] = -1.0
        row2[skew_base + m] = 1.0
        row2[skew_base + n_targets + m] = -1.0
        A_rows.append(row2)
        b_rows.append(-target_delay)

    n_constraints = len(A_rows)
    print(f"  Constraints: {n_constraints}")

    if n_constraints == 0:
        print("  No constraints. Nothing to optimize.")
        return {}

    A_ub = np.array(A_rows)
    b_ub = np.array(b_rows)

    # Bounds
    bounds = []
    for k in range(n_bufs):
        bounds.append(delay_bounds[k])  # d_k bounds
    for k in range(n_bufs):
        bounds.append((0, None))  # p_k >= 0
    for k in range(n_bufs):
        bounds.append((0, None))  # n_k >= 0
    # V35: s_m >= 0, t_m >= 0
    for _ in range(n_targets):
        bounds.append((0, None))  # s_m >= 0
    for _ in range(n_targets):
        bounds.append((0, None))  # t_m >= 0

    # ===== Solve =====
    import scipy
    _sv = tuple(int(x) for x in scipy.__version__.split('.')[:2])
    use_highs = _sv >= (1, 9)
    lp_method = 'highs' if use_highs else 'interior-point'
    print(f"  Solver: {lp_method} (scipy {scipy.__version__})")

    try:
        result = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method=lp_method)

        if result.success:
            print(f"  LP solved! (objective={result.fun:.4f})")

            # Extract optimal delays
            optimal_delays = result.x[:n_bufs]

            # Map to discrete sizes
            changes = {}
            for k, buf in enumerate(buffers):
                d_opt = optimal_delays[k]
                d_cur = current_delays[k]

                # Skip if no change (within 0.5ps tolerance)
                if abs(d_opt - d_cur) < 0.5:
                    continue

                # Find nearest Pareto point
                best_cell = buf['cell']
                best_dist = abs(d_opt - d_cur)
                for cell_name, delay_ps in pareto_info[k]:
                    dist = abs(d_opt - delay_ps)
                    if dist < best_dist:
                        best_dist = dist
                        best_cell = cell_name

                if best_cell != buf['cell']:
                    old_strength = get_drive_strength(buf['cell'])
                    new_strength = get_drive_strength(best_cell)
                    direction = 'upsize' if new_strength > old_strength else 'downsize'
                    changes[buf['inst']] = {
                        'inst': buf['inst'],
                        'old_cell': buf['cell'],
                        'new_cell': best_cell,
                        'direction': direction,
                        'delay_change': d_opt - d_cur,
                    }

            print(f"  Sizing changes: {len(changes)}")
            if changes:
                up = sum(1 for c in changes.values() if c['direction'] == 'upsize')
                down = sum(1 for c in changes.values() if c['direction'] == 'downsize')
                print(f"    Upsize: {up}, Downsize: {down}")

            return changes
        else:
            print(f"  LP failed: {result.message}")
            return {}

    except Exception as e:
        print(f"  LP solver error: {e}")
        return {}


# =========================================
# Main
# =========================================

def main():
    parser = argparse.ArgumentParser(description='LP-Based Pareto Buffer Sizing (V23c)')
    parser.add_argument('timing_edges_csv', help='Timing edges CSV file')
    parser.add_argument('buffer_info_csv', help='Buffer info CSV file')
    parser.add_argument('output_csv', help='Output CSV with sizing changes')
    parser.add_argument('--buf-masters', required=True,
                        help='Available buffer masters CSV from ODB query')
    parser.add_argument('--buf-delays', default=None,
                        help='Measured buffer cell delays CSV (optional)')
    parser.add_argument('--hold-margin', type=float, default=0.0,
                        help='Hold margin in ps (default: 0.0, hold slack must stay >= +margin)')
    parser.add_argument('--setup-margin', type=float, default=0.0,
                        help='Setup margin in ps (default: 0.0, setup slack must stay >= -margin)')
    parser.add_argument('--hold-weight', type=float, default=0.5,
                        help='Hold objective weight relative to setup (default: 0.5)')
    parser.add_argument('--reg-weight', type=float, default=0.01,
                        help='Regularization weight for change minimization (default: 0.01)')
    # V35: Post-CTS LP skew target guidance
    parser.add_argument('--skew-targets', default=None,
                        help='Post-CTS LP per-FF skew targets CSV (ff_name,target_arrival_ns,tier)')
    parser.add_argument('--skew-weight', type=float, default=0.5,
                        help='Skew target guidance weight in LP objective (default: 0.5)')
    # Unused but accepted for compatibility with buffer_sizing.py interface
    parser.add_argument('--slack-threshold', type=float, default=-100.0)
    parser.add_argument('--hold-threshold', type=float, default=0.0)
    parser.add_argument('--hold-factor', type=float, default=1.0)
    args = parser.parse_args()

    print("=" * 60)
    print("LP-Based Pareto Buffer Sizing (V35)")
    print("=" * 60)

    print(f"\n[1] Loading buffer masters: {args.buf_masters}")
    load_buffer_masters(args.buf_masters)

    print(f"\n[2] Reading timing edges: {args.timing_edges_csv}")
    edges = parse_timing_edges(args.timing_edges_csv)
    print(f"    {len(edges)} timing edges")

    print(f"\n[3] Reading buffer info: {args.buffer_info_csv}")
    buffers = parse_buffer_info(args.buffer_info_csv)
    print(f"    {len(buffers)} clock buffers")

    measured_delays = {}
    if args.buf_delays:
        print(f"\n[4] Loading measured delays: {args.buf_delays}")
        measured_delays = load_buffer_delays(args.buf_delays)
        print(f"    {len(measured_delays)} measured delays")
    else:
        print(f"\n[4] No measured delays provided, using estimated delays")

    # V35: Load post-CTS LP skew targets and aggregate to per-buffer
    buf_skew_targets = {}
    if args.skew_targets:
        print(f"\n[4b] Loading post-CTS LP skew targets: {args.skew_targets}")
        ff_targets = load_skew_targets(args.skew_targets)
        if ff_targets:
            buf_skew_targets = aggregate_targets_to_buffers(buffers, ff_targets)

    print(f"\n[5] Solving LP (hold_margin={args.hold_margin}ps, "
          f"setup_margin={args.setup_margin}ps, hold_weight={args.hold_weight}"
          f"{f', skew_weight={args.skew_weight}' if buf_skew_targets else ''})...")
    changes = solve_buffer_sizing_lp(
        buffers, edges, measured_delays,
        hold_margin=args.hold_margin,
        setup_margin=args.setup_margin,
        hold_weight=args.hold_weight,
        reg_weight=args.reg_weight,
        buf_skew_targets=buf_skew_targets,
        skew_weight=args.skew_weight,
    )

    # Write output CSV (same format as buffer_sizing.py for drop-in compatibility)
    print(f"\n[6] Writing: {args.output_csv}")
    with open(args.output_csv, 'w', newline='') as f:
        fieldnames = ['inst', 'old_cell', 'new_cell', 'direction',
                      'net_benefit', 'setup_launch', 'setup_capture',
                      'hold_launch', 'hold_capture']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ch in changes.values():
            writer.writerow({
                'inst': ch['inst'],
                'old_cell': ch['old_cell'],
                'new_cell': ch['new_cell'],
                'direction': ch['direction'],
                'net_benefit': round(abs(ch['delay_change']), 2),
                'setup_launch': 0,
                'setup_capture': 0,
                'hold_launch': 0,
                'hold_capture': 0,
            })

    print(f"\nDone! {len(changes)} sizing changes written.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
