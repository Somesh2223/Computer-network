"""Experiments: sweeps, figures and CSVs. Five seeds per point, 95 % confidence intervals.

  python experiments.py

Writes everything to results/. Numbers are findings, not targets: nothing here
is tuned to reproduce a figure written down earlier.

  1. crossover validation  - does repair stop helping below T* = tau + H + R?
  2. policy comparison     - ours vs B1 / B3 / B4: recovery bytes and freshness
  3. per-field table       - Age of Information of each field under each policy
  4. workload sensitivity  - how the saving depends on the byte-weight of fast fields
  5. invariants            - the hard checks of BUILD_PROMPT §11, over all runs
"""

import csv
import math
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import config
from src import schema
from src.crossover import crossover
from src.metrics import verify_no_false_suppression
from src.schema import DEFAULT_FIELDS, Field
from src.sim import Simulation
from src.supersession import Outcome

OUT = "results"
T_STAR = crossover(config.TICK_S, config.HOLD_DOWN_S, config.RTT_S)
POLICIES = ("ours", "arq", "udp", "snapshot")
LABEL = {"ours": "Ours", "arq": "B1 full ARQ", "udp": "B4 plain UDP", "snapshot": "B3 snapshot"}

# Student's t, two-sided 95 %, by degrees of freedom (n - 1).
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}

# Chart colours: one accent for our system, quiet greys for everything else.
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"
ACCENT, SECOND, GREY = "#2a78d6", "#eb6834", "#a3a29c"


def mean_ci(values):
    """Mean and the half-width of its 95 % confidence interval."""
    m = statistics.mean(values)
    if len(values) < 2:
        return m, 0.0
    return m, T95[len(values) - 1] * statistics.stdev(values) / math.sqrt(len(values))


def recovery_bytes(sim):
    """Bytes spent answering losses: repairs plus SKIP notices (heartbeat counted apart)."""
    return sim.stats["repair_bytes"] + sim.stats["skip_bytes"]


def write_csv(name, rows):
    with open(os.path.join(OUT, name), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def style(ax, title, subtitle=""):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold", pad=18 if subtitle else 8)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color=INK_2)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK_2)
    ax.tick_params(colors=INK_2, labelsize=9)


# --- 1. crossover validation ---------------------------------------------------

def crossover_sweep(duration_s=300.0):
    """Two-field probe schema: a filler at tick rate exposes losses, and a probe
    whose period sweeps T_f / R. Benefit of repairing = (AoI_udp - AoI_arq) / period.
    i.i.d. loss, because that is what the model assumes."""
    ratios = (0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 2, 3, 5, 8, 12, 20)
    rows = []
    for loss in (0.05, 0.20):
        for ratio in ratios:
            period = ratio * config.RTT_S
            effective = max(period, config.TICK_S)    # nothing can update faster than a tick
            schema.use([Field(0, "filler", 4, config.TICK_S, 0.020),
                        Field(1, "probe", 8, period, period)])
            benefit = []
            for seed in config.SEEDS:
                udp = Simulation(seed, "udp", loss_rate=loss, burst=False, duration_s=duration_s).run()
                arq = Simulation(seed, "arq", loss_rate=loss, burst=False, duration_s=duration_s).run()
                benefit.append((udp.metrics.avg_aoi(1) - arq.metrics.avg_aoi(1)) / effective)
            m, h = mean_ci(benefit)
            rows.append(dict(loss=loss, ratio=ratio, period_ms=round(period * 1000, 1),
                             benefit_pct=round(100 * m, 3), ci95_pct=round(100 * h, 3)))
        print(f"  crossover sweep at {loss:.0%} loss done")
    schema.use(DEFAULT_FIELDS)
    write_csv("crossover_sweep.csv", rows)

    fig, ax = plt.subplots(figsize=(8, 4.6), facecolor=SURFACE)
    for loss, colour in ((0.05, ACCENT), (0.20, SECOND)):
        pts = [r for r in rows if r["loss"] == loss]
        ax.errorbar([r["ratio"] for r in pts], [r["benefit_pct"] for r in pts],
                    yerr=[r["ci95_pct"] for r in pts], color=colour, marker="o", markersize=5,
                    linewidth=2, capsize=3, label=f"{loss:.0%} i.i.d. loss")
    ax.axvline(T_STAR / config.RTT_S, color=INK_2, linestyle="--", linewidth=1)
    ax.text(T_STAR / config.RTT_S * 1.06, ax.get_ylim()[1] * 0.92,
            f"predicted T* = {T_STAR*1000:.0f} ms", fontsize=9, color=INK_2)
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ax.set_xscale("log")
    ax.set_xlabel("probe update period / RTT  (T_f / R)", color=INK_2)
    ax.set_ylabel("benefit of repairing (% of one period)", color=INK_2)
    below = [r["benefit_pct"] for r in rows if r["loss"] == 0.05 and r["ratio"] * config.RTT_S < T_STAR]
    style(ax, "Benefit of repairing a field, against the predicted crossover",
          f"5 seeds per point, 95% CI. At 5% loss, largest mean benefit below T*: {max(below):.2f}% of a period")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_crossover.png"), dpi=160)
    plt.close(fig)
    return rows


# --- 2. policy comparison ----------------------------------------------------------

def policy_comparison():
    runs = {p: [Simulation(seed, p).run() for seed in config.SEEDS] for p in POLICIES}
    per_run, summary = [], []
    for p in POLICIES:
        for seed, sim in zip(config.SEEDS, runs[p]):
            m = sim.metrics
            per_run.append(dict(policy=p, seed=seed, data_lost=sim.stats["data_lost"],
                                wire_bytes=m.wire_bytes, recovery_bytes=recovery_bytes(sim),
                                heartbeat_bytes=sim.stats["heartbeat_bytes"],
                                efficiency=round(m.useful_delivery_efficiency(), 4),
                                overhead_ratio=round(m.total_overhead_ratio(), 4),
                                mean_aoi_s=round(m.mean_aoi_all(), 4),
                                latency_p95_ms=round(m.latency_percentile(95) * 1000, 2),
                                latency_p99_ms=round(m.latency_percentile(99) * 1000, 2),
                                decision_mean_us=round(m.mean_decision_us(), 2),
                                decision_p99_us=round(m.p99_decision_us(), 2),
                                false_suppressions=verify_no_false_suppression(
                                    sim.suppression_events, sim.transmissions)))
        rows = [r for r in per_run if r["policy"] == p]
        row = dict(policy=p)
        for key in ("wire_bytes", "recovery_bytes", "efficiency", "mean_aoi_s", "latency_p95_ms",
                    "decision_mean_us", "decision_p99_us"):
            m, h = mean_ci([r[key] for r in rows])
            row[key], row[key + "_ci95"] = round(m, 4), round(h, 4)
        row["false_suppressions"] = sum(r["false_suppressions"] for r in rows)
        summary.append(row)
    write_csv("policy_runs.csv", per_run)
    write_csv("policy_summary.csv", summary)

    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.4), facecolor=SURFACE)
    names = [LABEL[r["policy"]] for r in summary]
    colours = [ACCENT if r["policy"] == "ours" else GREY for r in summary]
    for ax, key, title, unit in ((left, "recovery_bytes", "Recovery traffic", "bytes per 30 s run"),
                                 (right, "mean_aoi_s", "Mean Age of Information", "seconds")):
        vals = [r[key] for r in summary]
        ax.bar(names, vals, yerr=[r[key + "_ci95"] for r in summary], color=colours,
               capsize=4, width=0.6, error_kw=dict(ecolor=INK_2, linewidth=1))
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:,.0f}" if key == "recovery_bytes" else f"{v:.3f}",
                    ha="center", va="bottom", fontsize=9, color=INK)
        ax.set_ylabel(unit, color=INK_2)
        style(ax, title, "10% burst loss, 5 seeds, 95% CI")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_policy.png"), dpi=160)
    plt.close(fig)
    return runs, summary


