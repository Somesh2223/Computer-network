"""Packet loss on the simulated link."""


class IIDLoss:
    """Each packet is dropped independently with probability loss_rate.

    The RNG is advanced once per packet even when loss_rate is 0, so a channel
    consumes the same random numbers however lossy it is.
    """

    def __init__(self, rng, loss_rate):
        self.rng = rng
        self.loss_rate = loss_rate

    def drops(self):
        return self.rng.random() < self.loss_rate
