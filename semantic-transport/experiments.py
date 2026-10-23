"""Experiments: sweeps, figures and CSVs. Five seeds per point, 95 % confidence intervals.

  python experiments.py                 all studies (about 12 minutes)
  python experiments.py fec workload    just the named studies
  python experiments.py realnet         figure from netem_bench.py's CSVs (run that first)
  python experiments.py errors          bit corruption and block FEC (about 1 minute)

Writes everything to results/. Numbers are findings, not targets: nothing here
is tuned to reproduce a figure written down earlier.

  1. crossover validation  - does repair stop helping below T* = tau + H + R?
  2. policy comparison     - ours vs B1 / B3 / B4: recovery bytes and freshness
  3. per-field table       - Age of Information of each field under each policy
  4. workload sensitivity  - how the saving depends on the byte-weight of fast fields
  5. fec                   - selective repetition vs blanket FEC (B2) at matched bytes
  6. invariants            - the hard checks of BUILD_PROMPT §11, over all runs
  7. realnet               - real sockets and kernel TCP under tc/netem (Phase 9)
  8. errors                - bit corruption, and block FEC for large critical fields
"""

import csv
import math
import os
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import config
from src import fec, schema
from src.crossover import crossover
from src.metrics import on_time_rate, verify_no_false_suppression
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


# --- 5. selective FEC vs blanket FEC ---------------------------------------------

