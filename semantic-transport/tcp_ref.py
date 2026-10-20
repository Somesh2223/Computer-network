"""Phase 9 reference: the same state stream over a real kernel TCP connection.

  python3 tcp_ref.py --role receiver --addr 10.77.0.2:47100 --t0 T --out r.json
  python3 tcp_ref.py --role sender   --addr 10.77.0.2:47100 --t0 T --out s.json

The sender runs the SAME generator (same seed, same field values and timings)
and packs each tick's updates with the SAME header.pack(); only the transport
differs. Each packet becomes one length-prefixed message on a TCP stream with
TCP_NODELAY, so TCP sends it at once. Loss recovery is entirely the kernel's:
every lost segment is retransmitted, and everything behind it waits.

This is the "existing system" comparison (Review 3 item 7). netem_bench.py runs
it between two network namespaces under tc/netem; freshness is computed the
same way as for udp_driver.py, by joining the two ends' logs.
"""

import argparse
import json
import random
import socket
import struct
import time

import config
from src import header
from src.generator import Generator
from src.state_table import StateTable

STOP_AFTER_S = 5.0     # stop this long after the run ends, however far behind TCP is.
                       # Freshness is only measured up to the end of the run, so anything
                       # TCP delivers later cannot change the result.

# Offsets into Linux's struct tcp_info (include/uapi/linux/tcp.h).
TCPI_UNACKED = 24            # u32
TCPI_TOTAL_RETRANS = 100     # u32
TCPI_NOTSENT_BYTES = 144     # u32
TCPI_BYTES_SENT = 200        # u64 (kernel >= 4.19)
TCPI_BYTES_RETRANS = 208     # u64


def tcp_info(sock):
    raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 232)
    return dict(unacked=struct.unpack_from("I", raw, TCPI_UNACKED)[0],
                notsent=struct.unpack_from("I", raw, TCPI_NOTSENT_BYTES)[0],
                total_retrans=struct.unpack_from("I", raw, TCPI_TOTAL_RETRANS)[0],
                bytes_sent=struct.unpack_from("Q", raw, TCPI_BYTES_SENT)[0],
                bytes_retrans=struct.unpack_from("Q", raw, TCPI_BYTES_RETRANS)[0])


def address(text):
    host, port = text.rsplit(":", 1)
    return host, int(port)


def run_sender(args, clock):
    gen = Generator(random.Random(args.seed + 1))     # same stream as the UDP runs
    sock = socket.create_connection(address(args.addr), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    while clock() < 0:                     # connected before the start, so the
        time.sleep(0.005)                  # handshake doesn't eat into the run
    generated, seq, app_bytes, behind_s = [], 0, 0, 0.0
    ticks = int(args.duration / config.TICK_S) + 1
    for k in range(ticks):
        tick = k * config.TICK_S
        if clock() > args.duration + STOP_AFTER_S:
            behind_s = clock() - tick       # how far behind real time TCP had pushed us
            break
        while clock() < tick:
            time.sleep(max(0.0, min(0.002, tick - clock())))
        updates = gen.due(tick)
        if not updates:
            continue
        for fid, (version, _) in updates.items():
            generated.append((tick, fid, version))
        wire = header.pack(header.Packet(header.DATA, seq, header.bitmap_of(updates), 0, updates))
        seq = (seq + 1) % (1 << config.SEQ_BITS)
        # sendall blocks if TCP's window is full: the application is held up behind
        # the retransmission, which is exactly TCP's behaviour for a state stream.
        stop = args.duration + STOP_AFTER_S
        sock.settimeout(max(0.01, stop - clock()))   # a send blocked by TCP gives up at the stop time
        try:
            sock.sendall(struct.pack(">H", len(wire)) + wire)
        except OSError:                     # timed out at the stop time, or the receiver stopped
            behind_s = clock() - tick
            break
        app_bytes += 2 + len(wire)
    # Give TCP until the stop time to deliver what it has, then read its counters.
    sock.settimeout(None)
    while clock() < args.duration + STOP_AFTER_S:
        info = tcp_info(sock)
        if info["unacked"] == 0 and info["notsent"] == 0:
            break
        time.sleep(0.05)
    info = tcp_info(sock)
    sock.close()
    return dict(role="sender", generated=generated, app_bytes=app_bytes, tcp_info=info,
                behind_s=round(behind_s, 2))


def run_receiver(args, clock):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(address(args.addr))
    listener.listen(1)
    listener.settimeout(10)
    conn, _ = listener.accept()
    conn.settimeout(0.2)
    state, deliveries, buf = StateTable(), [], b""
    end = args.duration + STOP_AFTER_S + 2.0
    while clock() < end:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        now = clock()
        while len(buf) >= 2 and len(buf) >= 2 + struct.unpack(">H", buf[:2])[0]:
            n = struct.unpack(">H", buf[:2])[0]
            pkt = header.unpack(buf[2:2 + n])
            buf = buf[2 + n:]
            for fid, (version, value) in pkt.updates.items():
                if state.apply(fid, version, value):
                    deliveries.append((now, fid, version, len(value)))
    conn.close()
    listener.close()
    return dict(role="receiver", deliveries=deliveries)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--role", choices=("sender", "receiver"), required=True)
    p.add_argument("--seed", type=int, default=config.SEEDS[0])
    p.add_argument("--duration", type=float, default=config.DURATION_S)
    p.add_argument("--addr", default="127.0.0.1:47100", help="the receiver's listening address")
    p.add_argument("--t0", type=float, required=True, help="shared start time (monotonic clock)")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    def clock():
        return time.monotonic() - args.t0

    while clock() < (-1.0 if args.role == "receiver" else -0.5):   # receiver listens first
        time.sleep(0.01)
    log = run_sender(args, clock) if args.role == "sender" else run_receiver(args, clock)
    with open(args.out, "w") as fh:
        json.dump(log, fh)


if __name__ == "__main__":
    main()
