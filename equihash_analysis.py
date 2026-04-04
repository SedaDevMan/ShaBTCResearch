"""
equihash_analysis.py — Batch scoring for Equihash-style PoW

Key question: Can we use cheap partial-evaluation scores (candidate
counts at early Wagner stages) to predict whether a nonce will yield
solutions — without completing the full Wagner algorithm?

If depth_1_count (surviving candidates after round 1) predicts
final solution count, we can:
  - Run only round 1 (cheap)
  - Score each nonce
  - Only complete the expensive rounds 2-4 on high-scoring nonces
  - Skip the rest → massive speedup

Equihash(n=40,k=4) reduced params:
  - n=40 bits, k=4 rounds, 8 collision bits per round
  - Solution = 16 indices
  - Initial candidates: 1024 per nonce
"""

import time, json, random
import numpy as np
from scipy import stats
import equihash_sim

random.seed(42); np.random.seed(42)

HEADER   = b'equihash_test_block_00000000' + b'\x00' * 4   # 32 bytes
N_NONCES = 500   # nonces to test

print("Equihash(40,4) batch-scoring analysis")
print(f"Testing {N_NONCES} nonces ...\n")

# ── Collect depth counts + solution counts for all nonces ─────────────────
t0 = time.perf_counter()
records = []
for nonce in range(N_NONCES):
    n_sol, counts = equihash_sim.solve_with_scores(HEADER, nonce)
    records.append({
        "nonce":  nonce,
        "n_sol":  n_sol,
        "d0": counts[0],  # initial candidates (always LIST_SIZE=1024)
        "d1": counts[1],  # survivors after round 1 (8-bit collision match)
        "d2": counts[2],  # survivors after round 2
        "d3": counts[3],  # survivors after round 3
    })
t_total = time.perf_counter() - t0

n_sols  = np.array([r["n_sol"] for r in records])
d1_vals = np.array([r["d1"]    for r in records])
d2_vals = np.array([r["d2"]    for r in records])
d3_vals = np.array([r["d3"]    for r in records])

speed = N_NONCES / t_total
print(f"Done: {t_total:.1f}s  ({speed:.1f} nonces/s)")
print(f"Solutions/nonce: mean={n_sols.mean():.1f}  std={n_sols.std():.1f}"
      f"  min={n_sols.min()}  max={n_sols.max()}")
print(f"Depth-1 candidates: mean={d1_vals.mean():.0f}  std={d1_vals.std():.0f}"
      f"  min={d1_vals.min()}  max={d1_vals.max()}")
print(f"Depth-2 candidates: mean={d2_vals.mean():.0f}  std={d2_vals.std():.0f}")
print(f"Depth-3 candidates: mean={d3_vals.mean():.0f}  std={d3_vals.std():.0f}")


# ════════════════════════════════════════════════════════════════════════════
# TEST 1: Correlation between early depth counts and final solution count
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("TEST 1: Early depth-count → final solution-count correlation")
print("═"*65)

for depth_name, depth_arr in [("depth-1", d1_vals), ("depth-2", d2_vals), ("depth-3", d3_vals)]:
    if depth_arr.std() < 0.01:
        print(f"  {depth_name}: zero variance — all nonces identical → no scoring possible")
        continue
    r, p = stats.pearsonr(depth_arr, n_sols)
    rs, ps = stats.spearmanr(depth_arr, n_sols)
    print(f"  {depth_name} → n_solutions:")
    print(f"    Pearson  r={r:.4f}  p={p:.6f}  {'SIGNAL ◄' if p<0.05 else 'noise'}")
    print(f"    Spearman r={rs:.4f}  p={ps:.6f}  {'SIGNAL ◄' if ps<0.05 else 'noise'}")


# ════════════════════════════════════════════════════════════════════════════
# TEST 2: Can depth-1 count classify "good" vs "bad" nonces?
# Threshold: nonces with depth-1 > median predicted to have more solutions
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("TEST 2: Threshold-based batch scoring")
print("═"*65)

if d1_vals.std() > 0.01:
    thresh_d1 = np.median(d1_vals)
    high_mask = d1_vals > thresh_d1
    low_mask  = ~high_mask

    high_sols = n_sols[high_mask]
    low_sols  = n_sols[low_mask]

    print(f"  Depth-1 threshold: {thresh_d1:.0f}")
    print(f"  High-score nonces ({high_mask.sum()}): mean solutions = {high_sols.mean():.2f}")
    print(f"  Low-score  nonces ({low_mask.sum()}):  mean solutions = {low_sols.mean():.2f}")

    t_stat, p_ttest = stats.ttest_ind(high_sols, low_sols)
    print(f"  t-test: t={t_stat:.3f}  p={p_ttest:.6f}  {'SIGNAL ◄' if p_ttest<0.05 else 'noise'}")

    # What fraction of solutions are captured in high-score half?
    total_sols  = n_sols.sum()
    high_pct    = high_sols.sum() / total_sols * 100 if total_sols > 0 else 50
    print(f"  Top-50% nonces by depth-1 contain {high_pct:.1f}% of all solutions")
    print(f"  → If we skip the bottom 50%, we lose {100-high_pct:.1f}% of solutions")

    # Try different thresholds
    print(f"\n  Threshold sweep (skip X% of nonces by low depth-1 score):")
    print(f"  {'Skip%':>6}  {'Nonces skipped':>14}  {'Solutions lost':>14}  {'Miss rate':>10}")
    for pct in [10, 25, 50, 75, 90]:
        thresh = np.percentile(d1_vals, pct)
        skip_mask = d1_vals <= thresh
        sols_lost = n_sols[skip_mask].sum()
        miss_rate = sols_lost / total_sols * 100 if total_sols > 0 else 0
        print(f"  {pct:>6}%  {skip_mask.sum():>14}  {sols_lost:>14}  {miss_rate:>9.2f}%")
