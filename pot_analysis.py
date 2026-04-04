"""
pot_analysis.py — Intra-POT nonce range structure analysis

For each algorithm + parameter combo, scan a POT and measure:
  - Winner density across 256 nonce buckets
  - Chi-squared test: is the distribution uniform?
  - If non-uniform: which buckets are hot (skip the cold ones)

Algorithms tested:
  SHA256-Nr : 1, 2, 4, 8, 16, 32, 64 rounds (like our AES round test)
  ETHash-lite: simplified DAG with 8 lookups
  SHA256d-midstate: does midstate value predict winner density?

Key question: at what round count does intra-POT uniformity break?
"""

import time, json
import numpy as np
from scipy import stats
import pot_skip

NBLOCKS    = 30       # test across multiple prev_hashes
SCAN_RANGE = 256_000  # 256K nonces per block = 1000 per bucket (256 buckets)
N_BUCKETS  = 256
TARGET_8BIT = b'\x01' + b'\x00' * 31   # 8-bit difficulty → ~1000 winners per scan (uniform hash)

import hashlib, struct

def make_chain(n=30):
    """Generate N synthetic prev_hashes by chaining SHA256d."""
    prevs = []
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    for _ in range(n):
        prevs.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return prevs

CHAIN = make_chain(NBLOCKS)

def chi2_uniformity(counts):
    """Chi-squared test for uniformity over N_BUCKETS buckets."""
    counts = np.array(counts, dtype=float)
    total = counts.sum()
    if total < 10:
        return None, None
    expected = total / N_BUCKETS
    chi2 = ((counts - expected)**2 / expected).sum()
    p    = 1 - stats.chi2.cdf(chi2, df=N_BUCKETS - 1)
    return float(chi2), float(p)

def find_target(algo_id, param, prev, desired_fraction=1/256):
    """
    For non-uniform algos: probe 4096 nonces to find the empirical
    desired_fraction percentile of hash outputs, use that as target.
    Falls back to TARGET_8BIT if output looks uniform.
    """
    PROBE = 4096
    outputs = []
    for nonce in range(PROBE):
        counts = pot_skip.winner_density(prev, 1, b'\xff' * 32, algo_id, param)
        # winner_density with target=0xff* gives all nonces — abuse it to get hash value
        break  # we'll use a different approach below
    # Simpler: use TARGET_8BIT for uniform algos (rounds >= 8)
    # For rounds 1-4, use a higher (easier) target to guarantee enough winners
    if algo_id == 0 and param <= 4:
        # Use 50th percentile target: always get 50% winners → force uniformity test
        return b'\x80' + b'\x00' * 31
    return TARGET_8BIT

