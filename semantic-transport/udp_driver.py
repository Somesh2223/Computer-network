"""Phase 7: the same sender and receiver code, over real UDP sockets.

  python3 udp_driver.py --policy ours --seed 11          both ends on localhost
  python3 udp_driver.py --role receiver ...              one end only (netem_bench.py
  python3 udp_driver.py --role sender ...                runs each in its own namespace)

Each end is its own process with its own UDP socket. They talk only through
those sockets, in real time. The protocol code is src/sim.py's, unchanged: we
hand it a UdpLink, so a packet that survives the loss model goes to the socket
instead of back onto the simulator's heap. Only the clock and the
transmit/receive calls differ from the simulator.

Impairment: by default the Python loss model still decides which packets drop
and adds RTT/2 + jitter, so a localhost run sees the same loss trace as the
simulator. Under netem pass --loss 0 --rtt 0 --jitter 0 and let the kernel do it.

Freshness needs each value's generation time, which only the sender knows. Each
end writes a log on exit, and the two are joined afterwards on the shared
monotonic clock (metrics.replay).

Run it on Linux (or WSL): Windows timers are too coarse for a 10 ms tick.
"""

import argparse
import heapq
import json
import os
import selectors
import socket
import subprocess
import sys
import tempfile
import time

import config
from src import schema
from src.metrics import replay, verify_no_false_suppression
from src.sim import FWD, REV, Simulation

DRAIN_S = 1.5      # after the last tick, keep answering NACKs and receiving repairs


class UdpLink:
    """Sends each packet to the peer's socket once its emulated one-way delay is up."""

    def __init__(self, sock, peer, clock):
        self.sock, self.peer, self.clock = sock, peer, clock
        self.pending = []      # heap of (due time, tie, wire)
        self._tie = 0

    def send(self, kind, wire, delay):
        self._tie += 1
        heapq.heappush(self.pending, (self.clock() + delay, self._tie, wire))

    def flush(self):
        while self.pending and self.pending[0][0] <= self.clock():
            _, _, wire = heapq.heappop(self.pending)
            self.sock.sendto(wire, self.peer)

    def next_due(self):
        return self.pending[0][0] if self.pending else None


def event_loop(sim, sock, link, incoming, clock, end, stop=None):
    """Drive one end in real time until `end` (or until `stop` is set): the
    simulator's timers fire from its heap, packets arrive from the socket."""
    selector = selectors.DefaultSelector()
    selector.register(sock, selectors.EVENT_READ)
    while clock() < end and not (stop is not None and stop.is_set()):
        while sim.next_event_time() is not None and sim.next_event_time() <= clock():
            when, _, kind, data = sim.pop_event()
            sim.now = when                      # keep ticks on schedule, not on when we woke
            sim.handle(kind, data)
        link.flush()
        wake = [end] + [t for t in (sim.next_event_time(), link.next_due()) if t is not None]
        timeout = max(0.0, min(wake) - clock())
        if stop is not None:
            timeout = min(timeout, 0.05)        # notice a stop request promptly
        for _ in selector.select(timeout):
            while True:
                try:
                    wire = sock.recv(65535)
                except (BlockingIOError, ConnectionResetError):
                    break
                sim.now = clock()
                sim.handle(incoming, wire)
    selector.close()


def address(text):
    host, port = text.rsplit(":", 1)
    return host, int(port)


def run_end(args):
    """One end (sender or receiver): an event loop over real time and one socket."""
    def clock():
        return time.monotonic() - args.t0       # both ends share t0, so their times line up

    sender = args.role == "sender"
    me, peer = (args.send_addr, args.recv_addr) if sender else (args.recv_addr, args.send_addr)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(address(me))
    sock.setblocking(False)
    link = UdpLink(sock, address(peer), clock)
    sim = Simulation(args.seed, args.policy, loss_rate=args.loss, burst=not args.iid,
                     rtt_s=args.rtt, jitter_s=args.jitter, duration_s=args.duration, link=link)
    incoming = REV if sender else FWD          # the sender only hears NACKs; the receiver, data
    while clock() < 0:                          # wait for the agreed start time
        time.sleep(min(0.05, -clock()))
    if sender:
        sim.start()
    end = args.duration + DRAIN_S + (0.0 if sender else 0.5)
    event_loop(sim, sock, link, incoming, clock, end)
    sock.close()

    log = dict(role=args.role, stats=sim.stats, wire_bytes=sim.metrics.wire_bytes)
    if sender:
        log.update(generated=sim.generated,
                   transmissions=[(t, seq, sorted(fids)) for t, seq, fids in sim.transmissions],
                   suppression_events=sim.suppression_events,
                   decisions=[(t, seq, fid, o.value) for t, seq, fid, o in sim.decisions],
                   decision_times_us=sim.metrics.decision_times_us)
    else:
        log.update(deliveries=sim.deliveries, fast_path_strikes=sim.gaps.fast_path_strikes)
    with open(args.out, "w") as fh:
        json.dump(log, fh)


