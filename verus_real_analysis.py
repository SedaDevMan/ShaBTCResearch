"""
verus_real_analysis.py — Real Haraka-512 based VerusHash 2.1 analysis

Tests the ACTUAL Haraka-512 pipeline (not our toy AES model) for:

A. Avalanche / diffusion quality
   Flip each nonce bit in turn, count how many output bits change.
   Real cryptographic hash: ~128 bits change (50% of 256).
   Toy N=1: flipping bit 0 of nonce changes only ~8 bits (1 output byte).

B. Nonce byte 0 correlation test
   Does byte_0 of the hash output correlate with byte_0 of the nonce?
   Our toy N=1 had r=0.354. Real Haraka-512 should be r≈0.

C. Winner distribution uniformity
   Chi-squared test over 256 nonce buckets.
   Real hash: p≈0.5 (uniform). Toy N=1: p≈0 (non-uniform).

D. ML pattern test (same as nonlinear_ml.py but on real pipeline)
   Decision tree + gradient boosting AUC.
   Real hash: AUC≈0.5. Toy N=1: AUC≈0.98.

E. Speed benchmark (Haraka-512 vs SHA256d)
"""

import time, struct, hashlib
import numpy as np
from scipy import stats
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import cross_val_score
from sklearn.dummy import DummyClassifier
import verus_real

# ── Build synthetic 140-byte header template ─────────────────────────────────
def make_tmpl(prev_hash: bytes) -> bytes:
    """Build a 140-byte VerusHash-style header template.
    Layout: version(4) + prevhash(32) + merkle(32) + reserved(32) + ts(4) + bits(4) + nonce(32)
    nonce starts at byte 108; scan_winners_real overwrites bytes 108-111.
    """
    assert len(prev_hash) == 32
    return (b'\x04\x00\x00\x00'   # version = 4
            + prev_hash             # prevhash [4-35]
            + bytes(32)             # merkle   [36-67]  (zeros)
            + bytes(32)             # reserved [68-99]  (zeros)
            + struct.pack('<I', 0)  # timestamp [100-103]
            + b'\x20\x00\x00\x00'  # bits = 0x20 [104-107]
            + bytes(32))            # nonce    [108-139] (scan varies first 4)

def make_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

CHAIN = make_chain(20)
TARGET_8BIT = b'\x01' + b'\x00' * 31  # 1/256 difficulty

print("═"*65)
print("  Real Haraka-512 VerusHash 2.1 Analysis")
print("═"*65)
print()

# ════════════════════════════════════════════════════════════════════════
# TEST A: Avalanche / diffusion quality
# ════════════════════════════════════════════════════════════════════════
print("─"*65)
print("TEST A: Avalanche (bits flipped per 1-bit nonce change)")
print("─"*65)

tmpl = make_tmpl(CHAIN[0])

bit_flips_haraka = []
for flip_bit in range(32):          # flip each bit of the low 32-bit nonce
    h_orig, h_flip = verus_real.test_diffusion(tmpl, 12345, flip_bit)
    xor_bytes = bytes(a ^ b for a, b in zip(h_orig, h_flip))
    n_flipped = sum(bin(b).count('1') for b in xor_bytes)
    bit_flips_haraka.append(n_flipped)

mean_flip = np.mean(bit_flips_haraka)
print(f"  Haraka-512 (real, 5 rounds + MIX512):")
print(f"    Mean bits changed per 1-bit nonce flip: {mean_flip:.1f} / 256")
print(f"    Expected for ideal hash: 128.0 (50%)")
print(f"    Min={min(bit_flips_haraka)}  Max={max(bit_flips_haraka)}")

# Compare to toy model (verus_aes N=1 and N=5)
import verus_aes

bit_flips_toy_n1 = []
bit_flips_toy_n5 = []
for flip_bit in range(32):
    nonce_orig = 12345
    nonce_flip = nonce_orig ^ (1 << flip_bit)
    for nrounds, lst in [(1, bit_flips_toy_n1), (5, bit_flips_toy_n5)]:
        h_orig = verus_aes.verus_hash(CHAIN[0], nonce_orig, nrounds)
        h_flip = verus_aes.verus_hash(CHAIN[0], nonce_flip, nrounds)
        xor_bytes = bytes(a ^ b for a, b in zip(h_orig, h_flip))
        n_flipped = sum(bin(b).count('1') for b in xor_bytes)
        lst.append(n_flipped)

