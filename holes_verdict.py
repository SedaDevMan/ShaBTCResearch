"""
holes_verdict.py — Decisive test for the "miner with holes" strategy

The miner_holes.py result showed:
  TEST 3: Spearman r=0.24 between 4-bit and 8-bit winner zones (p=0.0000)
  TEST 4: 1.07x speedup using 1-bit hot zones to guide 8-bit mining

BUT: both tests were INTRA-BLOCK (same prev_hash, just different thresholds).
That is useless — to know which zones are hot for block N you already have
to hash all nonces for block N. Same cost.

The user's strategy REQUIRES:
  "Scan block N at easy difficulty → learn hot zones →
   use those zones for block N+1 at hard difficulty"

That is INTER-BLOCK carry-over. We need to test:
  Spearman r( M_easy[i, :], M_hard[i+1, :] ) for consecutive blocks
  = does PREVIOUS block's easy scan predict CURRENT block's hard scan?

If r > 0 significantly → miner with holes WORKS.
If r ≈ 0 → no carry-over, strategy fails.

We also test a 2-phase strategy:
  Phase 1: scan block N at easy difficulty (cheap, many winners, fast map)
  Phase 2: scan block N+1 at hard difficulty, only in zones predicted by N
  Speedup = (fraction of winners captured) / (fraction of nonces scanned)

To be economically useful: speedup > 1.0 AND (phase1_cost + phase2_cost) < full_scan_cost
"""

import time, hashlib, struct
import numpy as np
from scipy import stats
import pot_skip

SCAN_RANGE  = 256_000
N_BUCKETS   = 256
K_BLOCKS    = 100     # need many consecutive block pairs
bucket_size = SCAN_RANGE // N_BUCKETS

TARGET_EASY = b'\x80' + b'\x00' * 31   # 1-bit: 50% winners
TARGET_HARD = b'\x01' + b'\x00' * 31   # 8-bit: 0.4% winners

def make_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

CHAIN = make_chain(K_BLOCKS + 1)

print("═"*70)
print("  Holes Strategy: INTER-BLOCK carry-over test (decisive test)")
print("═"*70)
print(f"  {K_BLOCKS} consecutive block pairs, {SCAN_RANGE:,} nonces, {N_BUCKETS} buckets")
print(f"  Strategy: scan block N at easy (1-bit) → predict block N+1 at hard (8-bit)")
print()

# ── Collect all bucket counts ────────────────────────────────────────────────
print("  Collecting bucket counts for all blocks ...")
t0 = time.perf_counter()

M_easy = np.zeros((K_BLOCKS + 1, N_BUCKETS))
M_hard = np.zeros((K_BLOCKS + 1, N_BUCKETS))

for i, prev in enumerate(CHAIN):
    M_easy[i] = pot_skip.winner_density(prev, SCAN_RANGE, TARGET_EASY, 0, 64)
    M_hard[i] = pot_skip.winner_density(prev, SCAN_RANGE, TARGET_HARD, 0, 64)

print(f"  Done: {time.perf_counter()-t0:.1f}s\n")

# ── TEST A: INTRA-block r (same block, easy vs hard) ─────────────────────────
# This is what miner_holes.py found: r≈0.24 for 4-bit vs 8-bit
print("─"*70)
print("  TEST A: INTRA-block correlation (same prev_hash, easy vs hard)")
print("  (This is NOT useful — you already have all the data for block N)")
print("─"*70)

intra_r = []
for i in range(K_BLOCKS + 1):
    if M_easy[i].sum() > 0 and M_hard[i].sum() > 0:
        r, _ = stats.spearmanr(M_easy[i], M_hard[i])
        intra_r.append(r)
intra_r = np.array(intra_r)
t_stat, p_intra = stats.ttest_1samp(intra_r, 0, alternative='greater')
print(f"  Mean r = {intra_r.mean():.4f}  Std = {intra_r.std():.4f}  p(r>0) = {p_intra:.6f}")
print(f"  → SIGNIFICANT but USELESS (same block: no time saving)")
print()

# ── TEST B: INTER-block r — decisive test ────────────────────────────────────
print("─"*70)
print("  TEST B: INTER-block correlation (block N easy → block N+1 hard)")
print("  (THIS is what the miner-with-holes strategy requires)")
print("─"*70)

inter_r = []
for i in range(K_BLOCKS):  # block i → predict block i+1
    prev_easy = M_easy[i]    # block N easy winners
    next_hard = M_hard[i+1]  # block N+1 hard winners (DIFFERENT prev_hash)
    if prev_easy.sum() > 0 and next_hard.sum() > 0:
        r, _ = stats.spearmanr(prev_easy, next_hard)
        inter_r.append(r)
inter_r = np.array(inter_r)
t_stat, p_inter = stats.ttest_1samp(inter_r, 0, alternative='greater')
print(f"  Mean r = {inter_r.mean():.4f}  Std = {inter_r.std():.4f}  p(r>0) = {p_inter:.6f}")
print(f"  → {'CARRY-OVER EXISTS!' if p_inter < 0.01 else 'No carry-over — strategy FAILS'}")
print()

# ── TEST C: Multi-block lookahead ────────────────────────────────────────────
print("─"*70)
print("  TEST C: Accumulated hot-zone map (use last K blocks to predict next)")
print("─"*70)