# --- 3. per-field table --------------------------------------------------------

def per_field_table(duration_s=180.0):
    runs = {p: [Simulation(seed, p, duration_s=duration_s).run() for seed in config.SEEDS]
            for p in ("ours", "arq", "udp")}
    rows = []
    for f in schema.active():
        row = dict(field=f.name, period_ms=round(f.period * 1000) if f.period else "event",
                   vs_t_star="event" if f.period is None else ("below" if f.period < T_STAR else "above"))
        for p in ("ours", "arq", "udp"):
            row[f"aoi_{p}_s"] = round(statistics.mean(s.metrics.avg_aoi(f.id) for s in runs[p]), 4)
        for outcome in Outcome:
            row[outcome.value.lower()] = sum(1 for s in runs["ours"] for _, _, fid, o in s.decisions
                                             if fid == f.id and o is outcome)
        rows.append(row)
    write_csv("per_field.csv", rows)
    return runs, rows


# --- 4. workload sensitivity ---------------------------------------------------

def workload_sweep():
    """Vary the width of the big fast field (lidar_scan) and measure the saving."""
    widths = (4, 50, 100, 200, 400, 700, 900, 1100, 1400)
    rows = []
    for width in widths:
        schema.use([Field(f.id, f.name, width, f.period, f.deadline, f.mean_gap)
                    if f.name == "lidar_scan" else f for f in DEFAULT_FIELDS])
        saving = []
        for seed in config.SEEDS:
            ours, arq = Simulation(seed, "ours").run(), Simulation(seed, "arq").run()
            saving.append(100 * (1 - recovery_bytes(ours) / arq.stats["repair_bytes"]))
        m, h = mean_ci(saving)
        rows.append(dict(lidar_width_b=width, saving_pct=round(m, 2), ci95_pct=round(h, 2)))
    schema.use(DEFAULT_FIELDS)
    write_csv("workload_sweep.csv", rows)

    fig, ax = plt.subplots(figsize=(8, 4.4), facecolor=SURFACE)
    xs, ys, hs = [r["lidar_width_b"] for r in rows], [r["saving_pct"] for r in rows], [r["ci95_pct"] for r in rows]
    ax.fill_between(xs, [y - h for y, h in zip(ys, hs)], [y + h for y, h in zip(ys, hs)],
                    color=ACCENT, alpha=0.15, linewidth=0)
    ax.plot(xs, ys, color=ACCENT, marker="o", markersize=5, linewidth=2)
    ax.axvline(900, color=INK_2, linestyle="--", linewidth=1)
    ax.text(915, min(ys), "evaluated schema (900 B)", fontsize=9, color=INK_2)
    ax.set_xlabel("width of the fast field lidar_scan (bytes)", color=INK_2)
    ax.set_ylabel("recovery bytes saved vs B1 (%)", color=INK_2)
    style(ax, "How much of the saving comes from the workload",
          f"Saving ranges from {min(ys):.1f}% (4 B) to {max(ys):.1f}%; 10% burst loss, 5 seeds, 95% CI band")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_workload.png"), dpi=160)
    plt.close(fig)
    return rows