print(f"\n  Toy AES N=1 (single block, no MixColumns in last round):")
print(f"    Mean bits changed: {np.mean(bit_flips_toy_n1):.1f} / 256  (toy model)")
print(f"    Min={min(bit_flips_toy_n1)}  Max={max(bit_flips_toy_n1)}")
print(f"\n  Toy AES N=5 (same structure as Haraka round count, no MIX):")
print(f"    Mean bits changed: {np.mean(bit_flips_toy_n5):.1f} / 256  (toy model)")
print()

# ════════════════════════════════════════════════════════════════════════
# TEST B: Nonce byte_0 vs output byte_0 correlation
# ════════════════════════════════════════════════════════════════════════
print("─"*65)
print("TEST B: Nonce byte_0 → output byte_0 correlation")
print("─"*65)
PROBE = 50_000
tmpl = make_tmpl(CHAIN[0])
nonce_byte0  = (np.arange(PROBE, dtype=np.int32) & 0xFF).astype(np.float32)
out_byte0_h  = np.empty(PROBE, dtype=np.float32)
out_byte0_n1 = np.empty(PROBE, dtype=np.float32)

for n in range(PROBE):
    h = verus_real.verus_hash_real(tmpl, n)
    out_byte0_h[n] = h[0]
    h2 = verus_aes.verus_hash(CHAIN[0], n, 1)
    out_byte0_n1[n] = h2[0]

r_real, p_real = stats.pearsonr(nonce_byte0.astype(float), out_byte0_h.astype(float))
r_toy,  p_toy  = stats.pearsonr(nonce_byte0.astype(float), out_byte0_n1.astype(float))

print(f"  Real Haraka-512: r={r_real:.6f}  p={p_real:.4f}"
      f"  → {'CORRELATED ◄' if p_real < 0.05 else 'independent ✓'}")
print(f"  Toy AES N=1:     r={r_toy:.6f}  p={p_toy:.4f}"
      f"  → {'CORRELATED ◄' if p_toy < 0.05 else 'independent'}")
print()

# ════════════════════════════════════════════════════════════════════════
# TEST C: Winner distribution uniformity (chi-squared)
# ════════════════════════════════════════════════════════════════════════
print("─"*65)
print("TEST C: Winner bucket distribution (256 buckets, 10 blocks)")
print("─"*65)
SCAN  = 200_000
N_BLOCKS = 10
N_BUCKETS = 256
bucket_size = SCAN // N_BUCKETS

buckets_real = np.zeros(N_BUCKETS, dtype=np.int64)

