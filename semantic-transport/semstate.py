"""Use the protocol in your own program: replicate named values over UDP.

    import semstate

    fields = [
        semstate.field("temperature", "float", every=0.1, deadline=0.3),
        semstate.field("valve", "str", size=8, every=None, deadline=0.5),   # event-driven
    ]
    tx = semstate.Sender(fields, bind="0.0.0.0:47001", peer="192.168.1.20:47002")
    tx.set("temperature", 23.5)

    rx = semstate.Receiver(fields, bind="0.0.0.0:47002", peer="192.168.1.10:47001")
    rx.get("temperature")            # -> 23.5, or None until it first arrives

Both ends must declare the same fields in the same order: that list is the
session handshake, which is why sizes never travel in a packet.

Under the hood this is exactly the code the experiments measure (src/sim.py
over udp_driver's real-socket link). The only difference is where values come
from: your set() calls instead of the synthetic sensor generator. Every 10 ms
tick the sender packs whatever you set since the last tick (newest value wins),
and on a loss it recovers only what is still worth recovering.

Field kinds and their sizes on the wire:
    float  8 bytes        int  8 bytes (signed)        bool  1 byte
    str    `size` bytes (UTF-8, padded)                bytes `size` bytes

`every` is how often you expect to set the field, in seconds (None if it
changes on events), and `deadline` how stale the receiving side may let it get.
The protocol measures the real rate itself; these two describe the field so
it can tell a critical value from one about to be replaced.
"""

import socket
import struct
import threading
import time

from src import schema
from src.schema import Field
from src.sim import FWD, REV, Simulation
from udp_driver import UdpLink, address, event_loop

WIDTHS = {"float": 8, "int": 8, "bool": 1}


def field(name, kind, deadline, every=None, size=None, mean_gap=10.0):
    """Describe one replicated value. `every=None` means event-driven."""
    if kind in WIDTHS:
        size = WIDTHS[kind]
    elif kind in ("str", "bytes"):
        if not size:
            raise ValueError(f"{name}: a {kind} field needs a size in bytes")
    else:
        raise ValueError(f"{name}: kind must be float, int, bool, str or bytes, not {kind!r}")
    return dict(name=name, kind=kind, size=size, every=every, deadline=deadline, mean_gap=mean_gap)


def _schema(fields):
    if not 1 <= len(fields) <= 16:
        raise ValueError("between 1 and 16 fields (one bit each in the header's bitmap)")
    return [Field(i, f["name"], f["size"], f["every"], f["deadline"], f["mean_gap"])
            for i, f in enumerate(fields)]


def encode(kind, size, value):
    if kind == "float":
        return struct.pack(">d", float(value))
    if kind == "int":
        return struct.pack(">q", int(value))
    if kind == "bool":
        return b"\x01" if value else b"\x00"
    raw = value.encode("utf-8") if kind == "str" else bytes(value)
    if len(raw) > size:
        raise ValueError(f"value is {len(raw)} bytes; this field holds {size}")
    return raw.ljust(size, b"\0")


def decode(kind, raw):
    if kind == "float":
        return struct.unpack(">d", raw)[0]
    if kind == "int":
        return struct.unpack(">q", raw)[0]
    if kind == "bool":
        return raw != b"\x00"
    if kind == "str":
        return raw.rstrip(b"\0").decode("utf-8", errors="replace")
    return raw


class AppSource:
    """Stands in for the sensor generator: values come from set() instead."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}            # field id -> value bytes set since the last tick
        self.version = {}

    def put(self, fid, value):
        with self.lock:
            self.pending[fid] = value          # several sets in one tick: the newest wins

    def due(self, now):
        with self.lock:
            out = {}
            for fid, value in self.pending.items():
                self.version[fid] = (self.version.get(fid, 0) + 1) % 256
                out[fid] = (self.version[fid], value)
            self.pending.clear()
            return out


class _End:
    """What Sender and Receiver share: a socket, the protocol, and a thread running it."""

    def __init__(self, fields, bind, peer, sender, policy, loss, rtt, jitter, burst, seed):
        self.kinds = {f["name"]: f["kind"] for f in fields}
        self.ids = {f["name"]: i for i, f in enumerate(fields)}
        # The field table is process-wide (it stands for the session handshake), so
        # one schema per process; close() puts back whatever was there before.
        self._previous = schema.active()
        schema.use(_schema(fields))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(address(bind))
        self.sock.setblocking(False)
        t0 = time.monotonic()
        clock = lambda: time.monotonic() - t0
        self.source = AppSource() if sender else None
        # loss/rtt/jitter emulate a bad link in software, for trying it on one machine.
        self.sim = Simulation(seed, policy, loss_rate=loss, burst=burst, rtt_s=rtt, jitter_s=jitter,
                              duration_s=float("inf"), link=UdpLink(self.sock, address(peer), clock),
                              source=self.source, record=False)
        self._stop = threading.Event()
        if sender:
            self.sim.start()
        self._thread = threading.Thread(
            target=event_loop, daemon=True,
            args=(self.sim, self.sock, self.sim.link, REV if sender else FWD, clock, float("inf"), self._stop))
        self._thread.start()

    def _fid(self, name):
        if name not in self.ids:
            raise KeyError(f"no field called {name!r}; declared: {', '.join(self.ids)}")
        return self.ids[name]

    @property
    def stats(self):
        """Counters: data sent/lost, NACKs, repairs, SKIPs, fields superseded, ..."""
        return dict(self.sim.stats)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()
        schema.use(self._previous)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Sender(_End):
    """The end that owns the values. set() them as often as they change."""

    def __init__(self, fields, bind, peer, policy="ours", loss=0.0, rtt=0.0, jitter=0.0,
                 burst=False, seed=1):
        super().__init__(fields, bind, peer, True, policy, loss, rtt, jitter, burst, seed)

    def set(self, name, value):
        fid = self._fid(name)
        f = schema.by_id(fid)
        self.source.put(fid, encode(self.kinds[name], f.width, value))


class Receiver(_End):
    """The end that keeps a replica. get() always returns the newest value it has."""

    def __init__(self, fields, bind, peer, policy="ours", loss=0.0, rtt=0.0, jitter=0.0,
                 burst=False, seed=2):
        super().__init__(fields, bind, peer, False, policy, loss, rtt, jitter, burst, seed)

    def get(self, name, default=None):
        raw = self.sim.receiver.value.get(self._fid(name))
        return default if raw is None else decode(self.kinds[name], raw)

    def snapshot(self):
        """Every field's newest value, as a dict (None for fields not yet received)."""
        return {name: self.get(name) for name in self.ids}
