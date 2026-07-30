#!/usr/bin/env python3
# ============================================================================
# CTS Debug: Cross-Version Comparison Tool
# ============================================================================
# Compares timing between two CTS versions (e.g., Pin3D vs V42) on the same
# design at the per-FF and per-edge level.  Analyzes LP target correlation,
# hold violation sources (FF->FF vs PI->FF), and clock latency distribution.
#
# Compatible with H-tree (V40/V41) and SGCTS (V42/V42a) modes.
# Also works without per-FF CSV data (report-level comparison only).
#
# Usage (auto-discovery):
#   python3 cts_debug_compare.py \
#     --base-dir <flow_dir> \
#     --test-dir <flow_dir>
#     --platform asap7_3D --design aes \
#     --output-dir ./debug_compare_output
#
# Usage (explicit files):
#   python3 cts_debug_compare.py \
#     --base-graph pre_cts_ff_timing_graph.csv \
#     --test-graph post_cts_ff_timing_graph.csv \
#     --lp-targets pre_cts_skew_targets.csv \
#     --io-edges pre_cts_io_timing_edges.csv \
#     --test-debug-ff cts_debug_per_ff.csv \
#     --output-dir ./debug_compare_output
# ============================================================================

import argparse
import base64
import csv
import html as html_mod
import json
import math
import os
import subprocess
import sys
import tempfile
import re
from collections import defaultdict

# ============================================================================
# Section 1: File Discovery
# ============================================================================

def results_dir(flow_dir, platform, design):
    return os.path.join(flow_dir, "results", platform, design, "openroad")

def reports_dir(flow_dir, platform, design):
    return os.path.join(flow_dir, "reports", platform, design, "openroad")

def discover_files(flow_dir, platform, design):
    """Auto-discover available data files from a flow directory."""
    rd = results_dir(flow_dir, platform, design)
    rpd = reports_dir(flow_dir, platform, design)
    files = {}
    candidates = {
        "pre_cts_graph": os.path.join(rd, "pre_cts_ff_timing_graph.csv"),
        "post_cts_graph": os.path.join(rd, "post_cts_ff_timing_graph.csv"),
        "lp_targets": os.path.join(rd, "pre_cts_skew_targets.csv"),
        "io_edges": os.path.join(rd, "pre_cts_io_timing_edges.csv"),
        "debug_per_ff": os.path.join(rd, "cts_debug_per_ff.csv"),
        "debug_per_edge": os.path.join(rd, "cts_debug_per_edge.csv"),
        "debug_clock_paths": os.path.join(rd, "cts_debug_clock_paths.csv"),
        "debug_buffers": os.path.join(rd, "cts_debug_buffers.csv"),
        "tree_plan": os.path.join(rd, "sg_cts_tree_plan.csv"),
        "timing_rpt": os.path.join(rpd, "4_cts_timing.rpt"),
        # LP stats JSON (produced by cts_skew_lp.py --stats-json)
        "lp_stats": os.path.join(rd, "pre_cts_lp_stats.json"),
        # cts_debug_summary.csv: full STA TNS/WNS (produced by cts_debug_extract.tcl)
        "debug_summary": os.path.join(rd, "cts_debug_summary.csv"),
    }
    for key, path in candidates.items():
        if os.path.exists(path):
            files[key] = path
    return files

# ============================================================================
# Section 2: CSV / Report Parsers
# ============================================================================

def fmt(val, spec=".1f"):
    """Format a value with the given spec, returning 'N/A' if None."""
    if val is None:
        return "N/A"
    return f'{val:{spec}}'

def safe_float(val, default=None):
    import math
    if val is None or val == "N/A" or val == "":
        return default
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default

def parse_timing_graph(csv_path):
    """Parse pre/post_cts_ff_timing_graph.csv -> list of edge dicts.
    Columns: from_ff, to_ff, slack_max_ns, slack_min_ns, arrival_max_ns, ..."""
    edges = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            edges.append(row)
    return edges

def parse_lp_targets(csv_path):
    """Parse pre_cts_skew_targets.csv -> {ff_name: target_ns}."""
    targets = {}
    with open(csv_path) as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for row in reader:
            if len(row) >= 2:
                targets[row[0]] = float(row[1])
    return targets

def parse_io_edges(csv_path):
    """Parse pre_cts_io_timing_edges.csv -> list of dicts.
    Columns: edge_type, port_name, ff_name, slack_setup_ns, slack_hold_ns"""
    edges = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            edges.append(row)
    return edges

def parse_debug_per_ff(csv_path):
    """Parse cts_debug_per_ff.csv -> list of dicts.
    Columns: ff_name, tier, lp_target_ps, clk_latency_ps, clk_delta_ps,
             setup_slack_ps, hold_slack_ps"""
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def parse_debug_buffers(csv_path):
    """Parse cts_debug_buffers.csv -> list of dicts.
    Columns: buf_name, cell_name, x_um, y_um, tier, fanout, output_net, avg_driven_lp_target_ps"""
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def parse_debug_clock_paths(csv_path):
    """Parse cts_debug_clock_paths.csv -> list of dicts.
    Columns: ff_name, hop_idx, pin_name, arrival_ps, incr_delay_ps, cell_or_net, pin_type"""
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def parse_timing_report(rpt_path):
    """Parse 4_cts_timing.rpt -> dict with setup/hold tns/wns.
    Supports two formats:
    (a) report_tns/report_wns style: 'tns max <val>' / 'wns max <val>'
    (b) cts_debug_summary.csv style: 'setup_tns_ps,<val>'
    """
    result = {}
    with open(rpt_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("tns max"):
                result["setup_tns"] = float(line.split()[-1])
            elif line.startswith("tns min"):
                result["hold_tns"] = float(line.split()[-1])
            elif line.startswith("wns max"):
                result["setup_wns"] = float(line.split()[-1])
            elif line.startswith("wns min"):
                result["hold_wns"] = float(line.split()[-1])
    return result

def parse_debug_summary(csv_path):
    """Parse cts_debug_summary.csv -> dict with setup/hold tns/wns (all in ps).
    Maps: setup_tns_ps->setup_tns, setup_wns_ps->setup_wns,
          hold_tns_ps->hold_tns, hold_wns_ps->hold_wns,
          clock_buffer_count->clock_buf_count, cts_mode->cts_mode."""
    result = {}
    key_map = {
        "setup_tns_ps": "setup_tns",
        "setup_wns_ps": "setup_wns",
        "hold_tns_ps":  "hold_tns",
        "hold_wns_ps":  "hold_wns",
        "clock_buffer_count": "clock_buf_count",
        "cts_mode": "cts_mode",
    }
    try:
        with open(csv_path) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 2 and parts[0] in key_map:
                    try:
                        result[key_map[parts[0]]] = float(parts[1])
                    except ValueError:
                        result[key_map[parts[0]]] = parts[1]  # keep string (e.g. cts_mode)
    except Exception:
        pass
    return result

# ============================================================================
# Section 3: Per-FF and Per-Edge Analysis
# ============================================================================

def compute_per_ff_from_edges(edges, debug_per_ff_rows=None):
    """Aggregate per-edge data to per-FF worst slack.
    Returns {ff_name: {worst_setup_ns, worst_hold_ns, as_launch_setup, as_capture_setup}}.

    debug_per_ff_rows: optional list of dicts from cts_debug_per_ff.csv.
    Used to supplement FFs that only appear as launch (never capture) in FF→FF graph,
    i.e. PI→FF paths. cts_debug_per_ff.csv is extracted via POST-CTS STA and covers
    all FFs regardless of whether they have FF→FF capture paths.
    """
    ff_data = defaultdict(lambda: {
        "worst_setup_ns": float("inf"),
        "worst_hold_ns": float("inf"),
        "n_launch": 0, "n_capture": 0,
        "arrival_max_ns": None, "arrival_min_ns": None,
    })
    for e in edges:
        from_ff = e["from_ff"]
        to_ff = e["to_ff"]
        s_max = safe_float(e.get("slack_max_ns"))
        s_min = safe_float(e.get("slack_min_ns"))
        # Capture FF gets the setup/hold slack
        if s_max is not None:
            ff_data[to_ff]["worst_setup_ns"] = min(ff_data[to_ff]["worst_setup_ns"], s_max)
        if s_min is not None:
            ff_data[to_ff]["worst_hold_ns"] = min(ff_data[to_ff]["worst_hold_ns"], s_min)
        ff_data[to_ff]["n_capture"] += 1
        ff_data[from_ff]["n_launch"] += 1
        # Store arrival times for capture FF
        arr_max = safe_float(e.get("arrival_max_ns"))
        if arr_max is not None:
            ff_data[to_ff]["arrival_max_ns"] = arr_max
    # Clean up inf values
    for ff in ff_data:
        if ff_data[ff]["worst_setup_ns"] == float("inf"):
            ff_data[ff]["worst_setup_ns"] = None
        if ff_data[ff]["worst_hold_ns"] == float("inf"):
            ff_data[ff]["worst_hold_ns"] = None

    # Supplement from cts_debug_per_ff.csv (POST-CTS STA, covers PI→FF paths).
    # Fills in None for FFs with no FF→FF capture edges, and also updates
    # existing entries if the debug value is worse (more negative = tighter).
    if debug_per_ff_rows:
        for row in debug_per_ff_rows:
            ff = row.get("ff_name", "")
            if not ff:
                continue
            s_ps = safe_float(row.get("setup_slack_ps"))
            h_ps = safe_float(row.get("hold_slack_ps"))
            s_ns = s_ps / 1000.0 if s_ps is not None else None
            h_ns = h_ps / 1000.0 if h_ps is not None else None
            if ff not in ff_data:
                ff_data[ff] = {"worst_setup_ns": None, "worst_hold_ns": None,
                               "n_launch": 0, "n_capture": 0,
                               "arrival_max_ns": None, "arrival_min_ns": None}
            # Take worst (min) of FF→FF graph value and debug per-FF value
            if s_ns is not None:
                cur = ff_data[ff]["worst_setup_ns"]
                ff_data[ff]["worst_setup_ns"] = min(cur, s_ns) if cur is not None else s_ns
            if h_ns is not None:
                cur = ff_data[ff]["worst_hold_ns"]
                ff_data[ff]["worst_hold_ns"] = min(cur, h_ns) if cur is not None else h_ns

    return dict(ff_data)

def compare_edges(base_edges, test_edges):
    """1:1 per-edge comparison between base and test.
    Matches by (from_ff, to_ff) key.
    Returns list of comparison dicts."""
    # Build lookup for base edges
    base_map = {}
    for e in base_edges:
        key = (e["from_ff"], e["to_ff"])
        base_map[key] = e
    comparisons = []
    for e in test_edges:
        key = (e["from_ff"], e["to_ff"])
        base_e = base_map.get(key)
        if base_e is None:
            continue  # edge only in test (new edge)
        base_setup = safe_float(base_e.get("slack_max_ns"))
        test_setup = safe_float(e.get("slack_max_ns"))
        base_hold = safe_float(base_e.get("slack_min_ns"))
        test_hold = safe_float(e.get("slack_min_ns"))
        comp = {
            "from_ff": key[0], "to_ff": key[1],
            "base_setup_ns": base_setup, "test_setup_ns": test_setup,
            "base_hold_ns": base_hold, "test_hold_ns": test_hold,
            "delta_setup_ps": (test_setup - base_setup) * 1000 if base_setup is not None and test_setup is not None else None,
            "delta_hold_ps": (test_hold - base_hold) * 1000 if base_hold is not None and test_hold is not None else None,
        }
        comparisons.append(comp)
    return comparisons

def compare_per_ff(base_ff, test_ff):
    """Per-FF comparison of worst slacks."""
    all_ffs = set(base_ff.keys()) | set(test_ff.keys())
    comparisons = []
    for ff in sorted(all_ffs):
        b = base_ff.get(ff, {})
        t = test_ff.get(ff, {})
        bs = b.get("worst_setup_ns")
        ts = t.get("worst_setup_ns")
        bh = b.get("worst_hold_ns")
        th = t.get("worst_hold_ns")
        comp = {
            "ff_name": ff,
            "base_setup_ns": bs, "test_setup_ns": ts,
            "base_hold_ns": bh, "test_hold_ns": th,
            "delta_setup_ps": (ts - bs) * 1000 if bs is not None and ts is not None else None,
            "delta_hold_ps": (th - bh) * 1000 if bh is not None and th is not None else None,
        }
        comparisons.append(comp)
    return comparisons

def analyze_lp_correlation(test_edges, pre_cts_edges, lp_targets):
    """Analyze LP target vs actual clock arrival shift.
    Uses pre_cts (ideal clock) as baseline, test (post_cts) as actual.
    For each capture FF: actual_shift = test_arrival - pre_cts_arrival.
    LP target = lp_targets[ff] (in ns)."""
    # Compute per-FF average arrival from edges (as capture FF)
    def avg_arrival(edges):
        ff_arr = defaultdict(list)
        for e in edges:
            arr = safe_float(e.get("arrival_max_ns"))
            if arr is not None:
                ff_arr[e["to_ff"]].append(arr)
        return {ff: sum(v)/len(v) for ff, v in ff_arr.items()}

    pre_arrivals = avg_arrival(pre_cts_edges)
    test_arrivals = avg_arrival(test_edges)

    results = []
    for ff in sorted(lp_targets.keys()):
        target_ns = lp_targets[ff]
        target_ps = target_ns * 1000.0
        pre_arr = pre_arrivals.get(ff)
        test_arr = test_arrivals.get(ff)
        if pre_arr is not None and test_arr is not None:
            shift_ps = (test_arr - pre_arr) * 1000.0
            gap_ps = target_ps - shift_ps
            results.append({
                "ff_name": ff,
                "lp_target_ps": target_ps,
                "actual_shift_ps": shift_ps,
                "gap_ps": gap_ps,
                "tier": "upper" if "_upper" in ff else "bottom",
            })
    return results

def analyze_io_hold(io_edges, lp_targets, test_per_ff_data=None):
    """Analyze PI->FF and FF->PO hold violations.
    io_edges are PRE-CTS (ideal clock). Post-CTS violations are worse
    because CTS adds latency to capture FFs."""
    pi_ff = [e for e in io_edges if e.get("edge_type") == "PI_TO_FF"]
    ff_po = [e for e in io_edges if e.get("edge_type") == "FF_TO_PO"]

    results = []
    for e in pi_ff:
        ff = e.get("ff_name", "")
        hold_ns = safe_float(e.get("slack_hold_ns"))
        setup_ns = safe_float(e.get("slack_setup_ns"))
        lp_target = lp_targets.get(ff, 0.0)
        lp_ps = lp_target * 1000.0
        # Projected post-CTS hold: pre_hold - lp_target
        # (LP adds lp_target to capture FF clock -> hold degrades by that amount)
        projected_hold_ps = (hold_ns * 1000.0 - lp_ps) if hold_ns is not None else None

        # Actual post-CTS hold (from debug data if available)
        actual_hold_ps = None
        if test_per_ff_data:
            ff_row = test_per_ff_data.get(ff)
            if ff_row:
                actual_hold_ps = safe_float(ff_row.get("hold_slack_ps"))

        results.append({
            "port": e.get("port_name", ""),
            "ff_name": ff,
            "pre_cts_hold_ns": hold_ns,
            "pre_cts_setup_ns": setup_ns,
            "lp_target_ps": lp_ps,
            "projected_hold_ps": projected_hold_ps,
            "actual_hold_ps": actual_hold_ps,
            "tier": "upper" if "_upper" in ff else "bottom",
        })
    return results, pi_ff, ff_po

# ============================================================================
# Section 4: Statistics
# ============================================================================

def ff_tier(ff_name):
    """Determine tier from FF name suffix: _upper or _bottom."""
    if "_upper" in ff_name:
        return "upper"
    elif "_bottom" in ff_name:
        return "bottom"
    return "unknown"

def edge_tier_type(from_ff, to_ff):
    """Classify edge as same-tier or cross-tier based on FF names."""
    ft = ff_tier(from_ff)
    tt = ff_tier(to_ff)
    if ft == "unknown" or tt == "unknown":
        return "unknown"
    return "same-tier" if ft == tt else "cross-tier"

def histogram_bins(values, bin_edges):
    """Count values into bins defined by edges. Returns list of counts."""
    counts = [0] * (len(bin_edges) + 1)
    for v in values:
        placed = False
        for i, edge in enumerate(bin_edges):
            if v < edge:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return counts

def percentile(values, p):
    """Simple percentile calculation."""
    if not values:
        return 0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])

