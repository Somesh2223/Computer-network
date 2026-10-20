"""Discrete-event harness: a simulated clock and link around the real code.

Every packet really goes through header.pack() / unpack(); only time and the
network are pretend. Events live in a heap of (time, tie, kind, data).

The protocol code here is also what runs over real sockets: udp_driver.py hands
in a `link`, and packets that survive the loss model go to a real UDP socket
instead of back onto the heap. Nothing else changes.

Policies:
  "ours"      per-field supersession + tolerance, plus the liveness heartbeat
  "ours_fec"  ours + selective repetition of small critical fields (fec.py)
  "arq"       B1 - the sender resends EVERY field of a lost packet, in full
  "udp"       B4 - no recovery at all
  "snapshot"  B3 - the whole state every tick, fragmented over the MTU; a
              snapshot counts only if every fragment arrives. No recovery.
  "fec"       B2 - blanket XOR parity: one parity packet per group of data
              packets, for every field alike. No NACKs.
ours, arq and udp see an identical data-loss trace. snapshot sends several
fragments per tick, so it draws more often and its trace differs.

Three independent random channels, so every policy sees the same data losses:
  fwd_data  one draw per DATA packet, nothing else
  fwd_ctrl  repairs, skips and heartbeats
  rev       NACKs (gap NACKs and heartbeat requests)
If repairs shared the data channel, a policy that sends more repairs would also
get a different loss pattern, and the comparison would be worthless.
"""

import heapq
import math
import random
import time

import config
from src import fec, header, schema, supersession
from src.gap_detector import GapDetector
from src.generator import Generator
from src.impairment import make_loss
from src.metrics import Metrics
from src.send_log import SendLog
from src.state_table import StateTable

TICK, FWD, TIMER, REV, HEARTBEAT = "TICK", "FWD", "TIMER", "REV", "HEARTBEAT"

OURS = ("ours", "ours_fec")              # supersession + tolerance + heartbeat
NACKING = ("ours", "ours_fec", "arq")    # policies whose receiver reports gaps
FEC_MEMORY_S = 1.0                       # B2 receiver keeps packets this long for rebuilds


def names(field_ids):
    return ", ".join(schema.by_id(fid).name for fid in sorted(field_ids))


