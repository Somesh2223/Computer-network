"""Measurement. (Phase 5 has only the safety audit; the rest arrives in Phase 6.)"""

from src.header import seq_newer


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
