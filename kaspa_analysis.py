"""
kaspa_analysis.py — Statistical analysis of HeavyHash winning nonces.

Tests:
  1. Bit correlation: prev_hash bits vs winning nonce bits
  2. Matrix row-sum correlation: does matrix row sum predict nonce bits?
  3. Permutation test: is any signal above chance?
  4. Matrix structure: are matrix values uniform? (sanity check)
  5. Summary verdict

HeavyHash pipeline reminder:
  inner = SHA3-256(prev_hash + nonce_LE8)
  nibbles = unpack(inner)                 # 64 nibbles
  product[i] = sum(matrix[i][j]*nibble[j]) mod 16
  xored[i] = product[i] XOR nibble[i]
  output = SHA3-256(pack(xored))

The matrix is FIXED per prev_hash. So if matrix structure encodes
information about which nonces win, that's a pattern.

Key question: given only prev_hash (not the matrix), can we predict
which nonces produce outputs below target?
"""

import json, random, time
import numpy as np
import heavyhash

random.seed(42)
np.random.seed(42)

N_PERM = 500   # permutation test iterations

# ── Load data ─────────────────────────────────────────────────────────────────
print("Loading kaspa_winners.json ...")
with open("kaspa_winners.json") as f:
    wdata = json.load(f)

SCAN_RANGE = wdata["config"]["scan_range"]
records    = wdata["winners_per_block"]
records    = [r for r in records if r["winners"]]  # skip blocks with 0 winners

print(f"  Blocks with winners: {len(records)}")
print(f"  Total pairs:         {sum(len(r['winners']) for r in records):,}")
print(f"  Avg winners/block:   {sum(len(r['winners']) for r in records)/len(records):.1f}\n")

# ── Helper: flatten 64×64 matrix to bytes ─────────────────────────────────────
def matrix_to_bytes(mat):
    return bytes(mat[r][c] for r in range(64) for c in range(64))

def matrix_to_flat(mat):
    return np.array([mat[r][c] for r in range(64) for c in range(64)], dtype=np.uint8)

# ── Build feature arrays ───────────────────────────────────────────────────────
print("Building feature arrays (prev_hash bits + matrix row sums) ...")

NONCE_BITS = 19   # 2^19 = 524288 > 500K scan range
HASH_BITS  = 64   # low 64 bits of 256-bit prev_hash (for correlation)

prev_bit_rows  = []   # shape (N, HASH_BITS): low 64 bits of prev_hash
mat_rowsum_rows = []  # shape (N, 64): row sums of matrix (0..960 each)
nonce_bit_rows  = []  # shape (N, NONCE_BITS): winning nonce bits

t0 = time.perf_counter()
for rec in records:
    prev_bytes = bytes.fromhex(rec["prev"])
    prev_int   = int.from_bytes(prev_bytes[:8], 'little')  # low 64 bits

    # Hash bits (low 64)
    pbits = [(prev_int >> b) & 1 for b in range(HASH_BITS)]

    # Matrix row sums
    mat_list = heavyhash.generate_matrix(prev_bytes)
    row_sums = [sum(mat_list[r]) for r in range(64)]

    for nonce in rec["winners"]:
        prev_bit_rows.append(pbits)
        mat_rowsum_rows.append(row_sums)
        nbits = [(nonce >> b) & 1 for b in range(NONCE_BITS)]
        nonce_bit_rows.append(nbits)

t_build = time.perf_counter() - t0

X_hash  = np.array(prev_bit_rows,   dtype=np.float32)   # (N, 64)
X_msum  = np.array(mat_rowsum_rows, dtype=np.float32)   # (N, 64)
Y_nonce = np.array(nonce_bit_rows,  dtype=np.float32)   # (N, NONCE_BITS)

N = len(Y_nonce)
print(f"  {N:,} (feature, nonce) pairs  built in {t_build:.1f}s\n")


# ── Utility: max absolute correlation between two 2D arrays ───────────────────
def max_abs_corr(A: np.ndarray, B: np.ndarray):
    """Return (max_corr, row_idx, col_idx) — vectorized."""
    N = len(A)
    Ac = A - A.mean(0)
    Bc = B - B.mean(0)
    A_std = A.std(0); B_std = B.std(0)
    # Zero-out constant columns
    Ac = Ac * (A_std > 0)
    Bc = Bc * (B_std > 0)
    # Correlation matrix: (A_cols × B_cols)
    cov = (Ac.T @ Bc) / N
    denom = np.outer(A_std + 1e-12, B_std + 1e-12)
    corr  = np.abs(cov / denom)
    idx   = np.unravel_index(np.argmax(corr), corr.shape)
    return float(corr[idx]), int(idx[0]), int(idx[1])


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1: Prev-hash bits → nonce bits
# ══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("TEST 1 — Bit correlation: prev_hash bits vs nonce bits")
print()

obs_corr_hash, r1, c1 = max_abs_corr(X_hash, Y_nonce)
print(f"  Max |corr|: {obs_corr_hash:.6f}  (prev_hash bit {r1} vs nonce bit {c1})")

# Permutation test: shuffle nonces
print(f"  Running {N_PERM} permutations ...")
null_hash = []
t0 = time.perf_counter()
for _ in range(N_PERM):
    Y_perm = Y_nonce[np.random.permutation(N)]
    c, _, _ = max_abs_corr(X_hash, Y_perm)
    null_hash.append(c)