def correlation(xs, ys):
    """Pearson correlation coefficient."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx < 1e-12 or sy < 1e-12:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (sx * sy)

# ============================================================================
# Section 4b: Negative Cycle Analysis (Tarjan SCC + Bellman-Ford)
# ============================================================================

def tarjan_scc(adj_list, N):
    """Iterative Tarjan's SCC algorithm. Returns list of SCCs (each a list of node indices)."""
    index_counter = [0]
    stack = []
    on_stack = [False] * N
    idx = [-1] * N
    lowlink = [-1] * N
    sccs = []

    for v in range(N):
        if idx[v] != -1:
            continue
        call_stack = [(v, 0)]
        while call_stack:
            node, ni = call_stack[-1]
            if idx[node] == -1:
                idx[node] = index_counter[0]
                lowlink[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack[node] = True
            neighbors = adj_list[node]
            found_child = False
            while ni < len(neighbors):
                w = neighbors[ni]
                ni += 1
                if idx[w] == -1:
                    call_stack[-1] = (node, ni)
                    call_stack.append((w, 0))
                    found_child = True
                    break
                elif on_stack[w]:
                    lowlink[node] = min(lowlink[node], idx[w])
            if not found_child:
                if lowlink[node] == idx[node]:
                    scc = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        scc.append(w)
                        if w == node:
                            break
                    sccs.append(scc)
                call_stack.pop()
                if call_stack:
                    parent, _ = call_stack[-1]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
    return sccs


def _find_negative_cycles_bf(scc_nodes, local_edges_setup, n_local, local_idx, scc_nodes_list):
    """Bellman-Ford negative cycle detection within an SCC."""
    dist = [0.0] * n_local
    pred = [-1] * n_local
    # N-1 relaxations
    for _ in range(n_local - 1):
        for li, lj, w in local_edges_setup:
            if dist[li] + w < dist[lj] - 1e-12:
                dist[lj] = dist[li] + w
                pred[lj] = li
    # Find relaxable edges → negative cycle
    cycles = []
    visited = set()
    for li, lj, w in local_edges_setup:
        if dist[li] + w < dist[lj] - 1e-12:
            v = lj
            for _ in range(n_local):
                if pred[v] == -1:
                    break
                v = pred[v]
            if v in visited:
                continue
            path = [v]
            u = pred[v]
            steps = 0
            while u != v and u != -1 and steps < n_local + 1:
                path.append(u)
                u = pred[u]
                steps += 1
            if u != v:
                continue
            path.reverse()
            for c in path:
                visited.add(c)
            # Convert local indices back to global
            cycle_global = [scc_nodes_list[li] for li in path]
            cycles.append(cycle_global)
    return cycles


def _find_negative_cycles_dfs(scc_set, adj_neg, max_cycles=30, max_len=6):
    """DFS-based short negative cycle finder."""
    cycles = []
    for start in scc_set:
        if len(cycles) >= max_cycles:
            break
        stack = [(start, [start], {start})]
        while stack and len(cycles) < max_cycles:
            node, path, visited = stack.pop()
            for neighbor in adj_neg.get(node, []):
                if neighbor == start and len(path) >= 2:
                    cycle_nodes = tuple(sorted(path))
                    is_dup = any(tuple(sorted(c)) == cycle_nodes for c in cycles)
                    if not is_dup:
                        cycles.append(list(path))
                elif neighbor not in visited and len(path) < max_len:
                    stack.append((neighbor, path + [neighbor], visited | {neighbor}))
    return cycles


def analyze_negative_cycles(test_edges, max_skew_ns=0.100):
    """Run negative cycle analysis on post-CTS timing graph edges.
    Returns a dict with all analysis results for HTML rendering."""
    # Build FF index map
    ff_map = {}
    ff_list = []
    edge_data = []  # (fi, fj, setup_ns, hold_ns, from_name, to_name)

    for e in test_edges:
        fn = e["from_ff"]
        tn = e["to_ff"]
        s_max = safe_float(e.get("slack_max_ns"))
        s_min = safe_float(e.get("slack_min_ns"))
        if s_max is None or s_min is None:
            continue
        for name in (fn, tn):
            if name not in ff_map:
                i = len(ff_list)
                ff_map[name] = i
                ff_list.append(name)
        edge_data.append((ff_map[fn], ff_map[tn], s_max, s_min, fn, tn))

    N = len(ff_list)
    if N == 0:
        return None

    # Build adjacency (pruned: edges with setup < 2*max_skew)
    prune = max_skew_ns * 2
    adj_list = [[] for _ in range(N)]
    for fi, fj, setup, hold, fn, tn in edge_data:
        if setup < prune:
            adj_list[fi].append(fj)

    # Tarjan SCC
    sccs = tarjan_scc(adj_list, N)
    nontrivial = [scc for scc in sccs if len(scc) > 1]
    nontrivial.sort(key=len, reverse=True)

    scc_ff_set = set()
    for scc in nontrivial:
        scc_ff_set.update(scc)

    # Violation classification
    viol_edges = [(fi, fj, s, h, fn, tn) for fi, fj, s, h, fn, tn in edge_data if s < 0]
    tns_total = sum(s for _, _, s, _, _, _ in viol_edges)
    both_in = [(fi, fj, s, h, fn, tn) for fi, fj, s, h, fn, tn in viol_edges
               if fi in scc_ff_set and fj in scc_ff_set]
    one_in = [(fi, fj, s, h, fn, tn) for fi, fj, s, h, fn, tn in viol_edges
              if (fi in scc_ff_set) != (fj in scc_ff_set)]
    free = [(fi, fj, s, h, fn, tn) for fi, fj, s, h, fn, tn in viol_edges
            if fi not in scc_ff_set and fj not in scc_ff_set]
    tns_both = sum(s for _, _, s, _, _, _ in both_in)
    tns_one = sum(s for _, _, s, _, _, _ in one_in)
    tns_free = sum(s for _, _, s, _, _, _ in free)

    # Find negative cycles
    all_cycles = []  # Each cycle = list of (from_name, to_name, setup_ns, hold_ns)
    scc_cycle_counts = []

    for scc in nontrivial:
        scc_set = set(scc)
        local_idx = {n: i for i, n in enumerate(scc)}
        n_local = len(scc)

        # Local edges with setup slack
        local_edges = []  # (local_i, local_j, setup)
        local_edge_full = {}  # (local_i, local_j) -> (setup, hold, fn, tn)
        for fi, fj, s, h, fn, tn in edge_data:
            if fi in scc_set and fj in scc_set:
                li, lj = local_idx[fi], local_idx[fj]
                local_edges.append((li, lj, s))
                key = (li, lj)
                if key not in local_edge_full or s < local_edge_full[key][0]:
                    local_edge_full[key] = (s, h, fn, tn)

        # Bellman-Ford cycles
        bf_cycles = _find_negative_cycles_bf(scc_set, local_edges, n_local, local_idx, scc)

        # DFS cycles (adjacency for negative-slack edges only)
        adj_neg = defaultdict(list)
        for fi, fj, s, h, fn, tn in edge_data:
            if fi in scc_set and fj in scc_set and s < 0:
                adj_neg[fi].append(fj)
        dfs_cycles = _find_negative_cycles_dfs(scc_set, adj_neg)

        # Merge and deduplicate
        seen = set()
        scc_cycles = []
        for cycle_nodes in bf_cycles + dfs_cycles:
            key = frozenset(cycle_nodes)
            if key in seen:
                continue
            seen.add(key)
            # Build edge list for this cycle
            cycle_edges = []
            valid = True
            for i in range(len(cycle_nodes)):
                ci = cycle_nodes[i]
                cj = cycle_nodes[(i + 1) % len(cycle_nodes)]
                li, lj = local_idx[ci], local_idx[cj]
                if (li, lj) in local_edge_full:
                    s, h, fn, tn = local_edge_full[(li, lj)]
                    cycle_edges.append((fn, tn, s, h))
                else:
                    valid = False
                    break
            if valid and cycle_edges:
                total_w = sum(s for _, _, s, _ in cycle_edges)
                if total_w < -1e-9:
                    scc_cycles.append(cycle_edges)

        scc_cycle_counts.append(len(scc_cycles))
        all_cycles.extend(scc_cycles)

    # Sort by total weight (most negative first)
    all_cycles.sort(key=lambda c: sum(s for _, _, s, _ in c))

    # Contention hotspots
    ff_cycle_count = defaultdict(int)
    ff_cycle_tns = defaultdict(float)
    for cycle in all_cycles:
        w = sum(s for _, _, s, _ in cycle)
        for fn, tn, s, h in cycle:
            ff_cycle_count[fn] += 1
            ff_cycle_count[tn] += 1
            ff_cycle_tns[fn] += w
            ff_cycle_tns[tn] += w
    hotspots = sorted(ff_cycle_count.keys(), key=lambda f: ff_cycle_count[f], reverse=True)

    # Cross-tier cycle stats
    cross_tier_cycles = 0
    for cycle in all_cycles:
        has_cross = any(ff_tier(fn) != ff_tier(tn) for fn, tn, _, _ in cycle)
        if has_cross:
            cross_tier_cycles += 1

    # SCC summary table data
    scc_table = []
    for si, scc in enumerate(nontrivial[:20]):
        scc_set = set(scc)
        scc_e = [(fi, fj, s, h) for fi, fj, s, h, fn, tn in edge_data
                 if fi in scc_set and fj in scc_set]
        scc_v = [s for _, _, s, _ in scc_e if s < 0]
        tiers = set(ff_tier(ff_list[n]) for n in scc)
        scc_table.append({
            "idx": si + 1,
            "size": len(scc),
            "n_edges": len(scc_e),
            "n_violated": len(scc_v),
            "tns_ps": sum(scc_v) * 1000 if scc_v else 0,
            "n_cycles": scc_cycle_counts[si] if si < len(scc_cycle_counts) else 0,
            "tiers": "/".join(sorted(t for t in tiers if t != "unknown")),
        })

    return {
        "n_ffs": N,
        "n_edges": len(edge_data),
        "n_viol": len(viol_edges),
        "tns_total_ps": tns_total * 1000,
        "n_sccs": len(nontrivial),
        "n_scc_ffs": len(scc_ff_set),
        "classification": {
            "both_in": (len(both_in), tns_both * 1000),
            "one_in": (len(one_in), tns_one * 1000),
            "free": (len(free), tns_free * 1000),
        },
        "n_cycles": len(all_cycles),
        "cycles": all_cycles[:20],  # top 20 worst
        "cycle_weights_ps": [sum(s for _, _, s, _ in c) * 1000 for c in all_cycles],
        "hotspots": [(ff, ff_cycle_count[ff], ff_cycle_tns[ff] * 1000)
                     for ff in hotspots[:20]],
        "cross_tier_cycles": cross_tier_cycles,
        "same_tier_cycles": len(all_cycles) - cross_tier_cycles,
        "scc_table": scc_table,
    }


# ============================================================================
# Section 4c: Cross-Tier Clock Tree Structure Analysis
# ============================================================================

def _classify_ff_path_tiers(clock_paths):
    """Classify each FF's clock path by buffer tier composition.
    Returns dict: {ff_name: 'bottom_only' | 'upper_only' | 'mixed'}"""
    ff_buf_tiers = defaultdict(set)  # ff_name -> set of tier strings
    for row in clock_paths:
        if row.get("pin_type") == "buf":
            ff_name = row["ff_name"]
            cell = row.get("cell_or_net", "")
            if "_upper" in cell:
                ff_buf_tiers[ff_name].add("upper")
            elif "_bottom" in cell:
                ff_buf_tiers[ff_name].add("bottom")
    result = {}
    for ff, tiers in ff_buf_tiers.items():
        if tiers == {"bottom"}:
            result[ff] = "bottom_only"
        elif tiers == {"upper"}:
            result[ff] = "upper_only"
        elif len(tiers) > 1:
            result[ff] = "mixed"
        else:
            result[ff] = "unknown"
    return result

def _count_hbt_crossings(clock_paths, buffers_by_name, ffs_by_name):
    """Count leaf→FF HBT crossings from clock path data.
    Returns (n_cross, n_same, cross_list) where cross_list has buf/ff info."""
    # For each FF, find last buffer in path, check if it crosses tier to FF
    ff_hops = defaultdict(list)
    for row in clock_paths:
        ff_hops[row["ff_name"]].append(row)

    n_cross = 0
    n_same = 0
    cross_details = []
    for ff_name, hops in ff_hops.items():
        ff_tier = ff_tier_func(ff_name)
        # Find last buffer hop
        last_buf_name = None
        last_buf_cell = ""
        for h in hops:
            if h.get("pin_type") == "buf":
                last_buf_name = h["pin_name"]
                last_buf_cell = h.get("cell_or_net", "")
        if last_buf_name is None:
            continue
        # Determine buffer tier from cell name
        if "_upper" in last_buf_cell:
            buf_tier = "upper"
        elif "_bottom" in last_buf_cell:
            buf_tier = "bottom"
        else:
            buf_tier = "unknown"

        if buf_tier != "unknown" and ff_tier != "unknown" and buf_tier != ff_tier:
            n_cross += 1
            cross_details.append({"buf": last_buf_name, "ff": ff_name,
                                  "buf_tier": buf_tier, "ff_tier": ff_tier})
        elif buf_tier == ff_tier:
            n_same += 1

    return n_cross, n_same, cross_details

# Alias to avoid name collision with ff_tier helper used in report
ff_tier_func = None  # Set dynamically before use

def analyze_cross_tier_comparison(base_buffers, test_buffers,
                                  base_clock_paths, test_clock_paths,
                                  base_debug_ff, test_debug_ff):
    """Compare cross-tier clock tree structure between base (Pin3D) and test (3DCTS).
    Returns dict with all comparison metrics, or None if insufficient data."""
    global ff_tier_func
    ff_tier_func = ff_tier  # bind the module-level ff_tier function

    result = {}

    # --- Buffer tier distribution ---
    def buf_tier_counts(buffers):
        counts = {"upper": 0, "bottom": 0, "unknown": 0}
        for b in buffers:
            t = b.get("tier", "").strip().lower()
            if t in counts:
                counts[t] += 1
            else:
                counts["unknown"] += 1
        return counts

    result["base_buf_tiers"] = buf_tier_counts(base_buffers) if base_buffers else None
    result["test_buf_tiers"] = buf_tier_counts(test_buffers) if test_buffers else None
    result["base_n_bufs"] = len(base_buffers) if base_buffers else 0
    result["test_n_bufs"] = len(test_buffers) if test_buffers else 0

    # --- FF clock path tier classification ---
    def path_tier_summary(clock_paths):
        """Classify FFs by their clock path buffer tiers, grouped by FF tier."""
        path_cls = _classify_ff_path_tiers(clock_paths)
        summary = {}
        for ff, cls in path_cls.items():
            ft = ff_tier(ff)
            if ft not in summary:
                summary[ft] = {"bottom_only": 0, "upper_only": 0, "mixed": 0, "total": 0}
            summary[ft][cls] = summary[ft].get(cls, 0) + 1
            summary[ft]["total"] += 1
        return summary

    result["base_path_tiers"] = path_tier_summary(base_clock_paths) if base_clock_paths else None
    result["test_path_tiers"] = path_tier_summary(test_clock_paths) if test_clock_paths else None

    # --- HBT crossing counts (leaf buffer → FF) ---
    base_bufs_dict = {b["buf_name"]: b for b in base_buffers} if base_buffers else {}
    test_bufs_dict = {b["buf_name"]: b for b in test_buffers} if test_buffers else {}
    base_ff_dict = {r["ff_name"]: r for r in base_debug_ff} if base_debug_ff else {}
    test_ff_dict = {r["ff_name"]: r for r in test_debug_ff} if isinstance(test_debug_ff, list) else (test_debug_ff or {})

    if base_clock_paths:
        bc, bs, _ = _count_hbt_crossings(base_clock_paths, base_bufs_dict, base_ff_dict)
        result["base_hbt_cross"] = bc
        result["base_hbt_same"] = bs
    else:
        result["base_hbt_cross"] = None
        result["base_hbt_same"] = None

    if test_clock_paths:
        tc, ts, cross_det = _count_hbt_crossings(test_clock_paths, test_bufs_dict, test_ff_dict)
        result["test_hbt_cross"] = tc
        result["test_hbt_same"] = ts
        # Direction breakdown: bottom→upper vs upper→bottom
        b2u = sum(1 for d in cross_det if d["buf_tier"] == "bottom" and d["ff_tier"] == "upper")
        u2b = sum(1 for d in cross_det if d["buf_tier"] == "upper" and d["ff_tier"] == "bottom")
        result["test_hbt_b2u"] = b2u
        result["test_hbt_u2b"] = u2b
    else:
        result["test_hbt_cross"] = None
        result["test_hbt_same"] = None
        result["test_hbt_b2u"] = 0
        result["test_hbt_u2b"] = 0

    # --- Per-tier latency comparison (base vs test) ---
    def tier_latency(debug_ff_rows):
        lat = {"upper": [], "bottom": []}
        rows = debug_ff_rows if isinstance(debug_ff_rows, list) else (
            list(debug_ff_rows.values()) if isinstance(debug_ff_rows, dict) else [])
        for r in rows:
            name = r.get("ff_name", "")
            t = ff_tier(name)
            v = safe_float(r.get("clk_latency_ps"))
            if v is not None and t in lat:
                lat[t].append(v)
        return lat

    result["base_tier_lat"] = tier_latency(base_debug_ff) if base_debug_ff else {"upper": [], "bottom": []}
    result["test_tier_lat"] = tier_latency(test_debug_ff) if test_debug_ff else {"upper": [], "bottom": []}

    # --- Per-tier hold slack comparison (base vs test) ---
    def tier_hold(debug_ff_rows):
        hold = {"upper": [], "bottom": []}
        rows = debug_ff_rows if isinstance(debug_ff_rows, list) else (
            list(debug_ff_rows.values()) if isinstance(debug_ff_rows, dict) else [])
        for r in rows:
            name = r.get("ff_name", "")
            t = ff_tier(name)
            v = safe_float(r.get("hold_slack_ps"))
            if v is not None and t in hold:
                hold[t].append(v)
        return hold

    result["base_tier_hold"] = tier_hold(base_debug_ff) if base_debug_ff else {"upper": [], "bottom": []}
    result["test_tier_hold"] = tier_hold(test_debug_ff) if test_debug_ff else {"upper": [], "bottom": []}

    return result


# ============================================================================
# Section 5: SVG Chart Generators
# ============================================================================

def svg_scatter(points, title, xlabel, ylabel, width=700, height=450,
                diag_line=False, zero_lines=True, color_func=None):
    """Generic scatter plot as inline SVG.
    points: list of (x, y) tuples.
    color_func: optional function (x, y) -> color string."""
    import math
    points = [(x, y) for x, y in points
              if x is not None and y is not None
              and math.isfinite(x) and math.isfinite(y)]
    if not points:
        return f'<p style="color:#888;">No data for: {title}</p>'

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_range = max(x_max - x_min, 1.0)
    y_range = max(y_max - y_min, 1.0)
    x_min -= x_range * 0.08; x_max += x_range * 0.08
    y_min -= y_range * 0.08; y_max += y_range * 0.08
    x_range = x_max - x_min
    y_range = y_max - y_min

    ml, mr, mt, mb = 75, 30, 40, 55
    pw, ph = width - ml - mr, height - mt - mb

    def tx(x): return ml + (x - x_min) / x_range * pw
    def ty(y): return mt + (1.0 - (y - y_min) / y_range) * ph

    # Use viewBox for responsive sizing — SVG scales to container width
    s = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"'
         f' style="max-width:{width}px;">',
         f'<rect width="{width}" height="{height}" fill="#fafafa" rx="4"/>',
         f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="white" stroke="#ccc"/>']

    # Grid
    for i in range(5):
        frac = i / 4.0
        gx = ml + frac * pw
        gy = mt + frac * ph
        s.append(f'<line x1="{gx}" y1="{mt}" x2="{gx}" y2="{mt + ph}" stroke="#eee"/>')
        s.append(f'<line x1="{ml}" y1="{gy}" x2="{ml + pw}" y2="{gy}" stroke="#eee"/>')
        xv = x_min + frac * x_range
        yv = y_max - frac * y_range
        s.append(f'<text x="{gx}" y="{mt + ph + 16}" text-anchor="middle" font-size="10" fill="#666">{xv:.0f}</text>')
        s.append(f'<text x="{ml - 6}" y="{gy + 4}" text-anchor="end" font-size="10" fill="#666">{yv:.0f}</text>')

    # Diagonal line y=x
    if diag_line:
        d_min = max(x_min, y_min)
        d_max = min(x_max, y_max)
        if d_min < d_max:
            s.append(f'<line x1="{tx(d_min)}" y1="{ty(d_min)}" x2="{tx(d_max)}" y2="{ty(d_max)}" '
                     f'stroke="#aaa" stroke-width="1.5" stroke-dasharray="6,4"/>')

    # Zero lines
    if zero_lines:
        if y_min <= 0 <= y_max:
            s.append(f'<line x1="{ml}" y1="{ty(0)}" x2="{ml + pw}" y2="{ty(0)}" stroke="#bbb"/>')
        if x_min <= 0 <= x_max:
            s.append(f'<line x1="{tx(0)}" y1="{mt}" x2="{tx(0)}" y2="{mt + ph}" stroke="#bbb"/>')

    # Points
    for x, y in points:
        c = color_func(x, y) if color_func else "#1f77b4"
        s.append(f'<circle cx="{tx(x)}" cy="{ty(y)}" r="3" fill="{c}" opacity="0.7"/>')

    # Labels
    s.append(f'<text x="{ml + pw / 2}" y="{height - 5}" text-anchor="middle" font-size="12" fill="#333">{xlabel}</text>')
    s.append(f'<text x="14" y="{mt + ph / 2}" text-anchor="middle" font-size="12" fill="#333" '
             f'transform="rotate(-90, 14, {mt + ph / 2})">{ylabel}</text>')
    s.append(f'<text x="{width / 2}" y="18" text-anchor="middle" font-size="13" font-weight="bold" fill="#333">{title}</text>')
    s.append('</svg>')
    return '\n'.join(s)

def svg_histogram(values, title, xlabel, width=600, height=300, n_bins=30,
                  zero_line=True, color_neg="#d62728", color_pos="#2ca02c"):
    """Histogram as inline SVG."""
    import math
    values = [v for v in values if v is not None and math.isfinite(v)]
    if not values:
        return f'<p style="color:#888;">No data for: {title}</p>'

    v_min, v_max = min(values), max(values)
    bin_w = max((v_max - v_min) / n_bins, 0.001)
    bins = [0] * n_bins
    for v in values:
        idx = min(int((v - v_min) / bin_w), n_bins - 1)
        bins[idx] += 1
    max_c = max(bins) if bins else 1

    ml, mr, mt, mb = 55, 20, 30, 50
    pw, ph = width - ml - mr, height - mt - mb
    bw = pw / n_bins

    # Use viewBox for responsive sizing
    s = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"'
         f' style="max-width:{width}px;">',
         f'<rect width="{width}" height="{height}" fill="#fafafa" rx="4"/>']

    if zero_line and v_min < 0 < v_max:
        zf = (0 - v_min) / (v_max - v_min)
        zx = ml + zf * pw
        s.append(f'<line x1="{zx}" y1="{mt}" x2="{zx}" y2="{mt + ph}" stroke="red" stroke-width="1.5" stroke-dasharray="4,3"/>')

    for i, cnt in enumerate(bins):
        bx = ml + i * bw
        bh = (cnt / max_c) * ph if max_c > 0 else 0
        by = mt + ph - bh
        mid = v_min + (i + 0.5) * bin_w
        c = color_neg if mid < 0 else color_pos
        s.append(f'<rect x="{bx}" y="{by}" width="{bw - 1}" height="{bh}" fill="{c}" opacity="0.7"/>')

    # X-axis ticks
    for i in range(5):
        frac = i / 4.0
        val = v_min + frac * (v_max - v_min)
        x = ml + frac * pw
        s.append(f'<text x="{x}" y="{mt + ph + 16}" text-anchor="middle" font-size="10" fill="#666">{val:.0f}</text>')

    neg = sum(1 for v in values if v < 0)
    s.append(f'<text x="{width / 2}" y="{height - 5}" text-anchor="middle" font-size="12" fill="#333">{xlabel}</text>')
    s.append(f'<text x="{width / 2}" y="18" text-anchor="middle" font-size="13" font-weight="bold" fill="#333">'
             f'{title} ({neg}/{len(values)} negative)</text>')
    s.append('</svg>')
    return '\n'.join(s)


