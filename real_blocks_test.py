"""
real_blocks_test.py — Two questions:

1. REAL BITCOIN BLOCK HASHES:
   Real prev_hashes always start with N leading zero bytes (they beat difficulty).
   Genesis block: 000000000019d6689c... (4 leading zero bytes at early difficulty).
   Our synthetic chain used random 32-byte values — no leading zeros.

   Q: Does the leading-zero structure in real prev_hashes create any winner
      distribution bias that our synthetic tests missed?

   Test: Run all previous tests (chi2 uniformity, Friedman cross-block,
   inter-block carry-over) using ACTUAL Bitcoin block hashes 0-19.

2. NONCE AUTO-CORRELATION:
   Within a block: if nonce N wins, is nonce N+1 more likely to win?
   This tests whether winners are CLUSTERED in the nonce space.
   A clustered distribution → scan a "winning neighborhood" when you find one.

   If winners are Poisson: P(nonce+k wins | nonce wins) = P(nonce wins) for all k.
   If clustered: P(nonce+k wins | nonce wins) > P(nonce wins) for small |k|.
"""

import hashlib, struct, time
import numpy as np
from scipy import stats
import pot_skip

SCAN_RANGE = 256_000
N_BUCKETS  = 256
bucket_size = SCAN_RANGE // N_BUCKETS

# ── Real Bitcoin block hashes, heights 0-18 ──────────────────────────────────
# These are SHA256d hashes of actual Bitcoin block headers.
# Notice they all start with many zero bytes — real blockchain property.
REAL_BITCOIN_HASHES = [
    "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",  # 0
    "00000000839a8e6886ab5951d76f411475428afc90947ee320161bbf18eb6048",  # 1
    "0000000082b5015589a3fdf2d4baff403e6f0be035a5d9742c1cae6295464449",  # 3
    "000000004ebadb55ee9096c9a2f8880e09da59c0d68b1c228da88e48844a1485",  # 4
    "000000009b7262315dbf071787ad3656097b892abffd1f95a1a022f896f533fc",  # 5
    "000000003031a0e73735690c5a1ff2a4be82553b2a12b776fbd3a215dc8f778d",  # 6
    "0000000071966c2b1d065fd446b1e485b2c9d9594acd2007ccbd5441cfc89444",  # 7
    "00000000408c48f847aa786c2268fc3e6ec2af68e8468a34a28c61b7f1de0dc6",  # 8
    "000000002c05cc2e78923c34df87fd108b22221ac6076c18f3ade378a4d915e9",  # 10
    "0000000097be56d606cdd9c54b04d4747e957d3608abe69198c661f2add73073",  # 11
    "0000000027c2488e2510d1acf4369787784fa20ee084c258b58d9fbd43802b5e",  # 12
    "000000005c51de2031a895adc145ee2242e919a01c6d61fb222a54a54b4d3089",  # 13
    "0000000080f17a0c5a67f663a9bc9969eb37e81666d9321125f0e293656f8a37",  # 14
    "00000000b3322c8c3ef7d2cf6da009a776e6a99ee65ec5a32f3f345712238473",  # 15
    "00000000174a25bb399b009cc8deff1c4b3ea84df7e93affaaf60dc3416cc4f5",  # 16
    "000000003ff1d0d70147acfbef5d6a87460ff5bcfce807c2d5b6f0a66bfdf809",  # 17
    "000000008693e98cf893e4c85a446b410bb4dfa129bd1be582c09ed3f0261116",  # 18
    "00000000841cb802ca97cf20fb9470480cae9e5daa5d06b4a18ae2d5dd7f186f",  # 19
]
REAL_HASHES_BYTES = [bytes.fromhex(h) for h in REAL_BITCOIN_HASHES]

# ── Synthetic chain for comparison ───────────────────────────────────────────
def make_synthetic_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

SYNTHETIC_CHAIN = make_synthetic_chain(len(REAL_HASHES_BYTES))

TARGETS = {
    "1-bit  ( 50%)": b'\x80' + b'\x00' * 31,
    "8-bit  (0.4%)": b'\x01' + b'\x00' * 31,
}

def chi2_test(counts):
    total = counts.sum()
    if total < N_BUCKETS * 2:
        return None, None
    expected = total / N_BUCKETS
    chi2 = ((counts - expected)**2 / expected).sum()
    p = 1 - stats.chi2.cdf(chi2, df=N_BUCKETS - 1)
    return float(chi2), float(p)

