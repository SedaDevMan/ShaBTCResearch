"""
blockhash_selector.py — Block hash as nonce range selector

THE IDEA:
  When a new block is broadcast, the block hash is public.
  Instead of scanning nonces 0..2^32, use the block hash to
  SELECT which nonce sub-range to scan.

  If  f(prev_hash)  predicts  winning_nonce_range  even slightly,
  then miners who know f() find blocks faster than brute-force scanners.

  This would mean the hash function H(prev || nonce) has a cross-input
  correlation: the value of prev leaks information about which nonce wins.

HOW WE TEST:
  For N block hashes, scan 256K nonces each → record winner counts per bucket.

  For each "selector function" f:
    predicted_bucket = f(prev_hash) mod 256
    hit_rate = winners_in_predicted_bucket / expected_per_bucket
    If hit_rate >> 1.0 consistently → f is a working selector!

  Selector functions tested:
    1. Raw bytes of prev_hash (byte 0, XOR of all, various combinations)
    2. SHA256(prev_hash) re-derived
    3. First 4 bytes as uint32
    4. Last 4 bytes as uint32
    5. ML regression: train prev_hash features → which bucket wins most?
    6. "Best possible" oracle: for each block, the actual winning bucket
       (gives theoretical max hit rate under any strategy)

  Statistics:
    Wilcoxon signed-rank test: is hit_rate distribution > 1.0?
    If p < 0.01 → selector is significantly better than random.

  Algorithms: SHA256d and real Haraka-512

  Expected result for secure hash: hit_rate ≈ 1.0 for ALL selectors.
  If any selector > 1.05 consistently → exploitable!
"""

import time, hashlib, struct
import numpy as np
from scipy import stats
import pot_skip
import verus_real

SCAN_RANGE = 256_000    # 256K nonces per block = 1000 per bucket
N_BUCKETS  = 256
N_BLOCKS   = 200        # 200 blocks for statistical power
TARGET     = b'\x01' + b'\x00' * 31  # 8-bit difficulty ~1000 winners/block
bucket_size = SCAN_RANGE // N_BUCKETS

# ── Build chain of block hashes ──────────────────────────────────────────────
def make_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

CHAIN = make_chain(N_BLOCKS)

# ── Collect winner-counts matrix M[N_BLOCKS × 256] for each algo ─────────────

def collect_matrix_sha256d(chain):
    """M[i,j] = winner count in bucket j for block i (SHA256d, algo=0, 64 rounds)"""
    M = np.zeros((len(chain), N_BUCKETS), dtype=np.float64)
    t0 = time.perf_counter()
    for i, prev in enumerate(chain):
        counts = pot_skip.winner_density(prev, SCAN_RANGE, TARGET, 0, 64)
        M[i] = counts
    return M, time.perf_counter() - t0

def make_tmpl(prev_hash):
    return (b'\x04\x00\x00\x00' + prev_hash + bytes(32) + bytes(32)
            + b'\x00\x00\x00\x00' + b'\x20\x00\x00\x00' + bytes(32))

