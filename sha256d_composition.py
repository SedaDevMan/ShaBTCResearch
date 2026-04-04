"""
sha256d_composition.py — SHA256d = SHA256(SHA256(x)) composition bias test

Core question: does H1 = SHA256(input) predict H2 = SHA256(H1)?

If yes → use SHA256 as a cheap pre-filter for SHA256d:
  - compute H1 (cheap)
  - if H1 > loose_threshold: skip → never compute H2
  - speedup proportional to how well H1 predicts H2

Three sub-tests:
  A. Raw correlation: does low H1 → low H2?
  B. Filter effectiveness: SHA256 winners ∩ SHA256d winners overlap?
  C. Composition uniformity: same bucket test as before on SHA256d
"""

import hashlib, struct, time
import numpy as np
from scipy import stats

NONCES     = 500_000
N_BUCKETS  = 256
PREV = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")

def sha256(data):
    return hashlib.sha256(data).digest()

def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def make_input(prev, nonce):
    return prev + struct.pack('<I', nonce)

print("SHA256d composition bias test")
print(f"Scanning {NONCES:,} nonces ...\n")

t0 = time.perf_counter()

H1 = np.empty(NONCES, dtype=np.uint64)
H2 = np.empty(NONCES, dtype=np.uint64)

for n in range(NONCES):
    inp = make_input(PREV, n)
    h1  = sha256(inp)
    h2  = sha256(h1)
    # Use first 8 bytes as uint64 (top bits — most significant for target comparison)
    H1[n] = int.from_bytes(h1[:8], 'big')
    H2[n] = int.from_bytes(h2[:8], 'big')

elapsed = time.perf_counter() - t0
print(f"Done: {elapsed:.1f}s  ({NONCES/elapsed/1e3:.1f} KH/s)\n")

# ── TEST A: Raw correlation H1 vs H2 ─────────────────────────────────────
print("═"*55)
print("TEST A: Raw value correlation  H1 → H2")
print("═"*55)

r_pearson, p_pearson = stats.pearsonr(H1.astype(float), H2.astype(float))
r_spear,   p_spear   = stats.spearmanr(H1, H2)

print(f"  Pearson  r = {r_pearson:.6f}  p = {p_pearson:.4f}")
print(f"  Spearman r = {r_spear:.6f}  p = {p_spear:.4f}")
print(f"  → {'CORRELATED ◄' if p_pearson < 0.05 else 'independent'}")


# ── TEST B: Filter effectiveness ─────────────────────────────────────────
print(f"\n{'═'*55}")
print("TEST B: SHA256 pre-filter for SHA256d")
print("═"*55)
print("  If H1 < k×target → candidate for H2 check")
print("  How many true SHA256d winners does the filter capture?\n")

# 8-bit target equivalent in uint64 space
# target_8bit = 0x0100...0 → top 64 bits = 0x0100000000000000
TARGET_8BIT_U64 = 0x0100000000000000

sha256d_winners = H2 < TARGET_8BIT_U64
n_true_winners  = sha256d_winners.sum()
print(f"  True SHA256d winners: {n_true_winners:,}  ({n_true_winners/NONCES*100:.2f}%)")
print()
print(f"  {'Filter (k×)':>12}  {'Candidates':>12}  {'Winners captured':>18}  {'Miss rate':>10}  {'Speedup':>8}")
print("  " + "-"*65)

for k in [1, 2, 4, 8, 16, 32]:
    threshold     = TARGET_8BIT_U64 * k
    filter_pass   = H1 < threshold
    n_candidates  = filter_pass.sum()
    winners_caught = (filter_pass & sha256d_winners).sum()
    miss_rate     = (n_true_winners - winners_caught) / n_true_winners * 100 if n_true_winners > 0 else 0
    # Speedup: normally compute H2 for all NONCES
    # With filter: compute H1 for all (cost=0.5 each) + H2 only for candidates (cost=1 each)
    # Total cost = NONCES*0.5 + n_candidates*1  vs  NONCES*1 (baseline)
    cost_with_filter = NONCES * 0.5 + n_candidates
    speedup = NONCES / cost_with_filter if cost_with_filter > 0 else 1.0
    useful  = speedup > 1.05 and miss_rate < 5.0
    print(f"  {k:>10}×  {n_candidates:>12,}  {winners_caught:>12,} ({100-miss_rate:.1f}%)  "
          f"{miss_rate:>9.2f}%  {speedup:>7.2f}×"
          f"  {'◄ USEFUL' if useful else ''}")


# ── TEST C: Composition uniformity (bucket test) ─────────────────────────
print(f"\n{'═'*55}")
print("TEST C: Winner bucket distribution (256 buckets)")
print("═"*55)

bucket_size = NONCES // N_BUCKETS
buckets = np.zeros(N_BUCKETS, dtype=np.int64)
for n in range(NONCES):
    if H2[n] < TARGET_8BIT_U64:
        buckets[min(n // bucket_size, N_BUCKETS-1)] += 1

total = buckets.sum()
expected = total / N_BUCKETS
chi2 = ((buckets - expected)**2 / expected).sum() if expected > 0 else 0
p_chi2 = 1 - stats.chi2.cdf(chi2, df=N_BUCKETS-1)

print(f"  Winners: {total:,}  Expected/bucket: {expected:.1f}")
print(f"  χ² = {chi2:.2f}  p = {p_chi2:.4f}")
print(f"  → {'NON-UNIFORM ◄' if p_chi2 < 0.01 else 'uniform ✓'}")
print(f"  Bucket range: min={buckets.min()}  max={buckets.max()}  std={buckets.std():.1f}")


# ── SUMMARY ───────────────────────────────────────────────────────────────
print(f"\n{'═'*55}")
print("SUMMARY")
print("═"*55)

any_signal = (p_pearson < 0.05) or (p_chi2 < 0.01)

if p_pearson < 0.05:
    print(f"  H1 correlates with H2 (r={r_pearson:.4f})")
    print(f"  → SHA256 output predicts SHA256d output")
    print(f"  → Pre-filtering viable: compute SHA256 first, skip bad candidates")
else:
    print(f"  H1 does NOT correlate with H2 (r={r_pearson:.6f}, p={p_pearson:.4f})")
    print(f"  → SHA256 output is independent of SHA256d output")

if p_chi2 < 0.01:
    print(f"  Winner distribution is NON-UNIFORM (χ²={chi2:.1f})")
else:
    print(f"  Winner distribution is uniform (χ²={chi2:.1f})")

if not any_signal:
    print()
    print("  CONCLUSION: SHA256d composition adds no exploitable bias.")
    print("  H2 = SHA256(H1) is as random as SHA256 of truly random input.")
    print("  The double application gives no structure — by design.")
