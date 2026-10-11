"""Discrete-event harness: a simulated clock and link around the real code.

Every packet really goes through header.pack() / unpack(); only time and the
network are pretend. Events live in a heap of (time, tie, kind, data).

Phase 2 skeleton: TICK and FWD only, and the link never loses anything.
"""

import heapq
import random

import config
from src import header, schema
from src.generator import Generator
from src.state_table import StateTable

TICK, FWD = "TICK", "FWD"


class Simulation:
    def __init__(self, seed, duration_s=config.DURATION_S, tick_s=config.TICK_S,
                 rtt_s=config.RTT_S, jitter_s=config.JITTER_S):
        self.duration_s, self.tick_s = duration_s, tick_s
        self.rtt_s, self.jitter_s = rtt_s, jitter_s

        self.generator = Generator(random.Random(seed + 1))
        self.jitter_rng = random.Random(seed + 5)   # extra stream; seed+1..4 are kept for later phases

        self.latest_sent = {}      # sender's truth: field id -> (version, value)
        self.receiver = StateTable()
        self.next_seq = 0
        self.packets_sent = 0
        self.bytes_sent = 0
        self.stale_rejected = 0    # arrivals the state table refused (reordered by jitter)

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
                self._on_arrival(data)

    def _on_tick(self):
        updates = self.generator.due(self.now)
        if updates:
            self._send_data(updates)
        if self.now + self.tick_s <= self.duration_s:
            self._push(self.now + self.tick_s, TICK)

    def _send_data(self, updates):
        self.latest_sent.update(updates)
        pkt = header.Packet(header.DATA, self.next_seq, header.bitmap_of(updates), 0, updates)
        self.next_seq = (self.next_seq + 1) % (1 << config.SEQ_BITS)
        wire = header.pack(pkt)
        self.packets_sent += 1
        self.bytes_sent += len(wire)
        delay = self.rtt_s / 2 + self.jitter_rng.uniform(0, self.jitter_s)
        self._push(self.now + delay, FWD, wire)

    def _on_arrival(self, wire):
        pkt = header.unpack(wire)
        for fid, (version, value) in pkt.updates.items():
            if not self.receiver.apply(fid, version, value):
                self.stale_rejected += 1

    def replicated(self):
        """True if the receiver holds exactly the sender's latest value of every field."""
        return all(self.receiver.version.get(fid) == v and self.receiver.value.get(fid) == val
                   for fid, (v, val) in self.latest_sent.items())


if __name__ == "__main__":
    for seed in config.SEEDS:
        sim = Simulation(seed)
        sim.run()
        names = {f.id: f.name for f in schema.active()}
        print(f"seed {seed}: {sim.packets_sent} packets, {sim.bytes_sent} B, "
              f"{len(sim.latest_sent)}/{len(names)} fields seen, "
              f"stale rejected {sim.stale_rejected}, replicated = {sim.replicated()}")
    print()
    print(f"{'field':<12}{'sender v':>9}{'receiver v':>11}  match")
    for fid, (v, val) in sorted(sim.latest_sent.items()):
        rv = sim.receiver.version.get(fid)
        print(f"{names[fid]:<12}{v:>9}{rv:>11}  {rv == v and sim.receiver.value[fid] == val}")
