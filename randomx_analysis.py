"""
randomx_analysis.py — RandomX-inspired simulation analysis

Tests two angles for exploitable structure:
  1. Standard: bit-correlation between prev_hash bits and winning nonce bits
  2. Novel: CBRANCH branch count as a timing side-channel
     - Does branch_count correlate with hash output?
     - Can high branch_count predict winning nonces?

RandomX design brief:
  - Program fixed per ~2048 blocks (same for all nonces in epoch)
  - Nonce only affects initial VM register state via SHA3(prev||nonce)
  - CBRANCH: conditional loops with data-dependent iteration count
  - 10-round AES finalization (full AES = no structural weakness from verus test)
"""

import struct, time, json, random
import numpy as np
from scipy import stats
import randomx_sim

random.seed(42); np.random.seed(42)

NBLOCKS    = 100
SCAN_RANGE = 200_000
# 8-bit difficulty: P=1/256, ~781 winners per 200K nonces
TARGET     = b'\x01' + b'\x00' * 31
N_PERM     = 300
NONCE_BITS = 18   # 2^18 > 200K
HASH_BITS  = 64

GENESIS = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")


# ── Mine a 100-block chain ────────────────────────────────────────────────
print("Mining 100-block chain (RandomX sim) ...")
t0 = time.perf_counter()
prev = GENESIS
chain_prevs = []

for i in range(NBLOCKS):
    block_key = randomx_sim.set_program(prev)
    # scan until we find at least one winner
    for attempt in range(1, 20):
        ws = randomx_sim.scan_winners(prev, 10_000 * attempt, TARGET)
        if ws:
            break
    chain_prevs.append(prev.hex())
    out_hash = randomx_sim.hash(prev, min(ws))
    prev = out_hash

t_mine = time.perf_counter() - t0
print(f"  Done: {t_mine:.2f}s  ({len(chain_prevs)} blocks)  prev_hashes ready\n")

# Quick benchmark
randomx_sim.set_program(GENESIS)
t0 = time.perf_counter()
for n in range(5000):
    randomx_sim.hash(GENESIS, n)
t_bench = time.perf_counter() - t0
mhs = 5000 / t_bench / 1e6
print(f"  Benchmark: {mhs:.4f} MH/s\n")


# ── Statistical helpers ────────────────────────────────────────────────────
def max_abs_corr(A: np.ndarray, B: np.ndarray) -> tuple:
    Ac = A - A.mean(0)
    Bc = B - B.mean(0)
    As = A.std(0) + 1e-12
    Bs = B.std(0) + 1e-12
    cov  = (Ac.T @ Bc) / len(A)
    corr = np.abs(cov / np.outer(As, Bs))
    idx  = np.unravel_index(np.argmax(corr), corr.shape)
    return float(corr[idx]), int(idx[0]), int(idx[1])


# ════════════════════════════════════════════════════════════════════════════
# TEST 1: Standard bit-correlation (prev_hash bits → winning nonce bits)
# ════════════════════════════════════════════════════════════════════════════
print("═"*65)
print("  TEST 1: Bit-correlation (prev_hash → winning nonce)")
print("═"*65)
print(f"  Scanning {SCAN_RANGE:,} nonces × {NBLOCKS} blocks ...")

t0    = time.perf_counter()
pairs = []
for ph in chain_prevs:
    prev_b = bytes.fromhex(ph)
    randomx_sim.set_program(prev_b)
    ws = randomx_sim.scan_winners(prev_b, SCAN_RANGE, TARGET)
    for n in ws:
        pairs.append((ph, n))

t_scan = time.perf_counter() - t0
N_pairs = len(pairs)
speed   = (SCAN_RANGE * NBLOCKS / 1e6) / t_scan
print(f"  Done: {t_scan:.1f}s  ({speed:.3f} MH/s)  |  {N_pairs:,} pairs  |  {N_pairs/NBLOCKS:.0f}/block")

X_bits  = []
Y_nonce = []
for ph, nonce in pairs:
    prev_int = int(ph, 16) & 0xFFFFFFFFFFFFFFFF
    X_bits.append([(prev_int >> b) & 1 for b in range(HASH_BITS)])
    Y_nonce.append([(nonce >> b) & 1 for b in range(NONCE_BITS)])

X = np.array(X_bits,  dtype=np.float32)
Y = np.array(Y_nonce, dtype=np.float32)

obs_corr, ri, ci = max_abs_corr(X, Y)

print(f"  Running {N_PERM} permutations ...")
null = []
for _ in range(N_PERM):
    c, _, _ = max_abs_corr(X, Y[np.random.permutation(N_pairs)])
    null.append(c)
