"""Builds frontend/explorer.html: the visual explainer, with real data baked in.

  python frontend/build.py

Nothing in the page re-implements the protocol. Every packet, loss, decision
and byte it shows comes from src/sim.py (seed 23, 10% burst loss, the same
run `python demo.py --seed 23` narrates), and every chart from the CSVs in
results/ that experiments.py and netem_bench.py wrote. Edit template.html,
then re-run this script.
"""

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import config                                    # noqa: E402
from src import fec, header, schema              # noqa: E402
from src.crossover import crossover              # noqa: E402
from src.sim import Simulation                   # noqa: E402
from src.supersession import Outcome             # noqa: E402

SEED, WINDOW_S = 23, 15.0
OUTCOME_CODE = {Outcome.SUPERSEDED: "S", Outcome.WITHIN_TOLERANCE: "T", Outcome.REPAIR: "R"}
EPISODES = (90, 862, 89)      # superseded, repaired, within tolerance (see demo.py --seed 23)


def r3(x):
    return round(x, 3)


def export_run(policy):
    sim = Simulation(SEED, policy, duration_s=WINDOW_S, trace=True).run()
    lost = {seq for t, seq, kind, _ in sim.trace if kind == "LOST"}
    sends = [[r3(t), seq, header.bitmap_of(fids), int(seq in lost)] for t, seq, fids in sim.transmissions]
    events = [[r3(t), seq, kind, text] for t, seq, kind, text in sim.trace if kind != "LOST"]
    decisions = [[r3(t), seq, fid, OUTCOME_CODE[o]] for t, seq, fid, o in sim.decisions]
    # Control traffic over time: everything that is not a DATA packet (repairs,
    # skips, heartbeats one way; NACKs the other), as cumulative bytes.
    ctrl, total = [], 0
    for t, kind, _, nbytes in sim.metrics.ring:
        if kind in ("ctrl", "rev"):
            total += nbytes
            ctrl.append([r3(t), total])
    fresh = {}
    for t, fid, version, _ in sim.deliveries:
        fresh.setdefault(fid, []).append([r3(t), r3(sim.gen_time[(fid, version)])])
    return dict(sends=sends, events=events, decisions=decisions, ctrl=ctrl, fresh=fresh,
                stats={k: sim.stats[k] for k in ("data_sent", "data_lost", "nacks_sent", "repairs_sent",
                                                 "skips_sent", "repair_bytes", "skip_bytes",
                                                 "fields_superseded", "fields_tolerated", "fields_repaired",
                                                 "hb_fields_reconciled")},
                strikes=sim.gaps.fast_path_strikes)


def episode(run, seq):
    steps = [e for e in run["events"] if e[1] == seq]
    sent = next(s for s in run["sends"] if s[1] == seq)
    return dict(seq=seq, sent=sent[0], fields=header.fields_in(sent[2]), steps=steps)


def read_csv(name):
    with open(os.path.join(ROOT, "results", name), newline="") as fh:
        return list(csv.DictReader(fh))


def numeric(rows):
    out = []
    for r in rows:
        row = {}
        for k, v in r.items():
            try:
                row[k] = float(v)
            except ValueError:
                row[k] = v
        out.append(row)
    return out


def example_packet():
    """The real bytes of a DATA packet carrying attitude and lidar_scan."""
    updates = {0: (7, bytes(4)), 2: (3, bytes(900))}
    wire = header.pack(header.Packet(header.DATA, 90, header.bitmap_of(updates), 0, updates))
    return dict(header_hex=wire[:5].hex(" "), length=len(wire), seq=90, bitmap=header.bitmap_of(updates))


def main():
    runs = {p: export_run(p) for p in ("ours", "arq", "udp")}
    data = dict(
        seed=SEED, window=WINDOW_S,
        config=dict(tick=config.TICK_S, hold=config.HOLD_DOWN_S, rtt=config.RTT_S,
                    heartbeat=config.HEARTBEAT_S, loss=config.LOSS_RATE,
                    tstar=crossover(config.TICK_S, config.HOLD_DOWN_S, config.RTT_S)),
        fields=[dict(id=f.id, name=f.name, width=f.width, period=f.period, deadline=f.deadline,
                     mean_gap=f.mean_gap, critical=fec.is_critical(f)) for f in schema.active()],
        runs=runs,
        episodes=[episode(runs["ours"], s) for s in EPISODES],
        decisions_by_seq={str(s): [d for d in runs["ours"]["decisions"] if d[1] == s] for s in EPISODES},
        packet=example_packet(),
        results=dict(
            crossover=numeric(read_csv("crossover_sweep.csv")),
            policy=numeric(read_csv("policy_summary.csv")),
            fields=numeric(read_csv("per_field.csv")),
            workload=numeric(read_csv("workload_sweep.csv")),
            fec=numeric(read_csv("fec_study.csv")),
            realnet=numeric(read_csv("realnet_summary.csv")),
            realnet_sweep=numeric(read_csv("realnet_sweep.csv"))),
    )
    blob = json.dumps(data, separators=(",", ":"))
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as fh:
        page = fh.read()
    page = page.replace("/*__DATA__*/null", blob)
    # The template is page content only (the form the Artifact viewer expects).
    # explorer.html wraps it into a complete document so it also opens offline.
    split = page.index("<nav")
    document = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
                + page[:split] + "</head>\n<body>\n" + page[split:] + "\n</body>\n</html>\n")
    out = os.path.join(HERE, "explorer.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(document)
    if len(sys.argv) > 1:                       # optional: content-only copy for publishing
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            fh.write(page)
    print(f"wrote {out} ({len(document) / 1024:.0f} KB, data {len(blob) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