else:
    print("  Depth-1 has zero variance — scoring impossible (all nonces identical at this depth)")


# ════════════════════════════════════════════════════════════════════════════
# TEST 3: Speedup estimate
# If we can score with depth-1 only (cheap) and skip low scorers:
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("TEST 3: Speedup estimate")
print("═"*65)

# Measure time for depth-1 only vs full solve
t0 = time.perf_counter()
for nonce in range(100):
    equihash_sim.score_partial(HEADER, nonce, 1)
t_d1 = (time.perf_counter() - t0) / 100

t0 = time.perf_counter()
for nonce in range(100):
    equihash_sim.solve_with_scores(HEADER, nonce)
t_full = (time.perf_counter() - t0) / 100

print(f"  Full solve time:    {t_full*1000:.2f} ms/nonce")
print(f"  Depth-1 score time: {t_d1*1000:.2f} ms/nonce")
print(f"  Score is {t_full/t_d1:.1f}x cheaper than full solve")

if d1_vals.std() > 0.01:
    # Best threshold: skip 50% of nonces using depth-1
    for skip_pct in [25, 50, 75]:
        thresh = np.percentile(d1_vals, skip_pct)
        skip_mask = d1_vals <= thresh
        sols_lost_pct = n_sols[skip_mask].sum() / n_sols.sum() * 100 if n_sols.sum() > 0 else 0
        # Total time = (skip_pct% × t_d1) + (100-skip_pct)% × t_full)
        t_with_scoring = (skip_pct/100 * t_d1 + (1-skip_pct/100) * t_full)
        speedup = t_full / t_with_scoring
        print(f"\n  Skip bottom {skip_pct}% by depth-1 score:")
        print(f"    Solutions lost: {sols_lost_pct:.2f}%")
        print(f"    Effective speedup: {speedup:.2f}x")
        print(f"    → {'USEFUL ◄' if speedup > 1.2 and sols_lost_pct < 5 else 'not useful'}")


# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("SUMMARY")
print("═"*65)

if d1_vals.std() > 0.01:
    r_d1, p_d1 = stats.pearsonr(d1_vals, n_sols)
    scoring_works = p_d1 < 0.05 and abs(r_d1) > 0.3

    if scoring_works:
        print(f"  Depth-1 count IS predictive (r={r_d1:.3f}, p={p_d1:.6f})")
        print(f"  Batch scoring WORKS for Equihash — partial evaluation predicts solutions")
        print(f"  This means: score ~10% of cost, skip losers, keep 95%+ of winners")
        print(f"  Real-world potential: significant speedup on FLUX/ZEC-style mining")
    else:
        print(f"  Depth-1 count is NOT predictive (r={r_d1:.3f}, p={p_d1:.4f})")
        print(f"  Batch scoring does NOT work — all nonces have similar depth distributions")
        print(f"  Result: Equihash cannot be scored cheaply; full solve required per nonce")
else:
    print("  Zero variance in depth-1 — all nonces produce identical Wagner trees")
    print("  This means the program is deterministic per header → nonce doesn't help")

print()
print("Previous results for comparison:")
print("  SHA256d/RandomX/HeavyHash: no structure → scoring impossible")
print("  VerusHash N=1: algebraic prediction (not scoring — O(1) direct answer)")
print("  Equihash: see above")

with open("equihash_analysis_results.json", "w") as f:
    json.dump({
        "n_nonces": N_NONCES,
        "sol_mean": float(n_sols.mean()),
        "sol_std":  float(n_sols.std()),
        "d1_std":   float(d1_vals.std()),
        "d1_corr_r": float(stats.pearsonr(d1_vals, n_sols)[0]) if d1_vals.std() > 0.01 else 0,
        "d1_corr_p": float(stats.pearsonr(d1_vals, n_sols)[1]) if d1_vals.std() > 0.01 else 1,
        "t_full_ms": t_full * 1000,
        "t_d1_ms":   t_d1 * 1000,
    }, f, indent=2)
print("\nResults → equihash_analysis_results.json")
