"""
miner_holes.py — Miner with holes: learn hot nonce zones at easy difficulty,
                  exploit them at hard difficulty.

THE IDEA:
  At genesis/easy difficulty → almost every nonce wins.
  With 128K winners per block you can precisely map:
    "Which nonce sub-ranges produce more winners than average?"

  If those hot zones are:
    (a) consistent across multiple block hashes  (cross-block stability)
    (b) persistent at harder difficulty          (difficulty scaling)

  → You have a "miner with holes":
     Skip the cold zones, focus only on hot zones.
     Speedup = 1/fraction_scanned if hot zones capture most winners.

WHY THIS TEST IS MORE SENSITIVE:
  8-bit difficulty  → ~3.9 winners/bucket → σ≈2.0 → CV≈51%  (previous tests)
  1-bit difficulty  → ~500  winners/bucket → σ≈22  → CV≈4.4% (30× more sensitive)
  0-bit difficulty  → ~4000 winners/bucket → σ≈63  → CV≈1.6% (130× more sensitive)

  If ANY real non-uniformity exists (even 1%), we'll find it at 0-bit difficulty.

TESTS:
  1. Intra-difficulty uniformity chi2 at each difficulty level
     → Does easier difficulty reveal non-uniformity invisible at 8-bit?

  2. Cross-block Friedman test at each difficulty level
     → Do hot zones persist across different block hashes?
     (Previously tested at 8-bit only and found nothing)

  3. Difficulty scaling test
     → If a nonce zone is "hot" at 1-bit difficulty, is it still hot at 8-bit?
     → Measure: rank(zone at easy diff) vs rank(zone at hard diff) correlation

  4. Oracle window test
     → If we knew the hot zones from 1-bit scan, what speedup do we get at 8-bit?

Algorithms: SHA256d (via pot_skip) and Real Haraka-512 (via verus_real)
"""

import time, hashlib, struct
import numpy as np
from scipy import stats
import pot_skip
import verus_real

SCAN_RANGE  = 256_000
N_BUCKETS   = 256
bucket_size = SCAN_RANGE // N_BUCKETS

# Difficulty levels: target = b'\xFF'/(2^bits) ...
# 0-bit: all nonces win (100%)
# 1-bit: top 50% of hash space wins
# 2-bit: top 25%
# 4-bit: top 6.25%
# 8-bit: top 0.39%

DIFFICULTY_LEVELS = {
    "0-bit  (100%)":  b'\xff' * 32,
    "1-bit  ( 50%)":  b'\x80' + b'\x00' * 31,
    "2-bit  ( 25%)":  b'\x40' + b'\x00' * 31,
    "4-bit  (  6%)":  b'\x10' + b'\x00' * 31,
    "8-bit  (0.4%)":  b'\x01' + b'\x00' * 31,
}

K_HEADERS = 50   # 50 block hashes for Friedman test

def make_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

CHAIN = make_chain(K_HEADERS)

def make_tmpl(prev_hash):
    return (b'\x04\x00\x00\x00' + prev_hash + bytes(32) + bytes(32)
            + b'\x00\x00\x00\x00' + b'\x20\x00\x00\x00' + bytes(32))

# ── Collect bucket counts at a given difficulty ───────────────────────────────

def collect_sha256d(prev, target):
    counts = pot_skip.winner_density(prev, SCAN_RANGE, target, 0, 64)
    return np.array(counts, dtype=np.float64)

