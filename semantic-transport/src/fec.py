"""Forward error correction: the "error protection" half of the project title.

Two tools, used by two different policies:

 * Selective repetition (policy "ours_fec", Review 1 §8.4). A small CRITICAL
   field - one the app cannot wait for, and which may not be sent again soon -
   rides again in k later data packets after it changes, as RTP's RFC 2198
   does for audio. A loss is then covered a few ticks later, with no round trip.
   Big critical fields (terrain_map) are left to the reactive repair: repeating
   1480 bytes k times would cost more than it saves.

 * Blanket XOR parity (baseline B2). After every group of n data packets the
   sender sends one parity packet, the XOR of the group. Any ONE lost packet in
   the group can be rebuilt from the others plus the parity. It protects every
   field alike, which is exactly what B2 is meant to test.
"""


def is_critical(field):
    """The app can't wait for the next natural update (or there may never be one)."""
    return field.period is None or field.deadline < field.period


def protected_fields(fields, max_width):
    """Fields that get proactive repetition: critical AND small."""
    return {f.id for f in fields if is_critical(f) and f.width <= max_width}


class Repeater:
    """Adds repeat copies of recently changed protected fields to outgoing packets.

    Copies go out every `spacing` packets rather than in the very next ones:
    losses come in bursts, and a copy sent straight after the original tends to
    fall into the same burst (time diversity, as in interleaved FEC).
    """

    def __init__(self, protected, k, spacing):
        self.protected, self.k, self.spacing = protected, k, spacing
        self.owed = {}           # field id -> [copies still owed, packets until the next one]

    def add(self, updates, latest):
        """updates: this tick's new values. latest: field id -> (version, value) last sent."""
        out = dict(updates)
        for fid in list(self.owed):
            if fid in updates:
                del self.owed[fid]           # a newer value replaces the one being repeated
                continue
            self.owed[fid][1] -= 1
            if self.owed[fid][1] == 0:
                out[fid] = latest[fid]       # same version, same value: a repeat, not news
                self.owed[fid] = [self.owed[fid][0] - 1, self.spacing]
                if self.owed[fid][0] == 0:
                    del self.owed[fid]
        for fid in updates:
            if fid in self.protected and self.k > 0:
                self.owed[fid] = [self.k, self.spacing]
        return out


def parity_of(wires):
    """XOR of the packets (zero-padded to the longest), prefixed by the XOR of their lengths."""
    width = max(len(w) for w in wires)
    lengths, acc = 0, 0
    for w in wires:
        lengths ^= len(w)
        acc ^= int.from_bytes(w.ljust(width, b"\0"), "big")
    return lengths.to_bytes(2, "big") + acc.to_bytes(width, "big")


def recover(parity, present):
    """Rebuild the one missing packet from the parity and the n-1 that arrived."""
    width = len(parity) - 2
    lengths, acc = int.from_bytes(parity[:2], "big"), int.from_bytes(parity[2:], "big")
    for w in present:
        lengths ^= len(w)
        acc ^= int.from_bytes(w.ljust(width, b"\0"), "big")
    return acc.to_bytes(width, "big")[:lengths]
