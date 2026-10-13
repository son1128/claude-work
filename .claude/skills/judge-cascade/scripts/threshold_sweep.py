#!/usr/bin/env python3
"""Calibration report and escalation-threshold sweep for a judge cascade.

Input: a CSV with one row per labeled judgment and columns
    confidence        cheap judge's confidence, 0..1
    correct           1 if the cheap verdict matched ground truth, else 0
    frontier_correct  (optional) 1 if the frontier model was correct on this item

Standard library only. See ../references/cost-and-calibration.md for the math.
"""
import argparse
import csv
import random
import sys


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            fc = r.get("frontier_correct")
            rows.append((
                float(r["confidence"]),
                int(float(r["correct"])),
                None if fc in (None, "") else int(float(fc)),
            ))
    if not rows:
        sys.exit("no rows in " + path)
    return rows


def demo(n, seed, overconfident):
    """Synthetic judgments: confidence skewed high; correctness drawn from it."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        c = 0.5 + 0.5 * (rng.random() ** 0.2)
        p = 0.5 + (c - 0.5) * (0.72 if overconfident else 1.0)
        hard = 1.0 - c  # low-confidence items are harder for the frontier too
        rows.append((c, int(rng.random() < p), int(rng.random() < 0.975 - 0.12 * hard)))
    return rows


def reliability(rows, bins):
    out, n, ece = [], len(rows), 0.0
    lo_edge = 0.5 if min(r[0] for r in rows) >= 0.5 else 0.0
    width = (1.0 - lo_edge) / bins
    for b in range(bins):
        lo, hi = lo_edge + b * width, lo_edge + (b + 1) * width
        sel = [r for r in rows if lo <= r[0] < hi or (b == bins - 1 and r[0] == 1.0)]
        if not sel:
            continue
        mc = sum(r[0] for r in sel) / len(sel)
        acc = sum(r[1] for r in sel) / len(sel)
        ece += len(sel) / n * abs(mc - acc)
        out.append((lo, hi, len(sel), mc, acc))
    return out, ece


def sweep(rows, taus, frontier_acc, p_cheap, p_frontier):
    n = len(rows)
    out = []
    for t in taus:
        kept = [r for r in rows if r[0] >= t]
        esc = [r for r in rows if r[0] < t]
        if esc and all(r[2] is not None for r in esc):
            esc_correct = sum(r[2] for r in esc)
        else:
            esc_correct = frontier_acc * len(esc)
        acc = (sum(r[1] for r in kept) + esc_correct) / n
        e = len(esc) / n
        cost = p_cheap + e * p_frontier  # per 1k judgments, same unit as inputs
        out.append((t, e, acc, cost, cost / p_frontier))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="labeled judgments CSV")
    ap.add_argument("--demo", action="store_true", help="use synthetic data instead of a CSV")
    ap.add_argument("--overconfident", action="store_true", help="(demo) make the synthetic judge overconfident")
    ap.add_argument("--n", type=int, default=2000, help="(demo) number of judgments")
    ap.add_argument("--seed", type=int, default=7, help="(demo) random seed")
    ap.add_argument("--frontier-acc", type=float, default=0.96, help="frontier accuracy on escalated items when no frontier_correct column")
    ap.add_argument("--cheap-cost", type=float, default=0.05, help="cheap judge price per 1k judgments")
    ap.add_argument("--frontier-cost", type=float, default=12.0, help="frontier price per 1k judgments")
    ap.add_argument("--target-acc", type=float, default=0.95, help="accuracy the cascade must reach")
    ap.add_argument("--bins", type=int, default=5)
    ap.add_argument("--step", type=float, default=0.05, help="threshold sweep step")
    a = ap.parse_args()

    if a.demo:
        rows = demo(a.n, a.seed, a.overconfident)
        print("demo data: %d synthetic judgments (%s judge)\n" % (len(rows), "overconfident" if a.overconfident else "calibrated"))
    elif a.csv:
        rows = load(a.csv)
    else:
        ap.error("give a CSV path or --demo")

    rel, ece = reliability(rows, a.bins)
    print("Reliability (is confidence = accuracy?)")
    print("  %-11s %6s %9s %9s %7s" % ("bin", "n", "mean conf", "accuracy", "gap"))
    for lo, hi, n, mc, acc in rel:
        flag = "  <- overconfident" if mc - acc > 0.05 else ""
        print("  %.2f-%.2f  %6d %9.3f %9.3f %+7.3f%s" % (lo, hi, n, mc, acc, acc - mc, flag))
    print("  ECE = %.3f\n" % ece)

    steps = int(round(0.5 / a.step))
    taus = [round(0.5 + i * a.step, 4) for i in range(steps + 1)]
    res = sweep(rows, taus, a.frontier_acc, a.cheap_cost, a.frontier_cost)
    print("Threshold sweep (escalate when confidence < tau)")
    print("  %5s %9s %9s %12s %9s" % ("tau", "escalated", "accuracy", "cost/1k", "fee ratio"))
    best = None
    for t, e, acc, cost, ratio in res:
        ok = acc >= a.target_acc
        if ok and (best is None or cost < best[3]):
            best = (t, e, acc, cost, ratio)
        print("  %5.2f %8.1f%% %8.2f%% %12.4f %8.1f%%%s" % (t, e * 100, acc * 100, cost, ratio * 100, "" if ok else "  (below target)"))

    cheap_only = sum(r[1] for r in rows) / len(rows)
    print("\nReference: cheap-only accuracy %.2f%%, frontier-only cost %.4f/1k" % (cheap_only * 100, a.frontier_cost))
    if best:
        print("Pick: tau=%.2f -> %.1f%% escalated, %.2f%% accuracy, %.1f%% of frontier-only judgment cost"
              % (best[0], best[1] * 100, best[2] * 100, best[4] * 100))
        print("Note: this is judgment cost only. Reads and writes by the LLM are not reduced by the cascade.")
    else:
        print("No threshold reaches %.2f%%. Escalate this fork always, or improve the cheap judge." % (a.target_acc * 100))


if __name__ == "__main__":
    main()
