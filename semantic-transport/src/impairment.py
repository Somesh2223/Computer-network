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


def make_loss(rng, loss_rate, burst, mean_burst_len):
    if burst:
        return GilbertElliottLoss(rng, loss_rate, mean_burst_len)
    return IIDLoss(rng, loss_rate)
