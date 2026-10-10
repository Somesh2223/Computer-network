# Build Spec — Semantic Error Protection & State Supersession

> **Starting out.** Put this file in an empty folder, open Claude Code there, and say:
>
> > Read BUILD_PROMPT.md and do Phase 1.
>
> **Resuming later.** Claude Code starts cold in each new session, so open with:
>
> > Read BUILD_PROMPT.md. Check what's already built in src/ and tests/, tell me
> > which phase we're on, then continue from there.
>
> **Keeping the rules across sessions.** After Phase 1, ask Claude Code to write a
> short `CLAUDE.md` holding §0 (readability rules), §13 (the do-not list), and the
> current phase. It reads that automatically every session, so the constraints
> survive even when this file isn't mentioned.
>
> This supersedes the earlier PROJECT_SPEC.md.

### A note on what this spec does and does not fix

This spec pins **design**: the wire format, the send log, the supersession test,
the heartbeat, the experimental methodology. Those come from the Review 1 design
and are the thing being built.

It deliberately does **not** pin **results**. Whatever the measurements come out
to is the finding, and the report describes the system — never the other way
round. If an experiment disagrees with something already written down, the
experiment wins and the write-up gets corrected. Section 11 says how to tell a
genuine result from a bug without using expected values as a target.

---

## 0. The one constraint that outranks everything

Three students must be able to explain this to a professor, out loud, at a
whiteboard, on **15 October**. Readability beats cleverness every time.

- Small files, one idea per file, named after the idea.
- Plain functions and simple classes. No metaclasses, no ABCs, no dependency
  injection, no plugin registries, no `asyncio`.
- Comments explain *why*. Variable names match the report: `send_log`,
  `resent_since`, `hold_down`, `crossover`, `update_bitmap`.
- No micro-optimisation. Correctness and clarity only.

**Do not build ahead.** Each phase ends in something runnable; stop and show it.

---

## 1. What it is

A transport middleware over UDP that replicates application *state* (named
fields) across a lossy link. When a packet is lost, instead of retransmitting
it blindly like TCP, the sender checks whether the fields in that packet have
**already been re-sent with newer values**. If so, the lost copy is obsolete and
recovery is cancelled. Only still-live fields are repaired.

**One sentence for the professor:** *TCP wastes bandwidth retransmitting data
that's already out of date; UDP loses the rare important data; we check, field by
field, whether a lost value is still worth recovering.*

**The worked example** (use this in any explanation): a drone link, RTT 60 ms.
`attitude` updates at 100 Hz — a repair takes ~90 ms, by which time 9 newer
values exist, so it is 100% waste. `fault_code` fires once per ~8 s — if lost and
not recovered it is gone forever. Same flow, same socket, opposite correct
answers. No existing transport can express that.

---

## 2. Repository layout

```
semantic-transport/
├── README.md
├── requirements.txt            # matplotlib, pytest  (nothing else)
├── config.py                   # ALL constants, plain module-level values
├── demo.py                     # ★ narrated demo — what the professor sees
├── experiments.py              # sweeps, figures, CSVs
├── udp_driver.py               # Phase 7: same modules over real UDP sockets
├── src/
│   ├── schema.py               # M1  field table + use()/active() handshake
│   ├── header.py               # M1  5-byte header, RFC 1982 wrap-safe compare
│   ├── generator.py            # M1  produces field updates at their rates
│   ├── send_log.py             # M2  last seq/version per field, packet contents
│   ├── supersession.py         # M2  ★ THE DECISION ENGINE — keep pristine
│   ├── state_table.py          # M2  receiver replica, never regresses
│   ├── gap_detector.py         # M2  gaps, hold-down, fast-path strike-off
│   ├── sim.py                  # M2  event harness, all four policies
│   ├── crossover.py            # the analytical model, ~15 lines
│   ├── impairment.py           # M3  Gilbert–Elliott + i.i.d. loss
│   └── metrics.py              # M3  ring buffer, AoI, invariant audit
├── tests/
│   ├── test_protocol.py        # 11 tests
│   └── test_supersession.py    # 7 tests
└── results/                    # generated CSVs + figures
```

