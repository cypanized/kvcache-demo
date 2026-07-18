#!/usr/bin/env python3
"""Summarize benchmark results across configs into a comparison table.

Usage: python3 analyze.py results/results_baseline.json results/results_cpu.json ...
"""
import json
import sys


def stats(xs):
    xs = sorted(xs)
    n = len(xs)
    return {
        "n": n,
        "mean": sum(xs) / n,
        "p50": xs[n // 2],
        "p95": xs[min(n - 1, int(n * 0.95))],
    }


rows = []
for path in sys.argv[1:]:
    with open(path) as f:
        data = json.load(f)
    res = data["results"]
    cold = stats([r["ttft"] for r in res if r["pass"] == 0])
    warm = stats([r["ttft"] for r in res if r["pass"] > 0])
    rows.append((data["label"], cold, warm))

base_warm = rows[0][2]["mean"] if rows else 1.0
print(f"{'config':<12} {'cold TTFT':>10} {'warm TTFT':>10} {'warm p95':>10} {'vs baseline':>12}")
for label, cold, warm in rows:
    speedup = base_warm / warm["mean"]
    print(f"{label:<12} {cold['mean']:>9.3f}s {warm['mean']:>9.3f}s {warm['p95']:>9.3f}s {speedup:>11.2f}x")
