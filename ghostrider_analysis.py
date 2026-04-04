"""
ghostrider_analysis.py — GhostRider algorithm selection analysis

KEY FINDINGS FROM SOURCE CODE (xmrig ghostrider.cpp):

1. Algorithm sequence is 100% deterministic from prev_block_hash (bytes 4-36)
2. select_indices reads nibbles left-to-right from prev_hash
3. For ALL PoW hashes (leading zeros), nibble[0] = 0 → blake512 ALWAYS first
4. 6 CryptoNight variants have different scratchpad sizes:
   cn/dark       = 512 KB  (medium)
   cn/dark-lite  = 256 KB  (fast)
   cn/fast       = 2048 KB (SLOW - 16x slower than turtle-lite)
   cn/lite       = 1024 KB (slow)
   cn/turtle     = 256 KB  (fast)
   cn/turtle-lite = 128 KB (FASTEST)

Structure per hash: [5 core + 1 CN] × 3 = 15 core hashes + 3 CN variants

HYPOTHESIS:
  Different blocks use different CN variants (deterministic from prev_hash).
  "Fast blocks" (small CN) allow significantly more hashes/second.
  "Slow blocks" (large CN) slow everyone down equally.

  Strategy: compute expected hash time from prev_hash → preferentially mine
  fast blocks, reduce effort on slow blocks → better average efficiency.

  Additional hypothesis: is the sequence SELECTION uniform, or does the
  leading-zero structure of real PoW hashes bias which algorithms appear first?

TESTS:
  A. Select_indices statistics — is selection uniform across positions?
     Special focus: position 0 (always blake512 for leading-zero hashes?)
  B. CN variant distribution — what fraction of blocks are "fast" vs "slow"?
  C. Performance ratio: fast_block_time / slow_block_time (relative speedup)
  D. Mining strategy: if we mine faster on fast blocks, what's the efficiency gain?
"""

import hashlib
import numpy as np
from scipy import stats

# ── Python implementation of select_indices ──────────────────────────────────

