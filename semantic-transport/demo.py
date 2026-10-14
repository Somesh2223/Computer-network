"""Narrated demo: watch the sender decide, field by field, what is worth recovering.

  python demo.py                  a few episodes + summary
  python demo.py --episodes 10    more episodes
  python demo.py --compare        ours vs B1 (full ARQ) vs B4 (UDP) on one loss trace
  python demo.py --quiet          summary only

The trace is grouped by lost packet, not by time: one loss interleaves with the
next, and raw time order is unreadable.
"""

import argparse
import sys
import textwrap

import config
from src import schema
from src.crossover import crossover
from src.metrics import verify_no_false_suppression
from src.sim import Simulation

SYMBOL = {"LOST": "✗ LOST  ", "GAP": "? GAP   ", "NACK": "← NACK  ",
          "NACK_LOST": "✗ NACK  ", "DECIDE": "• DECIDE", "SKIP": "○ SKIP  ",
          "REPAIR": "→ REPAIR", "REPLY_LOST": "✗ REPLY ", "HEARTBEAT": "♥ HEARTBEAT"}


def loss_label(args):
    return f"{args.loss:.0%} {'burst' if config.BURST else 'i.i.d.'} loss"


def banner(args):
    # T* from the analytical model (Review 1 §8.6), for the audience to check the
    # system against. The running system never computes it - see supersession.py.
    rtt_s = args.rtt
    t_star = crossover(config.TICK_S, config.HOLD_DOWN_S, rtt_s)
    print(f"\n  {loss_label(args)}, RTT {rtt_s*1000:.0f} ms, seed {args.seed}")
    print(f"  Crossover T* = tick + hold-down + RTT = "
          f"{config.TICK_S*1000:.0f} + {config.HOLD_DOWN_S*1000:.0f} + {rtt_s*1000:.0f} "
          f"= {t_star*1000:.0f} ms")
    print("  A lost field is worth repairing only if its next update is due later than T*")
    print("  AND later than the application's deadline for it.\n")
    print(f"  {'field':<12}{'size':>7}{'period':>10}   model predicts, if lost")
    for f in schema.active():
        period = f"{f.period*1000:.0f} ms" if f.period else f"~{f.mean_gap:.0f} s ev"
        if f.period is None:
            verdict = "REPAIR      (event: may never be sent again)"
        elif f.period < t_star:
            verdict = "SUPERSEDED  (next update beats any repair)"
        elif f.period <= f.deadline:
            verdict = f"TOLERANCE   (next update within its {f.deadline*1000:.0f} ms deadline)"
        else:
            verdict = f"REPAIR      (app needs it within {f.deadline*1000:.0f} ms)"
        print(f"  {f.name:<12}{f.width:>5} B{period:>10}   {verdict}")


def episodes(sim):
    """Group the trace by lost seq, keeping only packets that were actually lost."""
    by_seq = {}
    for t, seq, kind, text in sim.trace:
        by_seq.setdefault(seq, []).append((t, kind, text))
    return {seq: lines for seq, lines in by_seq.items()
            if seq is not None and lines[0][1] == "LOST"}


def pick(eps, n):
    """Lead with one episode of each verdict, then the rest in seq order."""
    def has(seq, word):
        return any(kind == "DECIDE" and word in text for _, kind, text in eps[seq])
    first = []
    for word in ("SUPERSEDED", "REPAIR -", "WITHIN_TOLERANCE"):
        seq = next((s for s in eps if has(s, word) and s not in first), None)
        if seq is not None:
            first.append(seq)
    rest = [s for s in eps if s not in first and any(k == "DECIDE" for _, k, _ in eps[s])]
    return (first + rest)[:n]


def print_line(t, kind, text):
    prefix = f"     t={t:7.3f}s  {SYMBOL[kind]}  "
    wrapped = textwrap.wrap(text, width=98 - len(prefix)) or [""]
    print(prefix + wrapped[0])
    for more in wrapped[1:]:
        print(" " * len(prefix) + more)


def print_episode(number, seq, lines):
    print(f"\n  ── episode {number}:  packet seq {seq} " + "─" * 34)
    for t, kind, text in lines:
        print_line(t, kind, text)


