"""THE DECISION ENGINE: is a lost value still worth recovering?

When a NACK for packet N arrives, look at each field that N carried (and that
the receiver still asks about) and ask two questions:

  TEST 1 - has this field been sent again, in a packet newer than N?
           If so, the lost copy is obsolete: SUPERSEDED, don't repair it.
  TEST 2 - does this field update often enough that the application will get
           a fresh value within its deadline anyway? WITHIN_TOLERANCE, don't.

Fields that survive both are repaired. If none survive, the sender replies
with a SKIP (header only, 0 payload bytes) so the receiver can close the gap.

Why Test 1 is enough to enforce the crossover, without ever computing it:

  A NACK reaches the sender exactly tau + H + R after the loss (tick + hold-down
  + round trip). The analytical model (crossover.py) derives the crossover - the
  update period above which a repair can still beat the next natural update -
  and gets the same quantity. So any field whose next value was due sooner has
  ALREADY been re-sent by the time the NACK lands, and Test 1 catches it. The
  send-log test enforces the analytical crossover by direct observation, never
  by estimating a rate. That is why it cannot produce a false suppression, and
  why the implementation never computes T* at runtime.

Test 2 is a genuinely separate filter asking whether the application even
cares, given D_f. Combined rule: repair f only if T_f > max(tau + H + R, D_f).
"""

from dataclasses import dataclass, field
from enum import Enum

from src import schema
from src.header import seq_newer


class Outcome(Enum):
    SUPERSEDED = "SUPERSEDED"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    REPAIR = "REPAIR"


@dataclass
class FieldDecision:
    field_id: int
    outcome: Outcome
    detail: str              # printed verbatim by demo.py, so written for a reader
    resent_seq: int | None = None


@dataclass
class Decision:
    seq: int
    fields: list = field(default_factory=list)   # FieldDecision, ascending field id

    def live(self):
        return [d.field_id for d in self.fields if d.outcome is Outcome.REPAIR]

    def reply_is_skip(self):
        return not self.live()


def _ms(seconds):
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def decide(seq, mask, send_log):
    """Adjudicate a NACK for DATA packet `seq`, given the receiver's candidate mask."""
    decision = Decision(seq)
    contents = send_log.contents_of(seq)
    if contents is None:          # too old to remember; nothing we could resend anyway
        return decision
    # The receiver guesses (every field it hasn't struck off); the send log knows.
    requested = [fid for fid in sorted(contents) if mask & (1 << fid)]
    for fid in requested:
        f = schema.by_id(fid)
        last = send_log.last_seq[fid]
        interval = send_log.ewma_interval.get(fid)    # None until sent twice
        if seq_newer(last, seq):                                           # TEST 1
            decision.fields.append(FieldDecision(
                fid, Outcome.SUPERSEDED,
                f"re-sent at seq {last} > {seq}; lost copy is obsolete ({f.width} B saved)",
                resent_seq=last))
        elif interval is not None and interval <= f.deadline:              # TEST 2
            decision.fields.append(FieldDecision(
                fid, Outcome.WITHIN_TOLERANCE,
                f"updates every ~{_ms(interval)}, within its {_ms(f.deadline)} tolerance; "
                f"the next value will do ({f.width} B saved)"))
        else:
            rate = f"updates every ~{_ms(interval)}" if interval is not None else "no update rate yet"
            decision.fields.append(FieldDecision(
                fid, Outcome.REPAIR,
                f"not re-sent since seq {seq}; {rate}, beyond its {_ms(f.deadline)} "
                f"tolerance ({f.width} B to resend)"))
    return decision