def fec_study(duration_s=180.0):
    """Does spending a few bytes on repetition of small critical fields beat spending
    the same bytes on uniform parity (B2)? Scored by the share of critical-field
    updates delivered within their deadline, at RTTs where reactive repair gets slow."""
    critical = [f for f in schema.active() if fec.is_critical(f)]
    names = {"udp": "B4 plain UDP", "ours": "Ours", "ours_fec": "Ours + repetition",
             "b2_matched": "B2 at matched bytes", "b2_full": "B2 every group"}
    rows, runs_ours_fec = [], []
    for rtt in (0.060, 0.300, 0.600):
        per = {k: [] for k in names}
        for seed in config.SEEDS:
            sims = {p: Simulation(seed, p, rtt_s=rtt, duration_s=duration_s).run()
                    for p in ("udp", "ours", "ours_fec")}
            sims["b2_full"] = Simulation(seed, "fec", rtt_s=rtt, duration_s=duration_s).run()
            # Matched: give B2 the same extra bytes ours_fec spends over plain UDP, by
            # sending parity for only every n-th group.
            budget = sims["ours_fec"].metrics.wire_bytes - sims["udp"].metrics.wire_bytes
            every = max(1, round(sims["b2_full"].stats["parity_bytes"] / budget))
            sims["b2_matched"] = Simulation(seed, "fec", rtt_s=rtt, duration_s=duration_s,
                                            fec_every=every).run()
            runs_ours_fec.append(sims["ours_fec"])
            base = sims["udp"].metrics.wire_bytes
            for key, sim in sims.items():
                per[key].append(dict(
                    extra=sim.metrics.wire_bytes - base,
                    on_time=100 * on_time_rate(critical, sim.generated, sim.deliveries, sim.gen_time,
                                               config.WARMUP_S, duration_s),
                    aoi=sim.metrics.mean_aoi_all()))
        for key, label in names.items():
            row = dict(rtt_ms=round(rtt * 1000), policy=label)
            for metric in ("extra", "on_time", "aoi"):
                m, h = mean_ci([r[metric] for r in per[key]])
                row[metric], row[metric + "_ci95"] = round(m, 4), round(h, 4)
            rows.append(row)
        print(f"  fec study at RTT {rtt*1000:.0f} ms done")
    write_csv("fec_study.csv", rows)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.6), facecolor=SURFACE,
                                      gridspec_kw=dict(width_ratios=[1.5, 1]))
    order = list(names.values())
    shades = {"Ours + repetition": ACCENT, "Ours": "#7fb0ea", "B4 plain UDP": GREY,
              "B2 at matched bytes": "#c9c8c2", "B2 every group": "#6f6e69"}
    # Dots with CI whiskers, not bars: the differences live in the top few percent,
    # and a bar's length would have to start at zero to be honest.
    offset = 0.13
    rtts = sorted({r["rtt_ms"] for r in rows})
    for i, label in enumerate(order):
        pts = [next(r for r in rows if r["rtt_ms"] == t and r["policy"] == label) for t in rtts]
        xs = [j + (i - 2) * offset for j in range(len(rtts))]
        left.errorbar(xs, [r["on_time"] for r in pts], yerr=[r["on_time_ci95"] for r in pts],
                      fmt="o", markersize=7, color=shades[label], label=label, capsize=3,
                      elinewidth=1.2, markeredgecolor=SURFACE)
    left.set_xticks(range(len(rtts)), [f"RTT {t} ms" for t in rtts])
    left.set_xlim(-0.5, len(rtts) - 0.5)
    left.set_ylabel("critical updates on time (%)", color=INK_2)
    style(left, "Critical-field updates delivered within their deadline",
          f"10% burst loss, {duration_s:.0f} s runs, 5 seeds, 95% CI")
    left.legend(frameon=False, fontsize=8, labelcolor=INK, ncol=3, loc="upper center",
                bbox_to_anchor=(0.5, -0.1))
    spenders = [label for label in order if label != "B4 plain UDP"]     # UDP is the zero baseline
    at60 = [next(r for r in rows if r["rtt_ms"] == 60 and r["policy"] == label) for label in spenders]
    # Dots, not bars: on a log axis a bar's length would not be its value.
    right.scatter([r["extra"] for r in at60], spenders, s=60, color=[shades[l] for l in spenders], zorder=3)
    right.set_xscale("log")
    right.set_xlim(1e4, 1e7)
    right.set_ylim(len(spenders) - 0.5, -0.5)
    for i, r in enumerate(at60):
        right.text(r["extra"], i, f"  {r['extra']:,.0f} B", va="center", fontsize=8, color=INK)
    right.set_xlabel("extra bytes on the wire vs plain UDP (log scale)", color=INK_2)
    style(right, "What each policy spends", "RTT 60 ms, per 180 s run")
    right.grid(axis="x", color=GRID, linewidth=0.8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_fec.png"), dpi=160)
    plt.close(fig)
    return rows, runs_ours_fec


# --- 8. bit corruption and block FEC ---------------------------------------------

def error_study():
    """Two checks of the error-protection objectives (Review 1 objectives 6 and 9).

    Corruption: bit errors on top of 10% burst loss. A corrupted packet fails the
    UDP checksum and is dropped, so the protocol must treat it as one more loss.
    Block FEC: on a 1.5 s round trip a repair of terrain_map lands about 2.3 s after
    a loss, past its 2 s deadline, so ours_fec protects it in advance.
    """
    critical = [f for f in schema.active() if fec.is_critical(f)]
    corruption = []
    for ber in (0.0, 1e-5, 3e-5):
        for policy in ("ours", "arq", "udp"):
            runs = [Simulation(seed, policy, bit_error_rate=ber).run() for seed in config.SEEDS]
            row = dict(bit_error_rate=ber, policy=LABEL[policy])
            for key, vals in (
                    ("corrupt_dropped", [s.stats["corrupt_dropped"] for s in runs]),
                    ("recovery_bytes", [recovery_bytes(s) for s in runs]),
                    ("critical_on_time_pct", [100 * on_time_rate(critical, s.generated, s.deliveries, s.gen_time,
                                                                 config.WARMUP_S, s.duration_s) for s in runs]),
                    ("mean_aoi_s", [s.metrics.mean_aoi_all() for s in runs])):
                m, h = mean_ci(vals)
                row[key], row[key + "_ci95"] = round(m, 3), round(h, 3)
            row["false_suppressions"] = sum(verify_no_false_suppression(s.suppression_events, s.transmissions)
                                            for s in runs)
            corruption.append(row)
    write_csv("corruption_study.csv", corruption)

    terrain = [f for f in schema.active() if f.name == "terrain_map"]
    block = []
    for policy, label in (("udp", "B4 plain UDP"), ("ours", "Ours"), ("ours_fec", "Ours + block FEC")):
        runs = [Simulation(seed, policy, rtt_s=1.5, duration_s=180.0).run() for seed in config.SEEDS]
        row = dict(rtt_ms=1500, policy=label)
        for key, vals in (
                ("terrain_on_time_pct", [100 * on_time_rate(terrain, s.generated, s.deliveries, s.gen_time,
                                                            config.WARMUP_S, s.duration_s) for s in runs]),
                ("parity_bytes", [s.stats["parity_bytes"] for s in runs]),
                ("fec_recovered", [s.stats["fec_recovered"] for s in runs])):
            m, h = mean_ci(vals)
            row[key], row[key + "_ci95"] = round(m, 2), round(h, 2)
        row["false_suppressions"] = sum(verify_no_false_suppression(s.suppression_events, s.transmissions)
                                        for s in runs)
        block.append(row)
    write_csv("block_fec_study.csv", block)
    return corruption, block


# --- 7. real network (Phase 9) --------------------------------------------------

REALNET_LABEL = {"ours": "Ours", "ours_fec": "Ours + repetition", "arq": "B1 full ARQ",
                 "udp": "B4 plain UDP", "tcp": "Kernel TCP"}


def read_csv(name):
    with open(os.path.join(OUT, name), newline="") as fh:
        return list(csv.DictReader(fh))


def realnet_figure():
    """Summarise and plot netem_bench.py's runs (real sockets, real kernel, tc/netem)."""
    runs, sweep = read_csv("realnet_runs.csv"), read_csv("realnet_sweep.csv")
    summary = []
    for policy, label in REALNET_LABEL.items():
        rows = [r for r in runs if r["policy"] == policy]
        row = dict(policy=label)
        for key in ("wire_bytes", "recovery_bytes", "mean_aoi_s", "critical_on_time_pct",
                    "latency_p95_ms", "latency_p99_ms"):
            m, h = mean_ci([float(r[key]) for r in rows])
            row[key], row[key + "_ci95"] = round(m, 3), round(h, 3)
        fs = [r["false_suppressions"] for r in rows if r["false_suppressions"] != ""]
        row["false_suppressions"] = sum(int(x) for x in fs) if fs else ""
        summary.append(row)
    write_csv("realnet_summary.csv", summary)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.6), facecolor=SURFACE)
    colours = {"Ours": ACCENT, "Ours + repetition": "#7fb0ea", "B1 full ARQ": GREY,
               "B4 plain UDP": "#c9c8c2", "Kernel TCP": SECOND}
    # Every run is a dot and the median a bar: TCP's runs spread over two orders of
    # magnitude, so a mean +/- CI would reach below zero, which a log axis can't show.
    for i, (policy, label) in enumerate(REALNET_LABEL.items()):
        vals = sorted(float(r["latency_p99_ms"]) for r in runs if r["policy"] == policy)
        left.scatter([i] * len(vals), vals, s=36, color=colours[label], alpha=0.8, zorder=3)
        med = statistics.median(vals)
        left.hlines(med, i - 0.15, i + 0.15, color=INK, linewidth=2, zorder=4)
        left.text(i + 0.18, med * 1.18, f"{med:,.0f} ms", fontsize=8, color=INK, va="center")
    left.set_yscale("log")
    left.set_xticks(range(len(REALNET_LABEL)), list(REALNET_LABEL.values()), rotation=15)
    left.set_xlim(-0.5, len(REALNET_LABEL) - 0.2)
    left.set_ylabel("p99 freshness latency (ms, log scale)", color=INK_2)
    style(left, "Worst-case delay of fresh state, real kernel under netem",
          "10% burst loss, RTT 60 ms, 30 s runs; dots = runs (5 seeds), bar = median")
    for policy, colour in (("ours", ACCENT), ("udp", GREY), ("tcp", SECOND)):
        losses = sorted({float(r["loss"]) for r in sweep})
        meds = []
        for loss in losses:
            vals = [float(r["latency_p99_ms"]) for r in sweep
                    if r["policy"] == policy and float(r["loss"]) == loss]
            right.scatter([100 * loss] * len(vals), vals, s=22, color=colour, alpha=0.55, zorder=3)
            meds.append(statistics.median(vals))
        right.plot([100 * x for x in losses], meds, color=colour, marker="o", markersize=5,
                   linewidth=2, label=REALNET_LABEL[policy], zorder=4)
    right.set_yscale("log")
    right.set_xlabel("i.i.d. loss rate (%)", color=INK_2)
    right.set_ylabel("p99 freshness latency (ms, log scale)", color=INK_2)
    style(right, "Tail latency against loss rate, real kernel under netem",
          "i.i.d. loss, RTT 60 ms, 20 s runs; dots = runs (3 seeds), line = median")
    right.legend(frameon=False, fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_realnet.png"), dpi=160)
    plt.close(fig)
    return summary


# --- 6. invariants -------------------------------------------------------------

def check_invariants(policy_runs, field_runs, fec_runs=()):
    checks = []
    if fec_runs:
        checks.append(("False suppressions = 0 with selective repetition",
                       all(verify_no_false_suppression(s.suppression_events, s.transmissions) == 0
                           for s in fec_runs)))
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
    wanted = set(sys.argv[1:]) or {"crossover", "policy", "fields", "workload", "fec", "realnet"}
    if "realnet" in wanted and os.path.exists(os.path.join(OUT, "realnet_runs.csv")):
        print("real-network figure")
        print("\nReal sockets + kernel under tc/netem (10% burst loss, 5 seeds)")
        for r in realnet_figure():
            print(f"  {r['policy']:<18} wire {r['wire_bytes']:>9,.0f} B  recovery {r['recovery_bytes']:>8,.0f} B"
                  f"  AoI {r['mean_aoi_s']:.3f} s  critical on time {r['critical_on_time_pct']:6.2f}%"
                  f"  p99 {r['latency_p99_ms']:>8,.0f} ms  false supp {r['false_suppressions']}")
    if "crossover" in wanted:
        print("crossover sweep")
        crossover_sweep()
    if "workload" in wanted:
        print("workload sweep")
        workload = workload_sweep()
        print("Workload sensitivity: " + ", ".join(f"{r['lidar_width_b']} B -> {r['saving_pct']}%"
                                                   for r in workload))
    fec_runs = ()
    if "fec" in wanted:
        print("fec study")
        rows, fec_runs = fec_study()
        print("\nSelective repetition vs blanket FEC (180 s, 10% burst loss, 5 seeds)")
        for r in rows:
            print(f"  RTT {r['rtt_ms']:>3} ms  {r['policy']:<20} extra {r['extra']:>11,.0f} B"
                  f"   critical on time {r['on_time']:6.2f} ± {r['on_time_ci95']:.2f} %   AoI {r['aoi']:.3f} s")
    if "errors" in wanted:
        print("error study")
        corruption, block = error_study()
        print("\nBit corruption on top of 10% burst loss (30 s, 5 seeds)")
        for r in corruption:
            print(f"  BER {r['bit_error_rate']:<7g} {r['policy']:<14} corrupted {r['corrupt_dropped']:>6.0f}"
                  f"  recovery {r['recovery_bytes']:>9,.0f} B  critical on time {r['critical_on_time_pct']:6.2f}%"
                  f"  AoI {r['mean_aoi_s']:.3f} s  false supp {r['false_suppressions']}")
        print("\nBlock FEC for terrain_map at RTT 1.5 s (180 s, 10% burst loss, 5 seeds)")
        for r in block:
            print(f"  {r['policy']:<18} terrain on time {r['terrain_on_time_pct']:6.2f} +/- {r['terrain_on_time_pct_ci95']:.2f}%"
                  f"  parity {r['parity_bytes']:>8,.0f} B  rebuilt {r['fec_recovered']:.1f}  false supp {r['false_suppressions']}")
    if not {"policy", "fields"} <= wanted:
        print(f"\nWrote results to {OUT}/ (invariants need the policy and fields studies)")
        return
    print("policy comparison")
    policy_runs, summary = policy_comparison()
    print("per-field table")
    field_runs, fields = per_field_table()
    ok, lines = check_invariants(policy_runs, field_runs, fec_runs)

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
    print("\nInvariants: " + ("ALL PASS" if ok else "FAILURES - see results/invariants.txt"))
    for line in lines:
        if line.startswith("FAIL"):
            print("  " + line)
    print(f"\nWrote CSVs and figures to {OUT}/")


if __name__ == "__main__":
    main()
