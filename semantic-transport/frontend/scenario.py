"""Run one scenario on the real simulator and package it for the explorer page.

Used by build.py (the built-in drone run baked into the page) and by app.py
(any fields and link conditions a user types into the page).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                    # noqa: E402
from src import fec, header, schema              # noqa: E402
from src.crossover import crossover              # noqa: E402
from src.metrics import on_time_counts, verify_no_false_suppression   # noqa: E402
from src.schema import DEFAULT_FIELDS, Field     # noqa: E402
from src.sim import Simulation                   # noqa: E402
from src.supersession import Outcome             # noqa: E402

OUTCOME_CODE = {Outcome.SUPERSEDED: "S", Outcome.WITHIN_TOLERANCE: "T", Outcome.REPAIR: "R"}
POLICIES = ("ours", "arq", "udp")
MAX_FIELD_BYTES = config.MTU - config.HEADER_BYTES - 1     # one packet: header + version byte + value


def r3(x):
    return round(x, 3)


def fields_from_spec(spec):
    """[{name, size, every_ms (None = event), mean_gap_s, deadline_ms}] -> schema Fields."""
    if not 1 <= len(spec) <= 16:
        raise ValueError("Use between 1 and 16 fields: each takes one bit of the header's bitmap.")
    fields, names = [], set()
    for i, f in enumerate(spec):
        name = str(f.get("name", "")).strip()
        if not name or name in names:
            raise ValueError(f"Field {i + 1} needs a unique name.")
        names.add(name)
        size, deadline = int(f["size"]), float(f["deadline_ms"]) / 1000
        if not 1 <= size <= MAX_FIELD_BYTES:
            raise ValueError(f"{name}: size must be 1 to {MAX_FIELD_BYTES} bytes, so it fits in one packet.")
        if not 0.001 <= deadline <= 120:
            raise ValueError(f"{name}: deadline must be between 1 ms and 120 s.")
        every = f.get("every_ms")
        if every in (None, ""):
            gap = float(f.get("mean_gap_s") or 10)
            if not 0.05 <= gap <= 600:
                raise ValueError(f"{name}: an event field's average gap must be 0.05 to 600 s.")
            fields.append(Field(i, name, size, None, deadline, gap))
        else:
            period = float(every) / 1000
            if not 0.001 <= period <= 120:
                raise ValueError(f"{name}: 'every' must be between 1 ms and 120 s.")
            fields.append(Field(i, name, size, period, deadline))
    return fields


def export_run(policy, seed, duration, loss, rtt, burst):
    warmup = min(config.WARMUP_S, duration / 5)
    sim = Simulation(seed, policy, loss_rate=loss, rtt_s=rtt, burst=burst, duration_s=duration,
                     warmup_s=warmup, trace=True).run()
    lost = {seq for t, seq, kind, _ in sim.trace if kind == "LOST"}
    sends = [[r3(t), seq, header.bitmap_of(fids), int(seq in lost)] for t, seq, fids in sim.transmissions]
    events = [[r3(t), seq, kind, text] for t, seq, kind, text in sim.trace if kind != "LOST"]
    decisions = [[r3(t), seq, fid, OUTCOME_CODE[o]] for t, seq, fid, o in sim.decisions]
    ctrl, total = [], 0
    for t, kind, _, nbytes in sim.metrics.ring:     # everything that is not a DATA packet
        if kind in ("ctrl", "rev"):
            total += nbytes
            ctrl.append([r3(t), total])
    fresh = {}
    for t, fid, version, _ in sim.deliveries:
        fresh.setdefault(fid, []).append([r3(t), r3(sim.gen_time[(fid, version)])])
    critical = [f for f in schema.active() if fec.is_critical(f)]
    summary = dict(
        data_sent=sim.stats["data_sent"], data_lost=sim.stats["data_lost"],
        recovery_bytes=sim.stats["repair_bytes"] + sim.stats["skip_bytes"], control_bytes=total,
        mean_aoi_s=r3(sim.metrics.mean_aoi_all()),
        # [delivered within deadline, scored]: counts, because a short run has few
        # critical events and a bare percentage would swing between 0 and 100.
        critical_on_time=(list(on_time_counts(critical, sim.generated, sim.deliveries, sim.gen_time,
                                              warmup, duration)) if critical else None),
        superseded=sim.stats["fields_superseded"], tolerated=sim.stats["fields_tolerated"],
        repaired=sim.stats["fields_repaired"],
        false_suppressions=verify_no_false_suppression(sim.suppression_events, sim.transmissions))
    return dict(sends=sends, events=events, decisions=decisions, ctrl=ctrl, fresh=fresh, summary=summary)


def pick_episodes(run):
    """One finished episode per verdict, earliest first: a lost packet whose NACK
    got a decision and whose reply arrived."""
    kinds_by_seq = {}
    for _, seq, kind, _ in run["events"]:
        kinds_by_seq.setdefault(seq, set()).add(kind)
    chosen = []
    for code in ("S", "R", "T"):
        for _, seq, _, outcome in run["decisions"]:
            k = kinds_by_seq.get(seq, set())
            if outcome == code and seq not in chosen and "DECIDE" in k and k & {"SKIP", "REPAIR"}:
                chosen.append(seq)
                break
    return chosen


def episode(run, seq):
    sent = next(s for s in run["sends"] if s[1] == seq)
    return dict(seq=seq, sent=sent[0], fields=header.fields_in(sent[2]),
                steps=[e for e in run["events"] if e[1] == seq])


def run_scenario(fields=None, seed=23, duration=15.0, loss=config.LOSS_RATE, rtt=config.RTT_S,
                 burst=True, episodes=None):
    schema.use(fields if fields is not None else DEFAULT_FIELDS)
    try:
        runs = {p: export_run(p, seed, duration, loss, rtt, burst) for p in POLICIES}
        seqs = list(episodes) if episodes else pick_episodes(runs["ours"])
        return dict(
            seed=seed, window=duration,
            config=dict(tick=config.TICK_S, hold=config.HOLD_DOWN_S, rtt=rtt, heartbeat=config.HEARTBEAT_S,
                        loss=loss, burst=burst, tstar=crossover(config.TICK_S, config.HOLD_DOWN_S, rtt)),
            fields=[dict(id=f.id, name=f.name, width=f.width, period=f.period, deadline=f.deadline,
                         mean_gap=f.mean_gap, critical=fec.is_critical(f)) for f in schema.active()],
            runs=runs,
            episodes=[episode(runs["ours"], s) for s in seqs],
            decisions_by_seq={str(s): [d for d in runs["ours"]["decisions"] if d[1] == s] for s in seqs})
    finally:
        schema.use(DEFAULT_FIELDS)
