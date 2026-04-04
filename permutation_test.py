"""
permutation_test.py

Two things in one:

1. PERMUTATION TEST — validates whether the bit correlation signal (0.091)
   is real or noise. Shuffles prev_hashes 1000x, builds null distribution.

2. SKIP POTENTIAL ANALYSIS — for every bit-pair that beats the null,
   asks the mining question:
     "If I only mine nonces where prev_hash_bit[pb]==X,
      what % of nonce space do I skip, and what % of winners do I keep?"
   This is the precision/recall of a 1-bit nonce filter.
"""

import json
import math
import random
import time

# ── Load data ────────────────────────────────────────────────────────────────
with open("winners.json") as f:
    data = json.load(f)

prev_hashes = [b["prev"] for b in data["winners_per_block"]]
all_winners = [b["winners"] for b in data["winners_per_block"]]

# Flatten to (prev_int, nonce) pairs
rows = []
for ph, ws in zip(prev_hashes, all_winners):
    ph_int = int(ph, 16)
    for w in ws:
        rows.append((ph_int, w))

print(f"Loaded {len(rows):,} (prev_hash, winning_nonce) pairs from {len(prev_hashes)} blocks\n")

SCAN_RANGE = data["config"]["scan_range"]
N = len(rows)


# ── Core: compute max bit-correlation for a given ordering ───────────────────
def max_bit_corr(ph_list: list[int], nonce_list: list[int]) -> tuple[float, int, int]:
    """Returns (max_corr, prev_bit, nonce_bit)."""
    n = len(ph_list)
    best = 0.0
    best_pb = best_nb = 0
    for pb in range(64):
        ph_bits = [(p >> pb) & 1 for p in ph_list]
        ph_mean = sum(ph_bits) / n
        if ph_mean in (0.0, 1.0):
            continue
        std_ph = math.sqrt(ph_mean * (1 - ph_mean))
        for nb in range(19):
            n_bits = [(w >> nb) & 1 for w in nonce_list]
            n_mean = sum(n_bits) / n
            if n_mean in (0.0, 1.0):
                continue
            cov  = sum((ph_bits[i]-ph_mean)*(n_bits[i]-n_mean) for i in range(n)) / n
            std_n = math.sqrt(n_mean * (1 - n_mean))
            if std_n == 0:
                continue
            c = abs(cov / (std_ph * std_n))
            if c > best:
                best, best_pb, best_nb = c, pb, nb
    return best, best_pb, best_nb


# ── Step 1: observed correlation ─────────────────────────────────────────────
ph_list    = [r[0] for r in rows]
nonce_list = [r[1] for r in rows]

print("Computing observed bit correlation …")
t0 = time.perf_counter()
obs_corr, obs_pb, obs_nb = max_bit_corr(ph_list, nonce_list)
print(f"  Observed max corr = {obs_corr:.6f}  "
      f"(prev_hash bit {obs_pb} ↔ nonce bit {obs_nb})  "
      f"[{time.perf_counter()-t0:.1f}s]\n")


# ── Step 2: permutation test ──────────────────────────────────────────────────
N_PERMS = 1000
print(f"Running {N_PERMS} permutations …")
null_scores = []
t0 = time.perf_counter()
shuffled_ph = ph_list[:]

for i in range(N_PERMS):
    random.shuffle(shuffled_ph)
    score, _, _ = max_bit_corr(shuffled_ph, nonce_list)
    null_scores.append(score)
    if (i + 1) % 100 == 0:
        elapsed = time.perf_counter() - t0
        eta = elapsed / (i+1) * (N_PERMS - i - 1)
        pval_so_far = sum(s >= obs_corr for s in null_scores) / len(null_scores)
        print(f"  {i+1}/{N_PERMS}  null_mean={sum(null_scores)/len(null_scores):.4f}  "
              f"p_so_far={pval_so_far:.3f}  eta={eta:.0f}s")

null_scores.sort()
p_value = sum(s >= obs_corr for s in null_scores) / N_PERMS
null_mean = sum(null_scores) / N_PERMS
null_95   = null_scores[int(0.95 * N_PERMS)]
null_99   = null_scores[int(0.99 * N_PERMS)]

print(f"\nPermutation test results:")
print(f"  Observed correlation : {obs_corr:.6f}")
print(f"  Null distribution    : mean={null_mean:.6f}  95th={null_95:.6f}  99th={null_99:.6f}")
print(f"  p-value              : {p_value:.4f}")
if p_value < 0.01:
    perm_verdict = "SIGNIFICANT (p<0.01) — signal is likely REAL"
elif p_value < 0.05:
    perm_verdict = "MARGINAL (0.01<p<0.05) — weak signal, needs more data"
else:
    perm_verdict = "NOT SIGNIFICANT (p>0.05) — likely NOISE"