Roughly 1,450 lines total. If a file is heading past 150 lines (except `sim.py`,
which lands near 380), it is doing too much.

---

## 3. The protocol

### 3.1 Field schema — `schema.py`

```python
@dataclass(frozen=True)
class Field:
    id: int             # 0..15, indexes one bit of the update bitmap
    name: str
    width: int          # payload bytes
    period: float | None  # seconds between updates; None = event-driven
    deadline: float     # D_f, staleness the application tolerates
    mean_gap: float = 0.0  # for event-driven: mean seconds between events
```

Use **exactly this schema** — it is what Review 2 reports:

| id | name | width | period | deadline | role |
|---|---|---|---|---|---|
| 0 | attitude | 4 B | 10 ms | 20 ms | fast, below T* |
| 1 | velocity | 4 B | 20 ms | 50 ms | fast, below T* |
| 2 | lidar_scan | 900 B | 50 ms | 100 ms | **big AND fast — the expensive mistake** |
| 3 | gps | 8 B | 100 ms | 300 ms | tolerance-suppressed |
| 4 | battery | 2 B | 1 s | 2 s | tolerance-suppressed |
| 5 | mode | 1 B | 5 s | **1 s** | critical |
| 6 | terrain_map | 1480 B | 10 s | **2 s** | critical, bulky |
| 7 | fault_code | 2 B | event ~8 s | **0.5 s** | critical |
| 8 | waypoint | 8 B | event ~12 s | **1 s** | critical |

Two things about this table need justifying, not just stating:

**`lidar_scan` — a bulky, fast-updating field.** Real UAV links carry exactly
this (lidar, video, depth frames), so it belongs in a representative workload.
But be honest about its effect: the size of the bandwidth saving depends heavily
on how many *bytes* sit in fields below the crossover. A schema of only small
fast fields shows a saving of a few percent; this one shows much more.

**That dependence is a result, so measure it rather than asserting it.** Phase 6
must include a workload-sensitivity run: sweep the width of the fast field (say
4 B → 1400 B) and plot the saving against it. Then the claim becomes "the benefit
scales with the byte-weight of sub-crossover fields, here is the curve", which is
defensible, instead of "we picked a workload and got a good number", which is not.
If a reviewer asks whether the workload was chosen to flatter the result, that
curve is the answer.

**Critical fields have deadlines SHORTER than their period.** That is what
"critical" means: the application cannot wait for the next natural update. Setting
`D_f` longer than the period tells the system "another one is coming, don't
bother" — correct for a periodic sample, badly wrong for a unique event like a
fault code.

Total state is 2,409 B, deliberately over one MTU so baseline B3 must fragment.

Also provide `use(fields)` / `active()`: a module-level active schema that
experiments can swap. This mirrors the real session handshake, and the crossover
sweep needs it to vary one field's period.

### 3.2 Wire format — `header.py`

5 bytes, big-endian, `struct` format `">HHB"`:

| Bits | Field | Meaning |
|---|---|---|
| 2 | TYPE | 0=DATA 1=REPAIR 2=SKIP 3=NACK |
| 14 | SEQ | sequence number, wrap-safe |
| 16 | UPDATE_BITMAP | bit *i* set ⇔ new value for field *i* |
| 8 | FEC_GROUP/IDX | parity group + index (0 = none) |

Payload: for each set bit **in ascending field-id order**, one version byte then
`width` value bytes. Criticality and widths are negotiated at handshake and never
appear in a packet — that is what keeps the header at 5 bytes.

```python
def seq_newer(a, b, bits=14):
    if a == b: return False
    return ((a - b) % (1 << bits)) < (1 << (bits - 1))
```

Same shape for the 8-bit `version_newer`. Without this a delayed old packet
silently clobbers newer state once the counter wraps. Two tests cover it.

---

## 4. The algorithms

### 4.1 Receiver — on packet arrival