for lookback in [1, 3, 5, 10, 20]:
    captured_wins  = 0
    total_wins     = 0
    top_n = 32  # scan top 12.5% of buckets

    for i in range(lookback, K_BLOCKS):
        # Build hot-zone map from PREVIOUS lookback blocks (all at easy diff)
        accum = M_easy[i-lookback:i].sum(axis=0)  # accumulated easy winners
        hot_buckets = np.argsort(-accum)[:top_n]   # top-N buckets

        total_wins    += M_hard[i].sum()
        captured_wins += M_hard[i][hot_buckets].sum()

    frac_scan     = top_n / N_BUCKETS
    frac_captured = captured_wins / total_wins if total_wins > 0 else 0
    speedup       = frac_captured / frac_scan if frac_scan > 0 else 1.0
    print(f"  Lookback={lookback:2d} blocks:  "
          f"scan {frac_scan*100:.1f}% → capture {frac_captured*100:.1f}% of wins  "
          f"speedup={speedup:.4f}×  "
          f"{'◄' if speedup > 1.05 else ''}")
print()

# ── TEST D: 2-phase strategy cost analysis ───────────────────────────────────
print("─"*70)
print("  TEST D: Full 2-phase cost model")
print("  Phase 1: scan block N at easy diff (same hash cost per nonce)")
print("  Phase 2: scan block N+1, only top-X% buckets at hard diff")
print()
print("  Cost model: 1 hash evaluation = 1 unit")
print("  Phase 1 cost: SCAN_RANGE units (full easy scan)")
print("  Phase 2 cost: top_n/256 × SCAN_RANGE units")
print("  Total cost:   SCAN_RANGE × (1 + top_n/256)")
print("  Baseline:     SCAN_RANGE × 1 (just scan everything at hard diff)")
print("─"*70)

for top_n in [8, 16, 32, 64]:
    # Best case using intra-block prediction (not usable in practice)
    captured_wins_intra = 0
    total_wins_intra    = 0
    # Use each block's own easy scan (impossible in practice — just measures correlation)
    for i in range(K_BLOCKS):
        hot_buckets = np.argsort(-M_easy[i])[:top_n]
        total_wins_intra    += M_hard[i].sum()
        captured_wins_intra += M_hard[i][hot_buckets].sum()
    frac_cap_intra = captured_wins_intra / total_wins_intra if total_wins_intra > 0 else 0
    eff_intra = frac_cap_intra / (top_n/N_BUCKETS)

    # Actual inter-block prediction (block N-1 easy → block N hard)
    captured_wins_inter = 0
    total_wins_inter    = 0
    for i in range(1, K_BLOCKS):
        hot_buckets = np.argsort(-M_easy[i-1])[:top_n]
        total_wins_inter    += M_hard[i].sum()
        captured_wins_inter += M_hard[i][hot_buckets].sum()
    frac_cap_inter = captured_wins_inter / total_wins_inter if total_wins_inter > 0 else 0
    eff_inter = frac_cap_inter / (top_n/N_BUCKETS)

    # 2-phase total cost vs baseline
    phase1_cost = 1.0                  # full easy scan of prev block
    phase2_cost = top_n / N_BUCKETS    # partial hard scan of current block
    total_cost  = phase1_cost + phase2_cost
    effective_win_rate = frac_cap_inter   # fraction of hard winners captured in phase2
    # Blocks found per unit cost: effective_win_rate / total_cost
    # vs baseline: 1.0 / 1.0 = 1.0 blocks per unit cost
    profit_ratio = effective_win_rate / total_cost

    print(f"  Top-{top_n:3d} ({top_n/N_BUCKETS*100:4.1f}%):  "
          f"intra eff={eff_intra:.4f}  "
          f"inter eff={eff_inter:.4f}  "
          f"2-phase cost={total_cost:.3f}  "
          f"profit_ratio={profit_ratio:.4f}  "
          f"{'◄ BETTER' if profit_ratio > 1.0 else 'worse'}")
print()
print("  profit_ratio > 1.0 means 2-phase strategy finds more blocks per unit work")
print("  profit_ratio < 1.0 means just scanning everything is better")
print()

# ── SUMMARY ──────────────────────────────────────────────────────────────────
print("═"*70)
print("  VERDICT")
print("═"*70)
print()
print(f"  Intra-block r (same block) : {intra_r.mean():.4f}  — REAL but not exploitable")
print(f"  Inter-block r (block N→N+1): {inter_r.mean():.4f}  p={p_inter:.4f}  "
      f"— {'SIGNAL!' if p_inter < 0.01 else 'no signal'}")
print()
if p_inter < 0.01 and inter_r.mean() > 0.05:
    print("  ► MINER WITH HOLES IS FEASIBLE!")
    print(f"    Hot zones from block N (easy scan) predict block N+1 winners.")
    print(f"    See TEST C / TEST D for optimal strategy parameters.")
else:
    print("  ✗ Miner with holes does NOT work for SHA256d:")
    print("    The intra-block correlation (r=0.24) is real but useless —")
    print("    you cannot know block N's easy zones without scanning block N,")
    print("    and block N's easy zones do NOT predict block N+1's hard zones.")
    print()
    print("  The reason: every new prev_hash completely rerandomizes which")
    print("  nonces produce small outputs. Each block is cryptographically fresh.")
    print()
    print("  However: this test was only for SHA256d.")
    print("  The concept WOULD work for any hash with cross-block nonce carry-over.")
    print("  (VerusHash toy N=1 also showed Friedman p=1.0 due to key change per block)")
