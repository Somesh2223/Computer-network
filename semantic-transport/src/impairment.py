"""Packet loss on the simulated link.

Both models draw exactly one random number per packet, even at 0% loss, so a
channel consumes the same random numbers however lossy it is.
"""


class IIDLoss:
    """Each packet is dropped independently with probability loss_rate."""

    def __init__(self, rng, loss_rate):
        self.rng = rng
        self.loss_rate = loss_rate

    def drops(self):
        return self.rng.random() < self.loss_rate


class GilbertElliottLoss:
    """Burst loss: the link flips between a GOOD state (delivers) and a BAD state (drops).

    Parameterised so the long-run fraction of time spent BAD equals loss_rate,
    and a BAD spell lasts mean_burst_len packets on average.
    """

    def __init__(self, rng, loss_rate, mean_burst_len):
        self.rng = rng
        self.p_bg = 1.0 / mean_burst_len                          # BAD -> GOOD
        self.p_gb = (loss_rate * self.p_bg) / (1.0 - loss_rate)   # GOOD -> BAD
        self.bad = False

    def drops(self):
        # Advance the chain once per packet, then the packet is lost if we're BAD.
        if self.bad:
            self.bad = self.rng.random() >= self.p_bg
        else:
            self.bad = self.rng.random() < self.p_gb
        return self.bad


class BitErrors:
    """Bit corruption: each bit flips independently with probability ber.

    Draws exactly one random number per packet (plus one more when it is hit),
    and flips one bit in a hit packet. At the low error rates of a real link a
    second flip in the same packet is far rarer than the first.
    """

    def __init__(self, rng, ber):
        self.rng = rng
        self.ber = ber

    def apply(self, wire):
        """The packet as it arrives: unchanged, or with one bit flipped."""
        hit = 1.0 - (1.0 - self.ber) ** (8 * len(wire))
        if self.rng.random() >= hit:
            return wire
        bit = self.rng.randrange(8 * len(wire))
        damaged = bytearray(wire)
        damaged[bit // 8] ^= 1 << (bit % 8)
        return bytes(damaged)


def udp_checksum(data):
    """The 16-bit ones'-complement checksum every UDP datagram carries (RFC 768, 1071).

    The receiving kernel recomputes it and silently drops a datagram that doesn't
    match. It catches every single-bit error, so to our protocol a corrupted
    packet looks exactly like a lost one. (The real checksum also covers the IP
    addresses; leaving them out changes nothing about which flips it catches.)
    """
    if len(data) % 2:
        data += b"\0"
    total = sum(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def make_loss(rng, loss_rate, burst, mean_burst_len):
    if burst:
        return GilbertElliottLoss(rng, loss_rate, mean_burst_len)
    return IIDLoss(rng, loss_rate)
