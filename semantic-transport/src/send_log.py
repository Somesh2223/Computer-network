"""The sender's memory: what it sent, and when each field was last sent.

This is the authoritative record the supersession engine consults. The
receiver never saw the lost packet, so only the sender knows what was in it.
"""

import config

KEEP_PACKETS = 1024      # recent packets kept for repairs (about 10 s at one packet per tick)


class SendLog:
    def __init__(self, keep=KEEP_PACKETS, alpha=config.EWMA_ALPHA):
        self.keep = keep
        self.alpha = alpha
        self.last_seq = {}           # field id -> seq of the DATA packet that last carried it
        self.last_version = {}       # field id -> version sent in that packet
        self.last_value = {}         # field id -> value sent in that packet (for heartbeat repairs)
        self.last_time = {}          # field id -> when it was last sent
        self.ewma_interval = {}      # field id -> smoothed gap between sends (absent until 2 sends)
        self.packet_contents = {}    # seq -> {field id: (version, value)}

    def record(self, seq, updates, now):
        """Note a DATA packet. Repairs are NOT recorded: they reuse the lost seq."""
        for fid, (version, value) in updates.items():
            if fid in self.last_time:
                gap = now - self.last_time[fid]
                old = self.ewma_interval.get(fid)
                # EWMA: mostly the old estimate, nudged towards the newest gap, so one
                # odd gap (e.g. an event field firing twice quickly) doesn't swing it.
                self.ewma_interval[fid] = gap if old is None else self.alpha * gap + (1 - self.alpha) * old
            self.last_time[fid] = now
            self.last_seq[fid] = seq
            self.last_version[fid] = version
            self.last_value[fid] = value
        self.packet_contents[seq] = updates
        # Prune in bulk once the table is twice its target size. Pruning on every
        # packet is O(n) each time, which makes a long run O(n^2).
        if len(self.packet_contents) > self.keep * 2:
            newest = list(self.packet_contents.items())[-self.keep:]
            self.packet_contents = dict(newest)

    def contents_of(self, seq):
        """{field id: (version, value)} carried by DATA packet seq, or None if forgotten."""
        return self.packet_contents.get(seq)

    def digest(self):
        """Heartbeat content: (field id, latest version) for every field sent so far."""
        return sorted(self.last_version.items())
