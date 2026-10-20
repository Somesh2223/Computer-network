"""Measurement: bytes, freshness (Age of Information), latency, decision cost.

Everything is kept in memory and only written out at teardown (flush_csv):
writing to disk inside the receive path would contaminate the latency figures.
"""

import bisect
import csv
from collections import deque

from src.header import seq_newer

RING_SIZE = 200_000      # most recent event rows kept for the CSV trace


class Metrics:
    def __init__(self, field_ids, warmup_s, end_s):
        self.warmup_s, self.end_s = warmup_s, end_s
        self.ring = deque(maxlen=RING_SIZE)    # (time, event, field id, value)

        self.wire_bytes = 0              # every packet put on the wire, any direction, lost or not
        self.useful_payload_bytes = 0    # value bytes that actually advanced the receiver's state
        self.fresh_latency_s = []        # generation -> available at receiver, per applied value
        self.decision_times_us = []      # CPU time of each supersession decision

        # Age of Information per field. Every field starts with a value generated
        # at t = 0 (the session handshake), so age is defined from the start.
        self.gen = {fid: 0.0 for fid in field_ids}      # generation time of the value held
        self.last_t = {fid: 0.0 for fid in field_ids}   # integrated up to here
        self.area = {fid: 0.0 for fid in field_ids}     # integral of age over [warmup, end]
        self.peak = {fid: 0.0 for fid in field_ids}

    def on_wire(self, nbytes, kind, now):
        self.wire_bytes += nbytes
        self.ring.append((now, kind, -1, nbytes))

    def on_decision(self, micros):
        self.decision_times_us.append(micros)

    def _integrate(self, fid, t):
        # Age grows linearly between deliveries: area of a trapezoid, clipped to
        # the measured window so start-up transients don't dominate the average.
        a, b = max(self.last_t[fid], self.warmup_s), min(t, self.end_s)
        if b > a:
            g = self.gen[fid]
            self.area[fid] += ((a - g) + (b - g)) / 2 * (b - a)
            self.peak[fid] = max(self.peak[fid], b - g)
        self.last_t[fid] = max(self.last_t[fid], t)

    def on_delivered(self, fid, now, gen_time, nbytes):
        """The receiver applied a newer value of fid, generated at gen_time."""
        self._integrate(fid, now)
        self.gen[fid] = gen_time
        self.useful_payload_bytes += nbytes
        if self.warmup_s <= now <= self.end_s:
            self.fresh_latency_s.append(now - gen_time)
        self.ring.append((now, "delivered", fid, now - gen_time))

    def finish(self):
        for fid in self.gen:
            self._integrate(fid, self.end_s)

    # --- derived ------------------------------------------------------------

    def avg_aoi(self, fid):
        return self.area[fid] / (self.end_s - self.warmup_s)

    def mean_aoi_all(self):
        return sum(self.avg_aoi(fid) for fid in self.gen) / len(self.gen)

    def useful_delivery_efficiency(self):
        return self.useful_payload_bytes / self.wire_bytes if self.wire_bytes else 0.0

    def total_overhead_ratio(self):
        """(headers + control + repair + redundant bytes) / useful payload bytes."""
        return (self.wire_bytes - self.useful_payload_bytes) / self.useful_payload_bytes

    def latency_percentile(self, q):
        return _percentile(self.fresh_latency_s, q)

    def mean_decision_us(self):
        return sum(self.decision_times_us) / len(self.decision_times_us) if self.decision_times_us else 0.0

    def p99_decision_us(self):
        return _percentile(self.decision_times_us, 99)

    def flush_csv(self, path):
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_s", "event", "field_id", "value"])
            w.writerows(self.ring)


def replay(field_ids, generated, deliveries, warmup_s, end_s, wire_bytes):
    """Freshness from two separate logs, for when sender and receiver are separate
    processes (udp_driver.py). Both logs use the same monotonic clock.

    generated:  (time, field id, version) at the sender
    deliveries: (time, field id, version, bytes) applied at the receiver
    Versions wrap every 256 updates, so a delivery is matched to the latest
    generation of that (field, version) at or before it arrived.
    """
    born = {}
    for t, fid, version in generated:
        born.setdefault((fid, version), []).append(t)
    m = Metrics(field_ids, warmup_s, end_s)
    m.wire_bytes = wire_bytes
    for t, fid, version, nbytes in sorted(deliveries):
        times = born.get((fid, version), [])
        i = bisect.bisect_right(times, t) - 1
        if i >= 0:
            m.on_delivered(fid, t, times[i], nbytes)
    m.finish()
    return m


def on_time_rate(fields, generated, deliveries, gen_time, warmup_s, end_s):
    """Share of critical-field updates delivered within their deadline (see on_time_counts)."""
    on_time, scored = on_time_counts(fields, generated, deliveries, gen_time, warmup_s, end_s)
    return on_time / scored if scored else 1.0


def on_time_counts(fields, generated, deliveries, gen_time, warmup_s, end_s):
    """(delivered on time, scored) for critical-field updates: the receiver had (or had something newer
    than) within the field's deadline D_f. Review 1's critical-field recovery rate.

    fields:     the critical Field objects to score
    generated:  (time, field id, version) at the sender
    deliveries: (time, field id, version, bytes) applied at the receiver
    gen_time:   (field id, version) -> generation time
    """
    arrivals = {}            # field id -> [(generation time of the value, when it arrived)]
    for t, fid, version, _ in deliveries:
        arrivals.setdefault(fid, []).append((gen_time[(fid, version)], t))
    scored = on_time = 0
    for f in fields:
        got = sorted(arrivals.get(f.id, []))
        for g, fid, _ in generated:
            if fid != f.id or not warmup_s <= g <= end_s - f.deadline:
                continue
            scored += 1
            # the first arrival of this value or any newer one
            i = bisect.bisect_left(got, (g, -1.0))
            first = min((t for _, t in got[i:]), default=None)
            if first is not None and first - g <= f.deadline:
                on_time += 1
    return on_time, scored


def _percentile(samples, q):
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(round(q / 100 * (len(ordered) - 1))))]


def verify_no_false_suppression(events, transmissions):
    """Count SUPERSEDED verdicts that the raw transmission history does not back up.

    events:        (time, lost seq, field id, resent seq) - SUPERSEDED decisions ONLY.
                   Tolerance suppressions must not be passed in: they legitimately
                   apply to fields that were never re-sent, so auditing them against
                   this rule would report false violations.
    transmissions: (time, seq, field ids) for every DATA packet the sender sent.

    A suppression of field f for lost seq N is legitimate only if f really went out
    again in a newer DATA packet before the decision. This re-derives that from the
    raw history rather than the send log; checking the log against itself proves nothing.
    """
    sent = {}
    for t, seq, fids in transmissions:
        for fid in fids:
            sent.setdefault(fid, []).append((t, seq))
    false = 0
    for t, lost_seq, fid, _ in events:
        if not any(ts <= t and seq_newer(s, lost_seq) for ts, s in sent.get(fid, [])):
            false += 1
    return false