null_hash = np.array(null_hash)
p_hash = (null_hash >= obs_corr_hash).mean()
print(f"  Null mean:  {null_hash.mean():.6f}  95th pct: {np.percentile(null_hash,95):.6f}")
print(f"  p-value:    {p_hash:.4f}  → {'SIGNIFICANT (p<0.05)' if p_hash < 0.05 else 'noise'}")
print()


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2: Matrix row sums → nonce bits
# ══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("TEST 2 — Correlation: matrix row sums vs nonce bits")
print()

obs_corr_msum, r2, c2 = max_abs_corr(X_msum, Y_nonce)
print(f"  Max |corr|: {obs_corr_msum:.6f}  (row {r2} sum vs nonce bit {c2})")

print(f"  Running {N_PERM} permutations ...")
null_msum = []
t0 = time.perf_counter()
for _ in range(N_PERM):
    Y_perm = Y_nonce[np.random.permutation(N)]
    c, _, _ = max_abs_corr(X_msum, Y_perm)
    null_msum.append(c)
null_msum = np.array(null_msum)
p_msum = (null_msum >= obs_corr_msum).mean()
print(f"  Null mean:  {null_msum.mean():.6f}  95th pct: {np.percentile(null_msum,95):.6f}")
print(f"  p-value:    {p_msum:.4f}  → {'SIGNIFICANT (p<0.05)' if p_msum < 0.05 else 'noise'}")
print()


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3: Matrix uniformity (sanity check)
# ══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("TEST 3 — Matrix value distribution (sanity check)")
print()

# Sample 5 blocks and check matrix value distribution
sample_prevs = [records[i]["prev"] for i in range(min(5, len(records)))]
all_vals = []
for ph in sample_prevs:
    mat = heavyhash.generate_matrix(bytes.fromhex(ph))
    all_vals.extend(mat[r][c] for r in range(64) for c in range(64))

all_vals = np.array(all_vals)
unique, counts = np.unique(all_vals, return_counts=True)
print(f"  Nibble values 0-15 across 5 matrices ({len(all_vals):,} values):")
print(f"  Min count: {counts.min()}  Max count: {counts.max()}")
expected = len(all_vals) / 16
chi2_stat = ((counts - expected)**2 / expected).sum()
from scipy import stats
p_chi2 = 1 - stats.chi2.cdf(chi2_stat, df=15)
print(f"  Chi-square uniformity: χ²={chi2_stat:.2f}  p={p_chi2:.4f}  → {'uniform ✓' if p_chi2 > 0.05 else 'non-uniform!'}")
print()


# ══════════════════════════════════════════════════════════════════════════════
# TEST 4: Nonce distribution uniformity
# ══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("TEST 4 — Winning nonce distribution (should be uniform)")
print()

all_nonces = []
for rec in records:
    all_nonces.extend(rec["winners"])
all_nonces = np.array(all_nonces)

# Split into 16 bins over scan range
SCAN_RANGE_USE = SCAN_RANGE
nbins = 16
bin_counts = np.bincount(all_nonces * nbins // SCAN_RANGE_USE, minlength=nbins)
expected_bin = len(all_nonces) / nbins
chi2_n = ((bin_counts - expected_bin)**2 / expected_bin).sum()
p_nonce = 1 - stats.chi2.cdf(chi2_n, df=nbins - 1)
print(f"  {len(all_nonces):,} total nonces in {nbins} bins:")
print(f"  Expected: {expected_bin:.0f}/bin  Actual range: {bin_counts.min()}–{bin_counts.max()}")
print(f"  Chi-square: χ²={chi2_n:.2f}  p={p_nonce:.4f}  → {'uniform ✓' if p_nonce > 0.05 else 'non-uniform!'}")
print()


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("SUMMARY")
print()
print(f"  Pairs analyzed:          {N:,}")
print(f"  Hash→nonce corr:         {obs_corr_hash:.6f}  p={p_hash:.4f}  {'SIGNAL' if p_hash<0.05 else 'noise'}")
print(f"  Matrix-sum→nonce corr:   {obs_corr_msum:.6f}  p={p_msum:.4f}  {'SIGNAL' if p_msum<0.05 else 'noise'}")
print()

if p_hash < 0.05 or p_msum < 0.05:
    print("  VERDICT: Significant correlation found!")
    print("  → HeavyHash shows exploitable structure.")
    print("  → Investigate: which matrix rows / hash bits drive the correlation?")
else:
    print("  VERDICT: No significant correlation found.")
    print("  → SHA3-256 wrapping defeats pattern detection in HeavyHash.")
    print("  → Algebraic inversion is blocked by SHA3-256 irreversibility.")
    print("  → Kaspa HeavyHash provides adequate PoW security on this metric.")
print()

# Save summary
summary = {
    "N_pairs":            N,
    "hash_nonce_corr":    float(obs_corr_hash),
    "hash_nonce_pvalue":  float(p_hash),
    "msum_nonce_corr":    float(obs_corr_msum),
    "msum_nonce_pvalue":  float(p_msum),
    "nonce_uniform_p":    float(p_nonce),
    "matrix_uniform_p":   float(p_chi2),
}
import json
with open("kaspa_analysis_results.json", "w") as f:
    json.dump(summary, f, indent=2)
print("Results saved → kaspa_analysis_results.json")