```
if seq_newer(pkt.seq, expected):
    for f in bits(pkt.update_bitmap):
        if version_newer(pkt.version[f], state.version[f]):
            state[f] = pkt.value[f]          # never regress
        pending_nack.discard(f)              # FAST PATH
if gap_detected(pkt.seq):
    schedule NACK after HOLD_DOWN
```

### 4.2 The NACK candidate set — a design point Review 2 documents in §2.2

**The receiver does not know which fields were in the packet it lost.** It never
saw that packet; it sees only a hole in the sequence space. So:

1. A pending NACK starts with **every field** as a candidate.
2. During hold-down, each field that arrives with a newer value is **struck off** —
   the receiver now holds something fresher than the lost packet could have held.
   Count these as `fast_path_strikes` (~477 over a 30 s run).
3. The NACK carries whatever remains. The sender intersects that mask with
   `send_log.contents_of(seq)`, which is the authoritative record, and adjudicates
   only the intersection.

Get this right or the fast path is cosmetic.

### 4.3 Sender on NACK — `supersession.py`, the heart of it

```
requested = send_log.contents_of(N) & mask
live = set()
for f in requested:
    if send_log.last_seq[f] > N:          # TEST 1: already re-sent
        suppress as SUPERSEDED            # lost copy is obsolete
        continue
    if ewma_interval[f] <= deadline[f]:   # TEST 2: staleness tolerated
        suppress as WITHIN_TOLERANCE
        continue
    live.add(f)

if not live: send SKIP(N)                 # 0 payload bytes
else:        send REPAIR(N, live)
```

**Put this in a comment in `supersession.py`** — it is the subtlest idea in the
project and the most likely viva question:

> A NACK reaches the sender exactly `τ + H + R` after the loss. §5 derives the
> crossover — the update period above which a repair can still beat the next
> natural update — and gets the same quantity. So any field whose next value was
> due sooner has **already been re-sent** by the time the NACK lands, and Test 1
> catches it. The send-log test enforces the analytical crossover **by direct
> observation, never by estimating a rate.** That is why it cannot produce a false
> suppression, and why the implementation never computes T* at runtime.
>
> Test 2 is a genuinely separate filter asking whether the application even cares,
> given D_f. Combined rule: repair f only if `T_f > max(τ + H + R, D_f)`.

Return a structured `Decision` with a per-field `outcome` (enum: SUPERSEDED /
WITHIN_TOLERANCE / REPAIR) and a human-readable `detail` string — the demo prints
those `detail` strings verbatim, so write them for a reader.

### 4.4 Heartbeat — correctness, not an optimisation

Tests 1 and 2 are **safe** (the most recent transmission of a field is never
suppressed) but not **live**. If the NACK covering a field's most recent
transmission is itself lost, or the field stops updating, the receiver keeps a
stale value forever and no sequence gap will ever reveal it.

So every `HEARTBEAT_S` the sender emits a digest of `(field_id, version)` pairs;
the receiver diffs it against its state table and requests anything it lacks.
DDS works the same way.

---

## 5. The model — `crossover.py`

Derived in Review 1 §8.6 *before* any measurement:

```
repair path   gap seen at τ + R/2, hold-down to τ + R/2 + H,
              NACK at sender τ + R + H, repair lands at  τ + H + 1.5R
natural path  next update of f lands at  T_f + R/2

repair wins iff  τ + H + 1.5R < T_f + R/2   ⟺   T* = τ + H + R
```

```python
def crossover(tick_s, hold_down_s, rtt_s):
    """Minimum update period above which a repair can still be useful."""
    return tick_s + hold_down_s + rtt_s

def expected_gain(period_s, tick_s, hold_down_s, rtt_s, loss_rate):
    """(1-p) * max(0, T_f - T*).  Zero below the crossover."""
    return (1 - loss_rate) * max(0.0, period_s - crossover(tick_s, hold_down_s, rtt_s))
```

With τ=10 ms, H=5 ms, R=60 ms this predicts **T\* = 75 ms**.

