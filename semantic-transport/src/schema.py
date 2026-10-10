"""The field table: what state is replicated, how big, how often, how fresh.

Both ends agree on this table at session setup, so widths and deadlines never
travel inside a packet. That is what keeps the header at 5 bytes.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    id: int                  # 0..15, indexes one bit of the update bitmap
    name: str
    width: int               # payload bytes
    period: float | None     # seconds between updates; None = event-driven
    deadline: float          # D_f, staleness the application tolerates
    mean_gap: float = 0.0    # event-driven only: mean seconds between events


# Critical fields (mode, terrain_map, fault_code, waypoint) have deadlines
# SHORTER than their period: the application cannot wait for the next natural
# update, so a lost copy must be repaired.
DEFAULT_FIELDS = (
    Field(0, "attitude",    4,    0.010, 0.020),   # fast, below T*
    Field(1, "velocity",    4,    0.020, 0.050),   # fast, below T*
    Field(2, "lidar_scan",  900,  0.050, 0.100),   # big AND fast: the expensive mistake
    Field(3, "gps",         8,    0.100, 0.300),   # tolerance-suppressed
    Field(4, "battery",     2,    1.0,   2.0),     # tolerance-suppressed
    Field(5, "mode",        1,    5.0,   1.0),     # critical
    Field(6, "terrain_map", 1480, 10.0,  2.0),     # critical, bulky
    Field(7, "fault_code",  2,    None,  0.5, mean_gap=8.0),    # critical, event-driven
    Field(8, "waypoint",    8,    None,  1.0, mean_gap=12.0),   # critical, event-driven
)

_active = DEFAULT_FIELDS


def use(fields):
    """Swap the active schema (the real session handshake; sweeps need it too)."""
    global _active
    ids = [f.id for f in fields]
    if len(set(ids)) != len(ids) or any(not 0 <= i <= 15 for i in ids):
        raise ValueError("field ids must be unique and in 0..15")
    _active = tuple(fields)


def active():
    return _active


def by_id(field_id):
    for f in _active:
        if f.id == field_id:
            return f
    raise KeyError(f"no field with id {field_id}")


def total_state_bytes():
    return sum(f.width for f in _active)
