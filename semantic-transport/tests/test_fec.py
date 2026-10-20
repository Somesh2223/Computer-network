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