def select_indices(seed_bytes: bytes, N: int) -> list:
    """
    Exact port of xmrig's select_indices().
    seed_bytes: 32-byte prev_block_hash
    N: number of algorithms (15 for core, 6 for CN)
    Returns: list of N indices, each in [0, N)
    """
    selected = [False] * N
    indices  = []

    for i in range(64):
        # nibble extraction: lo nibble first, then hi nibble
        byte_val = seed_bytes[i // 2]
        nibble   = (byte_val >> ((i & 1) * 4)) & 0xF
        idx      = nibble % N
        if not selected[idx]:
            selected[idx] = True
            indices.append(idx)
            if len(indices) >= N:
                return indices

    # Fallback: append any unselected in order
    for i in range(N):
        if not selected[i]:
            indices.append(i)

    return indices

# ── Algorithm names ───────────────────────────────────────────────────────────

CORE_NAMES = [
    "blake512",    # 0
    "bmw512",      # 1
    "groestl512",  # 2
    "jh512",       # 3
    "keccak512",   # 4
    "skein512",    # 5
    "luffa512",    # 6
    "cubehash512", # 7
    "shavite512",  # 8
    "simd512",     # 9
    "echo512",     # 10
    "hamsi512",    # 11
    "fugue512",    # 12
    "shabal512",   # 13
    "whirlpool",   # 14
]

CN_NAMES = [
    "cn/dark       (512 KB)",   # 0
    "cn/dark-lite  (256 KB)",   # 1
    "cn/fast       (2048 KB)",  # 2 ← SLOWEST
    "cn/lite       (1024 KB)",  # 3
    "cn/turtle     (256 KB)",   # 4
    "cn/turtle-lite (128 KB)",  # 5 ← FASTEST
]

# Relative time cost per CN variant (scratchpad size proportional to time)
# Normalized so turtle-lite = 1.0
CN_SIZES_KB = [512, 256, 2048, 1024, 256, 128]
CN_REL_COST = [s / 128 for s in CN_SIZES_KB]  # [4.0, 2.0, 16.0, 8.0, 2.0, 1.0]

# ── Generate test chains ──────────────────────────────────────────────────────

def make_synthetic_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

# Real Raptoreum-style hashes: all-zero prefixed (simulating real PoW difficulty)
# Real Raptoreum target difficulty ~20-24 bits = 3 leading zero bytes
def make_rtm_style_chain(n):
    """Simulate hashes with leading zeros (like real Raptoreum blocks)."""
    chain = []
    h = bytes.fromhex("000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f")
    for _ in range(n):
        chain.append(h)
        # Simulate mining: hash something and add leading zeros
        raw = hashlib.sha256(h).digest()
        # Force 3 leading zero bytes (simulates ~24-bit PoW difficulty)
        forced = b'\x00\x00\x00' + raw[3:]
        chain.append(forced)
        h = hashlib.sha256(forced).digest()
        h = b'\x00\x00\x00' + h[3:]
    return chain[:n]

N_BLOCKS = 10_000
SYNTHETIC = make_synthetic_chain(N_BLOCKS)
RTM_STYLE = make_rtm_style_chain(N_BLOCKS)

# ════════════════════════════════════════════════════════════════════════════════
print("═"*68)
print("  GhostRider Algorithm Selection Analysis")
print("═"*68)
print(f"  {N_BLOCKS:,} synthetic blocks + {N_BLOCKS:,} RTM-style (leading-zero) blocks")
print()

# ── TEST A: Core algorithm position frequencies ───────────────────────────────
print("─"*68)
print("  TEST A: Core algorithm position frequencies")
print("  Q: Is position 0 always blake512 for leading-zero hashes?")
print("─"*68)

for chain_name, chain in [("Synthetic (random hashes)  ", SYNTHETIC),
                           ("RTM-style (leading zeros)  ", RTM_STYLE)]:
    pos_counts  = np.zeros((15, 15), dtype=int)  # pos_counts[position][algo] = count
    first_algo  = []

    for prev in chain:
        ci = select_indices(prev, 15)
        first_algo.append(ci[0])
        for pos, algo in enumerate(ci):
            pos_counts[pos][algo] += 1

    first_algo = np.array(first_algo)
    blake_first_pct = (first_algo == 0).mean() * 100

    print(f"  {chain_name}: blake512 first = {blake_first_pct:.1f}%  "
          f"(expected random: {100/15:.1f}%)")

    # Which algo appears most often at position 0?
    top3 = np.argsort(-pos_counts[0])[:3]
    print(f"    Position-0 top3: "
          + "  ".join(f"{CORE_NAMES[i]}={pos_counts[0][i]/N_BLOCKS*100:.1f}%"
                      for i in top3))

print()

# ── TEST B: CN variant distribution ──────────────────────────────────────────
print("─"*68)
print("  TEST B: CN variant selection distribution")
print("  Q: Are some CN variants selected more often? (due to nibble bias)")
print("─"*68)

for chain_name, chain in [("Synthetic", SYNTHETIC), ("RTM-style (leading zeros)", RTM_STYLE)]:
    cn_slot_counts = np.zeros((3, 6), dtype=int)  # slot × variant
    block_costs    = []

    for prev in chain:
        ci = select_indices(prev, 6)
        total_cost = 0
        for slot in range(3):
            variant = ci[slot]
            cn_slot_counts[slot][variant] += 1
            total_cost += CN_REL_COST[variant]
        block_costs.append(total_cost)

    block_costs = np.array(block_costs)
    expected_cost = sum(CN_REL_COST) / 2  # average of first 3 if uniform

    print(f"\n  {chain_name}:")
    print(f"    {'CN Variant':25s}  {'Slot 0':>8}  {'Slot 1':>8}  {'Slot 2':>8}  Cost")
    print("    " + "-"*54)
    for v in range(6):
        pcts = [cn_slot_counts[s][v]/N_BLOCKS*100 for s in range(3)]
        print(f"    {CN_NAMES[v]:25s}  {pcts[0]:>7.1f}%  {pcts[1]:>7.1f}%  {pcts[2]:>7.1f}%  "
              f"{CN_REL_COST[v]:.0f}×")

    print(f"\n    Block cost distribution (relative to turtle-lite=1.0 per slot):")
    print(f"    Min={block_costs.min():.1f}  Max={block_costs.max():.1f}  "
          f"Mean={block_costs.mean():.2f}  Std={block_costs.std():.2f}")

    # Fast blocks = cost ≤ 6.0 (all three slots are fast: turtle-lite, dark-lite, or turtle)
    fast_threshold = 6.0  # three slots × cost ≤ 2.0 each
    fast_blocks = (block_costs <= fast_threshold).mean() * 100
    slow_threshold = 24.0  # at least one cn/fast (cost 16)
    slow_blocks = (block_costs >= slow_threshold).mean() * 100

    print(f"    Fast blocks (total cost ≤ {fast_threshold:.0f}): {fast_blocks:.1f}%")
    print(f"    Slow blocks (total cost ≥ {slow_threshold:.0f}): {slow_blocks:.1f}%")

    # Performance ratio
    fast_avg  = block_costs[block_costs <= fast_threshold].mean() if fast_blocks > 0 else 0
    slow_avg  = block_costs[block_costs >= slow_threshold].mean() if slow_blocks > 0 else 0
    overall   = block_costs.mean()

    if fast_blocks > 0 and slow_blocks > 0:
        ratio = slow_avg / fast_avg
        print(f"    Fast avg cost={fast_avg:.1f}  Slow avg cost={slow_avg:.1f}  "
              f"Ratio={ratio:.2f}× (slow blocks take {ratio:.1f}× longer)")

print()

# ── TEST C: Mining strategy — selective mining efficiency ─────────────────────
print("─"*68)
print("  TEST C: Selective mining strategy efficiency")
print("  Strategy: skip blocks where CN cost > threshold")
print("  (You can compute block cost from prev_hash BEFORE mining starts)")
print("─"*68)
print()

chain = SYNTHETIC
block_costs = []
for prev in chain:
    ci = select_indices(prev, 6)
    cost = sum(CN_REL_COST[ci[s]] for s in range(3))
    block_costs.append(cost)
block_costs = np.array(block_costs)

baseline_hashes_per_unit = 1.0 / block_costs.mean()

print(f"  {'Strategy':35s}  {'Blocks mined':>12}  {'Hash rate':>10}  {'Efficiency':>12}")
print("  " + "-"*72)

# Baseline: mine everything
print(f"  {'Mine all blocks':35s}  {100.0:>11.1f}%  {'1.000×':>10}  {'1.000×':>12}")

for threshold in [3.0, 4.0, 5.0, 6.0, 7.0, 9.0, 12.0, 16.0, 24.0]:
    mine_mask  = block_costs <= threshold
    pct_mined  = mine_mask.mean() * 100
    if pct_mined == 0:
        continue

    # When mining a block with cost C, you get 1/C hashes per unit time
    # (relative to turtle-lite baseline where you get 1.0 hashes per unit time)
    mined_costs = block_costs[mine_mask]
    avg_cost    = mined_costs.mean()
    hash_rate   = 1.0 / avg_cost           # hashes per unit time when mining
    # But you're idle during skipped blocks
    time_mining = pct_mined / 100
    time_idle   = 1 - time_mining
    effective_hr = hash_rate * time_mining  # overall effective hash rate
    efficiency  = effective_hr / baseline_hashes_per_unit

    print(f"  {'Skip blocks with cost > ' + str(threshold):35s}  "
          f"{pct_mined:>11.1f}%  {hash_rate:>9.3f}×  {efficiency:>11.4f}×"
          f"  {'◄ BETTER' if efficiency > 1.02 else ''}")

print()

# ── TEST D: Nibble bias from leading zeros ────────────────────────────────────
print("─"*68)
print("  TEST D: Nibble value frequencies in real vs synthetic hashes")
print("  Q: Do leading zeros bias the nibble distribution, skewing selection?")
print("─"*68)
print()

for chain_name, chain in [("Synthetic", SYNTHETIC), ("RTM-style (leading zeros)", RTM_STYLE)]:
    nibble_counts = np.zeros(16, dtype=int)
    for prev in chain:
        for i in range(64):
            byte_val = prev[i // 2]
            nibble   = (byte_val >> ((i & 1) * 4)) & 0xF
            nibble_counts[nibble] += 1

    total = nibble_counts.sum()
    chi2  = ((nibble_counts - total/16)**2 / (total/16)).sum()
    p     = 1 - stats.chi2.cdf(chi2, df=15)
    print(f"  {chain_name}:")
    print(f"    Nibble 0 frequency: {nibble_counts[0]/total*100:.2f}%  "
          f"(expected: {100/16:.2f}%)")
    print(f"    χ²={chi2:.1f}  p={p:.6f}  "
          f"→ {'BIASED ◄' if p < 0.01 else 'uniform'}")
    if p < 0.01:
        top_nibbles = np.argsort(-nibble_counts)[:4]
        print(f"    Most common nibbles: "
              + "  ".join(f"{n:X}={nibble_counts[n]/total*100:.1f}%"
                          for n in top_nibbles))
    print()

# ── SUMMARY ──────────────────────────────────────────────────────────────────
print("═"*68)
print("  SUMMARY")
print("═"*68)
print()
print("  GhostRider's algorithm sequence is FULLY DETERMINISTIC from prev_hash.")
print("  This creates two exploitable properties:")
print()
print("  1. BLAKE512 BIAS: For leading-zero hashes (all PoW), nibble[0]=0")
print("     → algorithm 0 (blake512) is always selected first in the sequence.")
print("     → The first algorithm is predictable, but since all 15 run anyway,")
print("       this doesn't help with nonce prediction.")
print()
print("  2. CN VARIANT COST ASYMMETRY (the real finding):")
print("     → Some blocks select cheap CN variants (128-256 KB scratchpad)")
print("     → Some blocks select expensive CN variants (2048 KB scratchpad)")
print("     → Cost ratio: up to 16× between fastest and slowest block")
print("     → You can compute the cost BEFORE mining starts (from prev_hash)")
print("     → Selective mining strategy: skip expensive blocks")
print("     → See TEST C for efficiency calculations")
print()
print("  Whether this is exploitable depends on:")
print("  - Fraction of 'fast' vs 'slow' blocks")
print("  - Whether you can afford to skip blocks (solo vs pool mining)")
print("  - Network: if everyone skips slow blocks, difficulty adjusts,")
print("    making slow-block skipping less valuable over time")