def svg_dual_histogram(vals1, vals2, label1, label2, title, xlabel,
                       color1="#1976d2", color2="#d62728",
                       width=600, height=300, n_bins=40):
    """Side-by-side grouped histogram of two datasets as inline SVG.
    Each bin has two bars placed side by side for clear comparison."""
    import math
    vals1 = [v for v in vals1 if v is not None and math.isfinite(v)]
    vals2 = [v for v in vals2 if v is not None and math.isfinite(v)]
    if not vals1 and not vals2:
        return f'<p style="color:#888;">No data for: {title}</p>'

    all_vals = vals1 + vals2
    v_min, v_max = min(all_vals), max(all_vals)
    rng = v_max - v_min
    if rng < 0.001:
        rng = 1.0
    bin_w = rng / n_bins

    def make_bins(vals):
        bins = [0] * n_bins
        for v in vals:
            idx = min(int((v - v_min) / bin_w), n_bins - 1)
            bins[idx] += 1
        return bins

    bins1 = make_bins(vals1) if vals1 else [0] * n_bins
    bins2 = make_bins(vals2) if vals2 else [0] * n_bins
    max_c = max(max(bins1), max(bins2), 1)

    ml, mr, mt, mb = 55, 20, 30, 55
    pw, ph = width - ml - mr, height - mt - mb
    bw = pw / n_bins
    # Side-by-side: each bar takes ~45% of bin width with 10% gap
    half_bw = (bw - 1) * 0.45
    gap_bw = (bw - 1) * 0.1

    # Use viewBox for responsive sizing
    s = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"'
         f' style="max-width:{width}px;">',
         f'<rect width="{width}" height="{height}" fill="#fafafa" rx="4"/>']

    # Zero line
    if v_min < 0 < v_max:
        zf = (0 - v_min) / rng
        zx = ml + zf * pw
        s.append(f'<line x1="{zx}" y1="{mt}" x2="{zx}" y2="{mt + ph}" '
                 f'stroke="red" stroke-width="1.5" stroke-dasharray="4,3"/>')

    # Draw bars side-by-side within each bin
    for i in range(n_bins):
        bx = ml + i * bw
        # Dataset 1 (left half)
        bh1 = (bins1[i] / max_c) * ph if max_c > 0 else 0
        if bh1 > 0:
            s.append(f'<rect x="{bx:.1f}" y="{mt + ph - bh1:.1f}" width="{half_bw:.1f}" '
                     f'height="{bh1:.1f}" fill="{color1}" opacity="0.8"/>')
        # Dataset 2 (right half)
        bh2 = (bins2[i] / max_c) * ph if max_c > 0 else 0
        if bh2 > 0:
            x2 = bx + half_bw + gap_bw
            s.append(f'<rect x="{x2:.1f}" y="{mt + ph - bh2:.1f}" width="{half_bw:.1f}" '
                     f'height="{bh2:.1f}" fill="{color2}" opacity="0.8"/>')

    # X-axis ticks
    for i in range(5):
        frac = i / 4.0
        val = v_min + frac * rng
        x = ml + frac * pw
        s.append(f'<text x="{x}" y="{mt + ph + 16}" text-anchor="middle" '
                 f'font-size="10" fill="#666">{val:.0f}</text>')

    # Legend — top-right corner with white background box
    legend_w = 150
    lx = ml + pw - legend_w - 5
    ly = mt + 5
    s.append(f'<rect x="{lx - 4}" y="{ly - 2}" width="{legend_w + 8}" height="32" '
             f'fill="white" stroke="#ccc" rx="3" opacity="0.9"/>')
    s.append(f'<rect x="{lx}" y="{ly + 2}" width="10" height="10" fill="{color1}" opacity="0.8"/>')
    s.append(f'<text x="{lx + 14}" y="{ly + 11}" font-size="10" fill="#333">{label1} ({len(vals1)})</text>')
    s.append(f'<rect x="{lx}" y="{ly + 16}" width="10" height="10" fill="{color2}" opacity="0.8"/>')
    s.append(f'<text x="{lx + 14}" y="{ly + 25}" font-size="10" fill="#333">{label2} ({len(vals2)})</text>')

    # Labels
    s.append(f'<text x="{ml + pw / 2}" y="{height - 5}" text-anchor="middle" '
             f'font-size="12" fill="#333">{xlabel}</text>')
    s.append(f'<text x="{width / 2}" y="18" text-anchor="middle" font-size="13" '
             f'font-weight="bold" fill="#333">{title}</text>')
    s.append('</svg>')
    return '\n'.join(s)


def svg_horizontal_bars(categories, title, width=600, height=None):
    """Horizontal bar chart for violation classification.
    categories: list of (label, value, color) tuples. value can be negative."""
    if not categories:
        return '<p style="color:#888;">No data</p>'
    max_abs = max(abs(v) for _, v, _ in categories) or 1
    n = len(categories)
    bar_h = 28
    gap = 8
    ml, mr, mt, mb = 200, 80, 35, 20
    if height is None:
        height = mt + mb + n * (bar_h + gap)
    pw = width - ml - mr

    s = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
         f'<rect width="{width}" height="{height}" fill="#fafafa" rx="4"/>',
         f'<text x="{width/2}" y="20" text-anchor="middle" font-size="13" '
         f'font-weight="bold" fill="#333">{title}</text>']

    for i, (label, val, color) in enumerate(categories):
        y = mt + i * (bar_h + gap)
        bw = abs(val) / max_abs * pw
        s.append(f'<text x="{ml - 8}" y="{y + bar_h/2 + 4}" text-anchor="end" '
                 f'font-size="11" fill="#333">{label}</text>')
        s.append(f'<rect x="{ml}" y="{y}" width="{bw}" height="{bar_h}" '
                 f'fill="{color}" rx="3" opacity="0.8"/>')
        s.append(f'<text x="{ml + bw + 5}" y="{y + bar_h/2 + 4}" '
                 f'font-size="11" fill="#333">{val:+.0f} ps</text>')

    s.append('</svg>')
    return '\n'.join(s)


# ============================================================================
# Section 6: HTML Report Generator
# ============================================================================




# html_spatial_improvement_map removed: delta <5ps (no color contrast),
# unknown-coordinate FFs stacking at corner. Section 6 scatter is more actionable.


