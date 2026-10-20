"""Phase 9: real kernel, real network emulation. Run as root on Linux or WSL2:

  sudo python3 netem_bench.py            full comparison + loss sweep (about 30 min)
  sudo python3 netem_bench.py --quick    one short run per policy, to check the setup

Builds two network namespaces joined by a veth pair

    ns_tx  10.77.0.1  vtx  <=====>  vrx  10.77.0.2  ns_rx

and puts tc/netem on BOTH ends: 30 ms +/- 1 ms each way (RTT 60 ms) and
Gilbert-Elliott burst loss with the same parameters as src/impairment.py. Then,
for each policy and seed, it starts the receiver in ns_rx and the sender in
ns_tx:
  * our protocol and baselines through udp_driver.py, with the Python loss model
    switched OFF (--loss 0 --rtt 0 --jitter 0): the kernel drops packets now;
  * the kernel TCP reference through tcp_ref.py.
Bytes on the wire are read from the veth counters, so IP/UDP/TCP headers, ACKs
and TCP's retransmissions are all counted. netem drops packets before the
device counts them, so these are the bytes that actually left the sender.

Writes results/realnet_runs.csv (10% burst loss) and results/realnet_sweep.csv
(i.i.d. loss 2-20%, see build_link for why). `python experiments.py realnet` draws the figure.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time

import config
from src import fec, schema
from src.metrics import on_time_rate, replay
import udp_driver

HERE = os.path.dirname(os.path.abspath(__file__))
TX, RX = "ns_tx", "ns_rx"
TX_IP, RX_IP = "10.77.0.1", "10.77.0.2"


def sh(*cmd):
    subprocess.run(cmd, check=True)


def build_link(loss_rate, burst=True):
    """Fresh namespaces, veth pair and netem on both directions.

    burst=True: Gilbert-Elliott with the same parameters as src/impairment.py.
    Note that netem, like our model, counts a burst in PACKETS. That suits a
    sender at 100 packets/s, but a TCP sender in retransmission back-off sends one
    packet per timeout, so a "4-packet" burst can last seconds for it. The loss
    sweep therefore uses i.i.d. loss (burst=False), which treats TCP fairly."""
    for ns in (TX, RX):
        subprocess.run(["ip", "netns", "del", ns], stderr=subprocess.DEVNULL)
    sh("ip", "netns", "add", TX)
    sh("ip", "netns", "add", RX)
    sh("ip", "link", "add", "vtx", "type", "veth", "peer", "name", "vrx")
    sh("ip", "link", "set", "vtx", "netns", TX)
    sh("ip", "link", "set", "vrx", "netns", RX)
    for ns, dev, ip in ((TX, "vtx", TX_IP), (RX, "vrx", RX_IP)):
        sh("ip", "-n", ns, "addr", "add", f"{ip}/24", "dev", dev)
        sh("ip", "-n", ns, "link", "set", dev, "up")
        sh("ip", "-n", ns, "link", "set", "lo", "up")
        # Same Gilbert-Elliott parameterisation as src/impairment.py.
        p_bg = 1.0 / config.MEAN_BURST_LEN
        p_gb = loss_rate * p_bg / (1.0 - loss_rate)
        netem = ["delay", "30ms", "1ms"]
        if loss_rate > 0 and burst:
            netem += ["loss", "gemodel", f"{100 * p_gb:.4f}%", f"{100 * p_bg:.4f}%"]
        elif loss_rate > 0:
            netem += ["loss", "random", f"{100 * loss_rate:.4f}%"]
        sh("ip", "netns", "exec", ns, "tc", "qdisc", "replace", "dev", dev, "root", "netem", *netem)
    # Static neighbour entries: otherwise the first packets of a run wait on ARP,
    # and under emulated loss a lost ARP request can cut the link for a second.
    mac = {ns: json.loads(subprocess.run(["ip", "-n", ns, "-j", "link", "show", "dev", dev],
                                         check=True, capture_output=True, text=True).stdout)[0]["address"]
           for ns, dev in ((TX, "vtx"), (RX, "vrx"))}
    sh("ip", "-n", TX, "neigh", "replace", RX_IP, "lladdr", mac[RX], "dev", "vtx", "nud", "permanent")
    sh("ip", "-n", RX, "neigh", "replace", TX_IP, "lladdr", mac[TX], "dev", "vrx", "nud", "permanent")


def tx_bytes(ns, dev):
    out = subprocess.run(["ip", "-n", ns, "-j", "-s", "link", "show", "dev", dev],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)[0]["stats64"]["tx"]["bytes"]


def in_ns(ns, script, args):
    return subprocess.Popen(["ip", "netns", "exec", ns, sys.executable, os.path.join(HERE, script)] + args)


def run_one(policy, seed, duration, tmp):
    """One run: receiver in ns_rx, sender in ns_tx. Returns a result row."""
    wire_before = tx_bytes(TX, "vtx") + tx_bytes(RX, "vrx")
    t0 = time.monotonic() + 2.0
    s_out, r_out = os.path.join(tmp, "s.json"), os.path.join(tmp, "r.json")
    if policy == "tcp":
        common = ["--seed", str(seed), "--duration", str(duration), "--addr", f"{RX_IP}:47100",
                  "--t0", repr(t0)]
        procs = [in_ns(RX, "tcp_ref.py", ["--role", "receiver", "--out", r_out] + common),
                 in_ns(TX, "tcp_ref.py", ["--role", "sender", "--out", s_out] + common)]
    else:
        common = ["--policy", policy, "--seed", str(seed), "--duration", str(duration),
                  "--loss", "0", "--rtt", "0", "--jitter", "0",
                  "--send-addr", f"{TX_IP}:47001", "--recv-addr", f"{RX_IP}:47002", "--t0", repr(t0)]
        procs = [in_ns(RX, "udp_driver.py", ["--role", "receiver", "--out", r_out] + common),
                 in_ns(TX, "udp_driver.py", ["--role", "sender", "--out", s_out] + common)]
    for proc in procs:
        if proc.wait() != 0:
            raise RuntimeError(f"{policy} seed {seed} failed")
    with open(s_out) as fh:
        slog = json.load(fh)
    with open(r_out) as fh:
        rlog = json.load(fh)
    wire = tx_bytes(TX, "vtx") + tx_bytes(RX, "vrx") - wire_before

    field_ids = [f.id for f in schema.active()]
    m = replay(field_ids, slog["generated"], rlog["deliveries"], config.WARMUP_S, duration, wire)
    gen_time = {(fid, v): t for t, fid, v in slog["generated"]}     # exact for the rare fields
    critical = [f for f in schema.active() if fec.is_critical(f)]
    row = dict(policy=policy, seed=seed, wire_bytes=wire, mean_aoi_s=round(m.mean_aoi_all(), 4),
               critical_on_time_pct=round(100 * on_time_rate(critical, slog["generated"], rlog["deliveries"],
                                                             gen_time, config.WARMUP_S, duration), 3),
               latency_p95_ms=round(m.latency_percentile(95) * 1000, 2),
               latency_p99_ms=round(m.latency_percentile(99) * 1000, 2))
    if policy == "tcp":
        row.update(recovery_bytes=slog["tcp_info"]["bytes_retrans"],
                   retransmissions=slog["tcp_info"]["total_retrans"], false_suppressions="",
                   tcp_unsent_bytes=slog["tcp_info"]["notsent"])
    else:
        joined = udp_driver.combine(slog, rlog, duration)
        row.update(recovery_bytes=joined["recovery_bytes"], retransmissions="",
                   false_suppressions=joined["false_suppressions"], tcp_unsent_bytes="")
    return row


def write_csv(name, rows):
    path = os.path.join(HERE, "results", name)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true")
    args = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root: network namespaces and netem need it")
    tmp = tempfile.mkdtemp(prefix="netem_")

    seeds = config.SEEDS[:1] if args.quick else config.SEEDS
    duration = 10.0 if args.quick else config.DURATION_S
    rows = []
    build_link(config.LOSS_RATE)
    for policy in ("ours", "ours_fec", "arq", "udp", "tcp"):
        for seed in seeds:
            row = dict(loss=config.LOSS_RATE, **run_one(policy, seed, duration, tmp))
            rows.append(row)
            print(f"  10% burst  {policy:9s} seed {seed}: wire {row['wire_bytes']:>9,}  "
                  f"recovery {row['recovery_bytes']:>7,}  AoI {row['mean_aoi_s']:.3f}  "
                  f"critical on time {row['critical_on_time_pct']:.1f}%  p95 {row['latency_p95_ms']:.0f} ms", flush=True)
    write_csv("realnet_runs.csv" if not args.quick else "realnet_quick.csv", rows)
    if args.quick:
        return

    sweep = []
    for loss in (0.02, 0.05, 0.10, 0.20):
        build_link(loss, burst=False)
        for policy in ("ours", "udp", "tcp"):
            for seed in config.SEEDS[:3]:
                row = dict(loss=loss, **run_one(policy, seed, 20.0, tmp))
                sweep.append(row)
                print(f"  loss {loss:.0%}  {policy:5s} seed {seed}: AoI {row['mean_aoi_s']:.3f}  "
                      f"p99 latency {row['latency_p99_ms']:.0f} ms  TCP unsent {row['tcp_unsent_bytes']}",
                      flush=True)
    write_csv("realnet_sweep.csv", sweep)
    for ns in (TX, RX):
        subprocess.run(["ip", "netns", "del", ns], stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
