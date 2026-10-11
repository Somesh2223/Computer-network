"""Receiver side: notice that a packet never arrived, then wait before asking.

A gap shows up only when a LATER packet arrives, so detection costs about half
an RTT after the loss. Hold-down gives a reordered packet a moment to show up
before we bother the sender with a NACK.
"""

from src.header import seq_newer

SEQ_SPACE = 1 << 14


class GapDetector:
    def __init__(self):
        self.expected = None     # the next DATA seq we expect to see
        self.pending = set()     # seqs that are missing and still unresolved

    def on_data(self, seq):
        """Process a DATA arrival. Returns the seqs newly found missing (may be empty)."""
        if self.expected is None:             # first packet ever: nothing to compare with
            self.expected = (seq + 1) % SEQ_SPACE
            return []
        if seq == self.expected:
            self.expected = (seq + 1) % SEQ_SPACE
            return []
        if seq_newer(seq, self.expected):     # skipped ahead: everything in between is missing
            hole = (seq - self.expected) % SEQ_SPACE
            missing = [(self.expected + i) % SEQ_SPACE for i in range(hole)]
            self.pending.update(missing)
            self.expected = (seq + 1) % SEQ_SPACE
            return missing
        self.pending.discard(seq)             # a late original turned up: no longer missing
        return []

    def hold_down_expired(self, seq):
        """Timer fired for seq. True if it is still missing, so a NACK should go out."""
        if seq in self.pending:
            self.pending.discard(seq)         # one NACK per gap; liveness is the heartbeat's job
            return True
        return False