Two consequences to state in comments: (a) a *single* periodic field can never
benefit from gap-based repair, because detection needs a later packet and if that
packet carries the same field the receiver already has something fresher —
**repair only has a job because fields share a flow**; (b) the derivation assumes
i.i.d. loss, so under bursts the crossover shifts right. Don't chase a closed form
for that; measure it.

---

## 6. The harness — `sim.py`

**Discrete-event simulation with real packet encoding.** Every transmission goes
through `header.pack()` / `unpack()`, so the wire format, bitmap, version counters
and wrap-safe comparisons are genuinely exercised. Only the clock and the link are
simulated. This buys determinism, exact AoI, and a feasible parameter sweep.

Event loop over a `heapq` of `(time, tie, kind, data)`. Kinds: `TICK`, `FWD`,
`TIMER`, `REV`, `HEARTBEAT`, `HB_RX`, `HB_REQ`.

Four policies, so they can be compared on one loss trace:

| | |
|---|---|
| `ours` | per-field supersession + tolerance |
| `arq` | B1: retransmit every lost field in full |
| `udp` | B4: no recovery |
| `snapshot` | B3: whole state every tick, fragmented over MTU, all fragments must survive |

### ⚠ Three bugs that will bite — avoid them

**1. Shared RNG streams invalidate the comparison.** Use **three independent
channels**: `fwd_data`, `fwd_ctrl`, `rev`. The data channel is consumed exactly
once per tick by every policy, so `ours`, `arq` and `udp` see an *identical* loss
trace. If repairs and data share one RNG, a policy that sends more repairs also
gets a different loss pattern and the whole comparison is worthless. Review 2 §3.3
claims this explicitly.

**2. `prune()` must be amortised.** Pruning `packet_contents` on every tick is
O(n) per packet and therefore O(n²) overall — a 180 s run takes minutes instead of
under a second. Only do work once the table exceeds `keep * 2`, then rebuild.

**3. The invariant audit must cover only SUPERSEDED events.** Tolerance
suppressions legitimately apply to fields that were *not* re-sent — that is their
entire purpose. Writing both kinds into one list and auditing all of them against
the supersession criterion reports a false violation. Keep `suppression_events`
and `tolerance_events` separate. (Review 2 §8.3 documents this as a defect the
property test caught — so it must actually be in the code as described.)

Also: exclude a `warmup_s = 3.0` window from AoI statistics, or startup transients
dominate everything.

---

## 7. Metrics — `metrics.py`

In-memory ring buffer, flushed to CSV at teardown. **Never write to disk inside
the receive loop** — it contaminates the latency numbers. Use
`time.perf_counter_ns()`, never `time.time()`.

Track: `wire_bytes`, `useful_payload_bytes`, `repair_bytes`, `heartbeat_bytes`,
`obsolete_bytes_suppressed`, `tolerance_bytes_suppressed`, `fast_path_strikes`,
per-field AoI (area / time / peak), `fresh_latency_s` samples,
`decision_times_us`, and the two suppression lists.

Derived: `useful_delivery_efficiency`, `total_overhead_ratio`, `avg_aoi(fid)`,
`mean_aoi_all`, `latency_percentile(q)`, `mean_decision_us`, `p99_decision_us`.

`verify_no_false_suppression(events, transmissions)` must re-derive the answer
from the **raw transmission history**, not from the send log, or it is a tautology.

---

## 8. Tests — 18 of them, all passing

`tests/test_protocol.py` (11): header round-trip single + 200 randomised;
header is exactly 5 bytes; wrong width rejected; `seq_newer` basic and
**across wrap** (0 newer than 16383); `version_newer` across wrap; state table
applies newer, rejects older and duplicate, rejects old across version wrap;
heartbeat digest finds behind-version and never-seen fields.

`tests/test_supersession.py` (7): re-sent field is superseded and reply is SKIP;
not-re-sent field is repaired; **mixed packet splits the decision** (the core
claim — attitude abandoned, terrain_map recovered, in one lost packet); tolerance
filter fires independently; most recent transmission never suppressed; **property
test** over 25 randomised runs (loss 5–45%, both loss models, three RTTs) asserting
zero false suppressions; and over 120 s, no field below T* is ever repaired and no
critical field is ever suppressed.