print(f"  Verdict              : {perm_verdict}\n")


# ── Step 3: skip potential for ALL bit-pairs above null 95th percentile ───────
print("═" * 65)
print("SKIP POTENTIAL ANALYSIS")
print("For each bit-pair above the null 95th percentile:")
print("  skip_ratio  = fraction of nonce space eliminated by the filter")
print("  winner_keep = fraction of winners still found (recall)")
print("  false_neg   = fraction of winners MISSED (the cost)\n")

candidates = []
for pb in range(64):
    ph_bits_all = [(r[0] >> pb) & 1 for r in rows]
    ph_mean = sum(ph_bits_all) / N
    if ph_mean in (0.0, 1.0):
        continue
    std_ph = math.sqrt(ph_mean * (1 - ph_mean))
    for nb in range(19):
        n_bits_all = [(r[1] >> nb) & 1 for r in rows]
        n_mean = sum(n_bits_all) / N
        if n_mean in (0.0, 1.0):
            continue
        cov  = sum((ph_bits_all[i]-ph_mean)*(n_bits_all[i]-n_mean) for i in range(N)) / N
        std_n = math.sqrt(n_mean * (1 - n_mean))
        if std_n == 0:
            continue
        c = abs(cov / (std_ph * std_n))
        if c >= null_95:
            candidates.append((c, pb, nb))

candidates.sort(reverse=True)

if not candidates:
    print("  No bit-pairs above null 95th percentile. No exploitable signal found.")
else:
    print(f"  {'corr':>8}  {'ph_bit':>6}  {'n_bit':>6}  "
          f"{'skip_ratio':>10}  {'winner_keep':>11}  {'false_neg':>9}")
    print("  " + "-" * 62)
    for corr, pb, nb in candidates[:20]:
        # For this bit-pair, find the best binary filter:
        # predict nonce_bit=v when prev_hash_bit=u → skip nonces where predicted nonce_bit ≠ v
        # Try both polarities (u=0→v=0, u=0→v=1)
        best_keep = 0.0
        best_skip = 0.0
        for ph_val in (0, 1):
            for n_val in (0, 1):
                # Filter: only mine nonces where nonce_bit[nb] == n_val,
                #         WHEN prev_hash_bit[pb] == ph_val
                # For rows where ph_bit==ph_val, keep only nonces with n_bit==n_val
                kept   = sum(1 for r in rows if ((r[0]>>pb)&1)==ph_val and ((r[1]>>nb)&1)==n_val)
                total  = sum(1 for r in rows if ((r[0]>>pb)&1)==ph_val)
                if total == 0:
                    continue
                keep_rate = kept / total
                # skip_ratio: when ph_bit==ph_val, we skip half nonce space (bit==1-n_val)
                skip = 0.5  # one bit always halves the space
                if keep_rate > best_keep:
                    best_keep = keep_rate
                    best_skip = skip
        print(f"  {corr:8.6f}  {pb:>6}  {nb:>6}  "
              f"{best_skip:>10.1%}  {best_keep:>11.1%}  {1-best_keep:>9.1%}")

# ── Step 4: combined multi-bit filter potential ───────────────────────────────
print(f"\n{'═'*65}")
print("MULTI-BIT FILTER POTENTIAL")
print("If we combine the top-2 independent bit-pair filters:")
if len(candidates) >= 2:
    (c1, pb1, nb1), (c2, pb2, nb2) = candidates[0], candidates[1]
    # skip ratio compounds: 0.5 * 0.5 = 0.25 (skip 75%)
    # winner keep = product of individual keep rates (if independent)
    # Approximate: compute directly on rows
    kept_both = sum(
        1 for r in rows
        if ((r[0]>>pb1)&1) == ((r[1]>>nb1)&1) and
           ((r[0]>>pb2)&1) == ((r[1]>>nb2)&1)
    )
    combined_keep = kept_both / N
    print(f"  Filters: (ph_bit {pb1} ↔ n_bit {nb1}) + (ph_bit {pb2} ↔ n_bit {pb2})")
    print(f"  Nonce space reduction: 75%  (skip 3/4 of all nonces)")
    print(f"  Winner keep rate     : {combined_keep:.1%}")
    print(f"  False negative rate  : {1-combined_keep:.1%}")
else:
    print("  Not enough signal for multi-bit analysis.")

print(f"\n{'═'*65}")
print("INTERPRETATION FOR MINING SKIP OPTIMIZATION")
print(f"  A useful filter needs: skip_ratio HIGH + false_neg LOW")
print(f"  Ideal: skip 50%+ of nonces, miss <1% of winners")
print(f"  Null baseline: any random 1-bit filter skips 50%, misses 50%")
print(f"  If our filter keeps {candidates[0][0]:.1%} of winners while skipping 50%")
print(f"  → net gain = keep_rate - 50%  (above random)")
