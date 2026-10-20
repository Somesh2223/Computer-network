"""The fields of the example: a pumping station reporting to a control room.

Both programs import this list, because both ends must declare the same fields
in the same order. Change it to describe your own system.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semstate  # noqa: E402

FIELDS = [
    # fast readings: a lost one is replaced 50 ms later, so never worth repairing
    semstate.field("flow_lpm", "float", every=0.05, deadline=0.2),
    semstate.field("pressure_bar", "float", every=0.05, deadline=0.2),
    # slower reading: within tolerance if its next update comes soon enough
    semstate.field("tank_level_pct", "float", every=1.0, deadline=3.0),
    # rare events: the control room must see these, so a loss is repaired
    semstate.field("pump_state", "str", size=8, deadline=0.5),
    semstate.field("alarm", "str", size=32, deadline=0.5),
]