---

## 9. `demo.py` — the viva deliverable

Single process, both ends in-process. **Group the trace by sequence number, not
time order** — one loss burst interleaves with the next and raw ordering is
unreadable. Print whole episodes: loss → gap → NACK → per-field decisions →
SKIP/REPAIR. Lead with one suppressed example and one repaired example so both
outcomes are visible immediately.

Target output:

```
  ── episode 3:  packet seq 50 ──────────────────────────────────
     t=  0.510s  ✗ LOST      seq 50 carrying [attitude, lidar_scan]
     t=  0.550s  ? GAP       receiver detects seq 50 never arrived
     t=  0.555s  ← NACK      asking sender about seq 50 (7 candidate fields)
     t=  0.587s  • DECIDE    lidar_scan  SUPERSEDED - re-sent at seq 55 > 50;
                             lost copy is obsolete (900 B saved)
     t=  0.615s  ○ SKIP      settled with 0 payload bytes
```

Open with a banner listing each field, its period, and the verdict the model
predicts — so the audience can check the system against the theory live.
Close with a summary ending in `FALSE SUPPRESSIONS  0  <-- PASS`.

Flags: `--episodes N`, `--compare` (ours vs B1 vs B4 side by side on one loss
trace), `--duration`, `--loss`, `--rtt`, `--seed`, `--quiet`.

---

## 10. Experiments — `experiments.py`

Five seeds per configuration, 95% CI, as Review 2 commits.

**Crossover validation (the headline figure).** Two-field probe schema: a filler
at tick rate plus a probe whose period sweeps `T_f/R` from 0.1 to 20. Measure
`(AoI_udp − AoI_arq) / period` — the benefit of repairing, normalised. Run at
**two loss rates (5% and 20%)** and plot both with the predicted crossover as a
vertical line. The 5% curve should sit at zero below T* and rise above it; the 20%
curve shows benefit below T* from consecutive losses, which is a real limit of the
first-order model and must be reported as such.

**Policy comparison.** Bar chart: repair bytes, and mean AoI with error bars.

**Per-field table.** AoI under ours / arq / udp for each field, against T*.

**Workload sensitivity (§3.1).** Sweep the width of the fast field from 4 B to
1400 B and plot repair-byte saving against it. This measures how much of the
result is the mechanism and how much is the chosen workload — which is a finding
in its own right, and the answer to "did you pick a workload that flatters you?"

---

## 10b. Reproducibility parameters

Pin these exactly. They do not change what the system *is*, but leaving them free
makes measurements drift between implementations for no reason.

```python
SEEDS = (11, 23, 37, 59, 71)        # five seeded runs

# RNG seeding inside Simulation(seed=s) — keep these offsets
generator   = random.Random(s + 1)
fwd_data    = random.Random(s + 2)   # DATA packets only
fwd_ctrl    = random.Random(s + 3)   # repairs, skips, heartbeats
rev         = random.Random(s + 4)   # NACKs

# Generator first-fire stagger, so fields don't all land on tick 0
periodic:      next_fire = rng.uniform(0, field.period)
event_driven:  next_fire = rng.expovariate(1.0 / field.mean_gap)

# Gilbert–Elliott, parameterised so steady-state BAD time == loss_rate
p_bg = 1.0 / mean_burst_len                      # BAD -> GOOD
p_gb = (loss_rate * p_bg) / (1.0 - loss_rate)    # GOOD -> BAD
drop  = currently in BAD   (advance the chain once per packet, then test)

# Default run configuration for the policy comparison
duration_s = 30.0,  loss_rate = 0.10,  burst = True,  mean_burst_len = 4.0
tick = 0.010,  hold_down = 0.005,  rtt = 0.060,  jitter = 0.002
heartbeat = 1.0,  ewma_alpha = 0.2,  warmup_s = 3.0
```

Even with all of this pinned, expect small numeric differences from any other
implementation — floating-point accumulation and event-queue tie-breaking are
enough to shift a loss trace. Magnitudes should land in the same neighbourhood;
the invariants below must hold exactly.

