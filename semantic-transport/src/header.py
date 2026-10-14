"""The 5-byte wire format and wrap-safe counter comparison.

  bits 15-14  TYPE    0=DATA 1=REPAIR 2=SKIP 3=NACK
  bits 13-0   SEQ     sequence number (wraps at 16384)
  16 bits     UPDATE_BITMAP   bit i set <=> this packet carries field i
  8 bits      FEC             high nibble = parity group, low nibble = index (0 = none)

Payload, for each set bit in ascending field-id order:
  DATA, REPAIR  one version byte, then `width` value bytes (width from the schema)
  SKIP          one version byte only. A plain SKIP has an empty bitmap and so no
                payload; a heartbeat digest is a SKIP that lists (field, version).
  NACK          nothing - its bitmap is a mask of the fields being asked about
"""

import struct
from dataclasses import dataclass

import config
from src import schema

DATA, REPAIR, SKIP, NACK = 0, 1, 2, 3

# The 2-bit TYPE has no room for heartbeat types, so heartbeat traffic (the digest
# SKIP, the receiver's request NACK and the sender's reply REPAIR) is marked by
# this reserved value in the FEC byte. Real parity groups use 1..14.
HB_MARK = 0xFF

_HEADER = struct.Struct(">HHB")
assert _HEADER.size == config.HEADER_BYTES


def _newer(a, b, bits):
    """RFC 1982 serial-number arithmetic: is a ahead of b, allowing for wrap?"""
    if a == b:
        return False
    return ((a - b) % (1 << bits)) < (1 << (bits - 1))


def seq_newer(a, b, bits=config.SEQ_BITS):
    return _newer(a, b, bits)


def version_newer(a, b, bits=config.VERSION_BITS):
    return _newer(a, b, bits)


@dataclass
class Packet:
    type: int
    seq: int
    bitmap: int = 0
    fec: int = 0
    updates: dict = None     # field id -> (version, value bytes)

    def __post_init__(self):
        if self.updates is None:
            self.updates = {}


def bitmap_of(field_ids):
    bitmap = 0
    for fid in field_ids:
        bitmap |= 1 << fid
    return bitmap


def fields_in(bitmap):
    """Field ids whose bit is set, ascending."""
    return [i for i in range(16) if bitmap & (1 << i)]


def _value_width(ptype, fid):
    """Value bytes that follow a field's version byte in this packet type."""
    return schema.by_id(fid).width if ptype in (DATA, REPAIR) else 0


def pack(pkt):
    if pkt.type == NACK:
        if pkt.updates:
            raise ValueError("a NACK carries no payload")
    elif pkt.bitmap != bitmap_of(pkt.updates):
        raise ValueError("bitmap does not match the fields carried")
    body = b""
    for fid in (fields_in(pkt.bitmap) if pkt.type != NACK else []):
        version, value = pkt.updates[fid]
        width = _value_width(pkt.type, fid)
        if len(value) != width:
            raise ValueError(f"field {fid} needs {width} bytes, got {len(value)}")
        body += bytes([version]) + value
    first = (pkt.type << config.SEQ_BITS) | pkt.seq
    return _HEADER.pack(first, pkt.bitmap, pkt.fec) + body


def unpack(data):
    first, bitmap, fec = _HEADER.unpack_from(data)
    pkt = Packet(first >> config.SEQ_BITS, first & ((1 << config.SEQ_BITS) - 1), bitmap, fec)
    pos = _HEADER.size
    for fid in (fields_in(bitmap) if pkt.type != NACK else []):
        width = _value_width(pkt.type, fid)
        pkt.updates[fid] = (data[pos], data[pos + 1:pos + 1 + width])
        pos += 1 + width
    if pos != len(data):
        raise ValueError("packet length does not match its bitmap")
    return pkt