class Simulation:
    def __init__(self, seed, policy="ours", loss_rate=config.LOSS_RATE,
                 duration_s=config.DURATION_S, tick_s=config.TICK_S,
                 hold_down_s=config.HOLD_DOWN_S, rtt_s=config.RTT_S,
                 jitter_s=config.JITTER_S, burst=config.BURST,
                 mean_burst_len=config.MEAN_BURST_LEN,
                 heartbeat_s=config.HEARTBEAT_S, warmup_s=config.WARMUP_S, trace=False,
                 link=None, repetitions=config.REPETITIONS, fec_group=config.FEC_GROUP,
                 fec_every=1):
        self.policy = policy
        self.duration_s, self.tick_s = duration_s, tick_s
        self.hold_down_s, self.rtt_s, self.jitter_s = hold_down_s, rtt_s, jitter_s
        self.heartbeat_s = heartbeat_s
        self.link = link             # None = simulated link (the heap); else a real socket

        self.generator = Generator(random.Random(seed + 1))
        self.fwd_data = random.Random(seed + 2)
        self.fwd_ctrl = random.Random(seed + 3)
        self.rev = random.Random(seed + 4)
        self.data_jitter = random.Random(seed + 5)   # extra stream, so jitter can't disturb seed+1..4
        self.data_loss = make_loss(self.fwd_data, loss_rate, burst, mean_burst_len)
        self.ctrl_loss = make_loss(self.fwd_ctrl, loss_rate, burst, mean_burst_len)
        self.rev_loss = make_loss(self.rev, loss_rate, burst, mean_burst_len)

        # sender
        self.latest_sent = {}        # truth: field id -> (version, value)
        self.send_log = SendLog()
        self.next_seq = 0
        self.repeater = fec.Repeater(
            fec.protected_fields(schema.active(), config.REPETITION_MAX_WIDTH),
            repetitions if policy == "ours_fec" else 0, config.REPETITION_SPACING)
        # B2: parity over each group of fec_group data packets; fec_every > 1 sends
        # parity for only every n-th group, to match another policy's byte budget.
        self.fec_group, self.fec_every = fec_group, fec_every
        self.fec_wires = []          # this group's packets, as sent
        self.fec_groups_done = 0
        self.fec_received = {}       # receiver: seq -> (arrival time, wire), for rebuilds
        # receiver
        self.receiver = StateTable()
        self.gaps = GapDetector(f.id for f in schema.active())

        # Raw history of every DATA transmission: (time, seq, field ids). Kept apart
        # from the send log so a false-suppression audit can't just agree with itself.
        self.transmissions = []
        # The two kinds of suppression are kept apart on purpose: a tolerance skip
        # applies to a field that was NOT re-sent, so auditing it against the
        # supersession rule would report a false violation.
        self.suppression_events = []   # SUPERSEDED: (time, lost seq, field id, resent seq)
        self.tolerance_events = []     # WITHIN_TOLERANCE: (time, lost seq, field id)
        self.decisions = []            # every verdict: (time, lost seq, field id, outcome)

        # Freshness is measured from when a value was generated. The receiver can't
        # know that in a real deployment; the simulator can, so it keeps the record.
        self.gen_time = {}             # (field id, version) -> generation time
        # The same facts as plain logs, for when sender and receiver are separate
        # processes (udp_driver.py) and freshness is worked out afterwards.
        self.generated = []            # (time, field id, version) at the sender
        self.deliveries = []           # (time, field id, version, bytes) applied at the receiver
        self.metrics = Metrics([f.id for f in schema.active()], warmup_s, duration_s)

        self.stats = dict(data_sent=0, data_lost=0, data_bytes=0,
                          nacks_sent=0, nacks_lost=0, nack_bytes=0,
                          repairs_sent=0, repairs_lost=0, repair_bytes=0,
                          skips_sent=0, skips_lost=0, skip_bytes=0,
                          fields_superseded=0, bytes_superseded=0,
                          fields_tolerated=0, bytes_tolerated=0, fields_repaired=0,
                          repair_fields_applied=0, repair_fields_obsolete=0,
                          repair_bytes_obsolete=0,
                          heartbeats_sent=0, heartbeat_bytes=0, hb_requests=0,
                          hb_fields_reconciled=0, repeat_bytes=0,
                          parity_sent=0, parity_bytes=0, fec_recovered=0)

        # Episode trace for demo.py: (time, seq, kind, text). Off unless asked for.
        self.trace = [] if trace else None

        self.now = 0.0
        self._heap = []
        self._tie = 0

    def _push(self, when, kind, data=None):
        self._tie += 1             # tie-breaker keeps same-time events in insertion order
        heapq.heappush(self._heap, (when, self._tie, kind, data))

    def _log(self, seq, kind, text):
        if self.trace is not None:
            self.trace.append((self.now, seq, kind, text))

    def start(self):
        """The sender's first events: the first tick, and the first heartbeat."""
        self._push(0.0, TICK)
        if self.policy in OURS:
            self._push(self.heartbeat_s, HEARTBEAT)

    def handle(self, kind, data):
        if kind == TICK:
            self._on_tick()
        elif kind == FWD:
            self._on_forward(data)
        elif kind == TIMER:
            self._on_timer(data)
        elif kind == REV:
            self._on_nack(data)
        elif kind == HEARTBEAT:
            self._on_heartbeat()

    def next_event_time(self):
        return self._heap[0][0] if self._heap else None

    def pop_event(self):
        """(time, tie, kind, data) of the earliest pending event."""
        return heapq.heappop(self._heap)

    def run(self):
        self.start()
        while self._heap:          # runs until the last in-flight packet has landed
            self.now, _, kind, data = heapq.heappop(self._heap)
            self.handle(kind, data)
        self.metrics.finish()
        return self

    # --- links ----------------------------------------------------------------

    def _deliver(self, kind, wire, delay):
        """A packet survived the loss model. FWD goes to the receiver, REV to the sender."""
        if self.link is None:
            self._push(self.now + delay, kind, wire)    # simulated link
        else:
            self.link.send(kind, wire, delay)           # real UDP socket (udp_driver.py)

    def _send_ctrl(self, wire):
        """Forward control channel (repairs, skips, heartbeats). Returns True if lost."""
        self.metrics.on_wire(len(wire), "ctrl", self.now)
        lost = self.ctrl_loss.drops()
        delay = self.rtt_s / 2 + self.fwd_ctrl.uniform(0, self.jitter_s)
        if not lost:
            self._deliver(FWD, wire, delay)
        return lost

    def _send_rev(self, wire):
        """Reverse channel (NACKs). Returns True if lost."""
        self.metrics.on_wire(len(wire), "rev", self.now)
        lost = self.rev_loss.drops()
        delay = self.rtt_s / 2 + self.rev.uniform(0, self.jitter_s)
        if not lost:
            self._deliver(REV, wire, delay)
        return lost

    # --- sender ---------------------------------------------------------------

    def _on_tick(self):
        updates = self.generator.due(self.now)
        for fid, (version, _) in updates.items():
            self.gen_time[(fid, version)] = self.now
            self.generated.append((self.now, fid, version))
        if self.policy == "snapshot":
            self.latest_sent.update(updates)
            if self.latest_sent:
                self._send_snapshot()
        else:
            new = updates
            updates = self.repeater.add(updates, self.latest_sent)   # no-op unless ours_fec
            for fid in updates:
                if fid not in new:
                    self.stats["repeat_bytes"] += 1 + schema.by_id(fid).width
            if updates:
                self._send_data(updates)
        if self.now + self.tick_s <= self.duration_s:
            self._push(self.now + self.tick_s, TICK)

    def _send_data(self, updates):
        self.latest_sent.update(updates)
        seq = self.next_seq
        self.next_seq = (seq + 1) % (1 << config.SEQ_BITS)
        self.send_log.record(seq, updates, self.now)
        self.transmissions.append((self.now, seq, frozenset(updates)))
        fec_byte = 0
        if self.policy == "fec":            # B2: mark the packet's place in its parity group
            group = self.fec_groups_done % 14 + 1
            fec_byte = header.fec_byte(group, len(self.fec_wires) + 1)
        wire = header.pack(header.Packet(header.DATA, seq, header.bitmap_of(updates), fec_byte, updates))
        self.stats["data_sent"] += 1
        self.stats["data_bytes"] += len(wire)
        self.metrics.on_wire(len(wire), "data", self.now)
        lost = self.data_loss.drops()
        delay = self.rtt_s / 2 + self.data_jitter.uniform(0, self.jitter_s)
        if lost:
            self.stats["data_lost"] += 1
            self._log(seq, "LOST", f"seq {seq} carrying [{names(updates)}]")
        else:
            self._deliver(FWD, wire, delay)
        if self.policy == "fec":
            self._fec_sent(seq, wire)

    def _fec_sent(self, seq, wire):
        """B2: once a group is full, send its XOR parity (on the control channel)."""
        self.fec_wires.append((seq, wire))
        if len(self.fec_wires) < self.fec_group:
            return
        first_seq, group = self.fec_wires[0][0], self.fec_groups_done % 14 + 1
        if self.fec_groups_done % self.fec_every == 0:
            parity = fec.parity_of([w for _, w in self.fec_wires])
            pwire = header.pack(header.Packet(header.DATA, first_seq, 0, header.fec_byte(group, 0),
                                              parity=parity))
            self.stats["parity_sent"] += 1
            self.stats["parity_bytes"] += len(pwire)
            self._send_ctrl(pwire)
        self.fec_groups_done += 1
        self.fec_wires = []

    def _on_parity(self, pkt):
        """B2 receiver: if exactly one packet of the group is missing, rebuild it."""
        # Only packets that arrived recently can belong to this group: sequence numbers
        # wrap, and a leftover from 16384 seqs ago (whose parity was lost) must not count.
        self.fec_received = {s: (t, w) for s, (t, w) in self.fec_received.items()
                             if self.now - t < FEC_MEMORY_S}
        group = [(pkt.seq + i) % (1 << config.SEQ_BITS) for i in range(self.fec_group)]
        present = [self.fec_received[s][1] for s in group if s in self.fec_received]
        if len(present) == self.fec_group - 1:
            self.stats["fec_recovered"] += 1
            self._on_forward(fec.recover(pkt.parity, present))
        for s in group:
            self.fec_received.pop(s, None)

    def _send_snapshot(self):
        """B3: pack the whole state as one DATA packet, split it into MTU-sized
        fragments (each with its own header), and deliver it only if all survive."""
        state = dict(self.latest_sent)
        seq = self.next_seq
        self.next_seq = (seq + 1) % (1 << config.SEQ_BITS)
        wire = header.pack(header.Packet(header.DATA, seq, header.bitmap_of(state), 0, state))
        payload = len(wire) - config.HEADER_BYTES
        fragments = math.ceil(payload / (config.MTU - config.HEADER_BYTES))
        nbytes = payload + fragments * config.HEADER_BYTES
        self.stats["data_sent"] += 1
        self.stats["data_bytes"] += nbytes
        self.metrics.on_wire(nbytes, "data", self.now)
        lost = False
        for _ in range(fragments):          # one draw per fragment, every time
            lost = self.data_loss.drops() or lost
        delay = self.rtt_s / 2 + self.data_jitter.uniform(0, self.jitter_s)
        if lost:
            self.stats["data_lost"] += 1
        else:
            self._deliver(FWD, wire, delay)

    def _on_nack(self, wire):
        nack = header.unpack(wire)
        if nack.fec == header.HB_MARK:
            self._on_heartbeat_request(nack)
            return
        contents = self.send_log.contents_of(nack.seq)
        if contents is None:                  # too old, already pruned
            return
        if self.policy == "arq":
            # B1: resend every field of the lost packet, obsolete or not.
            # (nack.bitmap, the receiver's candidate mask, is ignored on purpose.)
            self._log(nack.seq, "DECIDE", f"B1 resends all {len(contents)} fields, no questions asked")
            self._send_reply(header.REPAIR, nack.seq, contents)
            return

        started = time.perf_counter_ns()
        decision = supersession.decide(nack.seq, nack.bitmap, self.send_log)
        self.metrics.on_decision((time.perf_counter_ns() - started) / 1000)
        if not decision.fields:
            self._log(nack.seq, "DECIDE", "nothing left to judge: the fast path already "
                                          "struck off every field this packet carried")
        for d in decision.fields:
            f = schema.by_id(d.field_id)
            self._log(nack.seq, "DECIDE", f"{f.name}  {d.outcome.value} - {d.detail}")
            self.decisions.append((self.now, nack.seq, d.field_id, d.outcome))
            if d.outcome is supersession.Outcome.SUPERSEDED:
                self.stats["fields_superseded"] += 1
                self.stats["bytes_superseded"] += f.width
                self.suppression_events.append((self.now, nack.seq, d.field_id, d.resent_seq))
            elif d.outcome is supersession.Outcome.WITHIN_TOLERANCE:
                self.stats["fields_tolerated"] += 1
                self.stats["bytes_tolerated"] += f.width
                self.tolerance_events.append((self.now, nack.seq, d.field_id))
            else:
                self.stats["fields_repaired"] += 1
        if decision.reply_is_skip():
            self._send_reply(header.SKIP, nack.seq, {})
        else:
            self._send_reply(header.REPAIR, nack.seq, {fid: contents[fid] for fid in decision.live()})

    def _send_reply(self, ptype, seq, updates):
        wire = header.pack(header.Packet(ptype, seq, header.bitmap_of(updates), 0, updates))
        kind = "repair" if ptype == header.REPAIR else "skip"
        self.stats[f"{kind}s_sent"] += 1
        self.stats[f"{kind}_bytes"] += len(wire)
        if self._send_ctrl(wire):
            self.stats[f"{kind}s_lost"] += 1
            self._log(seq, "REPLY_LOST", f"the {kind.upper()} was lost on the way back")

    def _on_heartbeat(self):
        """Liveness: tell the receiver the latest version of every field.

        Tests 1 and 2 never suppress a field's newest value, but if the NACK for
        that newest value is lost, or the field stops updating, no sequence gap
        will ever reveal that the receiver is behind. The digest does.
        """
        digest = {fid: (version, b"") for fid, version in self.send_log.digest()}
        as_of = (self.next_seq - 1) % (1 << config.SEQ_BITS)   # last DATA seq sent
        wire = header.pack(header.Packet(header.SKIP, as_of, header.bitmap_of(digest),
                                         header.HB_MARK, digest))
        self.stats["heartbeats_sent"] += 1
        self.stats["heartbeat_bytes"] += len(wire)
        self._send_ctrl(wire)
        if self.now + self.heartbeat_s <= self.duration_s:
            self._push(self.now + self.heartbeat_s, HEARTBEAT)

    def _on_heartbeat_request(self, request):
        """The receiver says it is behind on these fields, as of digest seq `request.seq`."""
        resend = {}
        for fid in header.fields_in(request.bitmap):
            if header.seq_newer(self.send_log.last_seq[fid], request.seq):
                continue    # a newer copy went out after the digest; it is already on its way
            resend[fid] = (self.send_log.last_version[fid], self.send_log.last_value[fid])
        if resend:
            wire = header.pack(header.Packet(header.REPAIR, request.seq, header.bitmap_of(resend),
                                             header.HB_MARK, resend))
            self.stats["heartbeat_bytes"] += len(wire)
            self._send_ctrl(wire)

    # --- receiver -------------------------------------------------------------

    def _on_forward(self, wire):
        pkt = header.unpack(wire)
        if header.is_parity(pkt.type, pkt.bitmap, pkt.fec):
            self._on_parity(pkt)
            return
        if self.policy == "fec" and pkt.type == header.DATA:
            self.fec_received[pkt.seq] = (self.now, wire)
        if pkt.type == header.SKIP and pkt.fec == header.HB_MARK:
            self._on_digest(pkt)
            return
        if pkt.type == header.SKIP:
            self._log(pkt.seq, "SKIP", "settled with 0 payload bytes")
            return
        applied_ids, obsolete_ids = [], []
        for fid, (version, value) in pkt.updates.items():
            if self.receiver.apply(fid, version, value):
                applied_ids.append(fid)
                self.deliveries.append((self.now, fid, version, len(value)))
                if (fid, version) in self.gen_time:     # always, unless sender is another process
                    self.metrics.on_delivered(fid, self.now, self.gen_time[(fid, version)], len(value))
            else:
                obsolete_ids.append(fid)
        if pkt.type == header.REPAIR and pkt.fec == header.HB_MARK:
            self.stats["hb_fields_reconciled"] += len(applied_ids)
            if applied_ids:
                self._log(None, "HEARTBEAT", f"reconciled [{names(applied_ids)}] - "
                                             "staleness no sequence gap would have revealed")
        elif pkt.type == header.REPAIR:
            self.stats["repair_fields_applied"] += len(applied_ids)
            self.stats["repair_fields_obsolete"] += len(obsolete_ids)
            self.stats["repair_bytes_obsolete"] += sum(schema.by_id(f).width for f in obsolete_ids)
            text = f"recovered [{names(applied_ids)}]" if applied_ids else "nothing new"
            if obsolete_ids:
                text += f"; already had newer [{names(obsolete_ids)}]"
            self._log(pkt.seq, "REPAIR", text)
        elif self.policy in NACKING:
            for seq in self.gaps.on_data(pkt.seq, pkt.updates):
                self._log(seq, "GAP", f"receiver detects seq {seq} never arrived")
                self._push(self.now + self.hold_down_s, TIMER, seq)

    def _on_digest(self, digest):
        behind = self.receiver.missing_from((fid, v) for fid, (v, _) in digest.updates.items())
        if behind:
            wire = header.pack(header.Packet(header.NACK, digest.seq, header.bitmap_of(behind),
                                             header.HB_MARK))
            self.stats["hb_requests"] += 1
            self.stats["nack_bytes"] += len(wire)
            self._send_rev(wire)

    def _on_timer(self, seq):
        candidates = self.gaps.hold_down_expired(seq)
        if candidates is None:
            return
        wire = header.pack(header.Packet(header.NACK, seq, header.bitmap_of(candidates)))
        self.stats["nacks_sent"] += 1
        self.stats["nack_bytes"] += len(wire)
        if self._send_rev(wire):
            self.stats["nacks_lost"] += 1
            self._log(seq, "NACK_LOST", f"NACK for seq {seq} lost on the reverse path")
        else:
            self._log(seq, "NACK", f"asking sender about seq {seq} ({len(candidates)} candidate fields)")

    # --- checks ---------------------------------------------------------------

    def stale_fields(self):
        """Fields where the receiver ended up behind the sender."""
        return [fid for fid, (v, val) in self.latest_sent.items()
                if self.receiver.version.get(fid) != v or self.receiver.value.get(fid) != val]

    def replicated(self):
        return not self.stale_fields()