---

## 11. Telling a result from a bug

**There are no target numbers in this spec.** Whatever the experiments produce is
the finding. What follows are *invariants* — things that must hold because of how
the mechanism is defined, not because of how it happened to perform. A violation
means a defect; an unexpected magnitude does not.

**Hard invariants. If any of these fails, there is a bug:**

| Invariant | Why it must hold |
|---|---|
| False suppressions = 0, always | §4.3 Test 1 only fires when a field was provably re-sent |
| No field below T* is ever repaired | its next update always beats the NACK to the sender |
| No critical field is ever suppressed as superseded | by definition it has no newer copy |
| Ours never sends more repair bytes than B1 | its repair set is a strict subset of B1's |
| B4 never sends repair bytes | it has no recovery mechanism |
| Identical data-channel loss count across ours / B1 / B4 | §6, the three-channel design |
| Receiver state never regresses to an older version | `version_newer` guard in `state_table.apply` |

**Soft signals. Worth investigating, but could legitimately be the answer:**

- Repair-byte saving under ~10 %. Check the schema's byte distribution first
  (§3.1) — a workload of only small fast fields genuinely yields little, and that
  is a real finding about when the mechanism is worth deploying.
- Ours showing *better* mean AoI than B1. B1 repairs a superset, so it should be
  equal or fresher. If ours looks better, suspect divergent control-channel RNG
  before believing it.
- Measurable repair benefit below T* at high loss. This is real — it comes from
  consecutive losses, which the first-order model neglects. It should shrink
  toward zero as loss drops. If it does not shrink at 5 % loss, that is a bug.

**On reporting statistics:** compare confidence intervals before claiming a
difference. If they overlap, the honest statement is that the difference is not
statistically significant — not that one side won.

**If results disagree with the submitted Review 2.** They may, and that is fine.
Review 2 reported one run of one implementation; a reimplementation can land
elsewhere. Report what you measure, and note the discrepancy explicitly in
Review 3 along with why you think it occurred. A documented, explained difference
reads as careful work. Quietly tuning the code until it reproduces a previously
published number is the one thing that would actually damage the project.

---

## 12. Build order

**Phases are checkpoints, not a calendar.** Each is roughly 20–60 minutes of work.
Running all six in one sitting is fine — the point is to review after each, so a
failure has a small surface, and so the people presenting this have watched it
being built. Do not start a phase until the previous one runs.

### Block A — by 15 October (Review 2 viva)

| Phase | Build | Stop and show | Est. |
|---|---|---|---|
| 1 | `config.py`, `schema.py`, `header.py` + 11 protocol tests | tests passing | ~30 min |
| 2 | `state_table.py`, `generator.py`, `sim.py` skeleton, zero loss | state replicates | ~45 min |
| 3 | `impairment.py` (i.i.d. first), `gap_detector.py`, NACK path | B1 full ARQ works | ~60 min |
| 4 | `send_log.py`, `supersession.py`, `demo.py` | **the project — narrated demo** | ~60 min |
| 5 | heartbeat, `crossover.py`, tolerance filter, 7 decision tests | 18/18 passing | ~30 min |
| 6 | Gilbert–Elliott, B3/B4, `metrics.py`, `experiments.py`, figures, workload-sensitivity sweep (§3.1) | invariants in §11 hold | ~90 min |

The Review 2 document already describes all six as complete, so the viva demo has
to match it.

### Block B — 16 to 25 October (Review 3, due Oct 26–30)

| Phase | Build | Why it is required |
|---|---|---|
| 7 | `udp_driver.py`: the **same** sender/receiver modules over real UDP sockets on localhost | Review 3 item 3, "Complete Implementation". Review 2 honestly declared the discrete-event harness; this closes it. Only the transmit/receive calls change. |
| 8 | `fec.py` repetition coding + baseline **B2 blanket FEC at matched byte overhead** | B2 is the strongest competitor — no retransmission means no staleness. Omitting it leaves the comparison open to the obvious objection. |
| 9 | **Kernel TCP** reference under tc/netem over a veth pair; final sweep; all figures | Review 3 item 7, "Comparison with Existing System". Your own B1–B4 do not satisfy that — it wants a real system. |
| 10 | Review 3 document + 12-slide deck | — |