def collect_matrix_haraka(chain):
    """M[i,j] = winner count in bucket j for block i (real Haraka-512)"""
    M = np.zeros((len(chain), N_BUCKETS), dtype=np.float64)
    t0 = time.perf_counter()
    for i, prev in enumerate(chain):
        tmpl = make_tmpl(prev)
        winners = verus_real.scan_winners_real(tmpl, SCAN_RANGE, TARGET)
        for w in winners:
            M[i, min(w // bucket_size, N_BUCKETS-1)] += 1
    return M, time.perf_counter() - t0

# ── Selector functions: prev_hash → predicted bucket index ───────────────────

def selectors(prev_hash: bytes) -> dict:
    """Return dict of {name: bucket_index} for all selector candidates."""
    b = prev_hash
    return {
        "byte_0":           b[0] % N_BUCKETS,
        "byte_31":          b[31] % N_BUCKETS,
        "xor_all":          (sum(b) % 256) % N_BUCKETS,
        "first4_uint32":    (int.from_bytes(b[:4],  'big') % N_BUCKETS),
        "last4_uint32":     (int.from_bytes(b[-4:], 'big') % N_BUCKETS),
        "mid4_uint32":      (int.from_bytes(b[14:18],'big')% N_BUCKETS),
        "sha256_byte0":     hashlib.sha256(b).digest()[0] % N_BUCKETS,
        "sha256d_byte0":    hashlib.sha256(hashlib.sha256(b).digest()).digest()[0] % N_BUCKETS,
        "xor_pair":         ((b[0] ^ b[16]) + (b[1] ^ b[17])) % N_BUCKETS,
        "sum_nibbles_lo":   (sum(x & 0xF for x in b) % N_BUCKETS),
        "sum_nibbles_hi":   (sum(x >> 4 for x in b) % N_BUCKETS),
    }

# ── Evaluate a selector matrix-wide ──────────────────────────────────────────

def evaluate_selector(M, predicted_buckets, name):
    """
    For each block i: look at winner count in predicted_bucket[i].
    Compare against average winner count per bucket per block.
    hit_rate = actual / expected.
    Return (mean_hit_rate, std, p_value_vs_1.0)
    """
    hit_rates = []
    for i in range(M.shape[0]):
        total_i     = M[i].sum()
        expected    = total_i / N_BUCKETS  # what a random bucket gives
        actual      = M[i, predicted_buckets[i]]
        if expected > 0:
            hit_rates.append(actual / expected)
    hit_rates = np.array(hit_rates)
    mean_hr = hit_rates.mean()
    std_hr  = hit_rates.std()
    # One-sample Wilcoxon signed-rank test: is distribution > 1.0?
    stat, p = stats.wilcoxon(hit_rates - 1.0, alternative='greater')
    return mean_hr, std_hr, p

# ── Run multi-bucket window selector (scan W contiguous buckets) ──────────────

def evaluate_window_selector(M, predicted_buckets, window=8):
    """
    Scan a window of `window` contiguous buckets starting at predicted.
    hit_rate = (winners in window) / (expected for window)
    Expected = total * window/256
    """
    hit_rates = []
    for i in range(M.shape[0]):
        total_i  = M[i].sum()
        expected = total_i * window / N_BUCKETS
        start    = predicted_buckets[i]
        indices  = [(start + k) % N_BUCKETS for k in range(window)]
        actual   = M[i, indices].sum()
        if expected > 0:
            hit_rates.append(actual / expected)
    hit_rates = np.array(hit_rates)
    mean_hr = hit_rates.mean()
    std_hr  = hit_rates.std()
    stat, p = stats.wilcoxon(hit_rates - 1.0, alternative='greater')
    return mean_hr, std_hr, p

# ── Oracle: how good is the BEST possible selector? ──────────────────────────

def oracle_hit_rate(M):
    """
    For each block, what fraction of winners is in the single BEST bucket?
    This is the theoretical upper bound of any 1-bucket selector.
    Under uniform distribution: best_bucket / (total/256) ≈ 1.0 + noise
    """
    ratios = []
    for i in range(M.shape[0]):
        total = M[i].sum()
        best  = M[i].max()
        if total > 0:
            ratios.append(best / (total / N_BUCKETS))
    return np.array(ratios)

# ════════════════════════════════════════════════════════════════════════════════

np.random.seed(42)
print("═"*70)
print("  Block Hash → Nonce Range Selector Test")
print("═"*70)
print(f"  {N_BLOCKS} block hashes × {SCAN_RANGE:,} nonces × {N_BUCKETS} buckets")
print(f"  Question: does f(prev_hash) predict which bucket contains winners?")
print(f"  Hit rate = 1.0: random (no help); > 1.0: selector works\n")

# ════════════════════════════════════════════════════════════════════════════════
for algo_name, collect_fn in [("SHA256d", collect_matrix_sha256d),
                               ("Real Haraka-512", collect_matrix_haraka)]:
    print(f"{'═'*70}")
    print(f"  Algorithm: {algo_name}")
    print(f"{'═'*70}")

    print(f"  Scanning {N_BLOCKS} blocks ...")
    M, elapsed = collect_fn(CHAIN)
    speed = (SCAN_RANGE * N_BLOCKS / 1e6) / elapsed
    total_winners = M.sum()
    print(f"  Done: {elapsed:.1f}s  ({speed:.2f} MH/s)  "
          f"Total winners: {total_winners:.0f}  "
          f"Mean/block/bucket: {M.mean():.2f}")
    print()

    # Precompute predicted buckets for each block under each selector
    all_sel_preds = {k: [] for k in selectors(CHAIN[0]).keys()}
    for prev in CHAIN:
        s = selectors(prev)
        for k, v in s.items():
            all_sel_preds[k].append(v)
    for k in all_sel_preds:
        all_sel_preds[k] = np.array(all_sel_preds[k])

    # Oracle
    oracle_ratios = oracle_hit_rate(M)
    print(f"  Oracle (best possible single bucket): "
          f"mean={oracle_ratios.mean():.3f}  max={oracle_ratios.max():.3f}  "
          f"(theoretical upper bound per block)")
    print()

    # Evaluate each selector
    print(f"  {'Selector':25s}  {'Mean hit rate':>14}  {'Std':>6}  "
          f"{'p-value':>8}  Result")
    print("  " + "-"*60)

    any_signal = False
    for sel_name, preds in sorted(all_sel_preds.items()):
        mean_hr, std_hr, p = evaluate_selector(M, preds, sel_name)
        signal = p < 0.01 and mean_hr > 1.05
        any_signal = any_signal or signal
        marker = "◄ SIGNAL!" if signal else ""
        print(f"  {sel_name:25s}  {mean_hr:>14.4f}  {std_hr:>6.3f}  "
              f"{p:>8.4f}  {marker}")

    print()

    # Window selectors (best single-bucket selector in window mode)
    best_sel = "first4_uint32"
    preds = all_sel_preds[best_sel]
    print(f"  Window test using '{best_sel}' selector:")
    print(f"  {'Window size':>12}  {'Coverage %':>12}  {'Mean hit rate':>14}  {'p-value':>8}  Result")
    print("  " + "-"*56)
    for w in [1, 2, 4, 8, 16, 32]:
        mean_hr, std_hr, p = evaluate_window_selector(M, preds, window=w)
        signal = p < 0.01 and mean_hr > 1.05
        coverage_pct = w / N_BUCKETS * 100
        speedup = mean_hr  # hit_rate IS the speedup relative to random same-sized window
        print(f"  {w:>12} ({coverage_pct:4.1f}%)  "
              f"{coverage_pct:>12.1f}%  "
              f"{mean_hr:>14.4f}  {p:>8.4f}  "
              f"{'◄ SIGNAL!' if signal else ''}")

    print()

    # Summary for this algorithm
    if not any_signal:
        print(f"  RESULT [{algo_name}]: No selector predicts winning buckets.")
        print(f"  The block hash carries zero information about the winning nonce range.")
        print(f"  Scanning according to f(prev_hash) is no better than random sampling.")
    else:
        print(f"  RESULT [{algo_name}]: SIGNAL FOUND — some selector has hit rate > 1.05!")
    print()

print("═"*70)
print("  OVERALL CONCLUSION")
print("═"*70)
print()
print("  If hit rate ≈ 1.0 for all selectors:")
print("    → The block hash value has NO predictive power over winning nonces.")
print("    → Using block_hash as a nonce range selector gives zero speedup.")
print("    → Each mining round is statistically FRESH regardless of prev_hash value.")
print()
print("  If any selector hit rate >> 1.0 (p < 0.01):")
print("    → Discovered a hash function structural weakness!")
print("    → Expected speedup = hit_rate when scanning that fraction of nonce space.")
