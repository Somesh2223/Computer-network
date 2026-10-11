"""Discrete-event harness: a simulated clock and link around the real code.

Every packet really goes through header.pack() / unpack(); only time and the
network are pretend. Events live in a heap of (time, tie, kind, data).

Phase 3: i.i.d. loss, gap detection, NACKs, and two policies:
  "udp"  no recovery at all
  "arq"  B1 - the sender resends EVERY field of a lost packet, in full

Three independent random channels, so every policy sees the same data losses:
  fwd_data  one draw per DATA packet, nothing else
  fwd_ctrl  repairs (and, later, skips / heartbeats)
  rev       NACKs
"""

import heapq
import random

import config
from src import header, schema
from src.gap_detector import GapDetector
from src.generator import Generator
from src.impairment import IIDLoss
from src.state_table import StateTable

TICK, FWD, TIMER, REV = "TICK", "FWD", "TIMER", "REV"

KEEP_PACKETS = 1024      # how many recent packets the sender remembers for repairs


class Simulation:
    def __init__(self, seed, policy="arq", loss_rate=config.LOSS_RATE,
                 duration_s=config.DURATION_S, tick_s=config.TICK_S,
                 hold_down_s=config.HOLD_DOWN_S, rtt_s=config.RTT_S,
                 jitter_s=config.JITTER_S):
        self.policy = policy
        self.duration_s, self.tick_s = duration_s, tick_s
        self.hold_down_s, self.rtt_s, self.jitter_s = hold_down_s, rtt_s, jitter_s

        self.generator = Generator(random.Random(seed + 1))
        self.fwd_data = random.Random(seed + 2)
        self.fwd_ctrl = random.Random(seed + 3)
        self.rev = random.Random(seed + 4)
        self.data_jitter = random.Random(seed + 5)   # extra stream, so jitter can't disturb seed+1..4
        self.data_loss = IIDLoss(self.fwd_data, loss_rate)
        self.ctrl_loss = IIDLoss(self.fwd_ctrl, loss_rate)
        self.rev_loss = IIDLoss(self.rev, loss_rate)

        # sender
        self.latest_sent = {}        # truth: field id -> (version, value)
        self.packet_contents = {}    # seq -> {field id: (version, value)}, for repairs
        self.next_seq = 0
        # receiver
        self.receiver = StateTable()
        self.gaps = GapDetector()

        self.stats = dict(data_sent=0, data_lost=0, data_bytes=0,
                          nacks_sent=0, nacks_lost=0, nack_bytes=0,
                          repairs_sent=0, repairs_lost=0, repair_bytes=0,
                          repair_fields_applied=0, repair_fields_obsolete=0,
                          repair_bytes_obsolete=0)

        self.now = 0.0
        self._heap = []
        self._tie = 0

    def _push(self, when, kind, data=None):
        self._tie += 1             # tie-breaker keeps same-time events in insertion order
        heapq.heappush(self._heap, (when, self._tie, kind, data))

    def run(self):
        self._push(0.0, TICK)
        while self._heap:          # runs until the last in-flight packet has landed
            self.now, _, kind, data = heapq.heappop(self._heap)
            if kind == TICK:
                self._on_tick()
            elif kind == FWD:
                self._on_forward(data)
            elif kind == TIMER:
                self._on_timer(data)
            elif kind == REV:
                self._on_nack(data)

    # --- sender ---------------------------------------------------------------

    def _on_tick(self):
        updates = self.generator.due(self.now)
        if updates:
            self._send_data(updates)
        if self.now + self.tick_s <= self.duration_s:
            self._push(self.now + self.tick_s, TICK)

    def _send_data(self, updates):
        self.latest_sent.update(updates)
        seq = self.next_seq
        self.next_seq = (seq + 1) % (1 << config.SEQ_BITS)
        self._remember(seq, updates)
        wire = header.pack(header.Packet(header.DATA, seq, header.bitmap_of(updates), 0, updates))
        self.stats["data_sent"] += 1
        self.stats["data_bytes"] += len(wire)
        lost = self.data_loss.drops()
        delay = self.rtt_s / 2 + self.data_jitter.uniform(0, self.jitter_s)
        if lost:
            self.stats["data_lost"] += 1
        else:
            self._push(self.now + delay, FWD, wire)

    def _remember(self, seq, updates):
        self.packet_contents[seq] = updates
        # Prune in bulk: doing it every packet would make a long run O(n^2).
        if len(self.packet_contents) > KEEP_PACKETS * 2:
            newest = list(self.packet_contents.items())[-KEEP_PACKETS:]
            self.packet_contents = dict(newest)

    def _on_nack(self, wire):
        nack = header.unpack(wire)
        contents = self.packet_contents.get(nack.seq)
        if contents is None:                  # too old, already pruned
            return
        # B1: resend every field of the lost packet, whether or not it is obsolete.
        # (nack.bitmap, the receiver's candidate mask, is ignored on purpose.)
        repair = header.Packet(header.REPAIR, nack.seq, header.bitmap_of(contents), 0, contents)
        wire = header.pack(repair)
        self.stats["repairs_sent"] += 1
        self.stats["repair_bytes"] += len(wire)
        lost = self.ctrl_loss.drops()
        delay = self.rtt_s / 2 + self.fwd_ctrl.uniform(0, self.jitter_s)
        if lost:
            self.stats["repairs_lost"] += 1
        else:
            self._push(self.now + delay, FWD, wire)

    # --- receiver -------------------------------------------------------------

    def _on_forward(self, wire):
        pkt = header.unpack(wire)
        is_repair = pkt.type == header.REPAIR
        for fid, (version, value) in pkt.updates.items():
            applied = self.receiver.apply(fid, version, value)
            if is_repair:
                key = "applied" if applied else "obsolete"
                self.stats[f"repair_fields_{key}"] += 1
                if not applied:
                    self.stats["repair_bytes_obsolete"] += len(value)
        if pkt.type == header.DATA and self.policy != "udp":
            for seq in self.gaps.on_data(pkt.seq):
                self._push(self.now + self.hold_down_s, TIMER, seq)

    def _on_timer(self, seq):
        if not self.gaps.hold_down_expired(seq):
            return
        # The receiver never saw the lost packet, so it cannot say which fields it held:
        # every field is a candidate.
        mask = header.bitmap_of(f.id for f in schema.active())
        wire = header.pack(header.Packet(header.NACK, seq, mask))
        self.stats["nacks_sent"] += 1
        self.stats["nack_bytes"] += len(wire)
        lost = self.rev_loss.drops()
        delay = self.rtt_s / 2 + self.rev.uniform(0, self.jitter_s)
        if lost:
            self.stats["nacks_lost"] += 1
        else:
            self._push(self.now + delay, REV, wire)

    # --- checks ---------------------------------------------------------------

    def stale_fields(self):
        """Fields where the receiver ended up behind the sender."""
        return [fid for fid, (v, val) in self.latest_sent.items()
                if self.receiver.version.get(fid) != v or self.receiver.value.get(fid) != val]

    def replicated(self):
        return not self.stale_fields()