**Scoped out, and say so in the document rather than leaving them looking
forgotten:** block FEC (Reed–Solomon), the CycloneDDS baseline, and the
two-machine WiFi run. Each is a few days of setup and none changes the core claim.

---

## 12b. Review 3 end-state checklist

Review 3 asks for eleven things. Build toward these from Phase 1 rather than
retrofitting in the last week. Most are already covered by the work above.

| # | Review 3 requirement | What answers it | Status after Block A |
|---|---|---|---|
| 1 | Complete Working Project | the repo | ✓ |
| 2 | Final System Architecture | architecture diagram + §2 | ✓ |
| 3 | Complete Implementation | **needs Phase 7 (real sockets)** | gap |
| 4 | Live Demonstration | `demo.py --compare` | ✓ |
| 5 | Test Results | 18 tests + the property test | ✓ |
| 6 | Performance Analysis | crossover sweep, AoI, per-decision cost | ✓ |
| 7 | Comparison with Existing System | **needs Phase 9 (kernel TCP)**; B1–B4 alone are insufficient | gap |
| 8 | Achievement of Objectives | map each Review 1 objective to evidence | write-up only |
| 9 | Innovation / Real-World Application | sub-packet selective recovery; UAV / SCADA / teleoperation | write-up only |
| 10 | Future Enhancement | the scoped-out items, honestly listed | write-up only |
| 11 | **Outcome (Conference / Patent / Journal / Book Chapter)** | **needs a decision this week — see below** | gap |

### On item 11 — decide now, not on 25 October

This is a graded line and it may carry an external deadline. Options, in order of
how much is within your control:

1. **arXiv preprint.** Entirely in your hands, citable, dated. Needs a paper
   written, which is largely Review 3's content restructured. Safest answer.
2. **Poster or demo track** at a venue with a live CFP. Check current dates for
   COMSNETS, IEEE ANTS and NCC — full-paper deadlines for the next cycle may
   already have passed, and poster tracks usually run later.
3. **Named venue with a stated submission plan**, if deadlines have passed.
   Weaker than an actual submission but honest, and better than leaving it blank.

Talk to your faculty supervisor about this early. A faculty co-author is usually
needed for the venue anyway, and they will know the institutional options.

---

## 13. Explicit "do not"

- ❌ No `asyncio`, no ABCs, no inheritance hierarchies, no plugin registries
- ❌ No YAML/TOML/argparse config framework — `config.py` only
- ❌ No database. CSV.
- ❌ No `__slots__`, no Cython, no vectorising
- ❌ Don't reformat working code to make it "cleaner"
- ❌ Don't add features not in this spec — ask first
- ❌ Don't claim a difference the confidence intervals don't support, in either direction
- ❌ Don't tune code to reproduce a number written down earlier — report what you measure

---

## 14. Ownership

| Member | Reg. No. | Owns |
|---|---|---|
| Koliparthy Venkata Jahnvi | 24BDS0132 | `schema.py`, `header.py`, `generator.py`, protocol tests, microbenchmark |
| Somesh Tripathi | 24BCE0533 | `send_log.py`, `supersession.py`, `state_table.py`, `gap_detector.py`, `sim.py`, decision tests |
| Anushka Jaiswal | 24BCE2028 | `impairment.py`, `metrics.py`, baselines, `experiments.py`, figures |

---

## 15. First task

Build the system on its own terms. Measure it. Then write up what it did.

Phases are checkpoints, not a schedule — running several in one sitting is fine,
but stop and show after each so a failure has a small surface and the team has
watched the thing being built.

Phase 1 only: repo skeleton, `config.py`, `schema.py`, `header.py`, and
`tests/test_protocol.py`. Show me the 11 tests passing — especially the
across-wrap sequence comparison — then **stop and wait**. Do not continue into
Phase 2 unprompted.
