"""Produces new field values at each field's own rate (the drone's sensors)."""

import random

from src import schema


class Generator:
    def __init__(self, rng, fields=None):
        self.rng = rng
        self.fields = fields if fields is not None else schema.active()
        self.version = {f.id: 0 for f in self.fields}   # 8-bit, wraps at 256
        self.next_fire = {}
        for f in self.fields:
            # Stagger the first firing so every field doesn't land on tick 0.
            if f.period is not None:
                self.next_fire[f.id] = rng.uniform(0, f.period)
            else:
                self.next_fire[f.id] = rng.expovariate(1.0 / f.mean_gap)

    def due(self, now):
        """Fields that fired up to `now`, as {field id: (version, value)}.

        If a field fired twice since the last call, only the newest value is
        kept: the older one is already obsolete, which is the whole point.
        """
        updates = {}
        for f in self.fields:
            while self.next_fire[f.id] <= now:
                self.version[f.id] = (self.version[f.id] + 1) % 256
                updates[f.id] = (self.version[f.id], self.rng.randbytes(f.width))
                if f.period is not None:
                    self.next_fire[f.id] += f.period
                else:
                    self.next_fire[f.id] += self.rng.expovariate(1.0 / f.mean_gap)
        return updates
