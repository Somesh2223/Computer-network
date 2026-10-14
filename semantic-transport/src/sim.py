"""Discrete-event harness: a simulated clock and link around the real code.

Every packet really goes through header.pack() / unpack(); only time and the
network are pretend. Events live in a heap of (time, tie, kind, data).

Policies, all run against the same data-loss trace:
  "ours"  per-field supersession + tolerance, plus the liveness heartbeat
  "arq"   B1 - the sender resends EVERY field of a lost packet, in full
  "udp"   B4 - no recovery at all

Three independent random channels, so every policy sees the same data losses:
  fwd_data  one draw per DATA packet, nothing else
  fwd_ctrl  repairs, skips and heartbeats
  rev       NACKs (gap NACKs and heartbeat requests)
If repairs shared the data channel, a policy that sends more repairs would also
get a different loss pattern, and the comparison would be worthless.
"""

import heapq
import random

import config
from src import header, schema, supersession
from src.gap_detector import GapDetector
from src.generator import Generator
from src.impairment import make_loss
from src.send_log import SendLog
from src.state_table import StateTable

TICK, FWD, TIMER, REV, HEARTBEAT = "TICK", "FWD", "TIMER", "REV", "HEARTBEAT"


def names(field_ids):
    return ", ".join(schema.by_id(fid).name for fid in sorted(field_ids))


class Simulation:
    def __init__(self, seed, policy="ours", loss_rate=config.LOSS_RATE,
                 duration_s=config.DURATION_S, tick_s=config.TICK_S,
                 hold_down_s=config.HOLD_DOWN_S, rtt_s=config.RTT_S,
                 jitter_s=config.JITTER_S, burst=config.BURST,
                 mean_burst_len=config.MEAN_BURST_LEN,
                 heartbeat_s=config.HEARTBEAT_S, trace=False):
        self.policy = policy
        self.duration_s, self.tick_s = duration_s, tick_s
        self.hold_down_s, self.rtt_s, self.jitter_s = hold_down_s, rtt_s, jitter_s
        self.heartbeat_s = heartbeat_s

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

        self.stats = dict(data_sent=0, data_lost=0, data_bytes=0,
                          nacks_sent=0, nacks_lost=0, nack_bytes=0,
                          repairs_sent=0, repairs_lost=0, repair_bytes=0,
                          skips_sent=0, skips_lost=0, skip_bytes=0,
                          fields_superseded=0, bytes_superseded=0,
                          fields_tolerated=0, bytes_tolerated=0, fields_repaired=0,
                          repair_fields_applied=0, repair_fields_obsolete=0,
                          repair_bytes_obsolete=0,
                          heartbeats_sent=0, heartbeat_bytes=0, hb_requests=0,
                          hb_fields_reconciled=0)

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

    def run(self):
        self._push(0.0, TICK)
        if self.policy == "ours":
            self._push(self.heartbeat_s, HEARTBEAT)
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
            elif kind == HEARTBEAT:
                self._on_heartbeat()
        return self

    # --- links ----------------------------------------------------------------

    def _send_ctrl(self, wire):
        """Forward control channel (repairs, skips, heartbeats). Returns True if lost."""
        lost = self.ctrl_loss.drops()
        delay = self.rtt_s / 2 + self.fwd_ctrl.uniform(0, self.jitter_s)
        if not lost:
            self._push(self.now + delay, FWD, wire)
        return lost

    def _send_rev(self, wire):
        """Reverse channel (NACKs). Returns True if lost."""
        lost = self.rev_loss.drops()
        delay = self.rtt_s / 2 + self.rev.uniform(0, self.jitter_s)
        if not lost:
            self._push(self.now + delay, REV, wire)
        return lost

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
        self.send_log.record(seq, updates, self.now)
        self.transmissions.append((self.now, seq, frozenset(updates)))
        wire = header.pack(header.Packet(header.DATA, seq, header.bitmap_of(updates), 0, updates))
        self.stats["data_sent"] += 1
        self.stats["data_bytes"] += len(wire)
        lost = self.data_loss.drops()
        delay = self.rtt_s / 2 + self.data_jitter.uniform(0, self.jitter_s)
        if lost:
            self.stats["data_lost"] += 1
            self._log(seq, "LOST", f"seq {seq} carrying [{names(updates)}]")
        else:
            self._push(self.now + delay, FWD, wire)

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

        decision = supersession.decide(nack.seq, nack.bitmap, self.send_log)
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
        elif self.policy != "udp":
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
