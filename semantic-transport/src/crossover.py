"""The analytical model (Review 1 §8.6), derived before any measurement.

A packet carrying field f is lost at t = 0. Two events race to refresh the
receiver's copy:

  repair path   gap seen at tau + R/2, hold-down to tau + R/2 + H,
                NACK at sender tau + R + H, repair lands at tau + H + 1.5R
  natural path  next update of f lands at T_f + R/2

  repair wins iff  tau + H + 1.5R < T_f + R/2   <=>   T_f > tau + H + R = T*

Two consequences:
 (a) A single periodic field can never benefit from gap-based repair: detection
     needs a later packet, and if that packet carries the same field the
     receiver already has something fresher. Repair only has a job because
     fields share a flow.
 (b) This assumes independent (i.i.d.) loss. Under bursts the detecting packet
     is often lost too, so the crossover shifts right. We measure that rather
     than chase a closed form.

The running system never calls this: the send-log test in supersession.py
enforces the same boundary by observation. This is for the demo banner and
for checking experiments against the prediction.
"""


def crossover(tick_s, hold_down_s, rtt_s):
    """Minimum update period above which a repair can still be useful."""
    return tick_s + hold_down_s + rtt_s


def expected_gain(period_s, tick_s, hold_down_s, rtt_s, loss_rate):
    """(1-p) * max(0, T_f - T*).  Zero below the crossover."""
    return (1 - loss_rate) * max(0.0, period_s - crossover(tick_s, hold_down_s, rtt_s))