if __name__ == "__main__":
    print("zero loss, ARQ: nothing should be lost, asked for, or stale")
    for seed in config.SEEDS:
        sim = Simulation(seed, "arq", loss_rate=0.0)
        sim.run()
        print(f"  seed {seed}: lost {sim.stats['data_lost']}, nacks {sim.stats['nacks_sent']}, "
              f"replicated = {sim.replicated()}")

    print("\n10% i.i.d. loss, same loss trace under both policies")
    print(f"{'seed':>5} {'policy':>6} {'data lost':>10} {'nacks':>6} {'repairs':>8} "
          f"{'repair B':>9} {'obsolete B':>11} {'recovered':>10} {'stale@end':>10}")
    for seed in config.SEEDS:
        lost = {}
        for policy in ("udp", "arq"):
            sim = Simulation(seed, policy, loss_rate=0.10)
            sim.run()
            s = sim.stats
            lost[policy] = s["data_lost"]
            print(f"{seed:>5} {policy:>6} {s['data_lost']:>10} {s['nacks_sent']:>6} {s['repairs_sent']:>8} "
                  f"{s['repair_bytes']:>9} {s['repair_bytes_obsolete']:>11} "
                  f"{s['repair_fields_applied']:>10} {len(sim.stale_fields()):>10}")
        assert lost["udp"] == lost["arq"], "policies saw different data losses"