def heartbeat_lines(sim, n=3):
    lines = [(t, kind, text) for t, seq, kind, text in sim.trace if kind == "HEARTBEAT"]
    if lines:
        print("\n  ── liveness: the heartbeat catches what no gap ever would " + "─" * 12)
        for t, kind, text in lines[:n]:
            print_line(t, kind, text)


def summary(ours, arq):
    s, b = ours.stats, arq.stats
    # Our recovery traffic includes the SKIP replies: they replace repairs, so they count.
    ours_bytes = s["repair_bytes"] + s["skip_bytes"]
    saving = 100 * (1 - ours_bytes / b["repair_bytes"]) if b["repair_bytes"] else 0.0
    false = verify_no_false_suppression(ours.suppression_events, ours.transmissions)
    rows = [
        ("data packets lost", s["data_lost"]),
        ("NACKs sent", s["nacks_sent"]),
        ("fast-path strikes", ours.gaps.fast_path_strikes),
        ("replies: SKIP / REPAIR", f"{s['skips_sent']} / {s['repairs_sent']}"),
        ("fields superseded", f"{s['fields_superseded']}  ({s['bytes_superseded']} B not resent)"),
        ("fields within tolerance", f"{s['fields_tolerated']}  ({s['bytes_tolerated']} B not resent)"),
        ("fields repaired", s["fields_repaired"]),
        ("heartbeats / reconciled", f"{s['heartbeats_sent']} / {s['hb_fields_reconciled']} fields"
                                    f"  ({s['heartbeat_bytes']} B)"),
        ("stale fields at end", f"ours {len(ours.stale_fields())}, B1 {len(arq.stale_fields())}"),
        ("repair bytes: ours vs B1", f"{ours_bytes} vs {b['repair_bytes']}"
                                     f"  ({saving:.0f}% less)"),
    ]
    print("\n  ── summary " + "─" * 58)
    for label, value in rows:
        print(f"     {label:<28}{value}")
    print(f"     {'FALSE SUPPRESSIONS':<28}{false}  <-- {'PASS' if false == 0 else 'FAIL'}")


def compare(args):
    print(f"\n  Same seed, same data-loss trace, three policies "
          f"({args.duration:.0f} s, {loss_label(args)}, RTT {args.rtt*1000:.0f} ms)\n")
    cols = ("data lost", "NACKs", "repairs", "skips", "repair B", "wasted B", "recovered", "stale@end")
    print(f"     {'policy':<14}" + "".join(f"{c:>11}" for c in cols))
    label = {"ours": "ours", "arq": "B1 full ARQ", "udp": "B4 plain UDP"}
    for policy in ("ours", "arq", "udp"):
        sim = run(args, policy)
        s = sim.stats
        row = (s["data_lost"], s["nacks_sent"], s["repairs_sent"], s["skips_sent"],
               s["repair_bytes"] + s["skip_bytes"], s["repair_bytes_obsolete"],
               s["repair_fields_applied"], len(sim.stale_fields()))
        print(f"     {label[policy]:<14}" + "".join(f"{v:>11}" for v in row))
    print("\n     wasted B  = repair bytes that arrived but were refused (receiver already had newer)")
    print("     recovered = repaired fields the receiver actually applied")
    print("     ours also sends a small heartbeat every second; its bytes are not counted as repair bytes")


def run(args, policy, trace=False):
    return Simulation(args.seed, policy, loss_rate=args.loss, duration_s=args.duration,
                      rtt_s=args.rtt, trace=trace).run()


def main():
    sys.stdout.reconfigure(encoding="utf-8")   # the arrows and box lines are not cp1252
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episodes", type=int, default=6)
    p.add_argument("--compare", action="store_true")
    p.add_argument("--duration", type=float, default=config.DURATION_S)
    p.add_argument("--loss", type=float, default=config.LOSS_RATE)
    p.add_argument("--rtt", type=float, default=config.RTT_S, help="seconds")
    p.add_argument("--seed", type=int, default=config.SEEDS[0])
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    if args.compare:
        compare(args)
        return
    if not args.quiet:
        banner(args)
    ours = run(args, "ours", trace=True)
    arq = run(args, "arq")
    if not args.quiet:
        eps = episodes(ours)
        for number, seq in enumerate(pick(eps, args.episodes), start=1):
            print_episode(number, seq, eps[seq])
        heartbeat_lines(ours)
    summary(ours, arq)


if __name__ == "__main__":
    main()
