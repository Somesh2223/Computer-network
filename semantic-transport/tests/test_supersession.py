import random

import config
from src import schema
from src.crossover import crossover
from src.metrics import verify_no_false_suppression
from src.send_log import SendLog
from src.sim import Simulation
from src.supersession import Outcome, decide

ATTITUDE, GPS, TERRAIN = 0, 3, 6
ALL = (1 << 16) - 1                  # receiver still asks about every field


def value(fid):
    return bytes(schema.by_id(fid).width)


def outcomes(decision):
    return {d.field_id: d.outcome for d in decision.fields}


def test_resent_field_is_superseded_and_reply_is_skip():
    log = SendLog()
    log.record(10, {ATTITUDE: (1, value(ATTITUDE))}, now=0.00)
    log.record(11, {ATTITUDE: (2, value(ATTITUDE))}, now=0.01)
    d = decide(10, ALL, log)
    assert outcomes(d) == {ATTITUDE: Outcome.SUPERSEDED}
    assert d.reply_is_skip()


def test_field_not_resent_is_repaired():
    log = SendLog()
    log.record(5, {TERRAIN: (1, value(TERRAIN))}, now=0.0)
    log.record(900, {TERRAIN: (2, value(TERRAIN))}, now=10.0)   # updates every 10 s, deadline 2 s
    d = decide(900, ALL, log)
    assert outcomes(d) == {TERRAIN: Outcome.REPAIR}
    assert d.live() == [TERRAIN]


def test_mixed_packet_splits_the_decision():
    """THE CORE CLAIM: one lost packet, attitude abandoned, terrain_map recovered."""
    log = SendLog()
    log.record(19, {TERRAIN: (1, value(TERRAIN))}, now=0.0)
    log.record(20, {ATTITUDE: (1, value(ATTITUDE)), TERRAIN: (2, value(TERRAIN))}, now=10.0)
    log.record(21, {ATTITUDE: (2, value(ATTITUDE))}, now=10.01)
    d = decide(20, ALL, log)
    assert outcomes(d) == {ATTITUDE: Outcome.SUPERSEDED, TERRAIN: Outcome.REPAIR}
    assert d.live() == [TERRAIN]


def test_tolerance_filter_suppresses_within_deadline():
    log = SendLog()
    log.record(1, {GPS: (1, value(GPS))}, now=0.0)
    log.record(11, {GPS: (2, value(GPS))}, now=0.1)    # every 100 ms, deadline 300 ms
    d = decide(11, ALL, log)                           # not re-sent, so Test 1 can't fire
    assert outcomes(d) == {GPS: Outcome.WITHIN_TOLERANCE}
    assert d.reply_is_skip()


def test_most_recent_transmission_is_never_superseded():
    rng = random.Random(3)
    log = SendLog()
    ids = [f.id for f in schema.active()]
    for seq in range(500):
        chosen = rng.sample(ids, rng.randint(1, len(ids)))
        log.record(seq, {fid: (seq % 256, value(fid)) for fid in chosen}, now=seq * 0.01)
    for fid, last in log.last_seq.items():
        d = decide(last, 1 << fid, log)
        assert outcomes(d)[fid] is not Outcome.SUPERSEDED


def test_zero_false_suppression_over_random_runs():
    """Property test: 25 randomised runs, loss 5-45 %, both loss models, three RTTs."""
    rng = random.Random(2026)
    for run in range(25):
        sim = Simulation(seed=rng.randrange(10_000), policy="ours",
                         loss_rate=rng.uniform(0.05, 0.45), burst=(run % 2 == 0),
                         rtt_s=rng.choice((0.020, 0.060, 0.200)), duration_s=10.0).run()
        assert sim.suppression_events, "a run with no suppressions proves nothing"
        assert verify_no_false_suppression(sim.suppression_events, sim.transmissions) == 0


def test_fields_below_crossover_never_repaired_and_critical_never_superseded():
    sim = Simulation(seed=config.SEEDS[0], policy="ours", duration_s=120.0).run()
    t_star = crossover(config.TICK_S, config.HOLD_DOWN_S, config.RTT_S)
    for _, _, fid, outcome in sim.decisions:
        f = schema.by_id(fid)
        critical = f.period is None or f.deadline < f.period
        if outcome is Outcome.REPAIR:
            assert f.period is None or f.period > t_star, f"{f.name} repaired below T*"
        if critical:
            assert outcome is not Outcome.SUPERSEDED, f"critical {f.name} superseded"
