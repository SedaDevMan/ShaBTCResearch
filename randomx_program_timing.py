"""
randomx_program_timing.py — RandomX program execution time variation

QUESTION: Do different block hashes produce programs that execute in
          measurably different times? If so, can you predict which
          programs are fast BEFORE mining starts?

This mirrors the GhostRider CN variant analysis:
  GhostRider: CN scratchpad size deterministic from prev_hash → 16× cost range
  RandomX:    random program instruction mix deterministic from prev_hash → ?? cost range

METHODOLOGY:
  1. Generate N programs (one per block hash)
  2. Benchmark each program: time to compute K hashes
  3. Compute timing CV = std/mean across programs
  4. If CV > 0.05 (5%): measurable variation exists
  5. Test: can block hash features predict program speed?
  6. Selective mining model: skip slow programs, efficiency gain?
"""

import time, hashlib
import numpy as np
from scipy import stats
import randomx_sim

N_PROGRAMS   = 500       # distinct block programs to benchmark
BENCH_NONCES = 1000      # nonces per timing measurement (enough to average out noise)
GENESIS      = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")

print("═"*68)
print("  RandomX Program Timing Variation Analysis")
print("═"*68)
print(f"  {N_PROGRAMS} programs × {BENCH_NONCES} nonces each")
print(f"  Q: Do different random programs execute in different times?")
print()

# ── Generate N block hashes via SHA256d chain ─────────────────────────────────
def make_chain(n):
    h = GENESIS
    chain = [h]
    for _ in range(n - 1):
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
        chain.append(h)
    return chain

CHAIN = make_chain(N_PROGRAMS)

# ── Benchmark each program ────────────────────────────────────────────────────
print("  Benchmarking programs ...")
times = []
t_total = time.perf_counter()

for i, prev in enumerate(CHAIN):
    randomx_sim.set_program(prev)
    t0 = time.perf_counter()
    for n in range(BENCH_NONCES):
        randomx_sim.hash(prev, n)
    elapsed = time.perf_counter() - t0
    times.append(elapsed)
    if (i + 1) % 100 == 0:
        print(f"  ... {i+1}/{N_PROGRAMS}  elapsed={time.perf_counter()-t_total:.1f}s")

times = np.array(times)
print(f"  Done: {time.perf_counter()-t_total:.1f}s total\n")

# ── TEST 1: Timing distribution statistics ────────────────────────────────────
print("─"*68)
print("  TEST 1: Program execution time distribution")
print("─"*68)

mean_t  = times.mean()
std_t   = times.std()
cv      = std_t / mean_t
min_t   = times.min()
max_t   = times.max()
ratio   = max_t / min_t

print(f"  Time per {BENCH_NONCES} nonces (seconds):")
print(f"    Mean  = {mean_t:.4f}s")
print(f"    Std   = {std_t:.4f}s")
print(f"    CV    = {cv:.4f}  ({cv*100:.2f}%)")
print(f"    Min   = {min_t:.4f}s  (fastest program)")
print(f"    Max   = {max_t:.4f}s  (slowest program)")
print(f"    Ratio = {ratio:.3f}×  (slowest/fastest)")

# Percentiles
p10, p25, p50, p75, p90 = np.percentile(times, [10, 25, 50, 75, 90])
print(f"    P10={p10:.4f}  P25={p25:.4f}  P50={p50:.4f}  P75={p75:.4f}  P90={p90:.4f}")

# Is the distribution significantly non-constant? (F-test / ANOVA across repeated measurements)
# But we only have one measurement per program. Use Levene/Shapiro on the time distribution.
_, p_normal = stats.shapiro(times[:50])  # Shapiro on first 50 (limit)
print(f"\n  Shapiro-Wilk normality (first 50): p={p_normal:.4f}")

threshold_cv = 0.05
if cv > threshold_cv:
    print(f"\n  CV={cv*100:.2f}% > 5% → SIGNIFICANT timing variation between programs")
    print(f"  Max/min ratio = {ratio:.3f}× → slowest program takes {ratio:.2f}× longer")
else:
    print(f"\n  CV={cv*100:.2f}% ≤ 5% → programs execute in nearly identical time")
    print(f"  Variation is likely measurement noise, not structural difference")