def combine(sender_log, receiver_log, duration_s, warmup_s=config.WARMUP_S):
    """Join the two ends' logs into one result row."""
    field_ids = [f.id for f in schema.active()]
    wire = sender_log["wire_bytes"] + receiver_log["wire_bytes"]
    m = replay(field_ids, sender_log["generated"], receiver_log["deliveries"], warmup_s, duration_s, wire)
    stats = {k: sender_log["stats"][k] + receiver_log["stats"][k] for k in sender_log["stats"]}
    transmissions = [(t, seq, frozenset(fids)) for t, seq, fids in sender_log["transmissions"]]
    m.decision_times_us = sender_log["decision_times_us"]
    return dict(
        data_sent=stats["data_sent"], data_lost=stats["data_lost"],
        recovery_bytes=stats["repair_bytes"] + stats["skip_bytes"],
        repair_bytes=stats["repair_bytes"], heartbeat_bytes=stats["heartbeat_bytes"],
        app_wire_bytes=wire, mean_aoi_s=round(m.mean_aoi_all(), 4),
        aoi_by_field={schema.by_id(fid).name: round(m.avg_aoi(fid), 4) for fid in field_ids},
        latency_p95_ms=round(m.latency_percentile(95) * 1000, 2),
        decision_mean_us=round(m.mean_decision_us(), 2),
        fast_path_strikes=receiver_log["fast_path_strikes"],
        false_suppressions=verify_no_false_suppression(
            [tuple(e) for e in sender_log["suppression_events"]], transmissions))


def run_both(args):
    """Launch receiver and sender as two processes on localhost, then join their logs."""
    tmp = tempfile.mkdtemp(prefix="udp_driver_")
    t0 = time.monotonic() + 1.0                 # both start one second from now
    common = ["--policy", args.policy, "--seed", str(args.seed), "--duration", str(args.duration),
              "--loss", str(args.loss), "--rtt", str(args.rtt), "--jitter", str(args.jitter),
              "--send-addr", args.send_addr, "--recv-addr", args.recv_addr, "--t0", repr(t0)]
    if args.iid:
        common.append("--iid")
    ends = {}
    for role in ("receiver", "sender"):
        out = os.path.join(tmp, f"{role}.json")
        ends[role] = (subprocess.Popen([sys.executable, __file__, "--role", role, "--out", out] + common), out)
    logs = {}
    for role, (proc, out) in ends.items():
        if proc.wait() != 0:
            sys.exit(f"{role} failed")
        with open(out) as fh:
            logs[role] = json.load(fh)
    return combine(logs["sender"], logs["receiver"], args.duration)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--role", choices=("both", "sender", "receiver"), default="both")
    p.add_argument("--policy", default="ours")
    p.add_argument("--seed", type=int, default=config.SEEDS[0])
    p.add_argument("--duration", type=float, default=config.DURATION_S)
    p.add_argument("--loss", type=float, default=config.LOSS_RATE)
    p.add_argument("--iid", action="store_true", help="i.i.d. loss instead of bursts")
    p.add_argument("--rtt", type=float, default=config.RTT_S)
    p.add_argument("--jitter", type=float, default=config.JITTER_S)
    p.add_argument("--send-addr", default="127.0.0.1:47001")
    p.add_argument("--recv-addr", default="127.0.0.1:47002")
    p.add_argument("--t0", type=float, help="shared start time (monotonic clock)")
    p.add_argument("--out", help="where one end writes its log")
    p.add_argument("--json", help="write the joined result here (role both)")
    args = p.parse_args()

    if args.role != "both":
        run_end(args)
        return
    result = dict(policy=args.policy, seed=args.seed, **run_both(args))
    print(json.dumps(result, indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh)


if __name__ == "__main__":
    main()