def friedman_test(M):
    K, B = M.shape
    rank_matrix = np.zeros_like(M)
    for i in range(K):
        order = np.argsort(-M[i])
        ranks = np.empty_like(order, dtype=float)
        ranks[order] = np.arange(B)
        rank_matrix[i] = ranks
    mean_rank = rank_matrix.mean(axis=0)
    grand_mean = (B - 1) / 2.0
    SS_cols  = K * np.sum((mean_rank - grand_mean)**2)
    SS_total = np.sum((rank_matrix - grand_mean)**2)
    if SS_total == 0:
        return 0.0, 1.0
    stat = (K - 1) * SS_cols / SS_total
    p = 1 - stats.chi2.cdf(stat, df=B - 1)
    return float(stat), float(p)

# ════════════════════════════════════════════════════════════════════════════
print("═"*68)
print("  TEST 1: Real Bitcoin block hashes vs synthetic chain")
print("═"*68)
print()
print(f"  N = {len(REAL_HASHES_BYTES)} blocks  |  {SCAN_RANGE:,} nonces  |  {N_BUCKETS} buckets")
print(f"  Real hashes: all start with 8+ leading zero bits (met difficulty)")
print(f"  Synthetic:   random-looking 32-byte values (SHA256d chain)")
print()

for target_label, target in TARGETS.items():
    print(f"  ─── Target: {target_label} ─────────────────────────────────────")

    for chain_name, chain in [("Real Bitcoin hashes", REAL_HASHES_BYTES),
                               ("Synthetic chain    ", SYNTHETIC_CHAIN)]:
        M = np.zeros((len(chain), N_BUCKETS))
        for i, prev in enumerate(chain):
            counts = pot_skip.winner_density(prev, SCAN_RANGE, target, 0, 64)
            M[i] = counts

        # Aggregate chi2
        agg = M.sum(axis=0)
        chi2, p_chi2 = chi2_test(agg)
        # Friedman
        stat_f, p_f = friedman_test(M)
        # Inter-block carry-over
        inter_r = []
        for i in range(len(chain)-1):
            if M[i].sum() > 0 and M[i+1].sum() > 0:
                r, _ = stats.spearmanr(M[i], M[i+1])
                inter_r.append(r)
        inter_r = np.array(inter_r)
        mean_ir = inter_r.mean()
        _, p_ir = stats.ttest_1samp(inter_r, 0, alternative='greater') if len(inter_r) > 1 else (0, 1)

        print(f"    {chain_name}:  "
              f"chi2={chi2:.1f} p={p_chi2:.4f}  "
              f"Friedman={stat_f:.1f} p={p_f:.4f}  "
              f"inter-block r={mean_ir:.4f} p={p_ir:.4f}")
    print()

# ════════════════════════════════════════════════════════════════════════════
print("═"*68)
print("  TEST 2: Nonce auto-correlation — are winners clustered?")
print("═"*68)
print()
print("  Q: If nonce N wins, is nonce N+k more likely to win?")
print("  Method: collect exact winner nonces, compute run-length & gap stats.")
print()

TARGET_EASY = b'\x80' + b'\x00' * 31

