import random

from src import fec, header, schema
from src.header import DATA, Packet, pack, unpack


def test_xor_parity_rebuilds_any_one_missing_packet():
    rng = random.Random(5)
    wires = [rng.randbytes(rng.randint(9, 1500)) for _ in range(15)]
    parity = fec.parity_of(wires)
    for missing in range(len(wires)):
        present = wires[:missing] + wires[missing + 1:]
        assert fec.recover(parity, present) == wires[missing]


def test_parity_packet_round_trips_and_is_not_a_data_packet():
    parity = fec.parity_of([b"\x01\x02\x03", b"\x04\x05"])
    pkt = Packet(DATA, 100, 0, header.fec_byte(3, 0), parity=parity)
    back = unpack(pack(pkt))
    assert back.parity == parity and back.updates == {}
    assert not header.is_parity(DATA, 1, header.fec_byte(3, 0))   # carries a field: real data


def test_repeater_sends_k_spaced_copies_of_small_critical_fields_only():
    protected = fec.protected_fields(schema.active(), max_width=16)
    assert {schema.by_id(fid).name for fid in protected} == {"mode", "fault_code", "waypoint"}
    rep = fec.Repeater(protected, k=2, spacing=3)
    latest = {7: (1, b"\x00\x09"), 6: (1, bytes(1480))}           # fault_code, terrain_map
    copies = []
    for tick in range(10):
        new = dict(latest) if tick == 0 else {}
        out = rep.add(new, latest)
        if tick > 0 and 7 in out:
            copies.append(tick)
        assert tick == 0 or 6 not in out                           # terrain_map is never repeated
    assert copies == [3, 6]


def test_udp_checksum_catches_every_single_bit_flip():
    from src.impairment import udp_checksum
    wire = pack(Packet(DATA, 42, header.bitmap_of({7: (3, b"\x00\x09")}), 0, {7: (3, b"\x00\x09")}))
    good = udp_checksum(wire)
    for bit in range(8 * len(wire)):
        damaged = bytearray(wire)
        damaged[bit // 8] ^= 1 << (bit % 8)
        assert udp_checksum(bytes(damaged)) != good


def test_corrupted_packets_are_dropped_and_handled_like_losses():
    from src.metrics import verify_no_false_suppression
    from src.sim import Simulation
    clean = Simulation(11, "ours", duration_s=20).run()
    noisy = Simulation(11, "ours", duration_s=20, bit_error_rate=2e-5).run()
    assert clean.stats["corrupt_dropped"] == 0
    assert noisy.stats["corrupt_dropped"] > 50                    # ~14% of 900 B packets
    assert noisy.stats["data_lost"] == clean.stats["data_lost"]  # its own random stream
    assert noisy.stats["fields_repaired"] > 0
    assert verify_no_false_suppression(noisy.suppression_events, noisy.transmissions) == 0


def test_block_fec_only_for_large_critical_fields_whose_repair_is_too_late():
    fields = schema.active()
    assert fec.block_protected(fields, 16, repair_delay=0.105) == set()        # 60 ms RTT
    assert {schema.by_id(f).name for f in fec.block_protected(fields, 16, 2.3)} == {"terrain_map"}
    coder = fec.BlockCoder({6}, group=3, spacing=2)
    groups = [coder.sent(s, bytes([s]), {6} if s == 0 else {0}) for s in range(6)]
    assert groups[4] == [[(0, b"\x00"), (2, b"\x02"), (4, b"\x04")]]
    assert [g for i, g in enumerate(groups) if i != 4] == [[]] * 5


def test_block_fec_rebuilds_lost_terrain_map_without_a_round_trip():
    from src.metrics import verify_no_false_suppression
    from src.sim import Simulation
    sim = Simulation(23, "ours_fec", rtt_s=1.5, loss_rate=0.2, burst=False, duration_s=120).run()
    assert sim.stats["parity_sent"] > 0 and sim.stats["fec_recovered"] > 0
    assert verify_no_false_suppression(sim.suppression_events, sim.transmissions) == 0