def collect_haraka(prev, target):
    tmpl = make_tmpl(prev)
    winners = verus_real.scan_winners_real(tmpl, SCAN_RANGE, target)
    counts = np.zeros(N_BUCKETS, dtype=np.float64)
    for w in winners:
        counts[min(w // bucket_size, N_BUCKETS-1)] += 1
    return counts

# ── Statistical tests ─────────────────────────────────────────────────────────

def chi2_test(counts_matrix):
    """Aggregate counts across all blocks, chi2 for uniformity."""
    agg = counts_matrix.sum(axis=0)
    total = agg.sum()
    if total < N_BUCKETS * 2:
        return None, None
    expected = total / N_BUCKETS
    chi2 = ((agg - expected)**2 / expected).sum()
    p = 1 - stats.chi2.cdf(chi2, df=N_BUCKETS - 1)
    return float(chi2), float(p)

def friedman_test(counts_matrix):
    """Cross-block Friedman test: do ranks persist across headers?"""
    K, B = counts_matrix.shape
    rank_matrix = np.zeros_like(counts_matrix)
    for i in range(K):
        order = np.argsort(-counts_matrix[i])
        ranks = np.empty_like(order, dtype=float)
        ranks[order] = np.arange(B)
        rank_matrix[i] = ranks
    mean_rank = rank_matrix.mean(axis=0)
    grand_mean = (B - 1) / 2.0
    SS_cols = K * np.sum((mean_rank - grand_mean)**2)
    SS_total = np.sum((rank_matrix - grand_mean)**2)
    if SS_total == 0:
        return 0.0, 1.0
    stat = (K - 1) * SS_cols / SS_total
    p = 1 - stats.chi2.cdf(stat, df=B - 1)
    return float(stat), float(p)

def rank_correlation_between_difficulties(M_easy, M_hard):
    """
    For each block: rank buckets by winner count at easy vs hard difficulty.
    Compute Spearman r averaged across blocks.
    If r > 0 → hot zones persist across difficulties.
    """
    K = min(M_easy.shape[0], M_hard.shape[0])
    correlations = []
    for i in range(K):
        if M_easy[i].sum() > 0 and M_hard[i].sum() > 0:
            r, p = stats.spearmanr(M_easy[i], M_hard[i])
            correlations.append(r)
    return np.array(correlations)

# ════════════════════════════════════════════════════════════════════════════════

np.random.seed(42)
print("═"*72)
print("  Miner With Holes — Sensitivity Analysis Across Difficulty Levels")
print("═"*72)
print(f"  {K_HEADERS} block hashes × {SCAN_RANGE:,} nonces × {N_BUCKETS} buckets")
print(f"  Hypothesis: easy difficulty reveals structure invisible at 8-bit\n")

for algo_name, collect_fn in [("SHA256d (64 rounds)", collect_sha256d),
                               ("Real Haraka-512",    collect_haraka)]:
    print(f"{'═'*72}")
    print(f"  Algorithm: {algo_name}")
    print(f"{'═'*72}")

    # Collect matrices at ALL difficulty levels
    matrices = {}
    for diff_label, target in DIFFICULTY_LEVELS.items():
        t0 = time.perf_counter()
        M = np.zeros((K_HEADERS, N_BUCKETS))
        for i, prev in enumerate(CHAIN):
            M[i] = collect_fn(prev, target)
        elapsed = time.perf_counter() - t0
        matrices[diff_label] = M
        total = M.sum()
        winners_per_bucket = total / (K_HEADERS * N_BUCKETS)
        cv = M.std(axis=1).mean() / M.mean() if M.mean() > 0 else 0
        print(f"  {diff_label}  {elapsed:4.1f}s  "
              f"{winners_per_bucket:7.1f} w/bucket  CV={cv:.3f}")

    print()

    # ── TEST 1: Chi2 uniformity at each difficulty ──────────────────────────
    print(f"  TEST 1: Winner distribution uniformity (χ² test)")
    print(f"  {'Difficulty':20s}  {'χ²':>10}  {'p-value':>8}  {'Min bucket':>10}  {'Max bucket':>10}  Result")
    print("  " + "-"*68)
    for diff_label, M in matrices.items():
        chi2, p = chi2_test(M)
        agg = M.sum(axis=0)
        if chi2 is not None:
            signal = p < 0.01
            print(f"  {diff_label}  {chi2:>10.1f}  {p:>8.4f}  "
                  f"{agg.min():>10.0f}  {agg.max():>10.0f}  "
                  f"{'NON-UNIFORM ◄' if signal else 'uniform ✓'}")
        else:
            print(f"  {diff_label}  {'N/A':>10}  {'N/A':>8}  (too few winners)")
    print()

    # ── TEST 2: Friedman cross-block test at each difficulty ────────────────
    print(f"  TEST 2: Cross-block Friedman test (do hot zones persist?)")
    print(f"  {'Difficulty':20s}  {'Friedman stat':>14}  {'p-value':>8}  Result")
    print("  " + "-"*52)
    friedman_results = {}
    for diff_label, M in matrices.items():
        stat, p = friedman_test(M)
        signal = p < 0.01
        friedman_results[diff_label] = (stat, p, signal)
        print(f"  {diff_label}  {stat:>14.1f}  {p:>8.4f}  "
              f"{'SIGNAL ◄' if signal else 'noise'}")
    print()

    # ── TEST 3: Do hot zones at easy difficulty persist to hard difficulty? ──
    print(f"  TEST 3: Rank correlation between difficulty levels")
    print(f"  (Spearman r of bucket winner-counts: easy_diff vs hard_diff)")
    print(f"  {'Easy diff':20s}  {'Hard diff':20s}  {'Mean r':>8}  {'Std r':>6}  {'p(r>0)':>8}  Result")
    print("  " + "-"*74)

    diff_labels = list(matrices.keys())
    hard_label = "8-bit  (0.4%)"

    for easy_label in diff_labels[:-1]:  # all except 8-bit
        M_easy = matrices[easy_label]
        M_hard = matrices[hard_label]
        corrs = rank_correlation_between_difficulties(M_easy, M_hard)
        mean_r = corrs.mean()
        std_r  = corrs.std()
        # One-sample t-test: is mean r significantly > 0?
        t_stat, p_ttest = stats.ttest_1samp(corrs, 0, alternative='greater')
        signal = p_ttest < 0.01 and mean_r > 0.05
        print(f"  {easy_label}  {hard_label}  "
              f"{mean_r:>8.4f}  {std_r:>6.4f}  {p_ttest:>8.4f}  "
              f"{'CARRY-OVER ◄' if signal else 'no carry-over'}")
    print()

    # ── TEST 4: Oracle window test ───────────────────────────────────────────
    # If we use the 1-bit hot zones as a guide for 8-bit mining:
    # What hit rate do we get?
    print(f"  TEST 4: Oracle window — use 1-bit hot zones to guide 8-bit mining")
    easy_label = "1-bit  ( 50%)"
    M_easy = matrices[easy_label]
    M_hard = matrices[hard_label]

    print(f"  Strategy: for each block, scan only the top-N buckets by 1-bit rank")
    print(f"  {'Scan fraction':>14}  {'Buckets scanned':>16}  "
          f"{'Winners captured (8-bit)':>24}  {'Speedup':>8}")
    print("  " + "-"*68)

    for top_n in [8, 16, 32, 64, 128]:
        captured_wins = 0
        total_wins = 0
        for i in range(K_HEADERS):
            # Rank buckets by 1-bit winner count (descending)
            hot_buckets = np.argsort(-M_easy[i])[:top_n]
            total_wins    += M_hard[i].sum()
            captured_wins += M_hard[i][hot_buckets].sum()

        frac_scanned = top_n / N_BUCKETS
        frac_captured = captured_wins / total_wins if total_wins > 0 else 0
        # Speedup = efficiency / cost = (frac_captured) / (frac_scanned)
        speedup = frac_captured / frac_scanned if frac_scanned > 0 else 1.0
        scan_pct  = frac_scanned  * 100
        cap_pct   = frac_captured * 100
        is_useful = speedup > 1.05
        print(f"  {scan_pct:>13.1f}%  {top_n:>16} / {N_BUCKETS}  "
              f"{cap_pct:>18.1f}% of wins  "
              f"{speedup:>7.3f}×  {'◄ USEFUL' if is_useful else ''}")
    print()

    print(f"  NOTE: speedup > 1.0 means you capture more winners than your")
    print(f"  scan fraction → the 1-bit hot zones ARE informative for 8-bit.")
    print(f"  speedup = 1.0 means the 1-bit scan is no better than random.")
    print()

print("═"*72)
print("  INTERPRETATION GUIDE")
print("═"*72)
print()
print("  If TEST 1 uniform at all difficulties:")
print("    → The hash function is truly uniform at every scale.")
print("    → No hot zones exist, even detectable with 130× more data.")
print()
print("  If TEST 2 Friedman shows signal at easy but NOT hard:")
print("    → Hot zones exist AND persist across block hashes,")
print("      but only detectable with enough winners (easy difficulty).")
print("    → Same zones exist at hard difficulty — exploitable!")
print()
print("  If TEST 3 rank correlation > 0 (p < 0.01):")
print("    → Hot zones at easy difficulty ARE the hot zones at hard difficulty.")
print("    → Scan easy difficulty once to build the map, then mine hard.")
print()
print("  If TEST 4 speedup > 1.0:")
print("    → The 'miner with holes' strategy WORKS.")
print("    → Skip cold zones, gain efficiency proportional to speedup - 1.")
