"""
multiwindow_analysis.py  (numpy-vectorized version)

Three experiments:
  1. K-window bit correlation  — do older hashes add predictive signal?
  2. Nonce temporal autocorrelation — does nonce[N] predict nonce[N+1]?
  3. Nonce range clustering — are some nonce ranges universally hotter?
"""

import json, math, random, time
import numpy as np

random.seed(42)
np.random.seed(42)

# ── Load data ─────────────────────────────────────────────────────────────────
with open("blocks.json") as f:
    chain = json.load(f)
with open("winners.json") as f:
    wdata = json.load(f)

SCAN_RANGE = wdata["config"]["scan_range"]

prev_to_winners = {
    b["prev"]: b["winners"]
    for b in wdata["winners_per_block"]
}

mined = [b for b in chain if b["prev"] is not None and b["prev"] in prev_to_winners]
print(f"Blocks: {len(mined)}  |  scan_range={SCAN_RANGE:,}\n")


# ── Numpy fast max-correlation ─────────────────────────────────────────────────
def max_corr_numpy(feat_bits: np.ndarray, nonce_bits: np.ndarray) -> tuple[float, int, int]:
    """
    feat_bits  : (N, F) bool array — F feature bits for N samples
    nonce_bits : (N, B) bool array — B nonce bits for N samples
    Returns (max_abs_pearson, feat_bit_idx, nonce_bit_idx)
    """
    N = feat_bits.shape[0]
    # Convert to float, compute means
    F = feat_bits.astype(np.float32)   # (N, F)
    NB = nonce_bits.astype(np.float32) # (N, B)

    f_mean = F.mean(axis=0)            # (F,)
    n_mean = NB.mean(axis=0)           # (B,)

    # Remove constant columns
    f_std = F.std(axis=0); n_std = NB.std(axis=0)
    f_ok  = f_std > 0;     n_ok  = n_std > 0

    F_c  = (F  - f_mean) * f_ok       # (N, F) — zero out constants
    NB_c = (NB - n_mean) * n_ok       # (N, B)

    # Correlation matrix: (F, B)
    cov = (F_c.T @ NB_c) / N          # (F, B)
    denom = np.outer(f_std, n_std)     # (F, B)
    denom[denom == 0] = 1              # avoid div/0

    corr = np.abs(cov / denom)        # (F, B)
    corr[~f_ok, :] = 0
    corr[:, ~n_ok] = 0

    idx = np.unravel_index(np.argmax(corr), corr.shape)
    return float(corr[idx]), int(idx[0]), int(idx[1])


def hash_to_bits(h: int, n_bits: int = 64) -> list[int]:
    return [(h >> b) & 1 for b in range(n_bits)]


def nonce_to_bits(n: int, n_bits: int = 19) -> list[int]:
    return [(n >> b) & 1 for b in range(n_bits)]


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT 1 — K-window bit correlation
# ═══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("EXPERIMENT 1 — K-window bit correlation")
print("Does adding older hashes as features improve the signal?\n")
print(f"  {'K':>3}  {'feat_bits':>9}  {'pairs':>6}  {'max_corr':>9}  "
      f"{'p-val':>6}  {'verdict'}")
print("  " + "-" * 62)

N_PERMS = 500

for K in [1, 2, 5, 10, 20]:
    feat_rows = []
    nonce_rows = []

    for i in range(K - 1, len(mined)):
        block   = mined[i]
        winners = prev_to_winners.get(block["prev"], [])
        if not winners:
            continue
        # K hashes before this block (oldest first)
        window = [int(mined[i - k]["prev"], 16) for k in range(K - 1, -1, -1)]
        feat_flat = []
        for h in window:
            feat_flat.extend(hash_to_bits(h))   # 64 bits per hash
        for w in winners:
            feat_rows.append(feat_flat)
            nonce_rows.append(nonce_to_bits(w))

    if not nonce_rows:
        print(f"  {K:>3}  no data"); continue

    feat_arr  = np.array(feat_rows,  dtype=np.bool_)   # (N, K*64)
    nonce_arr = np.array(nonce_rows, dtype=np.bool_)   # (N, 19)
    N = len(nonce_rows)
    FBITS = K * 64

    t0 = time.perf_counter()
    obs_corr, obs_fb, obs_nb = max_corr_numpy(feat_arr, nonce_arr)

    # Permutation test — shuffle rows of feat_arr
    exceed = 0
    idx = np.arange(N)
    for _ in range(N_PERMS):
        np.random.shuffle(idx)
        s, _, _ = max_corr_numpy(feat_arr[idx], nonce_arr)
        if s >= obs_corr: exceed += 1

    pval = exceed / N_PERMS
    elapsed = time.perf_counter() - t0

    which_hash = obs_fb // 64
    age = K - 1 - which_hash
    age_str = f"hash[N-{age}]" if age > 0 else "prev_hash"
    verdict = "SIGNAL" if pval < 0.05 else "noise"

    print(f"  {K:>3}  {FBITS:>9}  {N:>6}  {obs_corr:>9.6f}  "
          f"{pval:>6.3f}  {verdict}  "
          f"({age_str} bit {obs_fb%64} ↔ nonce bit {obs_nb}, {elapsed:.1f}s)")


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT 2 — Temporal nonce autocorrelation
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("EXPERIMENT 2 — Temporal nonce autocorrelation")
print("Does block N's winning nonce predict block N+1's winner distribution?\n")