print()

# ── TEST 2: Instruction mix analysis ─────────────────────────────────────────
print("─"*68)
print("  TEST 2: Instruction mix variation across programs")
print("  (Count ADD/SUB/MUL/LOAD/etc per program and correlate with time)")
print("─"*68)
print()

# We need to expose instruction counts. Use set_program and analyze via Python
# by reimplementing select_indices logic for instruction counting.
# We'll use the Python ghostrider select_indices approach — but here we
# re-implement the program generation in Python to count instruction types.

OP_NAMES = ['ADD', 'SUB', 'XOR', 'MUL', 'ROT', 'LOAD', 'STORE', 'CBRANCH']
OP_TABLE = (
    [0]*15 +   # ADD
    [1]*10 +   # SUB
    [2]*10 +   # XOR
    [3]*12 +   # MUL
    [4]*10 +   # ROT
    [5]*8  +   # LOAD
    [6]*4  +   # STORE
    [7]*5      # CBRANCH
)

def xorshift64(state):
    state ^= (state << 13) & 0xFFFFFFFFFFFFFFFF
    state ^= (state >> 7)
    state ^= (state << 17) & 0xFFFFFFFFFFFFFFFF
    return state & 0xFFFFFFFFFFFFFFFF

def count_instructions(prev: bytes) -> np.ndarray:
    """Return instruction type counts for program derived from prev."""
    import hashlib as _hl
    seed = _hl.sha3_256(prev).digest()
    seed2 = _hl.sha3_256(seed).digest()
    prng = int.from_bytes(seed2[:8], 'little') or 1

    counts = np.zeros(8, dtype=int)
    cbranch_count = 0
    for _ in range(256):
        prng = xorshift64(prng)
        op = OP_TABLE[prng & 63]
        if op == 7 and cbranch_count >= 3:
            op = 2  # XOR instead
        if op == 7:
            cbranch_count += 1
        counts[op] += 1
    return counts

print("  Counting instruction types for all programs ...")
t0 = time.perf_counter()
instr_counts = np.array([count_instructions(prev) for prev in CHAIN])
print(f"  Done: {time.perf_counter()-t0:.1f}s\n")

print(f"  {'Op':8s}  {'Mean':>6}  {'Std':>5}  {'CV%':>6}  Corr with time  p-value")
print("  " + "-"*60)
for op_idx, op_name in enumerate(OP_NAMES):
    col = instr_counts[:, op_idx].astype(float)
    mean_c = col.mean()
    std_c  = col.std()
    cv_c   = std_c / mean_c if mean_c > 0 else 0
    r, p   = stats.pearsonr(col, times)
    sig    = "◄ SIGNAL" if p < 0.01 and abs(r) > 0.1 else ""
    print(f"  {op_name:8s}  {mean_c:>6.1f}  {std_c:>5.2f}  {cv_c*100:>5.1f}%  "
          f"r={r:+.4f}  p={p:.4f}  {sig}")

print()

# ── TEST 3: Can block hash features predict program speed? ────────────────────
print("─"*68)
print("  TEST 3: Block hash features vs execution time")
print("  Q: Can we predict fast/slow programs from prev_hash WITHOUT running?")
print("─"*68)
print()

# Features from prev_hash: nibble values, byte sums, etc.
features = {}
for prev in CHAIN:
    b = prev
    features.setdefault('byte0', []).append(b[0])
    features.setdefault('byte31', []).append(b[31])
    features.setdefault('xor_all', []).append(sum(b) % 256)
    features.setdefault('nibble0', []).append(b[0] & 0xF)
    features.setdefault('nibble1', []).append(b[0] >> 4)
    features.setdefault('sum_lo_nibbles', []).append(sum(x & 0xF for x in b) & 0xFF)
    features.setdefault('sum_hi_nibbles', []).append(sum(x >> 4 for x in b) & 0xFF)

