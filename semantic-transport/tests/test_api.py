import time

import semstate

FIELDS = [
    semstate.field("temperature", "float", every=0.02, deadline=0.1),
    semstate.field("count", "int", every=0.02, deadline=0.1),
    semstate.field("alarm", "str", size=16, deadline=0.5),        # event-driven, critical
]


def wait_for(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_values_set_on_one_end_arrive_on_the_other_over_a_lossy_link():
    # 20% emulated loss in both directions: the one-off alarm must still arrive
    # (repair or heartbeat), and the streamed values must end on the newest one.
    with semstate.Receiver(FIELDS, bind="127.0.0.1:47302", peer="127.0.0.1:47301", loss=0.2) as rx, \
         semstate.Sender(FIELDS, bind="127.0.0.1:47301", peer="127.0.0.1:47302", loss=0.2) as tx:
        tx.set("alarm", "PUMP 2 OVERHEAT")
        for i in range(100):
            tx.set("temperature", 20.0 + i / 10)
            tx.set("count", i)
            time.sleep(0.01)
        assert wait_for(lambda: rx.get("count") == 99), rx.snapshot()
        assert wait_for(lambda: rx.get("alarm") == "PUMP 2 OVERHEAT"), rx.snapshot()
        assert abs(rx.get("temperature") - 29.9) < 1e-9
        assert tx.stats["data_sent"] > 0