null  = np.array(null)
p_val = (null >= obs_corr).mean()

print(f"  Max|corr| = {obs_corr:.6f}  (hash bit {ri} vs nonce bit {ci})")
print(f"  Null mean = {null.mean():.6f}  95pct = {np.percentile(null,95):.6f}")
print(f"  p-value   = {p_val:.4f}  → {'SIGNAL ◄' if p_val<0.05 else 'noise'}")

all_n   = np.array([n for _, n in pairs])
bcounts = np.bincount(all_n * 16 // SCAN_RANGE, minlength=16)
chi2_n  = ((bcounts - N_pairs/16)**2 / (N_pairs/16)).sum()
p_unif  = 1 - stats.chi2.cdf(chi2_n, df=15)
print(f"  Nonce distribution: χ²={chi2_n:.2f}  p={p_unif:.4f}"
      f"  → {'uniform ✓' if p_unif>0.05 else 'NON-UNIFORM !'}")

result1 = {
    "test": "bit_correlation",
    "N_pairs": N_pairs,
    "corr": float(obs_corr),
    "pvalue": float(p_val),
    "null_mean": float(null.mean()),
    "p_uniform": float(p_unif),
    "verdict": "SIGNAL" if p_val < 0.05 else "noise",
}


# ════════════════════════════════════════════════════════════════════════════
# TEST 2: CBRANCH timing side-channel
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("  TEST 2: CBRANCH timing side-channel")
print("═"*65)

TIMING_BLOCKS = 20
TIMING_RANGE  = 50_000   # get ~195 winners + branch data

print(f"  Collecting branch counts for {TIMING_RANGE:,} nonces × {TIMING_BLOCKS} blocks ...")
t0 = time.perf_counter()

all_branches = []
all_is_winner = []
winner_branches = []
loser_branches  = []

for ph in chain_prevs[:TIMING_BLOCKS]:
    prev_b = bytes.fromhex(ph)
    randomx_sim.set_program(prev_b)
    data = randomx_sim.scan_with_timing(prev_b, TIMING_RANGE, TARGET)
    for nonce, bc, is_win in data:
        all_branches.append(bc)
        all_is_winner.append(is_win)
        if is_win:
            winner_branches.append(bc)
        else:
            loser_branches.append(bc)

t_timing = time.perf_counter() - t0
all_branches   = np.array(all_branches)
all_is_winner  = np.array(all_is_winner)
winner_branches = np.array(winner_branches)
loser_branches  = np.array(loser_branches)

n_winners_t2 = int(all_is_winner.sum())
print(f"  Done: {t_timing:.1f}s  |  {n_winners_t2:,} winners, {len(loser_branches):,} losers")
print(f"  Branch counts: min={all_branches.min()}  max={all_branches.max()}"
      f"  mean={all_branches.mean():.2f}  unique={len(np.unique(all_branches))}")

# 2a. Do winners have different branch counts than losers?
if n_winners_t2 >= 20 and len(loser_branches) >= 20:
    t_stat, p_ttest = stats.ttest_ind(winner_branches, loser_branches, equal_var=False)
    print(f"\n  2a. Winner vs loser branch count (t-test):")
    print(f"      Winners:  mean={winner_branches.mean():.3f}  std={winner_branches.std():.3f}")
    print(f"      Losers:   mean={loser_branches.mean():.3f}  std={loser_branches.std():.3f}")
    print(f"      t={t_stat:.3f}  p={p_ttest:.4f}  → {'SIGNAL ◄' if p_ttest<0.05 else 'noise'}")
else:
    print("  2a. Too few winners for t-test")
    p_ttest = 1.0
    t_stat  = 0.0

# 2b. Correlation between branch count and winner probability
# Group nonces by branch count, compute win rate
unique_bc = np.unique(all_branches)
if len(unique_bc) > 1:
    bc_win_rate = []
    bc_count    = []
    for bc_val in unique_bc:
        mask = (all_branches == bc_val)
        n_total = mask.sum()
        n_win   = all_is_winner[mask].sum()
        if n_total >= 10:
            bc_win_rate.append(n_win / n_total)
            bc_count.append(bc_val)

    if len(bc_count) >= 3:
        bc_count    = np.array(bc_count)
        bc_win_rate = np.array(bc_win_rate)
        r_pearson, p_pearson = stats.pearsonr(bc_count, bc_win_rate)
        print(f"\n  2b. Branch count → win-rate correlation:")
        print(f"      Groups: {len(bc_count)} (branch values with ≥10 nonces)")
        print(f"      Pearson r = {r_pearson:.4f}  p = {p_pearson:.4f}"
              f"  → {'SIGNAL ◄' if p_pearson<0.05 else 'noise'}")
        print(f"      Win-rate range: [{bc_win_rate.min():.4f}, {bc_win_rate.max():.4f}]"
              f"  expected ~{all_is_winner.mean():.4f}")
    else:
        r_pearson, p_pearson = 0.0, 1.0
        print("  2b. Not enough branch-count groups for correlation")
else:
    r_pearson, p_pearson = 0.0, 1.0
    print("  2b. All nonces have identical branch count — no signal possible")

# 2c. Chi-squared: is branch_count distribution the same for winners vs losers?
if n_winners_t2 >= 20:
    bc_vals = sorted(np.unique(all_branches))
    w_hist = np.array([np.sum(winner_branches == v) for v in bc_vals], dtype=float)
    l_hist = np.array([np.sum(loser_branches  == v) for v in bc_vals], dtype=float)
    # Normalize to same total
    l_hist_scaled = l_hist * (w_hist.sum() / l_hist.sum()) if l_hist.sum() > 0 else l_hist
    # Only include bins with expected count >= 5
    mask_chi = (l_hist_scaled >= 5)
    if mask_chi.sum() >= 2:
        chi2_bc = ((w_hist[mask_chi] - l_hist_scaled[mask_chi])**2 / l_hist_scaled[mask_chi]).sum()
        df_chi  = mask_chi.sum() - 1
        p_chi_bc = 1 - stats.chi2.cdf(chi2_bc, df=df_chi)
        print(f"\n  2c. Branch distribution: winner vs loser (χ²={chi2_bc:.2f} df={df_chi})")
        print(f"      p = {p_chi_bc:.4f}  → {'SIGNAL ◄' if p_chi_bc<0.05 else 'same distribution ✓'}")
    else:
        p_chi_bc = 1.0
        chi2_bc  = 0.0
        print("  2c. Too few bins for χ² test")
else:
    p_chi_bc = 1.0; chi2_bc = 0.0

result2 = {
    "test": "cbranch_timing",
    "n_winners": n_winners_t2,
    "winner_branch_mean": float(winner_branches.mean()) if n_winners_t2 > 0 else None,
    "loser_branch_mean": float(loser_branches.mean()) if len(loser_branches) > 0 else None,
    "p_ttest": float(p_ttest),
    "pearson_r": float(r_pearson),
    "pearson_p": float(p_pearson),
    "p_chi_bc": float(p_chi_bc),
    "verdict": "SIGNAL" if (p_ttest < 0.05 or p_pearson < 0.05 or p_chi_bc < 0.05) else "noise",
}


# ════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("FINAL SUMMARY — RandomX simulation analysis")
print(f"{'═'*65}\n")

for r in [result1, result2]:
    label = r["test"]
    v     = r["verdict"]
    if label == "bit_correlation":
        print(f"  1. Bit-correlation (prev_hash → nonce):")
        print(f"     Max|corr| = {r['corr']:.6f}  p = {r['pvalue']:.4f}  → {v}")
    else:
        print(f"  2. CBRANCH timing side-channel:")
        print(f"     t-test p  = {r['p_ttest']:.4f}  (winner vs loser branch count)")
        print(f"     Pearson p = {r['pearson_p']:.4f}  (branch count → win-rate)")
        print(f"     χ² p      = {r['p_chi_bc']:.4f}  (branch distribution)")
        print(f"     → {v}")

print()
any_signal = any(r["verdict"] == "SIGNAL" for r in [result1, result2])
if any_signal:
    print("CONCLUSION: SIGNAL DETECTED in RandomX simulation!")
    print("  → Investigate the flagged angle for exploitable structure")
else:
    print("CONCLUSION: No statistical signal found in RandomX simulation.")
    print("  → 10-round AES finalization defeats bit-correlation attacks")
    print("  → CBRANCH timing does not predict winning nonces")
    print("  → SHA3(prev||nonce) initial state provides full diffusion")

print()
print("Comparison vs previous results:")
print("  xxHash64    (non-crypto):           no corr signal (invertible algebraically)")
print("  HeavyHash   (SHA3-wrapped):         no signal")
print("  VerusHash N=1 (no MixColumns):      SIGNAL — algebraically predictable")
print("  VerusHash N=4 (≥2 MixColumns):      no signal")
print("  RandomX sim (10-round AES + CBRANCH): see above")

with open("randomx_analysis_results.json", "w") as f:
    json.dump([result1, result2], f, indent=2)
print("\nResults → randomx_analysis_results.json")
