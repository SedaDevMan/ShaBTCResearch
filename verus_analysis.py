"""
verus_analysis.py — VerusHash-inspired AES round-count analysis

Research question: Does reduced-round AES (like Haraka-512 in VerusHash 2.1)
show exploitable statistical patterns vs full-round AES?

Key structural findings (pre-analysis, by algebra):
  N=1 (aesenclast only):
    - NO MixColumns → each output byte depends on exactly ONE input byte
    - Nonce byte 0 → output byte 0; bytes 1-3 of output are FIXED per prev_hash
    - Winners are ALL nonces where nonce_byte0 = one specific value per block
    - Winning byte0 = sbox_inv[ rk1[0] XOR 0x00 ] XOR rk0[0] = f(prev_hash)
    - PERFECT predictability: given prev_hash, winning nonces are deterministic

  N=2 (aesenc + aesenclast):
    - MixColumns in round 1 mixes within each column
    - nonce_byte0 → out[0]; nonce_byte3 → out[1]; nonce_byte2 → out[2]; nonce_byte1 → out[3]
    - Still highly predictable: 4 nonce bytes independently determine 4 output bytes

  N=4+:
    - Multiple ShiftRows/MixColumns iterations → avalanche effect
    - Cross-byte mixing begins, making prediction harder

Difficulty: 8-bit (target = 0x0100...0), P=1/256
  - For N=1: minimum winner ≤ 255 (within byte0 range)
  - For N=10: winners uniformly distributed, min winner ≈ 250 on average
  - 500K scan: ~1953 winners/block, ~195K pairs per round count
"""

import struct, time, json, random
import numpy as np
from scipy import stats
import verus_aes

random.seed(42); np.random.seed(42)

NBLOCKS    = 100
SCAN_RANGE = 500_000
# 8-bit difficulty: P = 1/256, need out[0] == 0x00
TARGET     = b'\x01' + b'\x00' * 31
N_PERM     = 300
NONCE_BITS = 19   # 2^19 > 500K
HASH_BITS  = 64   # low 64 bits of prev_hash
ROUNDS_TO_TEST = [1, 2, 4, 10]
GENESIS = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")


# ── Mine ONE chain using N=10 ───────────────────────────────────────────────
print("Mining 100-block chain (N=10) ...")
t0   = time.perf_counter()
prev = GENESIS
chain_prevs = []

for i in range(NBLOCKS):
    # 8-bit difficulty: expected ~16 hashes to win
    ws = verus_aes.scan_winners(prev, 500, TARGET, 10)
    if not ws:
        ws = verus_aes.scan_winners(prev, 5_000, TARGET, 10)
    chain_prevs.append(prev.hex())
    block_hash = verus_aes.verus_hash(prev, min(ws), 10)
    prev       = block_hash

t_mine = time.perf_counter() - t0
print(f"  Done: {t_mine:.2f}s  ({len(chain_prevs)} prev_hashes ready)\n")


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


# ── Main loop ─────────────────────────────────────────────────────────────
results_summary = []