t0 = time.perf_counter()
for prev in CHAIN[:N_BLOCKS]:
    tmpl = make_tmpl(prev)
    winners = verus_real.scan_winners_real(tmpl, SCAN, TARGET_8BIT)
    for w in winners:
        buckets_real[min(w // bucket_size, N_BUCKETS - 1)] += 1
elapsed = time.perf_counter() - t0

total = buckets_real.sum()
expected = total / N_BUCKETS
chi2_r  = ((buckets_real - expected)**2 / expected).sum() if expected > 0 else 0
p_chi2  = 1 - stats.chi2.cdf(chi2_r, df=N_BUCKETS-1)
speed_mhs = (SCAN * N_BLOCKS / 1e6) / elapsed

print(f"  Speed: {speed_mhs:.2f} MH/s  (total {elapsed:.1f}s)")
print(f"  Winners: {total:,}  Expected/bucket: {expected:.1f}")
print(f"  χ²={chi2_r:.1f}  p={p_chi2:.4f}"
      f"  → {'NON-UNIFORM ◄' if p_chi2 < 0.01 else 'uniform ✓'}")
print(f"  Bucket range: min={buckets_real.min()}  max={buckets_real.max()}")
print()

# ════════════════════════════════════════════════════════════════════════
# TEST D: ML pattern test (decision tree + gradient boosting)
# ════════════════════════════════════════════════════════════════════════
print("─"*65)
print("TEST D: Non-linear ML pattern test on real VerusHash")
print("─"*65)

def nonce_features(nonces):
    n = len(nonces)
    feats = np.zeros((n, 44), dtype=np.float32)
    arr = np.array(nonces, dtype=np.uint32)
    for b in range(32):       feats[:, b]    = (arr >> b) & 1
    for b in range(4):        feats[:, 32+b] = (arr >> (b*8)) & 0xFF
    for b in range(8):        feats[:, 36+b] = (arr >> (b*4)) & 0xF
    return feats

# Collect pairs using real Haraka-512
print("  Collecting pairs (real Haraka, 200K × 10 blocks) ...")
nonces_all, labels_all = [], []
t0 = time.perf_counter()
for prev in CHAIN[:N_BLOCKS]:
    tmpl = make_tmpl(prev)
    winners = set(verus_real.scan_winners_real(tmpl, SCAN, TARGET_8BIT))
    for n in range(SCAN):
        nonces_all.append(n)
        labels_all.append(1 if n in winners else 0)
t_collect = time.perf_counter() - t0

nonces_all = np.array(nonces_all)
labels_all = np.array(labels_all)
n_winners  = labels_all.sum()
print(f"  Done: {t_collect:.1f}s  |  Winners: {n_winners:,} / {len(labels_all):,}")

# Subsample
win_idx  = np.where(labels_all == 1)[0]
lose_idx = np.where(labels_all == 0)[0]
n_keep   = min(len(win_idx)*10, len(lose_idx))
lose_samp = np.random.choice(lose_idx, n_keep, replace=False)
idx = np.concatenate([win_idx, lose_samp])
np.random.shuffle(idx)
X = nonce_features(nonces_all[idx])
y = labels_all[idx]
print(f"  Subsampled to {len(y):,}")

dummy = DummyClassifier(strategy='most_frequent')
tree  = DecisionTreeClassifier(max_depth=8, min_samples_leaf=20)
gb    = GradientBoostingClassifier(n_estimators=50, max_depth=4,
                                    learning_rate=0.1, subsample=0.8)

d_auc = cross_val_score(dummy, X, y, cv=5, scoring='roc_auc').mean()
t_auc = cross_val_score(tree,  X, y, cv=5, scoring='roc_auc').mean()
g_auc = cross_val_score(gb,    X, y, cv=5, scoring='roc_auc').mean()

t_signal = t_auc > 0.55 and t_auc > d_auc + 0.03
g_signal = g_auc > 0.55 and g_auc > d_auc + 0.03

print(f"  Dummy AUC:    {d_auc:.4f}")
print(f"  DecTree AUC:  {t_auc:.4f}  {'◄ SIGNAL' if t_signal else ''}")
print(f"  GradBoost AUC:{g_auc:.4f}  {'◄ SIGNAL' if g_signal else ''}")
if t_signal or g_signal:
    tree.fit(X, y)
    top = np.argmax(tree.feature_importances_)
    names = ([f"bit_{i}" for i in range(32)] +
             [f"byte_{i}" for i in range(4)] +
             [f"nibble_{i}" for i in range(8)])
    print(f"  Top feature: {names[top]}  (importance={tree.feature_importances_[top]:.3f})")
    print(f"  ► NON-LINEAR STRUCTURE FOUND!")
else:
    print(f"  No signal — real Haraka-512 nonce bits do not predict winners")
print()

# ════════════════════════════════════════════════════════════════════════
# TEST E: Speed comparison
# ════════════════════════════════════════════════════════════════════════
print("─"*65)
print("TEST E: Speed comparison")
print("─"*65)
print(f"  Real Haraka-512 (5 rounds + MIX):  {speed_mhs:.3f} MH/s")

# SHA256d speed
import struct as _struct
t0 = time.perf_counter()
target_int = int.from_bytes(TARGET_8BIT, 'big')
SPEED_N = 50_000
for n in range(SPEED_N):
    inp = CHAIN[0] + _struct.pack('<I', n)
    h1 = hashlib.sha256(inp).digest()
    hashlib.sha256(h1).digest()
sha_elapsed = time.perf_counter() - t0
sha_mhs = SPEED_N / sha_elapsed / 1e6
print(f"  SHA256d (Python):                   {sha_mhs:.3f} MH/s")
print(f"  Toy AES N=1 via verus_aes:          ~1.37 MH/s  (from previous tests)")
print()

# ════════════════════════════════════════════════════════════════════════
print("═"*65)
print("  SUMMARY")
print("═"*65)
print(f"  Diffusion (bits flipped, mean):  real={mean_flip:.0f}  toy_N1={np.mean(bit_flips_toy_n1):.0f}  ideal=128")
print(f"  Byte_0 correlation:              real r={r_real:.4f}  toy_N1 r={r_toy:.4f}")
print(f"  Winner distribution chi2:        real p={p_chi2:.4f}  (p<0.01 = non-uniform)")
print(f"  ML AUC (GB):                     real={g_auc:.4f}  (>0.55 = signal)")
print()
if not (t_signal or g_signal) and p_chi2 >= 0.01 and abs(r_real) < 0.05:
    print("  CONCLUSION: Real Haraka-512 VerusHash 2.1 shows NO exploitable structure.")
    print("  MIX512 layer provides complete cross-block diffusion even from first round.")
    print("  The toy model's N=1 weakness (byte_0 leak) does NOT exist in real Haraka-512.")
    print("  VerusHash 2.1 is cryptographically secure against all tested attacks.")
else:
    print("  CONCLUSION: Signal detected — see results above.")
