"""Receiver side: notice that a packet never arrived, then wait before asking.

A gap shows up only when a LATER packet arrives, so detection costs about half
an RTT after the loss. Hold-down gives a reordered packet a moment to show up
before we bother the sender with a NACK.

The receiver never saw the lost packet, so it cannot know which fields it held.
Each pending gap therefore starts with EVERY field as a candidate. During
hold-down, any field that arrives in a newer packet is struck off (the FAST
PATH): the receiver now holds something fresher than the lost packet could have
held. The NACK carries whatever candidates remain.
"""

from src.header import seq_newer

SEQ_SPACE = 1 << 14


class GapDetector:
    def __init__(self, all_field_ids):
        self.all_field_ids = set(all_field_ids)
        self.expected = None     # the next DATA seq we expect to see
        self.pending = {}        # missing seq -> set of candidate field ids
        self.fast_path_strikes = 0

    def on_data(self, seq, field_ids):
        """Process a DATA arrival. Returns the seqs newly found missing (may be empty)."""
        missing = []
        if self.expected is None:             # first packet ever: nothing to compare with
            self.expected = (seq + 1) % SEQ_SPACE
        elif seq == self.expected:
            self.expected = (seq + 1) % SEQ_SPACE
        elif seq_newer(seq, self.expected):   # skipped ahead: everything in between is missing
            hole = (seq - self.expected) % SEQ_SPACE
            missing = [(self.expected + i) % SEQ_SPACE for i in range(hole)]
            for m in missing:
                self.pending[m] = set(self.all_field_ids)
            self.expected = (seq + 1) % SEQ_SPACE
        else:
            self.pending.pop(seq, None)       # a late original turned up: no longer missing

        # Fast path: this packet is newer than any gap below it, so its fields
        # are fresher than whatever those lost packets carried.
        for gap_seq, candidates in self.pending.items():
            if seq_newer(seq, gap_seq):
                struck = candidates & set(field_ids)
                candidates -= struck
                self.fast_path_strikes += len(struck)
        return missing

    def hold_down_expired(self, seq):
        """Timer fired for seq. Returns the remaining candidates if a NACK should go
        out, or None if the gap already closed. One NACK per gap; liveness is the
        heartbeat's job (Phase 5)."""
        return self.pending.pop(seq, None)