# --- 5. invariants -------------------------------------------------------------

def check_invariants(policy_runs, field_runs):
    checks = []
    ours_all = policy_runs["ours"] + field_runs["ours"]
    checks.append(("False suppressions = 0",
                   all(verify_no_false_suppression(s.suppression_events, s.transmissions) == 0
                       for s in ours_all)))
    repaired_below, critical_superseded = 0, 0
    for s in ours_all:
        for _, _, fid, outcome in s.decisions:
            f = schema.by_id(fid)
            if outcome is Outcome.REPAIR and f.period is not None and f.period < T_STAR:
                repaired_below += 1
            if outcome is Outcome.SUPERSEDED and (f.period is None or f.deadline < f.period):
                critical_superseded += 1
    checks.append(("No field below T* is ever repaired", repaired_below == 0))
    checks.append(("No critical field is ever suppressed as superseded", critical_superseded == 0))
    for group in (policy_runs, field_runs):
        for i in range(len(config.SEEDS)):
            o, a, u = group["ours"][i], group["arq"][i], group["udp"][i]
            checks.append((f"Ours repair bytes <= B1 (seed {config.SEEDS[i]})",
                           o.stats["repair_bytes"] <= a.stats["repair_bytes"]))
            checks.append((f"B4 sends no repair bytes (seed {config.SEEDS[i]})",
                           u.stats["repair_bytes"] == 0))
            checks.append((f"Identical data losses ours/B1/B4 (seed {config.SEEDS[i]})",
                           o.stats["data_lost"] == a.stats["data_lost"] == u.stats["data_lost"]))
    lines = [f"{'PASS' if ok else 'FAIL'}  {name}" for name, ok in checks]
    lines.append("(Receiver state never regresses: asserted by tests 8-10 in tests/test_protocol.py)")
    with open(os.path.join(OUT, "invariants.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return all(ok for _, ok in checks), lines


def main():
    os.makedirs(OUT, exist_ok=True)
    print("1/4 crossover sweep")
    cross = crossover_sweep()
    print("2/4 policy comparison")
    policy_runs, summary = policy_comparison()
    print("3/4 per-field table")
    field_runs, fields = per_field_table()
    print("4/4 workload sweep")
    workload = workload_sweep()
    ok, lines = check_invariants(policy_runs, field_runs)

    print("\nPolicy comparison (10% burst loss, 30 s, 5 seeds, mean ± 95% CI)")
    for r in summary:
        print(f"  {LABEL[r['policy']]:<14} recovery {r['recovery_bytes']:>9,.0f} ± {r['recovery_bytes_ci95']:>7,.0f} B"
              f"   AoI {r['mean_aoi_s']:.3f} ± {r['mean_aoi_s_ci95']:.3f} s"
              f"   wire {r['wire_bytes']:>11,.0f} B   efficiency {r['efficiency']:.3f}")
    ours = next(r for r in summary if r["policy"] == "ours")
    print(f"  decision cost: {ours['decision_mean_us']:.2f} µs mean, {ours['decision_p99_us']:.2f} µs p99")
    print("\nPer-field AoI (180 s, 5 seeds; s) and decisions under ours")
    for r in fields:
        print(f"  {r['field']:<12} {str(r['period_ms']):>6} {r['vs_t_star']:>5}  ours {r['aoi_ours_s']:.4f}"
              f"  arq {r['aoi_arq_s']:.4f}  udp {r['aoi_udp_s']:.4f}   superseded {r['superseded']:>4}"
              f"  tolerance {r['within_tolerance']:>4}  repaired {r['repair']:>3}")
    print("\nWorkload sensitivity: " + ", ".join(f"{r['lidar_width_b']} B -> {r['saving_pct']}%" for r in workload))
    print("\nInvariants: " + ("ALL PASS" if ok else "FAILURES - see results/invariants.txt"))
    for line in lines:
        if line.startswith("FAIL"):
            print("  " + line)
    print(f"\nWrote CSVs and figures to {OUT}/")


if __name__ == "__main__":
    main()