print(f"  {'Feature':20s}  {'Pearson r':>10}  {'p-value':>8}  Result")
print("  " + "-"*48)
any_signal = False
for fname, fvals in features.items():
    fv = np.array(fvals, dtype=float)
    r, p = stats.pearsonr(fv, times)
    sig = p < 0.01 and abs(r) > 0.1
    any_signal = any_signal or sig
    print(f"  {fname:20s}  {r:>+10.4f}  {p:>8.4f}  {'◄ SIGNAL' if sig else ''}")

print()
if not any_signal:
    print("  No block hash feature predicts program speed.")
    print("  → The SHA3 chain from prev_hash to program seeds mixes everything away.")
else:
    print("  SIGNAL: some block hash feature correlates with program speed!")
print()

# ── TEST 4: Selective mining efficiency ───────────────────────────────────────
print("─"*68)
print("  TEST 4: Selective mining efficiency")
print("  Strategy: skip programs slower than threshold (idle during skipped blocks)")
print("─"*68)
print()

# Normalize times to relative cost (1.0 = mean)
rel_cost = times / times.mean()
baseline_hps = 1.0 / times.mean()  # hashes per second normalized

print(f"  {'Strategy':35s}  {'Blocks mined':>12}  {'Rel hash rate':>14}  {'Efficiency':>12}")
print("  " + "-"*76)
print(f"  {'Mine all blocks':35s}  {'100.0%':>12}  {'1.000×':>14}  {'1.000×':>12}")

for pct in [10, 20, 30, 40, 50, 60, 70, 80, 90]:
    threshold = np.percentile(rel_cost, pct)
    mine_mask = rel_cost <= threshold
    pct_mined = mine_mask.mean() * 100

    if pct_mined == 0:
        continue

    mined_costs = rel_cost[mine_mask]
    avg_rel_cost  = mined_costs.mean()
    hash_rate     = 1.0 / avg_rel_cost       # relative hash rate when mining
    time_mining   = mine_mask.mean()
    effective_hr  = hash_rate * time_mining  # overall including idle time
    efficiency    = effective_hr             # vs baseline of 1.0

    better = "◄ BETTER" if efficiency > 1.02 else ""
    print(f"  {'Skip slowest '+str(100-pct)+'%':35s}  "
          f"{pct_mined:>11.1f}%  "
          f"{hash_rate:>13.4f}×  "
          f"{efficiency:>11.4f}×  {better}")

print()

# ── SUMMARY ──────────────────────────────────────────────────────────────────
print("═"*68)
print("  SUMMARY")
print("═"*68)
print()
print(f"  Programs tested:      {N_PROGRAMS}")
print(f"  Timing CV:            {cv*100:.2f}%")
print(f"  Max/min ratio:        {ratio:.3f}×")
print()

if cv > 0.05:
    print("  TIMING VARIATION EXISTS:")
    print(f"  → Programs differ by up to {ratio:.2f}× in execution time")
    print(f"  → CV={cv*100:.1f}% is large enough to be exploitable")
    print(f"  → See TEST 3 to check if speed is predictable from block hash")
    if any_signal:
        print(f"  → BLOCK HASH PREDICTS SPEED — selective mining is feasible!")
    else:
        print(f"  → Block hash does NOT predict speed (SHA3 mixes it away)")
        print(f"  → Cannot exploit timing variation without running the program first")
else:
    print("  NO EXPLOITABLE TIMING VARIATION:")
    print(f"  → CV={cv*100:.1f}% is within measurement noise")
    print(f"  → All RandomX programs execute in essentially the same time")
    print(f"  → Selective mining based on program speed is NOT feasible")
    print()
    print("  WHY: RandomX uses a fixed instruction count (256 instructions)")
    print("  CBRANCH is capped at 3 per program and rarely fires (mask condition)")
    print("  → All programs do approximately the same work")
    print()
    print("  NOTE: Real RandomX has 2 key differences from this simulation:")
    print("  1. 256MB dataset (L3 scratchpad): LOAD/STORE dominate execution time")
    print("     Cache miss patterns vary by instruction mix → real timing variation")
    print("  2. Floating-point instructions (FMUL, FSQRT, etc.) are slower than INT")
    print("     A program heavy in FSQRT may be 2× slower than an ADD-heavy program")
    print("  → Real RandomX MIGHT have measurable timing variation")
    print("     but you'd need the actual RandomX library to test it")