HTML_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>3D CTS Analysis: Cross-Version Comparison</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       max-width: 1200px; margin: 20px auto; padding: 0 20px; background: #f5f5f5; color: #333; }
h1 { border-bottom: 2px solid #333; padding-bottom: 8px; }
h2 { color: #1a1a2e; margin-top: 30px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
.section { background: white; border-radius: 6px; padding: 20px; margin: 16px 0;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
th { background: #f0f0f0; text-align: left; padding: 6px 10px; border: 1px solid #ddd;
     position: sticky; top: 0; }
td { padding: 4px 10px; border: 1px solid #eee; }
tr:nth-child(even) { background: #fafafa; }
tr:hover { background: #f0f8ff; }
.neg { color: #d62728; font-weight: 600; }
.pos { color: #2ca02c; font-weight: 600; }
.ff { font-family: monospace; font-size: 12px; max-width: 220px; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; }
.summary td:first-child { font-weight: 600; width: 280px; }
.cause { font-size: 11px; color: #555; }
details { margin: 10px 0; }
summary { cursor: pointer; color: #1f77b4; font-weight: 600; }
svg { display: block; margin: 10px auto; }
.two-col { display: flex; gap: 20px; flex-wrap: wrap; }
.two-col > div { flex: 1; min-width: 300px; }
.insight { background: #e3f2fd; border-left: 4px solid #1976d2; padding: 10px 14px;
           margin: 12px 0; border-radius: 0 4px 4px 0; font-size: 13px; }
.insight b { color: #0d47a1; }
</style>
</head><body>
"""

HTML_FOOT = """
<div class="section" style="text-align:center; color:#999; font-size:12px;">
  <p>CTS Debug Compare | 3D CTS Project | Compatible with V40/V41/V42/V42a</p>
</div></body></html>"""


# ============================================================================
# Section 6b: Matplotlib PNG Generation & Base64 Embedding
# ============================================================================

def generate_matplotlib_pngs(test_flow_dir, platform, design, base_flow_dir=None,
                             base_label="Pin3D", test_label="3DCTS"):
    """Generate htree topology PNGs via plot_cts_topology.py (scripts_common).

    Generates 3 images:
      - htree_topology: 2D dual-tier (Bottom | Upper) with level-based styling
      - htree_3d: 3D progressive breakdown (4-panel, level-by-level)
      - htree_3d_clean: 3D single-panel overview
    """
    results = {}

    # Find plot_cts_topology.py in scripts_common (sibling of flow dirs)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vis_script = os.path.join(script_dir, "plot_cts_topology.py")
    if not os.path.isfile(vis_script):
        # Also try scripts_common at ORFS root level
        orfs_root = os.path.dirname(test_flow_dir)
        vis_script = os.path.join(orfs_root, "scripts_common", "plot_cts_topology.py")
    if not os.path.isfile(vis_script):
        print(f"  [WARN] plot_cts_topology.py not found, skipping htree topology")
        return results

    # Find clock_tree_edges.csv in test results
    edges_csv = os.path.join(test_flow_dir, "results", platform, design,
                             "openroad", "clock_tree_edges.csv")
    if not os.path.isfile(edges_csv):
        print(f"  [WARN] clock_tree_edges.csv not found at {edges_csv}")
        return results

    tmp_dir = os.path.join(tempfile.gettempdir(), f"htree_{platform}_{design}")

    try:
        cmd = [sys.executable, vis_script,
               "--edges", edges_csv,
               "--design", design,
               "--platform", platform,
               "--style", "all",
               "--output", tmp_dir]
        subprocess.run(cmd, timeout=120, check=True,
                      capture_output=True, text=True)

        # Read 2D dual-tier PNG
        dual_path = os.path.join(tmp_dir, "cts_2d_dual_tier.png")
        if os.path.isfile(dual_path):
            with open(dual_path, "rb") as f:
                results["htree_topology"] = base64.b64encode(f.read()).decode()

        # Read 3D breakdown PNG
        bd_path = os.path.join(tmp_dir, "cts_3d_breakdown.png")
        if os.path.isfile(bd_path):
            with open(bd_path, "rb") as f:
                results["htree_3d"] = base64.b64encode(f.read()).decode()

        # Read 3D clean overview PNG
        clean_path = os.path.join(tmp_dir, "cts_3d_clean.png")
        if os.path.isfile(clean_path):
            with open(clean_path, "rb") as f:
                results["htree_3d_clean"] = base64.b64encode(f.read()).decode()

        if results:
            print(f"  Htree topology PNGs generated ({len(results)} images)")
    except Exception as e:
        print(f"  [WARN] htree visualization failed: {e}")

    return results


def generate_report(base_label, test_label, base_rpt, test_rpt,
                    edge_comp, ff_comp, lp_corr, io_hold,
                    test_debug_per_ff, lp_targets,
                    output_dir, test_edges=None, neg_cycle_data=None,
                    cross_tier_data=None, matplotlib_pngs=None,
                    lp_stats=None, platform=None, design=None,
                    base_clock_paths=None, test_clock_paths=None,
                    base_debug_buffers=None, test_debug_buffers=None,
                    base_debug_ff=None, test_debug_ff_list=None):
    """Generate the full HTML comparison report.
    Section order:
      1. Summary Dashboard (timing report + per-FF stats + per-tier)
      2. Cross-Tier Clock Tree Structure (Pin3D vs 3DCTS structural comparison)
     2b. Interactive Clock Tree Schematic (D3.js: base vs test side-by-side)
     2c. Per-FF Spatial Improvement Map (physical position × slack delta)
      3. Negative Cycle Analysis (the theoretical limit)
      4. LP Target vs Actual (why the limit isn't reached)
      5. Per-Edge Slack Change Distribution
      6. Per-FF Slack Scatter (base vs test)
      7. PI→FF Hold Violation Detail
      8. Notable Changes (worst hold + best setup, collapsible)
      9. Full Per-FF Comparison (collapsible)
     10. Visual Analysis (embedded matplotlib PNGs)
    """
    os.makedirs(output_dir, exist_ok=True)
    # HTML filename reflects versions + platform + design
    safe_base = base_label.replace(" ", "_").replace("/", "-")
    safe_test = test_label.replace(" ", "_").replace("/", "-")
    design_suffix = f"_{platform}_{design}" if platform and design else ""
    html_filename = f"cts_debug_compare_{safe_base}_vs_{safe_test}{design_suffix}.html"
    html_path = os.path.join(output_dir, html_filename)
    csv_path = os.path.join(output_dir, "per_ff_comparison.csv")

    parts = [HTML_HEAD]
    parts.append(f'<h1>3D CTS Analysis: {base_label} vs {test_label}</h1>')

    # ==== Section 0: Executive Summary Banner ====
    # Dark-background compact summary visible without scrolling
    def _fmtd(base_v, test_v):
        """Format base→test with colored delta."""
        if base_v is None or test_v is None:
            return 'N/A'
        d = test_v - base_v
        color = '#4caf50' if d > 0 else '#ef5350' if d < 0 else '#aaa'
        sign = '+' if d > 0 else ''
        pct = f' ({sign}{d/abs(base_v)*100:.1f}%)' if base_v != 0 else ''
        return (f'<span style="color:#ccc">{base_v:>10.1f}</span>'
                f' → <span style="color:#fff">{test_v:>10.1f}</span>'
                f'  <span style="color:{color};font-weight:700">{sign}{d:.1f}ps{pct}</span>')

    s_tns_b = safe_float(base_rpt.get("setup_tns"))
    s_tns_t = safe_float(test_rpt.get("setup_tns"))
    h_tns_b = safe_float(base_rpt.get("hold_tns"))
    h_tns_t = safe_float(test_rpt.get("hold_tns"))
    s_wns_b = safe_float(base_rpt.get("setup_wns"))
    s_wns_t = safe_float(test_rpt.get("setup_wns"))
    h_wns_b = safe_float(base_rpt.get("hold_wns"))
    h_wns_t = safe_float(test_rpt.get("hold_wns"))

    n_edges = len(edge_comp) if edge_comp else 0
    setup_imp = sum(1 for e in edge_comp if (e.get('delta_setup_ps') or 0) > 0) if edge_comp else 0
    hold_imp = sum(1 for e in edge_comp if (e.get('delta_hold_ps') or 0) > 0) if edge_comp else 0

    banner_lines = []
    banner_lines.append(f'  Setup TNS   : {_fmtd(s_tns_b, s_tns_t)}')
    banner_lines.append(f'  Hold  TNS   : {_fmtd(h_tns_b, h_tns_t)}')
    banner_lines.append(f'  Setup WNS   : {_fmtd(s_wns_b, s_wns_t)}')
    banner_lines.append(f'  Hold  WNS   : {_fmtd(h_wns_b, h_wns_t)}')
    if n_edges:
        banner_lines.append(f'<span style="color:#888">  ────────────────────────────────</span>')
        banner_lines.append(f'  FF→FF Setup : <span style="color:#fff">{setup_imp}/{n_edges}</span>'
                            f' edges improved <span style="color:#aaa">({setup_imp/n_edges*100:.0f}%)</span>')
        banner_lines.append(f'  FF→FF Hold  : <span style="color:#fff">{hold_imp}/{n_edges}</span>'
                            f' edges improved <span style="color:#aaa">({hold_imp/n_edges*100:.0f}%)</span>')
    parts.append('<div style="background:#1a1a2e; color:#e0e0e0; font-family:\'Courier New\',monospace;'
                 ' font-size:13px; padding:16px 20px; border-radius:8px; margin:12px 0;'
                 ' line-height:1.7; box-shadow:0 2px 8px rgba(0,0,0,0.3);">')
    parts.append(f'<div style="color:#64b5f6; font-weight:700; font-size:14px; margin-bottom:6px;">'
                 f'═══ SUMMARY: {base_label} vs {test_label} ═══</div>')
    parts.append('<br>'.join(banner_lines))
    parts.append('</div>')

    # ==== Section 1: Summary Dashboard ====
    parts.append('<div class="section"><h2>1. Summary Dashboard</h2>')

    # 1a. Timing report comparison table
    parts.append('<table class="summary">')
    parts.append(f'<tr><th>Metric</th><th>{base_label}</th><th>{test_label}</th><th>Delta</th><th>%</th></tr>')
    # Timing rows: Setup/Hold TNS/WNS with improvement coloring
    # For TNS/WNS: smaller magnitude = better (less negative), so delta>0 means improvement
    for key, label in [("setup_tns", "Setup TNS (ps)"), ("setup_wns", "Setup WNS (ps)"),
                       ("hold_tns", "Hold TNS (ps)"), ("hold_wns", "Hold WNS (ps)")]:
        bv = base_rpt.get(key)
        tv = test_rpt.get(key)
        if bv is not None and tv is not None:
            delta = tv - bv
            pct = (delta / abs(bv) * 100) if bv != 0 else 0
            cls = "pos" if delta > 0 else "neg" if delta < 0 else ""
            sign = "+" if delta > 0 else ""
            parts.append(f'<tr><td>{label}</td><td>{bv:.1f}</td><td>{tv:.1f}</td>'
                         f'<td class="{cls}">{sign}{delta:.1f}</td>'
                         f'<td class="{cls}">{sign}{pct:.1f}%</td></tr>')
    # Clock buffer count row
    bbuf = base_rpt.get("clock_buf_count")
    tbuf = test_rpt.get("clock_buf_count")
    if bbuf is not None and tbuf is not None:
        parts.append(f'<tr><td>Clock Buffer Count</td><td>{int(bbuf)}</td><td>{int(tbuf)}</td>'
                     f'<td>{int(tbuf - bbuf):+d}</td><td></td></tr>')
    # CTS mode row
    bmode = base_rpt.get("cts_mode", "")
    tmode = test_rpt.get("cts_mode", "")
    if bmode or tmode:
        parts.append(f'<tr><td>CTS Mode</td><td>{bmode}</td><td>{tmode}</td>'
                     f'<td></td><td></td></tr>')
    parts.append('</table>')

    # 1b. Per-FF statistics (improved/degraded counts based on worst per-FF slack)
    if ff_comp:
        ff_matched = [(c, lp_targets.get(c["ff_name"], 0) * 1000 if lp_targets else 0)
                      for c in ff_comp
                      if c["base_setup_ns"] is not None and c["test_setup_ns"] is not None]
        ff_setup_deltas = [c["delta_setup_ps"] for c, _ in ff_matched if c["delta_setup_ps"] is not None]
        ff_hold_deltas = [c["delta_hold_ps"] for c, _ in ff_matched if c["delta_hold_ps"] is not None]

        parts.append('<h3>Per-FF Statistics (worst slack per FF)</h3>')
        parts.append('<table class="summary">')
        parts.append(f'<tr><td>Total FFs in comparison</td><td colspan="4">{len(ff_comp)}</td></tr>')
        parts.append(f'<tr><td>Matched FFs (both base &amp; test have data)</td>'
                     f'<td colspan="4">{len(ff_matched)}</td></tr>')

        if ff_setup_deltas:
            imp_s = sum(1 for d in ff_setup_deltas if d > 0)
            deg_s = sum(1 for d in ff_setup_deltas if d < 0)
            unch_s = len(ff_setup_deltas) - imp_s - deg_s
            mean_s = sum(ff_setup_deltas) / len(ff_setup_deltas)
            med_s = percentile(ff_setup_deltas, 50)
            tns_s = sum(ff_setup_deltas)
            parts.append(f'<tr><td>Setup: improved / degraded / unchanged</td><td colspan="4">'
                         f'<span class="pos">{imp_s}</span> / '
                         f'<span class="neg">{deg_s}</span> / {unch_s} '
                         f'({imp_s/len(ff_setup_deltas)*100:.0f}% / '
                         f'{deg_s/len(ff_setup_deltas)*100:.0f}%)</td></tr>')
            parts.append(f'<tr><td>Setup delta: mean / median / TNS</td><td colspan="4">'
                         f'{mean_s:+.1f}ps / {med_s:+.1f}ps / {tns_s:+.0f}ps</td></tr>')

        if ff_hold_deltas:
            imp_h = sum(1 for d in ff_hold_deltas if d > 0)
            deg_h = sum(1 for d in ff_hold_deltas if d < 0)
            unch_h = len(ff_hold_deltas) - imp_h - deg_h
            mean_h = sum(ff_hold_deltas) / len(ff_hold_deltas)
            med_h = percentile(ff_hold_deltas, 50)
            tns_h = sum(ff_hold_deltas)
            parts.append(f'<tr><td>Hold: improved / degraded / unchanged</td><td colspan="4">'
                         f'<span class="pos">{imp_h}</span> / '
                         f'<span class="neg">{deg_h}</span> / {unch_h} '
                         f'({imp_h/len(ff_hold_deltas)*100:.0f}% / '
                         f'{deg_h/len(ff_hold_deltas)*100:.0f}%)</td></tr>')
            parts.append(f'<tr><td>Hold delta: mean / median / TNS</td><td colspan="4">'
                         f'{mean_h:+.1f}ps / {med_h:+.1f}ps / {tns_h:+.0f}ps</td></tr>')

        # Both setup and hold analysis
        if ff_setup_deltas and ff_hold_deltas:
            both_imp = sum(1 for c, _ in ff_matched
                          if c["delta_setup_ps"] is not None and c["delta_setup_ps"] > 0
                          and c["delta_hold_ps"] is not None and c["delta_hold_ps"] > 0)
            setup_only = sum(1 for c, _ in ff_matched
                            if c["delta_setup_ps"] is not None and c["delta_setup_ps"] > 0
                            and c["delta_hold_ps"] is not None and c["delta_hold_ps"] < 0)
            hold_only = sum(1 for c, _ in ff_matched
                           if c["delta_setup_ps"] is not None and c["delta_setup_ps"] < 0
                           and c["delta_hold_ps"] is not None and c["delta_hold_ps"] > 0)
            both_deg = sum(1 for c, _ in ff_matched
                          if c["delta_setup_ps"] is not None and c["delta_setup_ps"] < 0
                          and c["delta_hold_ps"] is not None and c["delta_hold_ps"] < 0)
            parts.append(f'<tr><td>Both setup+hold improved</td>'
                         f'<td colspan="4"><span class="pos">{both_imp} FFs</span></td></tr>')
            parts.append(f'<tr><td>Setup improved, hold degraded (trade-off)</td>'
                         f'<td colspan="4"><span class="neg">{setup_only} FFs</span></td></tr>')
            parts.append(f'<tr><td>Hold improved, setup degraded</td>'
                         f'<td colspan="4">{hold_only} FFs</td></tr>')
            parts.append(f'<tr><td>Both degraded</td>'
                         f'<td colspan="4"><span class="neg">{both_deg} FFs</span></td></tr>')


        # LP target correlation: breakdown by target group
        if lp_targets and ff_setup_deltas:
            t0_setup = [c["delta_setup_ps"] for c, lp in ff_matched
                        if c["delta_setup_ps"] is not None and abs(lp) < 0.5]
            t_pos_setup = [c["delta_setup_ps"] for c, lp in ff_matched
                          if c["delta_setup_ps"] is not None and lp >= 0.5]
            t0_hold = [c["delta_hold_ps"] for c, lp in ff_matched
                       if c["delta_hold_ps"] is not None and abs(lp) < 0.5]
            t_pos_hold = [c["delta_hold_ps"] for c, lp in ff_matched
                         if c["delta_hold_ps"] is not None and lp >= 0.5]
            parts.append('</table>')
            parts.append('<h3>Per-FF by LP Target Group</h3>')
            parts.append('<table class="summary">')
            parts.append('<tr><th>Group</th><th>Count</th>'
                         '<th>Setup: improved / degraded</th><th>Mean Setup Δ</th>'
                         '<th>Hold: improved / degraded</th><th>Mean Hold Δ</th></tr>')
            if t0_setup:
                s_imp = sum(1 for d in t0_setup if d > 0)
                s_deg = sum(1 for d in t0_setup if d < 0)
                h_imp = sum(1 for d in t0_hold if d > 0)
                h_deg = sum(1 for d in t0_hold if d < 0)
                parts.append(f'<tr><td>LP target = 0</td><td>{len(t0_setup)}</td>'
                             f'<td><span class="pos">{s_imp}</span> / <span class="neg">{s_deg}</span></td>'
                             f'<td>{sum(t0_setup)/len(t0_setup):+.1f}ps</td>'
                             f'<td><span class="pos">{h_imp}</span> / <span class="neg">{h_deg}</span></td>'
                             f'<td>{sum(t0_hold)/len(t0_hold):+.1f}ps</td></tr>')
            if t_pos_setup:
                s_imp = sum(1 for d in t_pos_setup if d > 0)
                s_deg = sum(1 for d in t_pos_setup if d < 0)
                h_imp = sum(1 for d in t_pos_hold if d > 0)
                h_deg = sum(1 for d in t_pos_hold if d < 0)
                parts.append(f'<tr><td>LP target &gt; 0</td><td>{len(t_pos_setup)}</td>'
                             f'<td><span class="pos">{s_imp}</span> / <span class="neg">{s_deg}</span></td>'
                             f'<td>{sum(t_pos_setup)/len(t_pos_setup):+.1f}ps</td>'
                             f'<td><span class="pos">{h_imp}</span> / <span class="neg">{h_deg}</span></td>'
                             f'<td>{sum(t_pos_hold)/len(t_pos_hold):+.1f}ps</td></tr>')

        parts.append('</table>')


    # Clock latency per tier (from debug per-FF data if available)
    if test_debug_per_ff:
        tier_lat = {"upper": [], "bottom": []}
        for ff_name, ff_row in test_debug_per_ff.items():
            lat = safe_float(ff_row.get("clk_latency_ps"))
            tier = ff_tier(ff_name)
            if lat is not None and tier in tier_lat:
                tier_lat[tier].append(lat)
        if any(tier_lat.values()):
            parts.append('<h3>Clock Latency per Tier (post-CTS)</h3>')
            parts.append('<table class="summary">')
            for tier in ["upper", "bottom"]:
                vals = tier_lat[tier]
                if not vals:
                    continue
                mean_l = sum(vals) / len(vals)
                std_l = (sum((v - mean_l) ** 2 for v in vals) / len(vals)) ** 0.5
                min_l = min(vals)
                max_l = max(vals)
                parts.append(f'<tr><td>{tier.capitalize()} ({len(vals)} FFs)</td>'
                             f'<td colspan="4">mean={mean_l:.1f}ps, std={std_l:.1f}ps, '
                             f'min={min_l:.1f}ps, max={max_l:.1f}ps</td></tr>')
            # Inter-tier gap
            if tier_lat["upper"] and tier_lat["bottom"]:
                u_mean = sum(tier_lat["upper"]) / len(tier_lat["upper"])
                b_mean = sum(tier_lat["bottom"]) / len(tier_lat["bottom"])
                gap = u_mean - b_mean
                gap_cls = "neg" if abs(gap) > 5 else ""
                parts.append(f'<tr><td>Inter-tier gap (upper - bottom)</td>'
                             f'<td colspan="4" class="{gap_cls}">{gap:+.1f}ps</td></tr>')
            parts.append('</table>')


    parts.append('</div>')

    # ==== Section 2: Cross-Tier Clock Tree Structure ====
    parts.append('<div class="section"><h2>2. Cross-Tier Clock Tree Structure</h2>')
    parts.append('<p>Compares how clock signals cross tiers between base (Pin3D: all buffers on bottom) '
                 'and test (3D CTS: buffers on both tiers). Key question: how much did '
                 '<b>HBT (Hybrid Bonding Technology) crossings</b> change?</p>')

    if cross_tier_data:
        ct = cross_tier_data

        # 2a. Buffer placement comparison
        parts.append('<h3>Buffer Placement by Tier</h3>')
        parts.append('<table class="summary">')
        parts.append(f'<tr><th>Metric</th><th>{base_label}</th><th>{test_label}</th><th>Delta</th></tr>')

        bb = ct.get("base_buf_tiers") or {"upper": 0, "bottom": 0}
        tb = ct.get("test_buf_tiers") or {"upper": 0, "bottom": 0}
        bn = ct["base_n_bufs"]
        tn = ct["test_n_bufs"]

        parts.append(f'<tr><td>Total clock buffers</td><td>{bn}</td><td>{tn}</td>'
                     f'<td>{tn - bn:+d}</td></tr>')
        parts.append(f'<tr><td>Bottom-tier buffers</td><td>{bb["bottom"]}</td>'
                     f'<td>{tb["bottom"]}</td>'
                     f'<td>{tb["bottom"] - bb["bottom"]:+d}</td></tr>')
        parts.append(f'<tr><td>Upper-tier buffers</td><td>{bb.get("upper", 0)}</td>'
                     f'<td>{tb.get("upper", 0)}</td>'
                     f'<td class="{"pos" if tb.get("upper", 0) > bb.get("upper", 0) else ""}">'
                     f'{tb.get("upper", 0) - bb.get("upper", 0):+d}</td></tr>')
        if tn > 0:
            pct_upper = tb.get("upper", 0) / tn * 100
            parts.append(f'<tr><td>Upper-tier buffer ratio</td>'
                         f'<td>{bb.get("upper", 0) / bn * 100 if bn > 0 else 0:.1f}%</td>'
                         f'<td>{pct_upper:.1f}%</td><td></td></tr>')
        # Add FF counts per tier (from path_tier_summary totals)
        bp_ff = ct.get("base_path_tiers") or {}
        tp_ff = ct.get("test_path_tiers") or {}
        b_upper_ff = bp_ff.get("upper", {}).get("total", 0)
        b_bottom_ff = bp_ff.get("bottom", {}).get("total", 0)
        t_upper_ff = tp_ff.get("upper", {}).get("total", 0)
        t_bottom_ff = tp_ff.get("bottom", {}).get("total", 0)
        b_total_ff = b_upper_ff + b_bottom_ff
        t_total_ff = t_upper_ff + t_bottom_ff
        if b_total_ff > 0 or t_total_ff > 0:
            parts.append('<tr><td colspan="4" style="background:#f0f4fa;font-weight:bold;'
                         'padding:4px 8px;font-size:12px">FFs</td></tr>')
            parts.append(f'<tr><td>Total FFs</td><td>{b_total_ff}</td><td>{t_total_ff}</td>'
                         f'<td>{t_total_ff - b_total_ff:+d}</td></tr>')
            parts.append(f'<tr><td>Upper-tier FFs</td><td>{b_upper_ff}</td><td>{t_upper_ff}</td>'
                         f'<td>{t_upper_ff - b_upper_ff:+d}</td></tr>')
            parts.append(f'<tr><td>Bottom-tier FFs</td><td>{b_bottom_ff}</td><td>{t_bottom_ff}</td>'
                         f'<td>{t_bottom_ff - b_bottom_ff:+d}</td></tr>')
            if b_total_ff > 0 and t_total_ff > 0:
                parts.append(f'<tr><td>Upper FF ratio</td>'
                             f'<td>{b_upper_ff / b_total_ff * 100:.1f}%</td>'
                             f'<td>{t_upper_ff / t_total_ff * 100:.1f}%</td>'
                             f'<td></td></tr>')
        parts.append('</table>')

        # 2b. FF clock path tier classification
        bp = ct.get("base_path_tiers")
        tp = ct.get("test_path_tiers")
        if bp or tp:
            parts.append('<h3>FF Clock Path Tier Classification</h3>')
            parts.append('<p><b>bottom_only</b>: all buffers in FF\'s clock path are on bottom tier. '
                         '<b>mixed</b>: path includes buffers on both tiers (3D CTS placed leaf buffer on same tier as FF).</p>')
            parts.append('<table>')
            parts.append(f'<tr><th>FF Tier</th><th>Classification</th>'
                         f'<th>{base_label}</th><th>{test_label}</th><th>Delta</th></tr>')
            for ft in ["upper", "bottom"]:
                for cls in ["bottom_only", "mixed", "upper_only"]:
                    bv = bp.get(ft, {}).get(cls, 0) if bp else 0
                    tv = tp.get(ft, {}).get(cls, 0) if tp else 0
                    if bv == 0 and tv == 0:
                        continue
                    delta = tv - bv
                    dcls = "pos" if (cls == "mixed" and ft == "upper" and delta > 0) else (
                           "neg" if (cls == "bottom_only" and ft == "upper" and delta < 0) else "")
                    parts.append(f'<tr><td>{ft}</td><td>{cls}</td>'
                                 f'<td>{bv}</td><td>{tv}</td>'
                                 f'<td class="{dcls}">{delta:+d}</td></tr>')
            parts.append('</table>')

        # 2c. HBT crossing count
        bhc = ct.get("base_hbt_cross")
        thc = ct.get("test_hbt_cross")
        if bhc is not None or thc is not None:
            parts.append('<h3>Leaf Buffer → FF HBT Crossings</h3>')
            parts.append('<table class="summary">')
            parts.append(f'<tr><th>Metric</th><th>{base_label}</th><th>{test_label}</th><th>Delta</th></tr>')
            bhc_v = bhc if bhc is not None else 0
            thc_v = thc if thc is not None else 0
            bhs = ct.get("base_hbt_same") or 0
            ths = ct.get("test_hbt_same") or 0
            delta_cross = thc_v - bhc_v
            dcls = "pos" if delta_cross < 0 else "neg" if delta_cross > 0 else ""
            parts.append(f'<tr><td>Cross-tier (HBT) leaf→FF connections</td>'
                         f'<td>{bhc_v}</td><td>{thc_v}</td>'
                         f'<td class="{dcls}">{delta_cross:+d}</td></tr>')
            parts.append(f'<tr><td>Same-tier leaf→FF connections</td>'
                         f'<td>{bhs}</td><td>{ths}</td>'
                         f'<td>{ths - bhs:+d}</td></tr>')
            total_b = bhc_v + bhs
            total_t = thc_v + ths
            if total_b > 0 and total_t > 0:
                parts.append(f'<tr><td>Cross-tier ratio</td>'
                             f'<td>{bhc_v / total_b * 100:.1f}%</td>'
                             f'<td>{thc_v / total_t * 100:.1f}%</td><td></td></tr>')
            # Direction breakdown for test
            b2u = ct.get("test_hbt_b2u", 0)
            u2b = ct.get("test_hbt_u2b", 0)
            if b2u > 0 or u2b > 0:
                parts.append(f'<tr><td>{test_label} HBT direction: bottom→upper / upper→bottom</td>'
                             f'<td colspan="3">{b2u} / {u2b}</td></tr>')
            # HBT crossings as % of upper-tier FFs (upper FFs are the ones crossing via HBT)
            bp_ff2 = ct.get("base_path_tiers") or {}
            tp_ff2 = ct.get("test_path_tiers") or {}
            b_upper_ff2 = bp_ff2.get("upper", {}).get("total", 0)
            t_upper_ff2 = tp_ff2.get("upper", {}).get("total", 0)
            if b_upper_ff2 > 0 or t_upper_ff2 > 0:
                b_pct = bhc_v / b_upper_ff2 * 100 if b_upper_ff2 > 0 else 0
                t_pct = thc_v / t_upper_ff2 * 100 if t_upper_ff2 > 0 else 0
                parts.append(f'<tr><td>HBT crossings / upper FFs'
                             f' (base={b_upper_ff2}, test={t_upper_ff2})</td>'
                             f'<td>{b_pct:.0f}%</td><td>{t_pct:.0f}%</td>'
                             f'<td>{t_pct - b_pct:+.0f}pp</td></tr>')
            parts.append('</table>')

        # 2d. Per-tier latency comparison (base vs test)
        bl = ct.get("base_tier_lat", {})
        tl = ct.get("test_tier_lat", {})
        if any(bl.values()) and any(tl.values()):
            parts.append('<h3>Clock Latency by Tier: Base vs Test</h3>')
            parts.append('<table class="summary">')
            parts.append(f'<tr><th>Tier</th><th>{base_label} mean (ps)</th>'
                         f'<th>{test_label} mean (ps)</th><th>Delta</th></tr>')
            for tier in ["upper", "bottom"]:
                bvals = bl.get(tier, [])
                tvals = tl.get(tier, [])
                if not bvals or not tvals:
                    continue
                bm = sum(bvals) / len(bvals)
                tm = sum(tvals) / len(tvals)
                d = tm - bm
                parts.append(f'<tr><td>{tier.capitalize()} ({len(tvals)} FFs)</td>'
                             f'<td>{bm:.1f}</td><td>{tm:.1f}</td>'
                             f'<td>{d:+.1f}</td></tr>')
            # Inter-tier gap comparison
            if bl.get("upper") and bl.get("bottom") and tl.get("upper") and tl.get("bottom"):
                b_gap = sum(bl["upper"]) / len(bl["upper"]) - sum(bl["bottom"]) / len(bl["bottom"])
                t_gap = sum(tl["upper"]) / len(tl["upper"]) - sum(tl["bottom"]) / len(tl["bottom"])
                parts.append(f'<tr><td>Inter-tier gap (upper - bottom)</td>'
                             f'<td>{b_gap:+.1f}</td><td>{t_gap:+.1f}</td>'
                             f'<td>{t_gap - b_gap:+.1f}</td></tr>')
            parts.append('</table>')


        # 2f. Insight box
        # Build dynamic insight based on data
        insight_parts = []
        if ct.get("test_buf_tiers") and ct["test_buf_tiers"].get("upper", 0) > 0:
            n_upper_buf = ct["test_buf_tiers"]["upper"]
            insight_parts.append(f'3D CTS places <b>{n_upper_buf} buffers on upper tier</b> '
                                 f'(Pin3D: 0). ')
        tp = ct.get("test_path_tiers")
        if tp and tp.get("upper", {}).get("mixed", 0) > 0:
            n_mixed = tp["upper"]["mixed"]
            n_total = tp["upper"].get("total", n_mixed)
            pct = n_mixed / n_total * 100 if n_total > 0 else 0
            insight_parts.append(f'<b>{pct:.0f}%</b> of upper-tier FFs now receive clock '
                                 f'from a same-tier leaf buffer (mixed path), reducing last-mile '
                                 f'HBT wire delay. ')
        if bhc is not None and thc is not None and bhc > thc:
            reduction = (bhc - thc) / bhc * 100
            insight_parts.append(f'HBT leaf→FF crossings reduced by <b>{reduction:.0f}%</b> '
                                 f'({bhc} → {thc}). ')
        elif bhc is not None and thc is not None and thc > bhc:
            # 3D CTS may increase crossings if bottom FFs get upper buffers
            increase = (thc - bhc) / bhc * 100 if bhc > 0 else 0
            insight_parts.append(f'HBT crossings increased by <b>{increase:.0f}%</b> '
                                 f'({bhc} → {thc}) — some bottom FFs routed through upper buffers. ')
        if insight_parts:
            parts.append('<div class="insight">')
            parts.append(''.join(insight_parts))
            parts.append('</div>')
    else:
        parts.append('<p style="color:#888;">No cross-tier data available. '
                     'Run with CTS_ENABLE_DEBUG_EXTRACT=1 on both base and test versions.</p>')

    parts.append('</div>')

    # ==== Section 2b: Clock Tree Spatial Topology (htree PNG) ====
    pngs = matplotlib_pngs or {}
    parts.append('<div class="section"><h2>2b. Clock Tree Spatial Topology</h2>')
    if pngs.get("htree_topology"):
        parts.append('<p>Per-tier 2D topology showing H-tree buffers (\u25cb), grouped delay chain buffers (\u25b3), '
                     'and cross-tier connections (dashed lines). Color = H-tree level.</p>')
        parts.append(f'<img src="data:image/png;base64,{pngs["htree_topology"]}" '
                     f'style="max-width:100%; border:1px solid #ddd; border-radius:4px;" '
                     f'alt="H-Tree 2-Panel Topology">')
        if pngs.get("htree_3d"):
            parts.append('<details><summary>3D Progressive Build-up (click to expand)</summary>')
            parts.append(f'<img src="data:image/png;base64,{pngs["htree_3d"]}" '
                         f'style="max-width:100%; border:1px solid #ddd; border-radius:4px;" '
                         f'alt="H-Tree 3D Progressive Build-up">')
            parts.append('</details>')
        if pngs.get("htree_3d_clean"):
            parts.append('<details><summary>3D Clean Overview (click to expand)</summary>')
            parts.append(f'<img src="data:image/png;base64,{pngs["htree_3d_clean"]}" '
                         f'style="max-width:100%; border:1px solid #ddd; border-radius:4px;" '
                         f'alt="H-Tree 3D Clean Overview">')
            parts.append('</details>')
    else:
        parts.append('<p class="insight">\u26a0 Clock tree topology visualization not available. '
                     'Run <code>extract_clock_tree_topology.tcl</code> during CTS to generate '
                     '<code>clock_tree_edges.csv</code>.</p>')
    parts.append('</div>')

    # Section 2c removed: spatial improvement map had <5ps deltas (no color contrast)
    # and unknown-coordinate FFs stacking at corner. Section 6 scatter is more actionable.

    # ==== Section 3: Negative Cycle Analysis (Setup) ====
    parts.append('<div class="section"><h2>3. Negative Cycle Analysis (Setup)</h2>')
    parts.append('<p>Negative cycles = groups of FFs whose setup violations are '
                 '<b>mutually conflicting</b>. Fixing one edge worsens another. '
                 'No skew can reduce cycle-locked TNS.</p>')
    if neg_cycle_data:
        nc = neg_cycle_data
        # Per-endpoint TNS for context
        ep_worst = {}
        if test_edges:
            for e in test_edges:
                s = safe_float(e.get("slack_max_ns"))
                if s is not None:
                    ff = e["to_ff"]
                    if ff not in ep_worst or s < ep_worst[ff]:
                        ep_worst[ff] = s
        ep_tns = sum(v for v in ep_worst.values() if v < 0) * 1000

        # 2a. Summary stats
        parts.append('<h3>Graph &amp; SCC Summary</h3>')
        parts.append('<table>')
        parts.append(f'<tr><td>FFs in graph</td><td>{nc["n_ffs"]}</td></tr>')
        parts.append(f'<tr><td>Total edges (FF→FF)</td><td>{nc["n_edges"]}</td></tr>')
        parts.append(f'<tr><td>Violated edges (setup &lt; 0)</td>'
                     f'<td class="neg">{nc["n_viol"]} ({nc["n_viol"]/nc["n_edges"]*100:.1f}%)</td></tr>')
        parts.append(f'<tr><td><b>Per-endpoint Setup TNS</b> (STA-compatible)</td>'
                     f'<td class="neg"><b>{ep_tns:+.0f} ps</b></td></tr>')
        parts.append(f'<tr><td>Per-edge Setup TNS (sum of all neg edges)</td>'
                     f'<td class="neg">{nc["tns_total_ps"]:+.0f} ps</td></tr>')
        parts.append(f'<tr><td style="font-size:11px;color:#888;">↳ per-edge / per-endpoint ratio</td>'
                     f'<td style="font-size:11px;color:#888;">'
                     f'{nc["tns_total_ps"]/ep_tns:.1f}x (one FF has multiple fanin edges)</td></tr>'
                     if ep_tns != 0 else '')
        parts.append(f'<tr><td>Non-trivial SCCs (size&gt;1)</td>'
                     f'<td>{nc["n_sccs"]}</td></tr>')
        parts.append(f'<tr><td>FFs in SCCs</td>'
                     f'<td>{nc["n_scc_ffs"]} / {nc["n_ffs"]} '
                     f'({nc["n_scc_ffs"]/nc["n_ffs"]*100:.1f}%)</td></tr>')
        parts.append(f'<tr><td>Negative cycles found</td>'
                     f'<td>{nc["n_cycles"]}</td></tr>')
        if nc["cycle_weights_ps"]:
            parts.append(f'<tr><td>Worst cycle weight</td>'
                         f'<td class="neg">{min(nc["cycle_weights_ps"]):+.0f} ps</td></tr>')
        parts.append('</table>')

        # 2b. Violation classification bar chart
        cls = nc["classification"]
        both_n, both_tns = cls["both_in"]
        one_n, one_tns = cls["one_in"]
        free_n, free_tns = cls["free"]
        parts.append('<h3>Violation Classification (per-edge)</h3>')
        parts.append('<p>Cycle-locked = both FFs in an SCC → skew cannot help. '
                     'Partially fixable = one FF in SCC. Cycle-free = fully fixable by skew.</p>')
        bars = [
            (f"Cycle-locked ({both_n} edges)", both_tns, "#d32f2f"),
            (f"Partially fixable ({one_n} edges)", one_tns, "#f57c00"),
            (f"Cycle-free ({free_n} edges)", free_tns, "#388e3c"),
        ]
        parts.append(svg_horizontal_bars(bars,
            "Setup Violation TNS by Fixability (ps)"))

        if nc["tns_total_ps"] != 0:
            pct_locked = both_tns / nc["tns_total_ps"] * 100
            pct_fixable = (one_tns + free_tns) / nc["tns_total_ps"] * 100
            parts.append(f'<div class="insight">'
                         f'<b>{pct_locked:.0f}%</b> of per-edge setup TNS is cycle-locked '
                         f'(irreducible by skew). '
                         f'Only <b>{pct_fixable:.0f}%</b> is potentially fixable. '
                         f'This means useful skew can improve at most '
                         f'<b>{abs(one_tns + free_tns):.0f}ps</b> of per-edge TNS '
                         f'— the rest is determined by the netlist structure itself.</div>')

        # 2c. SCC summary table
        if nc["scc_table"]:
            parts.append('<h3>SCC Summary Table</h3>')
            parts.append('<table><tr><th>SCC#</th><th>Size</th><th>Edges</th>'
                         '<th>Violated</th><th>Setup TNS (ps)</th>'
                         '<th>Cycles</th><th>Tiers</th></tr>')
            for row in nc["scc_table"]:
                parts.append(f'<tr><td>{row["idx"]}</td><td>{row["size"]}</td>'
                             f'<td>{row["n_edges"]}</td><td>{row["n_violated"]}</td>'
                             f'<td class="neg">{row["tns_ps"]:+.0f}</td>'
                             f'<td>{row["n_cycles"]}</td><td>{row["tiers"]}</td></tr>')
            parts.append('</table>')

        # 2d. Top worst cycles detail (collapsible)
        if nc["cycles"]:
            n_show = min(5, len(nc["cycles"]))
            parts.append(f'<h3>Top {n_show} Worst Negative Cycles</h3>')
            parts.append(f'<details><summary>Click to expand</summary>')
            for ci, cycle in enumerate(nc["cycles"][:n_show]):
                total_w = sum(s for _, _, s, _ in cycle) * 1000
                total_h = sum(h for _, _, _, h in cycle) * 1000
                path_parts = []
                for fn, tn, s, h in cycle:
                    short = fn.replace("__bottom", "(B)").replace("__upper", "(U)")
                    path_parts.append(short)
                path_parts.append(path_parts[0])
                parts.append(f'<div style="background:#fff8f0;border:1px solid #e0c8a0;'
                             f'border-radius:6px;padding:10px;margin:8px 0;">')
                parts.append(f'<b>Cycle #{ci+1}</b> '
                             f'(length={len(cycle)}, '
                             f'weight=<span class="neg">{total_w:+.0f}ps</span>)')
                parts.append(f'<br><code style="font-size:11px;">'
                             f'{" &rarr; ".join(path_parts)}</code>')
                parts.append('<table style="margin-top:6px;font-size:12px;">'
                             '<tr><th>From</th><th>To</th>'
                             '<th>Setup (ps)</th><th>Hold (ps)</th></tr>')
                for fn, tn, s, h in cycle:
                    s_cls = "neg" if s < 0 else "pos"
                    h_cls = "neg" if h < 0 else "pos"
                    fn_short = fn.replace("__bottom", "(B)").replace("__upper", "(U)")
                    tn_short = tn.replace("__bottom", "(B)").replace("__upper", "(U)")
                    parts.append(f'<tr><td class="ff">{fn_short}</td>'
                                 f'<td class="ff">{tn_short}</td>'
                                 f'<td class="{s_cls}">{s*1000:+.1f}</td>'
                                 f'<td class="{h_cls}">{h*1000:+.1f}</td></tr>')
                parts.append(f'<tr style="font-weight:bold;border-top:2px solid #999;">'
                             f'<td colspan="2">CYCLE TOTAL</td>'
                             f'<td class="neg">{total_w:+.1f}</td>'
                             f'<td>{total_h:+.1f}</td></tr>')
                parts.append('</table></div>')
            parts.append('</details>')

        # 2e. Contention hotspots
        if nc["hotspots"]:
            n_hot = min(10, len(nc["hotspots"]))
            parts.append(f'<h3>Contention Hotspots (Top {n_hot})</h3>')
            parts.append('<p>FFs appearing in the most negative cycles — '
                         'high contention = hard to optimize with skew.</p>')
            parts.append('<table><tr><th>FF</th><th>Tier</th>'
                         '<th>#Cycles</th><th>Cycle TNS (ps)</th></tr>')
            for ff_name, n_cyc, cyc_tns in nc["hotspots"][:n_hot]:
                tier = ff_tier(ff_name)
                short = ff_name.replace("__bottom", "(B)").replace("__upper", "(U)")
                parts.append(f'<tr><td class="ff">{short}</td>'
                             f'<td>{tier}</td><td>{n_cyc}</td>'
                             f'<td class="neg">{cyc_tns:+.0f}</td></tr>')
            parts.append('</table>')

        # 2f. Cross-tier cycle count (inline in stats)
        if nc.get("cross_tier_cycles", 0) > 0:
            cross_ct = nc["cross_tier_cycles"]
            total_ct = cross_ct + nc.get("same_tier_cycles", 0)
            parts.append(f'<div class="insight">{cross_ct}/{total_ct} cycles include cross-tier edges.</div>')
    else:
        parts.append('<p style="color:#888;">No post-CTS timing graph available for '
                     'negative cycle analysis.</p>')
    parts.append('</div>')

    # ==== Section 4: LP Target vs Actual ====
    parts.append('<div class="section"><h2>4. LP Target vs Actual Clock Shift</h2>')

    # LP solver stats: show only status (optimal/infeasible), drop debug internals
    # Detect LP INFEASIBLE: objective is null or status string contains "infeasible"
    lp_infeasible = bool(lp_stats and (
        lp_stats.get("objective") is None or
        "infeasible" in str(lp_stats.get("status", "")).lower()
    ))
    if lp_stats:
        status_col = "#388e3c" if lp_stats.get("status") == "optimal" else "#d32f2f"
        parts.append(f'<p style="margin-bottom:8px">LP status: '
                     f'<b style="color:{status_col}">{lp_stats.get("status","—")}</b></p>')
    if not lp_infeasible:
        parts.append('<p>X: LP target a<sub>i</sub> (ps), Y: actual arrival shift from pre-CTS (ps). '
                     'Points on the y=x diagonal = LP perfectly delivered.</p>')
    if lp_corr:
        targets = [r["lp_target_ps"] for r in lp_corr]
        shifts  = [r["actual_shift_ps"] for r in lp_corr]
        gaps    = [r["gap_ps"] for r in lp_corr]
        r_val   = correlation(targets, shifts)
        delivered_5  = sum(1 for g in gaps if abs(g) < 5)
        n_zero    = sum(1 for t in targets if abs(t) < 0.5)
        n_nonzero = len(targets) - n_zero

        # Compute realization % (needed for both highlight box and bottom-line)
        nz_targets = [r["lp_target_ps"] for r in lp_corr if abs(r["lp_target_ps"]) > 0.5]
        nz_shifts  = [r["actual_shift_ps"] for r in lp_corr if abs(r["lp_target_ps"]) > 0.5]
        realization = 0.0
        avg_t = avg_s = 0.0
        if nz_targets:
            avg_t = sum(nz_targets) / len(nz_targets)
            avg_s = sum(nz_shifts) / len(nz_shifts)
            realization = (avg_s / avg_t * 100) if avg_t != 0 else 0.0

        # === Show INFEASIBLE warning OR normal KPI visualizations ===
        if lp_infeasible:
            # LP was infeasible: all targets = 0 → show warning, skip misleading KPI/scatter/table
            constraints = lp_stats.get("constraints", {}) if lp_stats else {}
            hard_pi = constraints.get("hard_pi_hold", 0)
            wns_mm  = constraints.get("wns_minimax", 0)
            hold_c  = constraints.get("hold", 0)
            total_c = constraints.get("total", 0)
            n_ff_lp = lp_stats.get("n_ff", 0) if lp_stats else 0
            parts.append(
                '<div style="background:#fff3f3;border:3px solid #d32f2f;border-radius:8px;'
                'padding:20px 24px;margin-bottom:16px">'
                '<div style="font-size:22px;font-weight:bold;color:#d32f2f;margin-bottom:10px">'
                '&#9888; LP INFEASIBLE &mdash; All targets = 0; CTS ran as balanced tree</div>'
                f'<p style="margin:4px 0">Total constraints: <b>{total_c}</b> '
                f'(n_ff={n_ff_lp}). Simultaneously infeasible groups:</p>'
                '<ul style="margin:4px 0 8px 20px">'
                f'<li><b>hard_pi_hold = {hard_pi}</b> '
                '&#8212; hard upper bound on a<sub>j</sub> for positive-slack PI&#8594;FF edges</li>'
                f'<li><b>wns_minimax = {wns_mm}</b> '
                '&#8212; W &#8805; v<sub>k</sub> for every setup path (&#947;<sub>wns</sub> active)</li>'
                f'<li><b>hold = {hold_c}</b> '
                '&#8212; FF&#8594;FF + PI&#8594;FF soft hold constraints</li>'
                '</ul>'
                '<p style="margin:4px 0">Root cause: propagated-clock slack already consumed '
                '~43 ps of baseline delay &rarr; remaining room too small '
                'for all constraint groups simultaneously.</p>'
                '<p style="margin:8px 0 0;color:#555">Fix options: '
                '<code>PRE_CTS_HARD_PI_HOLD=0</code> (remove hard bound) or '
                '<code>PRE_CTS_GAMMA_WNS=0</code> (disable WNS minimax) or '
                'revert to ideal-clock Phase 0 (V43-style).</p>'
                '</div>'
            )
        else:
            # ---- Headline KPIs: R and Realization prominently highlighted ----
            parts.append(
                '<div style="display:flex;gap:32px;align-items:center;'
                'background:#fff3f3;border:2px solid #d32f2f;border-radius:8px;'
                'padding:16px 24px;margin-bottom:16px">'
                f'<div style="text-align:center">'
                f'<div style="font-size:36px;font-weight:bold;color:#d32f2f">{r_val:.3f}</div>'
                f'<div style="font-size:13px;color:#555">Correlation R<br>(ideal = 1.0)</div>'
                f'</div>'
                f'<div style="font-size:28px;color:#999">|</div>'
                f'<div style="text-align:center">'
                f'<div style="font-size:36px;font-weight:bold;color:#d32f2f">{realization:.0f}%</div>'
                f'<div style="font-size:13px;color:#555">LP Realization<br>(avg achieved / avg target)</div>'
                f'</div>'
                f'<div style="font-size:28px;color:#999">|</div>'
                f'<div style="text-align:center">'
                f'<div style="font-size:36px;font-weight:bold;color:#d32f2f">'
                f'{delivered_5/max(len(gaps),1)*100:.0f}%</div>'
                f'<div style="font-size:13px;color:#555">Delivered &lt;5ps gap<br>({delivered_5}/{len(gaps)} FFs)</div>'
                f'</div>'
                f'</div>'
            )

            # ---- Scatter plot (y=x diagonal already enabled) ----
            pts = [(r["lp_target_ps"], r["actual_shift_ps"]) for r in lp_corr]
            def lp_color(x, y):
                gap = abs(x - y)
                if gap < 5:  return "#2ca02c"
                elif gap < 20: return "#ff7f0e"
                else: return "#d62728"
            parts.append(svg_scatter(pts, "LP Delivery: Target vs Actual",
                                     "LP Target (ps)", "Actual Shift (ps)",
                                     diag_line=True, color_func=lp_color))

            # ---- Achievement by Target Range: table + inline bar chart ----
            ranges = [(0, 5, "0~5"), (5, 15, "5~15"), (15, 30, "15~30"),
                      (30, 50, "30~50"), (50, 75, "50~75"), (75, 200, "75+")]
            parts.append('<h3>LP Achievement by Target Range</h3>')
            # Compute per-range stats first (needed for both table and bar chart)
            range_stats = []
            for lo, hi, lbl in ranges:
                grp = [r for r in lp_corr if lo <= r["lp_target_ps"] < hi]
                if not grp:
                    continue
                at  = sum(r["lp_target_ps"] for r in grp) / len(grp)
                as_ = sum(r["actual_shift_ps"] for r in grp) / len(grp)
                ag  = sum(abs(r["gap_ps"]) for r in grp) / len(grp)
                dlv = sum(1 for r in grp if abs(r["gap_ps"]) < 5) / len(grp) * 100
                range_stats.append((lbl, len(grp), at, as_, ag, dlv))

            # Table
            parts.append('<table><tr><th>Range (ps)</th><th>FFs</th><th>Avg Target</th>'
                         '<th>Avg Shift</th><th>Avg |Gap|</th><th>% Delivered (&lt;5ps)</th></tr>')
            for lbl, n, at, as_, ag, dlv in range_stats:
                dlv_cls = "pos" if dlv >= 50 else "neg" if dlv < 10 else ""
                parts.append(f'<tr><td>{lbl}</td><td>{n}</td><td>{at:.1f}</td>'
                             f'<td>{as_:.1f}</td><td>{ag:.1f}</td>'
                             f'<td class="{dlv_cls}">{dlv:.0f}%</td></tr>')
            parts.append('</table>')

            # Bar chart: delivery % drops as target increases
            if range_stats:
                bar_w, bar_h_max = 50, 120
                svg_w = 80 + len(range_stats) * (bar_w + 12)
                svg_h = bar_h_max + 60
                bars = ['<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;margin-top:8px" '
                        'xmlns="http://www.w3.org/2000/svg">'.format(w=svg_w, h=svg_h),
                        f'<rect width="{svg_w}" height="{svg_h}" fill="#fafafa" rx="4"/>',
                        f'<text x="{svg_w/2}" y="14" text-anchor="middle" '
                        f'font-size="12" font-weight="bold" fill="#333">'
                        f'Delivery Rate (%) by LP Target Range</text>']
                for idx, (lbl, n, at, as_, ag, dlv) in enumerate(range_stats):
                    bx = 60 + idx * (bar_w + 12)
                    bh = dlv / 100 * bar_h_max
                    by = 20 + bar_h_max - bh
                    col = "#2ca02c" if dlv >= 50 else "#ff7f0e" if dlv >= 10 else "#d62728"
                    bars.append(f'<rect x="{bx}" y="{by:.1f}" width="{bar_w}" height="{bh:.1f}" '
                                 f'fill="{col}" rx="3" opacity="0.85"/>')
                    bars.append(f'<text x="{bx + bar_w/2}" y="{by - 3:.1f}" '
                                 f'text-anchor="middle" font-size="11" font-weight="bold" fill="{col}">'
                                 f'{dlv:.0f}%</text>')
                    bars.append(f'<text x="{bx + bar_w/2}" y="{20 + bar_h_max + 14}" '
                                 f'text-anchor="middle" font-size="10" fill="#555">{lbl}</text>')
                    bars.append(f'<text x="{bx + bar_w/2}" y="{20 + bar_h_max + 26}" '
                                 f'text-anchor="middle" font-size="9" fill="#888">n={n}</text>')
                # Y-axis labels
                for pct in [0, 25, 50, 75, 100]:
                    gy = 20 + bar_h_max - pct / 100 * bar_h_max
                    bars.append(f'<line x1="55" y1="{gy:.1f}" x2="{svg_w - 5}" y2="{gy:.1f}" '
                                 f'stroke="#e0e0e0" stroke-width="1"/>')
                    bars.append(f'<text x="50" y="{gy + 4:.1f}" text-anchor="end" '
                                 f'font-size="9" fill="#888">{pct}%</text>')
                bars.append('</svg>')
                parts.append('\n'.join(bars))

            # One-line explanation (replaces long "Why 0% delivery" paragraph)
            parts.append('<div class="insight">'
                         'H-tree is branch-balanced by design → per-FF LP targets cannot be delivered; '
                         'SGCTS builds the tree topology directly from LP targets instead.'
                         '</div>')

            # ---- Skew Delivery Failures: Top 5 only ----
            failures = sorted([r for r in lp_corr if r["lp_target_ps"] >= 0.5],
                              key=lambda r: -abs(r["gap_ps"]))
            if failures:
                ff_slack_map = {c["ff_name"]: c for c in ff_comp} if ff_comp else {}
                parts.append('<h3>Skew Delivery Failures (Top 5)</h3>')
                parts.append('<p>FFs with largest gap between LP target and achieved shift.</p>')
                parts.append('<table><tr><th>#</th><th>FF</th><th>Tier</th>'
                             '<th>LP Target (ps)</th><th>Achieved (ps)</th><th>Gap (ps)</th>'
                             '<th>Setup Slack (ps)</th></tr>')
                for i, r in enumerate(failures[:5]):
                    gap = r["gap_ps"]
                    gap_cls = "neg" if gap > 5 else "pos" if gap < -5 else ""
                    setup_str = "N/A"
                    ff_c = ff_slack_map.get(r["ff_name"])
                    if ff_c and ff_c.get("test_setup_ns") is not None:
                        setup_str = f'{ff_c["test_setup_ns"]*1000:.0f}'
                    parts.append(f'<tr><td>{i+1}</td>'
                                 f'<td class="ff">{html_mod.escape(r["ff_name"])}</td>'
                                 f'<td>{r["tier"]}</td>'
                                 f'<td>{r["lp_target_ps"]:.1f}</td>'
                                 f'<td>{r["actual_shift_ps"]:.1f}</td>'
                                 f'<td class="{gap_cls}">{gap:+.1f}</td>'
                                 f'<td>{setup_str}</td></tr>')
                parts.append('</table>')
                if len(failures) > 5:
                    parts.append(f'<p style="color:#888;">{len(failures)} total FFs with target &gt; 0 '
                                 f'(showing worst 5; remaining rows show same pattern).</p>')

            # Section 3 closing insight
            if nz_targets:
                parts.append(f'<div class="insight">'
                             f'<b>Bottom line:</b> LP Realization is <b>{realization:.0f}%</b>. '
                             f'Even the {pct_fixable:.0f}% of TNS that is theoretically fixable by skew '
                             f'(from Section 3) is barely delivered by the CTS mechanism. '
                             f'Together: cycle-locked ({pct_locked:.0f}%) + delivery failure '
                             f'({100-realization:.0f}% of remainder) → almost no useful skew realized.'
                             f'</div>' if neg_cycle_data and neg_cycle_data["tns_total_ps"] != 0 else '')

    else:
        parts.append('<p style="color:#888;">No LP correlation data.</p>')
    parts.append('</div>')

    # ==== Section 5: Per-Edge Slack Change Distribution ====
    parts.append('<div class="section"><h2>5. Per-Edge Slack Change Distribution</h2>')
    parts.append(f'<p>Delta = {test_label} slack - {base_label} slack (positive = improvement)</p>')
    if edge_comp:
        d_setup = [c["delta_setup_ps"] for c in edge_comp if c["delta_setup_ps"] is not None]
        d_hold = [c["delta_hold_ps"] for c in edge_comp if c["delta_hold_ps"] is not None]
        parts.append('<div class="two-col">')
        parts.append('<div>')
        parts.append(svg_histogram(d_setup, "Setup Slack Change (ps)", "Delta Setup (ps)",
                                   color_neg="#d62728", color_pos="#2ca02c"))
        parts.append('</div><div>')
        parts.append(svg_histogram(d_hold, "Hold Slack Change (ps)", "Delta Hold (ps)",
                                   color_neg="#d62728", color_pos="#2ca02c"))
        parts.append('</div></div>')

        # Cross-tier vs same-tier edge breakdown
        same_edges = [c for c in edge_comp if edge_tier_type(c["from_ff"], c["to_ff"]) == "same-tier"]
        cross_edges = [c for c in edge_comp if edge_tier_type(c["from_ff"], c["to_ff"]) == "cross-tier"]
        if same_edges or cross_edges:
            parts.append('<h3>Cross-Tier vs Same-Tier Edge Analysis</h3>')
            parts.append('<table><tr><th>Category</th><th>Edges</th>'
                         '<th>Setup: imp / deg</th><th>Setup TNS Δ (ps)</th>'
                         '<th>Hold: imp / deg</th><th>Hold TNS Δ (ps)</th></tr>')
            for cat_label, cat_edges in [("Same-tier", same_edges), ("Cross-tier", cross_edges)]:
                if not cat_edges:
                    continue
                cs = [c["delta_setup_ps"] for c in cat_edges if c["delta_setup_ps"] is not None]
                ch = [c["delta_hold_ps"] for c in cat_edges if c["delta_hold_ps"] is not None]
                s_imp = sum(1 for d in cs if d > 0) if cs else 0
                s_deg = sum(1 for d in cs if d < 0) if cs else 0
                h_imp = sum(1 for d in ch if d > 0) if ch else 0
                h_deg = sum(1 for d in ch if d < 0) if ch else 0
                s_tns = sum(cs) if cs else 0
                h_tns = sum(ch) if ch else 0
                s_cls = "pos" if s_tns > 0 else "neg" if s_tns < 0 else ""
                h_cls = "pos" if h_tns > 0 else "neg" if h_tns < 0 else ""
                parts.append(f'<tr><td>{cat_label}</td><td>{len(cat_edges)}</td>'
                             f'<td><span class="pos">{s_imp}</span> / <span class="neg">{s_deg}</span></td>'
                             f'<td class="{s_cls}">{s_tns:+.0f}</td>'
                             f'<td><span class="pos">{h_imp}</span> / <span class="neg">{h_deg}</span></td>'
                             f'<td class="{h_cls}">{h_tns:+.0f}</td></tr>')
            parts.append('</table>')


    else:
        parts.append('<p style="color:#888;">No matched edges for comparison.</p>')
    parts.append('</div>')

    # ==== Section 6: Per-FF Slack Scatter ====
    parts.append('<div class="section"><h2>6. Per-FF Slack Scatter ({0} vs {1})</h2>'.format(base_label, test_label))
    if ff_comp:
        # Build LP target lookup for color coding
        lp_set = set()
        if lp_targets:
            lp_set = {name for name, t in lp_targets.items() if abs(t) > 0.0005}

        # Setup scatter with LP coloring
        setup_pts_lp = []  # (x, y, has_lp_target)
        hold_pts_lp = []
        for c in ff_comp:
            bs = c["base_setup_ns"]
            ts = c["test_setup_ns"]
            bh = c["base_hold_ns"]
            th = c["test_hold_ns"]
            has_lp = c["ff_name"] in lp_set
            if bs is not None and ts is not None:
                setup_pts_lp.append((bs * 1000, ts * 1000, has_lp))
            if bh is not None and th is not None:
                hold_pts_lp.append((bh * 1000, th * 1000, has_lp))

        setup_pts = [(x, y) for x, y, _ in setup_pts_lp]
        hold_pts = [(x, y) for x, y, _ in hold_pts_lp]

        # Color function: blue = LP target > 0, gray = LP target = 0
        setup_lp_map = {(x, y): has_lp for x, y, has_lp in setup_pts_lp}
        hold_lp_map = {(x, y): has_lp for x, y, has_lp in hold_pts_lp}

        def setup_color(x, y):
            return "#d62728" if setup_lp_map.get((x, y), False) else "#1976d2"
        def hold_color(x, y):
            return "#d62728" if hold_lp_map.get((x, y), False) else "#1976d2"

        parts.append('<p>Red = LP target &gt; 0 (skew requested). '
                     'Blue = LP target = 0. Points above diagonal = improved.</p>')
        parts.append('<div class="two-col"><div>')
        parts.append(svg_scatter(setup_pts, "Setup Slack per FF (ps)",
                                 f"{base_label} Setup (ps)", f"{test_label} Setup (ps)",
                                 diag_line=True, color_func=setup_color))
        parts.append('</div><div>')
        parts.append(svg_scatter(hold_pts, "Hold Slack per FF (ps)",
                                 f"{base_label} Hold (ps)", f"{test_label} Hold (ps)",
                                 diag_line=True, color_func=hold_color))
        parts.append('</div></div>')
    else:
        parts.append('<p style="color:#888;">No per-FF comparison data.</p>')
    parts.append('</div>')

    # ==== Section 7: PI→FF Hold Violation Detail ====
    parts.append('<div class="section"><h2>7. PI→FF Hold Violation Detail</h2>')
    parts.append('<p>LP adds clock latency to capture FF → PI→FF hold degrades. '
                 'Projected = pre_cts_hold - LP_target.</p>')
    if io_hold:
        io_results, pi_ff, ff_po = io_hold
        # IO summary stats (moved from old Section 1)
        n_pi_ff = len(pi_ff)
        n_projected_viol = sum(1 for r in io_results if r["projected_hold_ps"] is not None and r["projected_hold_ps"] < 0)
        proj_tns = sum(min(r["projected_hold_ps"], 0) for r in io_results if r["projected_hold_ps"] is not None)
        parts.append('<table class="summary">')
        parts.append(f'<tr><td>PI→FF edges</td><td>{n_pi_ff}</td></tr>')
        parts.append(f'<tr><td>Projected violations</td>'
                     f'<td class="neg">{n_projected_viol}/{n_pi_ff}</td></tr>')
        parts.append(f'<tr><td>Projected PI→FF hold TNS</td>'
                     f'<td class="neg">{proj_tns:.0f} ps</td></tr>')
        parts.append('</table>')
        # Detail table
        io_sorted = sorted(io_results, key=lambda r: r["projected_hold_ps"] or 0)
        parts.append('<details><summary>Click to expand (top 20 edges)</summary>')
        parts.append('<table><tr><th>#</th><th>Port</th><th>FF</th><th>Tier</th>'
                     '<th>Pre Hold (ps)</th><th>LP Target (ps)</th><th>Projected Hold (ps)</th>'
                     '<th>Actual Hold (ps)</th></tr>')
        for i, r in enumerate(io_sorted[:20]):
            proj = r["projected_hold_ps"]
            cls = "neg" if proj is not None and proj < 0 else ""
            actual_str = f'{r["actual_hold_ps"]:.1f}' if r["actual_hold_ps"] is not None else "N/A"
            actual_cls = "neg" if r["actual_hold_ps"] is not None and r["actual_hold_ps"] < 0 else ""
            pre_hold_str = f'{r["pre_cts_hold_ns"] * 1000:.1f}' if r["pre_cts_hold_ns"] is not None else "N/A"
            lp_str = f'{r["lp_target_ps"]:.1f}'
            proj_str = f'{proj:.1f}' if proj is not None else "N/A"
            parts.append(f'<tr><td>{i+1}</td>'
                         f'<td class="ff">{html_mod.escape(r["port"])}</td>'
                         f'<td class="ff">{html_mod.escape(r["ff_name"])}</td>'
                         f'<td>{r["tier"]}</td>'
                         f'<td>{pre_hold_str}</td>'
                         f'<td>{lp_str}</td>'
                         f'<td class="{cls}">{proj_str}</td>'
                         f'<td class="{actual_cls}">{actual_str}</td></tr>')
        parts.append('</table>')
        if len(io_sorted) > 20:
            parts.append(f'<p style="color:#888;">Showing 20 of {len(io_sorted)}.</p>')
        parts.append('</details>')
    else:
        parts.append('<p style="color:#888;">No IO edge data.</p>')
    parts.append('</div>')

    # ==== Section 8: Notable Changes (merged worst hold + best setup) ====
    parts.append('<div class="section"><h2>8. Notable Changes</h2>')
    if edge_comp:
        # 7a. Worst hold degradation
        sorted_hold_deg = sorted(edge_comp, key=lambda c: c["delta_hold_ps"] if c["delta_hold_ps"] is not None else 0)
        parts.append(f'<details><summary>Worst Hold Degradation (Top 10)</summary>')
        parts.append(f'<p>Edges with biggest hold degradation from {base_label} to {test_label}.</p>')
        parts.append('<table><tr><th>#</th><th>Launch FF</th><th>Capture FF</th>'
                     f'<th>{base_label} Hold (ns)</th><th>{test_label} Hold (ns)</th>'
                     '<th>Delta (ps)</th></tr>')
        for i, c in enumerate(sorted_hold_deg[:10]):
            dh = c["delta_hold_ps"]
            cls = "neg" if dh is not None and dh < -5 else "pos" if dh is not None and dh > 5 else ""
            parts.append(f'<tr><td>{i+1}</td>'
                         f'<td class="ff">{html_mod.escape(c["from_ff"])}</td>'
                         f'<td class="ff">{html_mod.escape(c["to_ff"])}</td>'
                         f'<td>{fmt(c["base_hold_ns"], ".4f")}</td>'
                         f'<td>{fmt(c["test_hold_ns"], ".4f")}</td>'
                         f'<td class="{cls}">{fmt(dh)}</td></tr>')
        parts.append('</table></details>')
        # Hold degradation insight
        if sorted_hold_deg:
            worst_ff = sorted_hold_deg[0].get("to_ff", "N/A")
            parts.append(f'<p class="insight"><b>Insight:</b> Worst hold regression on '
                         f'<code>{worst_ff.split("/")[-1]}</code></p>')

        # 7b. Best setup improvements
        sorted_setup_imp = sorted(edge_comp, key=lambda c: -(c["delta_setup_ps"] or 0))
        parts.append(f'<details><summary>Best Setup Improvements (Top 10)</summary>')
        parts.append('<table><tr><th>#</th><th>Launch FF</th><th>Capture FF</th>'
                     f'<th>{base_label} Setup (ns)</th><th>{test_label} Setup (ns)</th>'
                     '<th>Delta (ps)</th></tr>')
        for i, c in enumerate(sorted_setup_imp[:10]):
            ds = c["delta_setup_ps"]
            cls = "pos" if ds is not None and ds > 5 else ""
            parts.append(f'<tr><td>{i+1}</td>'
                         f'<td class="ff">{html_mod.escape(c["from_ff"])}</td>'
                         f'<td class="ff">{html_mod.escape(c["to_ff"])}</td>'
                         f'<td>{fmt(c["base_setup_ns"], ".4f")}</td>'
                         f'<td>{fmt(c["test_setup_ns"], ".4f")}</td>'
                         f'<td class="{cls}">{fmt(ds)}</td></tr>')
        parts.append('</table></details>')
        # Setup improvement insight
        if sorted_setup_imp:
            best_ff = sorted_setup_imp[0].get("to_ff", "N/A")
            parts.append(f'<p class="insight"><b>Insight:</b> Best setup improvement on '
                         f'<code>{best_ff.split("/")[-1]}</code></p>')
    parts.append('</div>')

    parts.append(HTML_FOOT)

    with open(html_path, "w") as f:
        f.write('\n'.join(parts))
    print(f"  HTML report: {html_path}")


# ============================================================================
# Section 7: Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="CTS Debug: Cross-Version Comparison Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery mode (recommended):
  python3 cts_debug_compare.py \\
    --base-dir <flow_dir> \\
    --test-dir <flow_dir>
    --platform asap7_3D --design aes --output-dir ./debug_out

  # Compare pre-CTS vs post-CTS within same version:
  python3 cts_debug_compare.py \\
    --base-graph results/.../pre_cts_ff_timing_graph.csv \\
    --test-graph results/.../post_cts_ff_timing_graph.csv \\
    --lp-targets results/.../pre_cts_skew_targets.csv \\
    --output-dir ./debug_out
""")
    # Auto-discovery args
    parser.add_argument("--base-dir", help="Flow directory for baseline (e.g., Pin3D)")
    parser.add_argument("--test-dir", help="Flow directory for test version (e.g., V42)")
    parser.add_argument("--platform", default="asap7_3D", help="Platform name")
    parser.add_argument("--design", default="aes", help="Design name")
    # Explicit file args (override auto-discovery)
    parser.add_argument("--base-graph", help="Base timing graph CSV (pre or post CTS)")
    parser.add_argument("--test-graph", help="Test timing graph CSV (post CTS)")
    parser.add_argument("--lp-targets", help="LP skew targets CSV")
    parser.add_argument("--io-edges", help="IO timing edges CSV")
    parser.add_argument("--test-debug-ff", help="Test version cts_debug_per_ff.csv")
    # Labels
    parser.add_argument("--base-label", help="Label for baseline version")
    parser.add_argument("--test-label", help="Label for test version")
    # Output
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: auto-named from version labels)")
    # Multi-design
    parser.add_argument("--all-designs", action="store_true",
                        help="Run for all known platform/design combinations")

    args = parser.parse_args()

    # Known platform/design combinations for --all-designs
    KNOWN_COMBOS = [
        ("asap7_3D", "aes"),
        ("asap7_3D", "ibex"),
        ("asap7_3D", "jpeg"),
        ("asap7_3D", "gcd"),
    ]

    # Resolve labels early so we can auto-name the output directory
    base_label = args.base_label or (
        os.path.basename(os.path.abspath(args.base_dir)).replace("flow-", "")
        if args.base_dir else "Base")
    test_label = args.test_label or (
        os.path.basename(os.path.abspath(args.test_dir)).replace("flow-", "")
        if args.test_dir else "Test")

    # Auto-name output directory from version labels if not explicitly given
    if args.output_dir:
        output_dir = args.output_dir
    else:
        safe_base = base_label.replace(" ", "_").replace("/", "-")
        safe_test = test_label.replace(" ", "_").replace("/", "-")
        output_dir = f"./debug_compare_{safe_base}_vs_{safe_test}"

    # Determine which platform/design combos to run
    if args.all_designs:
        combos = KNOWN_COMBOS
    else:
        combos = [(args.platform, args.design)]

    print("\n" + "=" * 60)
    print("CTS Debug: Cross-Version Comparison")
    print(f"  {base_label} vs {test_label}")
    if args.all_designs:
        print(f"  All designs: {combos}")
    print(f"  Output dir: {output_dir}")
    print("=" * 60)

    for platform, design in combos:
        _run_single_comparison(args, platform, design, output_dir,
                               base_label, test_label)


def _run_single_comparison(args, platform, design, output_dir, base_label, test_label):
    """Run comparison for a single platform/design combination."""
    print(f"\n--- {platform}/{design} ---")

    # Resolve files
    base_files = {}
    test_files = {}

    if args.base_dir:
        base_files = discover_files(args.base_dir, platform, design)
        print(f"  Base dir: {args.base_dir}")
        print(f"  Base files found: {list(base_files.keys())}")

    if args.test_dir:
        test_files = discover_files(args.test_dir, platform, design)
        print(f"  Test dir: {args.test_dir}")
        print(f"  Test files found: {list(test_files.keys())}")

    # Explicit files override auto-discovery (only for single-design mode)
    if args.base_graph:
        base_files["post_cts_graph"] = args.base_graph
    if args.test_graph:
        test_files["post_cts_graph"] = args.test_graph
    if args.lp_targets:
        test_files["lp_targets"] = args.lp_targets
    if args.io_edges:
        test_files["io_edges"] = args.io_edges
    if args.test_debug_ff:
        test_files["debug_per_ff"] = args.test_debug_ff

    # ---- Load data ----
    print("\nLoading data...")

    # Base edges: prefer post_cts_graph, fall back to pre_cts_graph
    base_edges = []
    if "post_cts_graph" in base_files:
        base_edges = parse_timing_graph(base_files["post_cts_graph"])
        print(f"  Base edges (post_cts): {len(base_edges)}")
    elif "pre_cts_graph" in test_files:
        # If base has no post-CTS data, use test's pre-CTS as baseline
        # (same starting point for both Pin3D and 3DCTS)
        base_edges = parse_timing_graph(test_files["pre_cts_graph"])
        print(f"  Base edges (pre_cts from test, ideal clock baseline): {len(base_edges)}")
    elif "pre_cts_graph" in base_files:
        base_edges = parse_timing_graph(base_files["pre_cts_graph"])
        print(f"  Base edges (pre_cts): {len(base_edges)}")

    test_edges = []
    if "post_cts_graph" in test_files:
        test_edges = parse_timing_graph(test_files["post_cts_graph"])
        print(f"  Test edges (post_cts): {len(test_edges)}")

    pre_cts_edges = []
    if "pre_cts_graph" in test_files:
        pre_cts_edges = parse_timing_graph(test_files["pre_cts_graph"])
        print(f"  Pre-CTS edges: {len(pre_cts_edges)}")

    lp_targets = {}
    if "lp_targets" in test_files:
        lp_targets = parse_lp_targets(test_files["lp_targets"])
        print(f"  LP targets: {len(lp_targets)} FFs")

    io_edges_data = []
    if "io_edges" in test_files:
        io_edges_data = parse_io_edges(test_files["io_edges"])
        print(f"  IO edges: {len(io_edges_data)}")

    test_debug_ff = {}
    if "debug_per_ff" in test_files:
        rows = parse_debug_per_ff(test_files["debug_per_ff"])
        test_debug_ff = {r["ff_name"]: r for r in rows}
        print(f"  Test debug per-FF: {len(test_debug_ff)} FFs")

    # Base and test debug data for cross-tier comparison
    base_debug_ff = []
    if "debug_per_ff" in base_files:
        base_debug_ff = parse_debug_per_ff(base_files["debug_per_ff"])
        print(f"  Base debug per-FF: {len(base_debug_ff)} FFs")

    base_debug_buffers = []
    if "debug_buffers" in base_files:
        base_debug_buffers = parse_debug_buffers(base_files["debug_buffers"])
        print(f"  Base debug buffers: {len(base_debug_buffers)}")
    test_debug_buffers = []
    if "debug_buffers" in test_files:
        test_debug_buffers = parse_debug_buffers(test_files["debug_buffers"])
        print(f"  Test debug buffers: {len(test_debug_buffers)}")

    base_clock_paths = []
    if "debug_clock_paths" in base_files:
        base_clock_paths = parse_debug_clock_paths(base_files["debug_clock_paths"])
        print(f"  Base clock paths: {len(base_clock_paths)} hops")
    test_clock_paths = []
    if "debug_clock_paths" in test_files:
        test_clock_paths = parse_debug_clock_paths(test_files["debug_clock_paths"])
        print(f"  Test clock paths: {len(test_clock_paths)} hops")

    # V43: Load LP solver stats JSON (auto-discovered or from --test-dir)
    lp_stats = None
    if "lp_stats" in test_files:
        try:
            with open(test_files["lp_stats"]) as f:
                lp_stats = json.load(f)
            print(f"  LP stats: status={lp_stats.get('status')}, "
                  f"obj={lp_stats.get('objective')}, "
                  f"time={lp_stats.get('solve_time_s')}s")
        except Exception as e:
            print(f"  LP stats load failed: {e}")

    # Timing reports: prefer cts_debug_summary.csv (full STA TNS/WNS),
    # fall back to parse_timing_report (4_cts_timing.rpt, top-20 paths only)
    base_rpt = {}
    if "debug_summary" in base_files:
        base_rpt = parse_debug_summary(base_files["debug_summary"])
        print(f"  Base debug summary: setup_tns={base_rpt.get('setup_tns'):.1f}ps  hold_tns={base_rpt.get('hold_tns'):.1f}ps")
    if not base_rpt and "timing_rpt" in base_files:
        base_rpt = parse_timing_report(base_files["timing_rpt"])
        print(f"  Base timing report (fallback): {base_rpt}")
    test_rpt = {}
    if "debug_summary" in test_files:
        test_rpt = parse_debug_summary(test_files["debug_summary"])
        print(f"  Test debug summary: setup_tns={test_rpt.get('setup_tns'):.1f}ps  hold_tns={test_rpt.get('hold_tns'):.1f}ps")
    if not test_rpt and "timing_rpt" in test_files:
        test_rpt = parse_timing_report(test_files["timing_rpt"])
        print(f"  Test timing report (fallback): {test_rpt}")

    # ---- Analysis ----
    print("\nRunning analysis...")

    # Per-edge comparison
    edge_comp = []
    if base_edges and test_edges:
        edge_comp = compare_edges(base_edges, test_edges)
        print(f"  Edge comparisons: {len(edge_comp)} matched")

    # Per-FF comparison
    # Supplement FF→FF edge graph with cts_debug_per_ff.csv rows so that
    # FFs only reachable via PI→FF (no FF→FF capture path) are not N/A.
    ff_comp = []
    if base_edges and test_edges:
        base_ff = compute_per_ff_from_edges(base_edges, debug_per_ff_rows=base_debug_ff)
        test_ff = compute_per_ff_from_edges(test_edges,
                                            debug_per_ff_rows=list(test_debug_ff.values())
                                            if isinstance(test_debug_ff, dict) else test_debug_ff)
        ff_comp = compare_per_ff(base_ff, test_ff)
        print(f"  Per-FF comparisons: {len(ff_comp)}")

    # LP correlation
    lp_corr = []
    if pre_cts_edges and test_edges and lp_targets:
        lp_corr = analyze_lp_correlation(test_edges, pre_cts_edges, lp_targets)
        print(f"  LP correlation: {len(lp_corr)} FFs analyzed")

    # IO hold analysis
    io_hold = None
    if io_edges_data and lp_targets:
        io_hold = analyze_io_hold(io_edges_data, lp_targets, test_debug_ff)
        io_results, pi_ff, ff_po = io_hold
        print(f"  IO hold analysis: {len(pi_ff)} PI→FF, {len(ff_po)} FF→PO")

    # Negative cycle analysis
    neg_cycle_data = None
    if test_edges:
        neg_cycle_data = analyze_negative_cycles(test_edges)
        if neg_cycle_data:
            print(f"  Negative cycles: {neg_cycle_data['n_cycles']} cycles, "
                  f"{neg_cycle_data['n_scc_ffs']}/{neg_cycle_data['n_ffs']} FFs in SCCs")

    # Cross-tier clock tree structure comparison
    cross_tier_data = None
    if base_debug_buffers or test_debug_buffers:
        cross_tier_data = analyze_cross_tier_comparison(
            base_debug_buffers, test_debug_buffers,
            base_clock_paths, test_clock_paths,
            base_debug_ff, test_debug_ff)
        if cross_tier_data:
            bh = cross_tier_data.get("base_hbt_cross") or 0
            th = cross_tier_data.get("test_hbt_cross") or 0
            print(f"  Cross-tier: base HBT={bh}, test HBT={th}")

    # ---- Generate matplotlib PNGs ----
    print("\nGenerating matplotlib visualizations...")
    matplotlib_pngs = generate_matplotlib_pngs(
        test_flow_dir=args.test_dir, platform=platform, design=design,
        base_flow_dir=args.base_dir, base_label=base_label, test_label=test_label)

    # ---- Generate report ----
    print("\nGenerating report...")
    generate_report(base_label, test_label, base_rpt, test_rpt,
                    edge_comp, ff_comp, lp_corr, io_hold,
                    test_debug_ff, lp_targets,
                    output_dir, test_edges=test_edges,
                    neg_cycle_data=neg_cycle_data,
                    cross_tier_data=cross_tier_data,
                    matplotlib_pngs=matplotlib_pngs,
                    lp_stats=lp_stats,
                    platform=platform, design=design,
                    # Section 2b: interactive clock tree schematic (D3.js)
                    base_clock_paths=base_clock_paths,
                    test_clock_paths=test_clock_paths,
                    base_debug_buffers=base_debug_buffers,
                    test_debug_buffers=test_debug_buffers,
                    # Section 2c: per-FF spatial improvement map
                    base_debug_ff=base_debug_ff,
                    test_debug_ff_list=list(test_debug_ff.values()) if test_debug_ff else [])

    # ---- Console summary ----
    print("\n" + "=" * 60)
    print(f"SUMMARY: {base_label} vs {test_label}")
    print("=" * 60)
    if base_rpt and test_rpt:
        for key, label in [("setup_tns", "Setup TNS"), ("hold_tns", "Hold TNS"),
                           ("setup_wns", "Setup WNS"), ("hold_wns", "Hold WNS")]:
            bv = base_rpt.get(key, 0)
            tv = test_rpt.get(key, 0)
            delta = tv - bv
            pct = (delta / abs(bv) * 100) if bv != 0 else 0
            print(f"  {label:12s}: {bv:10.1f} -> {tv:10.1f}  ({delta:+.1f}ps, {pct:+.1f}%)")

    if edge_comp:
        d_setup = [c["delta_setup_ps"] for c in edge_comp if c["delta_setup_ps"] is not None]
        d_hold = [c["delta_hold_ps"] for c in edge_comp if c["delta_hold_ps"] is not None]
        if d_setup:
            imp = sum(1 for d in d_setup if d > 0)
            print(f"  FF→FF Setup: {imp}/{len(d_setup)} edges improved ({imp/len(d_setup)*100:.0f}%)")
        if d_hold:
            imp = sum(1 for d in d_hold if d > 0)
            print(f"  FF→FF Hold:  {imp}/{len(d_hold)} edges improved ({imp/len(d_hold)*100:.0f}%)")

    if lp_corr:
        targets = [r["lp_target_ps"] for r in lp_corr]
        shifts = [r["actual_shift_ps"] for r in lp_corr]
        r_val = correlation(targets, shifts)
        delivered = sum(1 for r in lp_corr if abs(r["gap_ps"]) < 5)
        print(f"  LP Corr R={r_val:.3f}, Delivered(<5ps): {delivered}/{len(lp_corr)} ({delivered/len(lp_corr)*100:.0f}%)")

    if io_hold:
        io_results, _, _ = io_hold
        n_viol = sum(1 for r in io_results if r["projected_hold_ps"] is not None and r["projected_hold_ps"] < 0)
        proj_tns = sum(min(r["projected_hold_ps"], 0) for r in io_results if r["projected_hold_ps"] is not None)
        print(f"  PI→FF Hold: {n_viol} projected violations, TNS={proj_tns:.0f}ps")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
