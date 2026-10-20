# Joint Semantic Error Protection and State Supersession for Redundancy-Minimal Transport Protocols

A transport middleware over UDP that replicates application *state* (named fields) across a lossy
link. When a packet is lost, the sender checks, field by field, whether each lost value has already
been re-sent with a newer one. Obsolete copies are never repaired, values the application can wait
for are left to the next update, and only still-live fields are recovered. Small critical fields can
also be protected in advance by spaced repetition.

> TCP wastes bandwidth retransmitting data that's already out of date; UDP loses the rare important
> data; we check, field by field, whether a lost value is still worth recovering.

BCSE308P Computer Networks Laboratory, VIT Vellore, Fall 2026-27. Koliparthy Venkata Jahnvi
(24BDS0132), Somesh Tripathi (24BCE0533), Anushka Jaiswal (24BCE2028).

## Quick start

Python 3.11+ with `pytest` and `matplotlib` (`pip install -r requirements.txt`). Run from this folder.

| Command | What it does |
| --- | --- |
| `python -m pytest -v` | 21 tests (protocol, decision engine, FEC) |
| `python demo.py --seed 23` | Narrated demo: every verdict, the heartbeat, a summary |
| `python demo.py --compare` | Ours vs full ARQ vs plain UDP on one loss trace |
| `python experiments.py` | All simulator studies, CSVs and figures into `results/` (about 12 min) |
| `python3 udp_driver.py --policy ours` | Same code over real UDP sockets, two processes (Linux/WSL) |
| `sudo python3 netem_bench.py` | Real kernel: our protocol and kernel TCP under tc/netem (about 35 min) |
| `python experiments.py realnet` | Figure from the netem results |

## How it works

1. The sender packs each tick's changed fields into one packet: a 5-byte header
   (type, 14-bit sequence number, 16-bit field bitmap, FEC byte) plus one version byte and the
   value per field.
2. The receiver notices a gap when a later packet arrives, waits a 5 ms hold-down, crosses off
   every field a newer packet already refreshed (the fast path), and sends a NACK for the rest.
3. The sender judges each field the lost packet carried:
   - **SUPERSEDED** if a newer packet already carried it,
   - **WITHIN_TOLERANCE** if it updates faster than its deadline,
   - **REPAIR** otherwise. With nothing to repair it replies with a 5-byte SKIP.
4. A heartbeat every second lists each field's latest version, so nothing stays stale when a NACK
   or a repair is lost.

The NACK reaches the sender exactly tau + H + R after a loss, the same as the analytical crossover
T* = tau + H + R (75 ms here). So the send-log check enforces the model by observation and can
never suppress a field's newest value.

## Layout

| Path | Contents |
| --- | --- |
| `config.py` | Every constant |
| `src/schema.py`, `src/header.py`, `src/generator.py` | Field table, wire format, sensor generator |
| `src/send_log.py`, `src/supersession.py` | The sender's record and the decision engine |
| `src/state_table.py`, `src/gap_detector.py` | Receiver replica and loss detection |
| `src/fec.py` | Selective repetition and XOR parity (B2) |
| `src/sim.py` | Event harness: all policies, simulated clock and link |
| `src/impairment.py`, `src/metrics.py`, `src/crossover.py` | Loss models, measurement, the model |
| `udp_driver.py`, `tcp_ref.py`, `netem_bench.py` | Real sockets, kernel TCP, netem benchmark |
| `demo.py`, `experiments.py` | Narrated demo, studies and figures |
| `tests/` | 21 tests |
| `results/` | Generated CSVs and figures |

## Policies compared

| Name | Behaviour |
| --- | --- |
| `ours` | Supersession + tolerance + heartbeat |
| `ours_fec` | Ours + spaced repetition of small critical fields |
| `arq` (B1) | Resend every field of a lost packet |
| `fec` (B2) | One XOR parity packet per 15 data packets, for every field |
| `snapshot` (B3) | Whole state every tick, fragmented over the MTU |
| `udp` (B4) | No recovery |
| kernel TCP | `tcp_ref.py`, real Linux TCP under netem |

Results are findings, not targets: see `results/` and the Review 3 document. The simulator is
deterministic, so the same seed always reproduces the same run.