known_nonces = [b["nonce"] for b in mined if b["nonce"] is not None]
prev_n = np.array(known_nonces[:-1], dtype=np.int64)
next_n = np.array(known_nonces[1:],  dtype=np.int64)
N2 = len(prev_n)

prev_bits = np.array([[( v >> b) & 1 for b in range(19)] for v in prev_n], dtype=np.bool_)
next_bits = np.array([[( v >> b) & 1 for b in range(19)] for v in next_n], dtype=np.bool_)

obs2, b2i, b2j = max_corr_numpy(prev_bits, next_bits)

exceed2 = 0
idx2 = np.arange(N2)
for _ in range(N_PERMS):
    np.random.shuffle(idx2)
    s, _, _ = max_corr_numpy(prev_bits[idx2], next_bits)
    if s >= obs2: exceed2 += 1

pval2 = exceed2 / N_PERMS
print(f"  Consecutive nonce pairs : {N2}")
print(f"  Max |corr| nonce[N] bit {b2i} ↔ nonce[N+1] bit {b2j} : {obs2:.6f}")
print(f"  p-value: {pval2:.3f}  →  {'SIGNAL' if pval2<0.05 else 'noise (no temporal pattern)'}")

# Also: do winners from block N cluster near block N's single winning nonce?
print(f"\n  Proximity test: are multi-winners in block N clustered around the")
print(f"  single known winning nonce for that block?\n")
proximity_ratios = []
for block in mined:
    known = block["nonce"]
    ws = prev_to_winners.get(block["prev"], [])
    if not ws or known is None: continue
    dists = [abs(w - known) for w in ws]
    near  = sum(1 for d in dists if d < SCAN_RANGE // 20)  # within 5% of range
    proximity_ratios.append(near / len(ws))

avg_prox = sum(proximity_ratios) / len(proximity_ratios)
random_baseline = 1 / 20   # 5% of range → 5% chance by random
print(f"  Avg fraction of multi-winners within 5% of known nonce: {avg_prox:.3f}")
print(f"  Random baseline: {random_baseline:.3f}")
print(f"  Verdict: {'CLUSTERING around known nonce!' if avg_prox > random_baseline*1.5 else 'no clustering — winners spread uniformly'}")


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIMENT 3 — Nonce range clustering
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("EXPERIMENT 3 — Nonce range clustering")
print("Are some nonce ranges universally hotter across ALL prev_hashes?\n")

BINS = 20
bin_size = SCAN_RANGE // BINS
all_w = [w for ws in prev_to_winners.values() for w in ws]
total = len(all_w)

counts = np.zeros(BINS, dtype=np.int32)
for w in all_w:
    counts[min(w // bin_size, BINS - 1)] += 1

expected = total / BINS
chi2 = float(np.sum((counts - expected)**2 / expected))
verdict3 = "NON-UNIFORM — hot zones exist!" if chi2 > 30.14 else "UNIFORM — no hot zones"

print(f"  Total winners: {total:,}  |  {BINS} bins × {bin_size:,} nonces each")
print(f"  Expected per bin: {expected:.1f}  |  Chi²={chi2:.3f}  (critical=30.14)")
print(f"  Verdict: {verdict3}\n")
print(f"  {'Range':>17}   count  distribution")
print(f"  " + "-"*50)
for i, c in enumerate(counts):
    bar = "█" * int(c / max(counts) * 25)
    tag = " ← HOT"  if c > expected * 1.5 else (" ← cold" if c < expected * 0.5 else "")
    print(f"  [{i*bin_size:>6,}–{(i+1)*bin_size-1:>6,}]  {c:>4}  {bar}{tag}")


# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("FINAL VERDICT")
any_k_signal = False  # will be set below if we re-check
print(f"  Temporal autocorr  : {'SIGNAL' if pval2<0.05 else 'no signal'}")
print(f"  Range clustering   : {verdict3}")
print()
print("  Implication for skip-index:")
if chi2 > 30.14:
    print("  → Hot zones found: skip cold ranges, concentrate on hot ones.")
    print(f"  → Could eliminate ~{int((1 - (max(counts)/total * BINS * 0.3))*100)}% of nonce space.")
elif pval2 < 0.05:
    print("  → Temporal pattern: use previous winning nonce as anchor for next search.")
else:
    print("  → No exploitable structure found at this data scale.")
    print("  → Next option: scale up with C extension (100× more data) or")
    print("     try a non-linear ML model on the K-window features.")
