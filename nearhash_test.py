"""
nearhash_test.py — Cross-POT nonce rank correlation test

Tests whether certain nonces are "inherently lucky" across multiple
different block headers. If yes → near-hash tracking works.

Method:
  For each nonce n in 0..PROBE_SIZE:
    compute H(header_k, n) for K different headers
    compute rank of H(header_k, n) among all nonces (lower = better)

  Test: are ranks correlated across headers?
    Pearson r across K headers for same nonce → should be 0 if independent
    Mean rank variance across nonces → some nonces consistently lower?

Algorithms tested: SHA256-Nr (1,4,64 rounds), VerusHash (N=1,N=10)
"""

import time, json
import numpy as np
from scipy import stats
import pot_skip, verus_aes

PROBE_SIZE = 10_000    # nonces per header (enough for rank stability)
K_HEADERS  = 20        # number of different headers
NEAR_THRESH_FRAC = 0.05  # "near-hash" = top 5% closest to target

import hashlib
def make_headers(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    headers = []
    for _ in range(n):
        headers.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return headers

HEADERS = make_headers(K_HEADERS)

# ── Compute hash values as integers ──────────────────────────────────────
def hash_as_int(out32):
    return int.from_bytes(out32, 'big')

def collect_outputs_sha256nr(header, nonces, nrounds):
    """SHA256-Nr: collect hash outputs for given nonces."""
    TARGET_ALL = b'\xff' * 32  # accept everything
    # Use winner_density with full range to get bucket counts
    # Instead: call pot_skip directly for individual nonces via a workaround
    # We'll use the full scan and record the raw output values via a custom scan
    outputs = []
    for n in nonces:
        # Use winner_density with target just above this nonce's expected output
        # Actually we need raw hash values — use a different approach
        # pot_skip doesn't expose raw values, so we use the rank approach:
        # compare nonce n against nonce n+1 via winner counts
        pass
    return outputs

# Better: implement rank test directly via winner_density buckets
# For PROBE_SIZE nonces split into 256 buckets, rank = which bucket it falls in

def collect_bucket_ranks(header, probe_size, algo_id, param, n_buckets=100):
    """
    For each of n_buckets nonce sub-ranges, count winners at very easy target.
    Returns array[n_buckets] of winner counts → proxy for average hash value in range.
    Lower winner count = higher average hash value in that range (fewer beat easy target).
    """
    # Easy target: accept top 50% (target = 0x80...)
    target_50 = b'\x80' + b'\x00' * 31
    counts = pot_skip.winner_density(header, probe_size, target_50, algo_id, param)
    return np.array(counts[:n_buckets], dtype=float)

def collect_bucket_ranks_verus(header, probe_size, nrounds, n_buckets=256):
    """VerusHash version using verus_aes."""
    target_50 = b'\x80' + b'\x00' * 31
    winners_by_bucket = np.zeros(n_buckets, dtype=float)
    bucket_size = probe_size // n_buckets
    for b in range(n_buckets):
        start = b * bucket_size
        ws = verus_aes.scan_winners(header, bucket_size, target_50, nrounds)
        # Filter to this bucket range
        ws_in_bucket = [w for w in ws if start <= w < start + bucket_size]
        winners_by_bucket[b] = len(ws_in_bucket)
    return winners_by_bucket


print("═"*65)
print("  Cross-POT Nonce Rank Correlation Test")
print("═"*65)
print(f"  {PROBE_SIZE:,} nonces × {K_HEADERS} headers × 256 buckets")
print(f"  Question: do certain nonce ranges win more often across ALL headers?")
print()

results = []

# ════════════════════════════════════════════════════════════════════════
# For each algo: collect bucket winner-counts across K_HEADERS headers
# Then test: is the winner-count in bucket B correlated across headers?
# ════════════════════════════════════════════════════════════════════════

def run_cross_pot_test(algo_name, algo_id, param, probe=PROBE_SIZE, k=K_HEADERS):
    """
    Matrix M[k × 256]: M[i,j] = winner count in bucket j for header i
    If column j has high variance AND is correlated across rows → structure!
    Specifically: are the column RANKS stable across rows?
    """
    print(f"  Testing {algo_name} ...")
    t0 = time.perf_counter()

    M = np.zeros((k, 256), dtype=float)
    for i, header in enumerate(HEADERS[:k]):
        counts = pot_skip.winner_density(header, probe, b'\x80'+b'\x00'*31,
                                          algo_id, param)
        M[i] = np.array(counts, dtype=float)

    elapsed = time.perf_counter() - t0

    # Rank each row (header) independently: which buckets won most?
    # rank_matrix[i,j] = rank of bucket j in header i (0=best, 255=worst)
    rank_matrix = np.zeros_like(M)
    for i in range(k):
        order = np.argsort(-M[i])  # descending: best bucket first
        ranks = np.empty_like(order)
        ranks[order] = np.arange(len(order))
        rank_matrix[i] = ranks

    # For each bucket j: compute mean rank across all headers
    mean_rank = rank_matrix.mean(axis=0)   # shape [256]
    std_rank  = rank_matrix.std(axis=0)

    # If bucket j consistently has low rank (wins more) → it's "lucky"
    # Test: is variance of mean_rank across buckets significant?
    # Under null (random): mean_rank[j] ~ uniform(0,255), std across buckets ≈ 73.6
    # Under alternative: some buckets have consistently lower rank

    # Friedman test: non-parametric test for consistent ranking across headers
    # H0: all buckets have same mean rank
    # H1: some buckets are consistently higher/lower ranked
    n_rows, n_cols = rank_matrix.shape
    grand_mean = (n_cols - 1) / 2.0
    SS_cols = n_rows * np.sum((mean_rank - grand_mean)**2)
    SS_total = np.sum((rank_matrix - grand_mean)**2)
    # Friedman statistic (approximation)
    if SS_total > 0:
        friedman_stat = (n_rows - 1) * SS_cols / SS_total
    else:
        friedman_stat = 0.0
    # Approximate chi2 distribution with df = n_cols - 1
    p_friedman = 1 - stats.chi2.cdf(friedman_stat, df=n_cols-1)

    # Also: find "hot" buckets = those with consistently low rank
    hot_threshold = 64   # top 25% = rank < 64
    hot_buckets   = np.where(mean_rank < hot_threshold)[0]
    pct_hot       = len(hot_buckets) / 256 * 100

    # If we only scan hot buckets: what fraction of winners do we get?
    total_wins     = M.sum()
    hot_wins       = M[:, hot_buckets].sum() if len(hot_buckets) > 0 else 0
    hot_win_frac   = hot_wins / total_wins * 100 if total_wins > 0 else 0

    speedup = hot_win_frac / pct_hot if pct_hot > 0 else 1.0

    is_signal = p_friedman < 0.01

    print(f"    Time: {elapsed:.1f}s  |  Friedman stat={friedman_stat:.1f}  p={p_friedman:.4f}"
          f"  → {'SIGNAL ◄' if is_signal else 'noise'}")
    if is_signal:
        print(f"    Hot buckets (top 25% by rank): {len(hot_buckets)} buckets = {pct_hot:.0f}% of nonce space")
        print(f"    Those buckets contain {hot_win_frac:.1f}% of winners → {speedup:.2f}× speedup")
        print(f"    ► Near-hash tracking WORKS for {algo_name}!")
    else:
        print(f"    No consistent lucky nonce ranges across headers")
        print(f"    Mean rank std: {mean_rank.std():.1f} (expected ~73.6 for random)")

    return {
        "algo": algo_name, "friedman": float(friedman_stat),
        "p": float(p_friedman), "signal": is_signal,
        "hot_buckets": len(hot_buckets), "hot_win_frac": float(hot_win_frac),
        "speedup": float(speedup), "mean_rank_std": float(mean_rank.std()),
    }

# SHA256 at different round counts
for nrounds in [1, 4, 64]:
    r = run_cross_pot_test(f"SHA256-{nrounds}rounds", 0, nrounds,
                           probe=PROBE_SIZE, k=K_HEADERS)
    results.append(r)
    print()

# ETHash-lite
for header in HEADERS[:K_HEADERS]:
    pot_skip.build_dag(header)  # rebuild dag per header
r = run_cross_pot_test("ETHash-lite", 1, 0, probe=PROBE_SIZE, k=K_HEADERS)
results.append(r)
print()

# SHA256d midstate
r = run_cross_pot_test("SHA256d-midstate", 2, 0, probe=PROBE_SIZE, k=K_HEADERS)
results.append(r)
print()

# ── VerusHash N=1 and N=10 (direct via verus_aes) ────────────────────────
print("  Testing VerusHash (direct) ...")
for nrounds, label in [(1, "VerusHash-N1"), (10, "VerusHash-N10")]:
    t0 = time.perf_counter()
    M = np.zeros((K_HEADERS, 256), dtype=float)
    bucket_size = PROBE_SIZE // 256

    for i, header in enumerate(HEADERS[:K_HEADERS]):
        ws = verus_aes.scan_winners(header, PROBE_SIZE,
                                     b'\x80'+b'\x00'*31, nrounds)
        for w in ws:
            b = min(w // bucket_size, 255)
            M[i, b] += 1

    elapsed = time.perf_counter() - t0

    rank_matrix = np.zeros_like(M)
    for i in range(K_HEADERS):
        order = np.argsort(-M[i])
        ranks = np.empty_like(order)
        ranks[order] = np.arange(len(order))
        rank_matrix[i] = ranks

    mean_rank = rank_matrix.mean(axis=0)
    n_rows, n_cols = rank_matrix.shape
    grand_mean = (n_cols - 1) / 2.0
    SS_cols = n_rows * np.sum((mean_rank - grand_mean)**2)
    SS_total = np.sum((rank_matrix - grand_mean)**2)
    friedman_stat = (n_rows-1) * SS_cols / SS_total if SS_total > 0 else 0
    p_friedman = 1 - stats.chi2.cdf(friedman_stat, df=n_cols-1)
    is_signal = p_friedman < 0.01

    hot_buckets = np.where(mean_rank < 64)[0]
    total_wins  = M.sum()
    hot_wins    = M[:, hot_buckets].sum() if len(hot_buckets) > 0 else 0
    hot_win_frac = hot_wins / total_wins * 100 if total_wins > 0 else 0
    speedup = hot_win_frac / (len(hot_buckets)/256*100) if len(hot_buckets) > 0 else 1.0

    print(f"    {label}: {elapsed:.1f}s  Friedman={friedman_stat:.1f}  p={p_friedman:.4f}"
          f"  → {'SIGNAL ◄' if is_signal else 'noise'}")
    if is_signal:
        print(f"    Hot 25% nonce zone contains {hot_win_frac:.1f}% of winners → {speedup:.2f}× speedup")

    results.append({"algo": label, "friedman": float(friedman_stat),
                     "p": float(p_friedman), "signal": is_signal,
                     "hot_win_frac": float(hot_win_frac), "speedup": float(speedup)})
print()


# ════════════════════════════════════════════════════════════════════════
print("═"*65)
print("  SUMMARY — Cross-POT near-hash tracking")
print("═"*65)
print(f"  {'Algorithm':25s}  {'Friedman':>10}  {'p-value':>8}  {'Speedup':>8}  Result")
print("  " + "-"*62)
for r in results:
    s = f"{r['speedup']:.2f}×" if r['signal'] else "  1.00×"
    print(f"  {r['algo']:25s}  {r['friedman']:>10.1f}  {r['p']:>8.4f}  {s:>8}  "
          f"{'WORKS ◄' if r['signal'] else 'no carry-over'}")

print()
any_signal = any(r["signal"] for r in results)
if any_signal:
    print("CONCLUSION: Near-hash tracking DOES carry over between POTs!")
    print("  → Collect near-hashes from previous POTs")
    print("  → Focus next POT scan on those nonce zones")
    print("  → Speedup as shown above")
else:
    print("CONCLUSION: No cross-POT correlation found.")
    print("  → Near-hash nonce positions are independent between POTs")
    print("  → Tracking near-hashes from previous POTs provides no advantage")
    print("  → Each POT's winner distribution is fresh/independent")

with open("nearhash_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("\nResults → nearhash_results.json")