for chain_name, chain in [("Real Bitcoin", REAL_HASHES_BYTES),
                           ("Synthetic   ", SYNTHETIC_CHAIN)]:
    all_gaps = []

    for prev in chain:
        # Get exact winners via slow Python scan for small range
        # Use pot_skip bucket data to get approximate gap distribution
        counts = pot_skip.winner_density(prev, SCAN_RANGE, TARGET_EASY, 0, 64)

        # Reconstruct gap distribution from bucket-level counts
        # Gap between winners = gap within same bucket (fine-grained)
        # + cross-bucket gaps (coarse)
        # For auto-correlation, we need the intra-bucket fine structure.
        # Do a direct Python scan on a small range.
        pass  # replaced below

    # Direct Python scan for gap analysis (smaller range for speed)
    SMALL_RANGE = 20_000
    target_int = int.from_bytes(TARGET_EASY, 'big')
    all_winner_positions = []

    for prev in chain[:5]:  # 5 blocks sufficient
        winners = []
        for n in range(SMALL_RANGE):
            inp = prev + struct.pack('<I', n)
            h1 = hashlib.sha256(inp).digest()
            h2 = hashlib.sha256(h1).digest()
            if int.from_bytes(h2, 'big') < target_int:
                winners.append(n)
        all_winner_positions.append(winners)

    # Compute inter-winner gaps
    all_gaps = []
    for winners in all_winner_positions:
        if len(winners) > 1:
            gaps = np.diff(winners)
            all_gaps.extend(gaps.tolist())

    all_gaps = np.array(all_gaps, dtype=np.float64)

    if len(all_gaps) == 0:
        print(f"  {chain_name}: no gaps found")
        continue

    # Expected gap for Poisson winners at 50% rate: geometric distribution mean=2
    expected_mean_gap = 2.0
    expected_var_gap  = 2.0  # geometric variance = (1-p)/p^2 = 0.5/0.25 = 2

    # Test: are gaps geometrically distributed? (= independent, no clustering)
    # If geometric: CV = std/mean ≈ sqrt(2)/2 ≈ 1.0 for p=0.5
    mean_gap = all_gaps.mean()
    std_gap  = all_gaps.std()
    cv_gap   = std_gap / mean_gap

    # Runs test for clustering: are gaps shorter than expected?
    # Under H0 (geometric): fraction of gap=1 = 0.5 (P(winner|prev_winner)=P(winner)=0.5)
    frac_gap1 = (all_gaps == 1).mean()

    # Auto-correlation at lag 1: does winning at position N predict N+1?
    # ac = correlation between indicator[n] and indicator[n+1]
    # Build indicator array
    all_ac = []
    for prev in chain[:5]:
        ind = np.zeros(SMALL_RANGE, dtype=np.float32)
        for n in range(SMALL_RANGE):
            inp = prev + struct.pack('<I', n)
            h2 = hashlib.sha256(hashlib.sha256(inp).digest()).digest()
            if int.from_bytes(h2, 'big') < target_int:
                ind[n] = 1
        # Auto-correlation at lag 1
        r, p = stats.pearsonr(ind[:-1], ind[1:])
        all_ac.append(r)
    all_ac = np.array(all_ac)

    print(f"  {chain_name}:  n_gaps={len(all_gaps):,}  mean_gap={mean_gap:.3f}  "
          f"cv={cv_gap:.3f}  frac_gap1={frac_gap1:.3f}  ac_lag1={all_ac.mean():.5f}")
    print(f"  {'':13}  Expected (independent): mean_gap≈2.0  cv≈1.0  "
          f"frac_gap1≈0.50  ac_lag1≈0.0")
    clustered = abs(all_ac.mean()) > 0.02
    print(f"  {'':13}  → {'CLUSTERED ◄' if clustered else 'independent — no clustering'}")
    print()

# ════════════════════════════════════════════════════════════════════════════
print("═"*68)
print("  TEST 3: Leading-zero count in prev_hash vs winner density variance")
print("═"*68)
print()
print("  Do more leading zeros → less or more variance in winner distribution?")
print()

# Create chain with varying leading-zero counts
test_hashes = {
    "0 leading zeros  ": bytes.fromhex("face0000000000000000000000000000000000000000000000000000000000ff"),
    "1 leading zero   ": bytes.fromhex("00face000000000000000000000000000000000000000000000000000000cafe"),
    "2 leading zeros  ": bytes.fromhex("0000face00000000000000000000000000000000000000000000000000000042"),
    "4 leading zeros  ": bytes.fromhex("00000000face0000000000000000000000000000000000000000000000000007"),
    "8 leading zeros  ": bytes.fromhex("00000000000000001234567890abcdef1234567890abcdef1234567890abcdef"),
    "Bitcoin genesis   ": bytes.fromhex("000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"),
}

TARGET_EASY = b'\x80' + b'\x00' * 31

print(f"  {'prev_hash type':22s}  {'χ²':>8}  {'p-value':>8}  {'min bucket':>10}  {'max bucket':>10}")
print("  " + "-"*58)
for label, prev in test_hashes.items():
    counts = np.array(pot_skip.winner_density(prev, SCAN_RANGE, TARGET_EASY, 0, 64), dtype=float)
    chi2, p = chi2_test(counts)
    print(f"  {label}  {chi2:>8.1f}  {p:>8.4f}  {counts.min():>10.0f}  {counts.max():>10.0f}")
print()
print("  If leading zeros create bias: chi2 would be systematically higher for more zeros.")
print("  If all chi2 similar: leading-zero structure is irrelevant (hash mixes it away).")
