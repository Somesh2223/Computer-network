"""Every tunable constant lives here, as plain module-level values.

Defaults are the pinned reproducibility parameters from BUILD_PROMPT.md §10b.
"""

SEEDS = (11, 23, 37, 59, 71)       # five seeded runs per configuration

# --- default run configuration for the policy comparison -------------------
DURATION_S = 30.0
LOSS_RATE = 0.10
BURST = True                       # Gilbert-Elliott instead of i.i.d.
MEAN_BURST_LEN = 4.0
TICK_S = 0.010                     # sender packs pending updates every tick
HOLD_DOWN_S = 0.005                # receiver waits this long before NACKing
RTT_S = 0.060
JITTER_S = 0.002
BIT_ERROR_RATE = 0.0              # chance each bit flips in transit; 0 = no corruption
HEARTBEAT_S = 1.0
EWMA_ALPHA = 0.2                   # smoothing for the per-field update interval
WARMUP_S = 3.0                     # excluded from AoI statistics

# --- wire format ------------------------------------------------------------
SEQ_BITS = 14
VERSION_BITS = 8
HEADER_BYTES = 5
MTU = 1500                         # B3 (snapshot) fragments above this

# --- forward error correction (Phase 8) -------------------------------------
REPETITIONS = 2                    # ours_fec: a small critical field rides in k more packets
REPETITION_MAX_WIDTH = 16          # ...if it is at most this many bytes
REPETITION_SPACING = 5             # packets between copies: more than a typical loss burst
FEC_GROUP = 15                     # B2: data packets per XOR parity packet (4-bit index caps it)
BLOCK_GROUP = 3                    # ours_fec: a large critical update + 2 later packets share one parity
BLOCK_SPACING = 5                  # packets between members of that group, like REPETITION_SPACING
