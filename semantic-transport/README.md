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
| `python app.py` | The visual explorer in live mode at http://127.0.0.1:8000: type your own fields and link, press Run |
| open `frontend/explorer.html` | The same explorer offline, showing the built-in drone run |
| `python -m pytest -v` | 26 tests (protocol, decision engine, FEC, corruption, the set/get API) |
| `python demo.py --seed 23` | Narrated demo: every verdict, the heartbeat, a summary |
| `python demo.py --compare` | Ours vs full ARQ vs plain UDP on one loss trace |
| `python experiments.py` | All simulator studies, CSVs and figures into `results/` (about 12 min) |
| `python experiments.py errors` | Bit corruption and block FEC for large critical fields (about 1 min) |
| `python3 udp_driver.py --policy ours` | Same code over real UDP sockets, two processes (Linux/WSL) |
| `sudo python3 netem_bench.py` | Real kernel: our protocol and kernel TCP under tc/netem (about 35 min) |
| `python experiments.py realnet` | Figure from the netem results |

## Use it in your own program

`semstate.py` exposes the protocol as a set/get interface over named fields. Describe your fields
once (both ends use the same list, in the same order), then:

```python
import semstate

fields = [
    semstate.field("temperature", "float", every=0.1, deadline=0.3),   # set about every 100 ms
    semstate.field("alarm", "str", size=32, deadline=0.5),             # changes on events
]
tx = semstate.Sender(fields, bind="0.0.0.0:47001", peer="192.168.1.20:47002")
tx.set("temperature", 23.5)

rx = semstate.Receiver(fields, bind="0.0.0.0:47002", peer="192.168.1.10:47001")
rx.get("temperature")          # 23.5, or None until it first arrives
```

Kinds are `float`, `int`, `bool`, `str` and `bytes`. Pass `loss=0.1` to either end to emulate a bad
link on one machine. A worked example, a pumping station and its control room:

```
python examples/plant_dashboard.py                  # start the receiver first
python examples/plant_sender.py --loss 0.1          # then the sender
```

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
5. Bit corruption (`BIT_ERROR_RATE`) is caught by UDP's checksum and the packet dropped, so the
   protocol handles it as one more loss.

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
| `src/fec.py` | Selective repetition, selective block FEC, and XOR parity (B2) |
| `src/sim.py` | Event harness: all policies, simulated clock and link |
| `src/impairment.py`, `src/metrics.py`, `src/crossover.py` | Loss models, measurement, the model |
| `udp_driver.py`, `tcp_ref.py`, `netem_bench.py` | Real sockets, kernel TCP, netem benchmark |
| `semstate.py`, `examples/` | The set/get API for your own programs, and an example using it |
| `app.py`, `frontend/` | The visual explorer: `build.py` bakes in real data, `scenario.py` runs your inputs |
| `demo.py`, `experiments.py` | Narrated demo, studies and figures |
| `tests/` | 26 tests |
| `results/` | Generated CSVs and figures |

## Policies compared

| Name | Behaviour |
| --- | --- |
| `ours` | Supersession + tolerance + heartbeat |
| `ours_fec` | Ours + spaced repetition of small critical fields, and block FEC for large critical fields whose repair would miss the deadline |
| `arq` (B1) | Resend every field of a lost packet |
| `fec` (B2) | One XOR parity packet per 15 data packets, for every field |
| `snapshot` (B3) | Whole state every tick, fragmented over the MTU |
| `udp` (B4) | No recovery |
| kernel TCP | `tcp_ref.py`, real Linux TCP under netem |

Results are findings, not targets: see `results/` and the Review 3 document. The simulator is
deterministic, so the same seed always reproduces the same run.