def run_test(algo_id, param, label, n_blocks=NBLOCKS):
    """
    Run winner_density for n_blocks prev_hashes, aggregate,
    run chi-squared test.
    Returns dict of stats.
    """
    # For very low round counts, SHA256 output is biased — use easier target
    if algo_id == 0 and param <= 4:
        target = b'\x80' + b'\x00' * 31  # 1-bit difficulty: ~50% winners
    else:
        target = TARGET_8BIT

    all_counts = np.zeros(N_BUCKETS, dtype=np.int64)
    t0 = time.perf_counter()

    for prev in CHAIN[:n_blocks]:
        if algo_id == 1:
            pot_skip.build_dag(prev)
        counts = pot_skip.winner_density(prev, SCAN_RANGE, target, algo_id, param)
        all_counts += np.array(counts, dtype=np.int64)

    elapsed = time.perf_counter() - t0
    speed   = (SCAN_RANGE * n_blocks / 1e6) / elapsed

    total   = int(all_counts.sum())
    chi2, p = chi2_uniformity(all_counts)

    # Hot/cold bucket analysis
    mean_c  = all_counts.mean()
    hot_mask = all_counts > mean_c * 1.5
    cold_mask = all_counts < mean_c * 0.5
    n_hot   = int(hot_mask.sum())
    n_cold  = int(cold_mask.sum())

    # If we skip the coldest 25% of buckets, how many winners do we miss?
    sorted_idx  = np.argsort(all_counts)
    bottom_25   = sorted_idx[:N_BUCKETS//4]
    missed_25   = int(all_counts[bottom_25].sum())
    miss_pct_25 = missed_25 / total * 100 if total > 0 else 0

    is_signal = (p is not None and p < 0.01)
    chi2_str  = f"{chi2:.1f}" if chi2 is not None else "N/A"
    p_str     = f"{p:.4f}"   if p    is not None else "N/A"

    print(f"  {label:35s}  {speed:6.2f} MH/s  "
          f"chi2={chi2_str:>8}  p={p_str:>7}  "
          f"{'NON-UNIFORM ◄' if is_signal else ('uniform ✓' if p is not None else 'too few winners')}")
    if is_signal:
        print(f"    Hot buckets (>1.5× avg): {n_hot}   Cold (<0.5×): {n_cold}")
        print(f"    Skip coldest 25% of nonce space → miss only {miss_pct_25:.1f}% of winners")
        print(f"    → SKIP RATIO: 25% of work, lose {miss_pct_25:.1f}% of blocks")

    return {
        "label": label, "algo": algo_id, "param": param,
        "total_winners": total, "chi2": chi2, "p": p,
        "speed_mhs": speed, "signal": is_signal,
        "n_hot": n_hot, "n_cold": n_cold,
        "miss_pct_skip25": miss_pct_25,
        "bucket_counts": all_counts.tolist(),
    }


# ════════════════════════════════════════════════════════════════════════════
print("═"*72)
print("  POT Intra-Nonce Range Analysis — winner density per 256 buckets")
print("═"*72)
print(f"  Config: {SCAN_RANGE:,} nonces × {NBLOCKS} blocks × {N_BUCKETS} buckets")
print(f"  Target: 8-bit difficulty (~{SCAN_RANGE//256} winners/block expected)")
print()

all_results = []

# ── ALGO 0: SHA256-Nr (reduced rounds) ──────────────────────────────────
print("── SHA256 reduced rounds (algo 0) ──────────────────────────────────")
for nrounds in [1, 2, 4, 8, 16, 32, 64]:
    r = run_test(0, nrounds, f"SHA256-{nrounds}rounds")
    all_results.append(r)
print()

# ── ALGO 2: SHA256d midstate ─────────────────────────────────────────────
print("── SHA256d midstate scoring (algo 2) ───────────────────────────────")
r = run_test(2, 0, "SHA256d-midstate (full)")
all_results.append(r)
print()

# ── ALGO 1: ETHash-lite ──────────────────────────────────────────────────
print("── ETHash-lite DAG simulation (algo 1) ─────────────────────────────")
r = run_test(1, 0, "ETHash-lite (8 DAG lookups)", n_blocks=10)
all_results.append(r)
print()


# ════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ════════════════════════════════════════════════════════════════════════════
print("═"*72)
print("  SUMMARY")
print("═"*72)
print(f"  {'Algorithm':35s}  {'χ²':>8}  {'p-value':>8}  {'Skip 25%→miss':>14}  Result")
print("  " + "-"*70)

for r in all_results:
    chi2_s = f"{r['chi2']:.1f}" if r['chi2'] is not None else "N/A"
    p_s    = f"{r['p']:.4f}"   if r['p']    is not None else "N/A"
    miss   = f"{r['miss_pct_skip25']:.1f}%"
    verdict = "NON-UNIFORM ◄" if r["signal"] else ("uniform" if r["p"] is not None else "no data")
    print(f"  {r['label']:35s}  {chi2_s:>8}  {p_s:>8}  {miss:>14}  {verdict}")

print()
signals = [r for r in all_results if r["signal"]]
if signals:
    print(f"  SIGNALS FOUND: {len(signals)} algorithms show intra-POT structure!")
    for r in signals:
        print(f"    ► {r['label']}: skip 25% of nonce space, lose only {r['miss_pct_skip25']:.1f}% of winners")
    print()
    print("  What this means:")
    print("  → Split each POT into 256 nonce buckets")
    print("  → Pre-score each bucket using the scoring function")
    print("  → Skip cold buckets (save compute), keep hot buckets (find blocks)")
else:
    print("  No intra-POT structure found at any round count tested.")
    print("  → All algorithms produce uniformly distributed winners")
    print("  → Nonce range skipping not possible for these algorithms")

print()
print("Structural transition (SHA256 rounds):")
for r in all_results:
    if r["algo"] == 0:
        status = "SIGNAL" if r["signal"] else "noise "
        print(f"  SHA256-{r['param']:2d}rounds: {status}  χ²={r['chi2']:.1f}  p={r['p']:.4f}")

with open("pot_analysis_results.json", "w") as f:
    # Don't save full bucket counts to keep file small
    summary = [{k:v for k,v in r.items() if k != "bucket_counts"} for r in all_results]
    json.dump(summary, f, indent=2)
print("\nResults → pot_analysis_results.json")
