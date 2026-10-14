import random

import pytest

import config
from src import schema
from src.header import (DATA, NACK, REPAIR, SKIP, Packet, bitmap_of, pack,
                        seq_newer, unpack, version_newer)
from src.state_table import StateTable


def make_packet(ptype, seq, field_ids, version=1, fec=0):
    updates = {fid: (version, bytes([fid + 1]) * schema.by_id(fid).width) for fid in field_ids}
    return Packet(ptype, seq, bitmap_of(field_ids), fec, updates)


# --- header -----------------------------------------------------------------

def test_header_round_trip_single():
    pkt = make_packet(REPAIR, 50, [0, 2, 6], version=7, fec=0x21)
    assert unpack(pack(pkt)) == pkt


def test_header_round_trip_200_random():
    rng = random.Random(1)
    ids = [f.id for f in schema.active()]
    for _ in range(200):
        ptype = rng.choice([DATA, REPAIR, SKIP, NACK])
        if ptype in (DATA, REPAIR):
            chosen = rng.sample(ids, rng.randint(0, len(ids)))
            pkt = make_packet(ptype, rng.randrange(1 << 14), sorted(chosen),
                              version=rng.randrange(256), fec=rng.randrange(256))
        elif ptype == SKIP:   # a heartbeat digest: version bytes only
            chosen = rng.sample(range(16), rng.randint(0, 16))
            digest = {fid: (rng.randrange(256), b"") for fid in chosen}
            pkt = Packet(SKIP, rng.randrange(1 << 14), bitmap_of(chosen), rng.randrange(256), digest)
        else:   # NACK: no payload, the bitmap is a plain mask
            pkt = Packet(NACK, rng.randrange(1 << 14), rng.randrange(1 << 16), rng.randrange(256))
        assert unpack(pack(pkt)) == pkt


def test_header_is_exactly_5_bytes():
    assert len(pack(Packet(SKIP, 1))) == 5 == config.HEADER_BYTES


def test_wrong_width_rejected():
    pkt = Packet(DATA, 1, bitmap_of([0]), 0, {0: (1, b"\x00" * 3)})  # attitude is 4 B
    with pytest.raises(ValueError):
        pack(pkt)


# --- wrap-safe comparison ---------------------------------------------------

def test_seq_newer_basic():
    assert seq_newer(5, 4)
    assert not seq_newer(4, 5)
    assert not seq_newer(7, 7)


def test_seq_newer_across_wrap():
    assert seq_newer(0, 16383)        # 0 comes right after 16383
    assert not seq_newer(16383, 0)


def test_version_newer_across_wrap():
    assert version_newer(0, 255)
    assert not version_newer(255, 0)


# --- receiver state table ---------------------------------------------------

def test_state_applies_newer():
    st = StateTable()
    assert st.apply(0, 1, b"aaaa")
    assert st.apply(0, 2, b"bbbb")
    assert st.value[0] == b"bbbb" and st.version[0] == 2


def test_state_rejects_older_and_duplicate():
    st = StateTable()
    st.apply(0, 5, b"new!")
    assert not st.apply(0, 4, b"old!")
    assert not st.apply(0, 5, b"dup!")
    assert st.value[0] == b"new!"


def test_state_rejects_old_across_version_wrap():
    st = StateTable()
    st.apply(0, 1, b"post")           # counter has wrapped past 255
    assert not st.apply(0, 250, b"pre!")   # a delayed pre-wrap packet
    assert st.value[0] == b"post"


# --- heartbeat digest -------------------------------------------------------

def test_digest_finds_behind_version_and_never_seen():
    st = StateTable()
    st.apply(0, 3, b"aaaa")
    st.apply(1, 9, b"bbbb")
    digest = [(0, 5), (1, 9), (4, 1)]   # 0 is behind, 1 is current, 4 never seen
    assert st.missing_from(digest) == [0, 4]