for nrounds in ROUNDS_TO_TEST:
    sep = "═" * 65
    print(f"\n{sep}")
    print(f"  N = {nrounds} AES round{'s' if nrounds>1 else ''}")
    print(sep)

    # Collect winners
    print(f"  Scanning {SCAN_RANGE:,} nonces × {NBLOCKS} blocks ...")
    t0    = time.perf_counter()
    pairs = []
    for ph in chain_prevs:
        prev_b = bytes.fromhex(ph)
        ws     = verus_aes.scan_winners(prev_b, SCAN_RANGE, TARGET, nrounds)
        for n in ws:
            pairs.append((ph, n))
    t_scan = time.perf_counter() - t0
    N_pairs = len(pairs)
    speed   = (SCAN_RANGE * NBLOCKS / 1e6) / t_scan
    print(f"  Done: {t_scan:.1f}s  ({speed:.2f} MH/s)  |  {N_pairs:,} pairs  |  {N_pairs/NBLOCKS:.0f}/block")

    if N_pairs < 200:
        print(f"  Too few pairs — skipping (check target or scan range)")
        results_summary.append({"nrounds": nrounds, "N_pairs": N_pairs, "verdict": "too few"})
        continue

    # Build features
    X_bits  = []
    Y_nonce = []
    for ph, nonce in pairs:
        prev_int = int(ph, 16) & 0xFFFFFFFFFFFFFFFF
        X_bits.append([(prev_int >> b) & 1 for b in range(HASH_BITS)])
        Y_nonce.append([(nonce >> b) & 1 for b in range(NONCE_BITS)])

    X = np.array(X_bits,  dtype=np.float32)
    Y = np.array(Y_nonce, dtype=np.float32)

    # Correlation
    obs_corr, ri, ci = max_abs_corr(X, Y)

    # Permutation test
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

    # Winner distribution stats
    all_n   = np.array([n for _, n in pairs])
    bcounts = np.bincount(all_n * 16 // SCAN_RANGE, minlength=16)
    chi2_n  = ((bcounts - N_pairs/16)**2 / (N_pairs/16)).sum()
    p_unif  = 1 - stats.chi2.cdf(chi2_n, df=15)
    print(f"  Nonce distribution: χ²={chi2_n:.2f}  p={p_unif:.6f}"
          f"  → {'uniform ✓' if p_unif>0.05 else 'NON-UNIFORM (clustered) !'}")

    # Winners-per-block distribution
    winners_per_block = []
    i = 0
    for ph in chain_prevs:
        cnt = sum(1 for p, _ in pairs if p == ph)
        winners_per_block.append(cnt)
    wpb = np.array(winners_per_block)
    print(f"  Winners/block: mean={wpb.mean():.1f}  std={wpb.std():.1f}"
          f"  min={wpb.min()}  max={wpb.max()}")
    if wpb.std() > wpb.mean() * 0.5:
        print(f"  HIGH VARIANCE in winners/block → structural weakness detected!")

    results_summary.append({
        "nrounds":   nrounds,
        "N_pairs":   N_pairs,
        "corr":      float(obs_corr),
        "pvalue":    float(p_val),
        "null_mean": float(null.mean()),
        "null_95":   float(np.percentile(null, 95)),
        "p_uniform": float(p_unif),
        "wpb_std":   float(wpb.std()),
        "wpb_mean":  float(wpb.mean()),
        "verdict":   "SIGNAL" if p_val < 0.05 else "noise",
    })


# ══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("FINAL SUMMARY — AES round count vs exploitability")
print(f"{'═'*65}\n")
print(f"  {'Rounds':>8}  {'Pairs':>8}  {'Max|corr|':>10}  {'p-value':>8}  {'Nonce-uniform':>14}  Verdict")
print("  " + "-" * 72)
for r in results_summary:
    if "corr" not in r:
        print(f"  {r['nrounds']:>8}  {r['N_pairs']:>8}  {'N/A':>10}  {'N/A':>8}  {'N/A':>14}  {r['verdict']}")
    else:
        m = " ◄" if r["verdict"] == "SIGNAL" else ""
        unif = "YES" if r["p_uniform"] > 0.05 else f"NO(p={r['p_uniform']:.4f})"
        print(f"  {r['nrounds']:>8}  {r['N_pairs']:>8,}  {r['corr']:>10.6f}  {r['pvalue']:>8.4f}"
              f"  {unif:>14}  {r['verdict']}{m}")

print()
print("Structural analysis (algebra):")
print("  N=1: no MixColumns → each output byte independent of others")
print("       winning nonce bytes are perfectly predictable per block")
print("  N=2: 1× MixColumns → column mixing, but each output byte")
print("       still depends on only 1 nonce byte")
print("  N=4: 2+ MixColumns rounds → avalanche begins across columns")
print("  N=10: full AES diffusion → cryptographically strong")
print()

# Compare to SHA3
print("Comparison vs our previous SHA3-based results:")
print("  xxHash64   (64-bit, non-crypto):      p=0.890  noise (but invertible!)")
print("  HeavyHash  (SHA3-wrapped 256-bit):    p=0.958  noise")
print("  verus_aes  (this test, 256-bit AES):  see table above")
print()

if any(r.get("verdict") == "SIGNAL" for r in results_summary):
    low = min(r["nrounds"] for r in results_summary if r.get("verdict") == "SIGNAL")
    print(f"CONCLUSION: AES with N≤{low} rounds shows exploitable structure!")
    print("  → Round count directly determines security: fewer rounds = more predictable")
else:
    print("CONCLUSION: No statistical signal at any round count tested.")
    print("  → SHA3 key derivation independently randomizes each AES key")
    print("  → Even 1-round AES: key derivation provides the main security")

with open("verus_analysis_results.json", "w") as f:
    json.dump(results_summary, f, indent=2)
print("\nResults → verus_analysis_results.json")
