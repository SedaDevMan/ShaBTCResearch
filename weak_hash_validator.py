"""
weak_hash_validator.py

Validates that our correlation/skip detector WORKS by testing it on
hash functions with known, provable leakage — then comparing to xxHash64.

Hash ladder (weakest → strongest):
  1. xor_shift   — nonce bits directly XORed into prev_hash positions
                   MUST show strong correlation (our test must find this)
  2. single_mix  — one multiply+XOR round, partial diffusion, weak avalanche
  3. xxhash64    — full non-crypto hash, no known shortcuts

For each hash we:
  - Generate 220 random prev_hashes
  - Scan 500K nonces per prev_hash → collect all winners
  - Run permutation test (200 shuffles, faster than full 1000)
  - Report: p-value, max correlation, skip potential

If xor_shift → not detected: our test is blind → go back to drawing board
If xor_shift → detected + xxhash64 → not detected: xxHash is genuinely strong
"""

import struct
import math
import random
import time
import xxhash

DIFFICULTY_BITS = 16
TARGET          = (2**64) >> DIFFICULTY_BITS
SCAN_RANGE      = 500_000
N_PREV_HASHES   = 220
N_PERMS         = 200     # faster for comparative study

random.seed(42)

# ── Hash functions ─────────────────────────────────────────────────────────────

def xor_shift_hash(prev_bytes: bytes, nonce: int) -> int:
    """
    ULTRA-WEAK — provably leaky.

    Winning condition: nonce & 0xFFFF  ==  prev_int >> 48
    (the bottom 16 bits of nonce must equal the top 16 bits of prev_hash)

    output top-16 bits = (prev_top16 XOR nonce_low16) << 48
    For output < TARGET those top-16 must be 0  →  nonce_low16 = prev_top16

    Expected winners per 500K scan: 500000/65536 ≈ 7.6  ✓
    Correlation: prev_bit[48+i] == nonce_bit[i]  for i=0..15  (provable)
    """
    prev_int  = int.from_bytes(prev_bytes[:8], 'big')
    prev_top  = (prev_int >> 48) & 0xFFFF
    top       = ((prev_top ^ (nonce & 0xFFFF)) << 48)
    bottom    = (prev_int ^ (nonce >> 16)) & 0xFFFFFFFFFFFF   # 48 bits, any value ok
    return top | bottom


def single_mix_hash(prev_bytes: bytes, nonce: int) -> int:
    """
    WEAK — one round of multiply+XOR (like a stripped-down MurmurHash).
    Some diffusion but no avalanche. Partial correlation expected.
    """
    prev_int = int.from_bytes(prev_bytes[:8], 'big')
    h = (prev_int ^ nonce) & 0xFFFFFFFFFFFFFFFF
    h = (h ^ (h >> 30)) & 0xFFFFFFFFFFFFFFFF
    h = (h * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    h = (h ^ (h >> 27)) & 0xFFFFFFFFFFFFFFFF
    return h


def xxhash64_fn(prev_bytes: bytes, nonce: int) -> int:
    """STRONG — full xxHash64, our baseline."""
    return xxhash.xxh64(prev_bytes + struct.pack(">I", nonce)).intdigest()


HASH_FUNCTIONS = [
    ("xor_shift  (ultra-weak)", xor_shift_hash),
    ("single_mix (weak)      ", single_mix_hash),
    ("xxhash64   (strong)    ", xxhash64_fn),
]

# ── Core routines ──────────────────────────────────────────────────────────────

def scan_winners(prev_bytes: bytes, hash_fn, scan_range: int) -> list[int]:
    return [n for n in range(scan_range)
            if hash_fn(prev_bytes, n) < TARGET]


def max_bit_corr(rows: list[tuple]) -> tuple[float, int, int]:
    n = len(rows)
    if n == 0:
        return 0.0, 0, 0
    best = 0.0
    best_pb = best_nb = 0
    for pb in range(64):
        ph_bits = [(r[0] >> pb) & 1 for r in rows]
        ph_mean = sum(ph_bits) / n
        if ph_mean in (0.0, 1.0):
            continue
        std_ph = math.sqrt(ph_mean * (1 - ph_mean))
        for nb in range(19):
            n_bits = [(r[1] >> nb) & 1 for r in rows]
            n_mean = sum(n_bits) / n
            if n_mean in (0.0, 1.0):
                continue
            cov = sum((ph_bits[i]-ph_mean)*(n_bits[i]-n_mean) for i in range(n)) / n
            std_n = math.sqrt(n_mean * (1 - n_mean))
            if std_n == 0:
                continue
            c = abs(cov / (std_ph * std_n))
            if c > best:
                best, best_pb, best_nb = c, pb, nb
    return best, best_pb, best_nb


def permutation_pvalue(rows: list[tuple], obs_corr: float, n_perms: int) -> float:
    ph_list    = [r[0] for r in rows]
    nonce_list = [r[1] for r in rows]
    shuffled   = ph_list[:]
    exceed = 0
    for _ in range(n_perms):
        random.shuffle(shuffled)
        score, _, _ = max_bit_corr(list(zip(shuffled, nonce_list)))
        if score >= obs_corr:
            exceed += 1
    return exceed / n_perms


def skip_potential(rows: list[tuple], pb: int, nb: int) -> dict:
    """
    Best 1-bit filter using (prev_hash_bit[pb], nonce_bit[nb]).
    Returns skip_ratio and winner_keep_rate.
    """
    best_keep = 0.0
    for ph_val in (0, 1):
        for n_val in (0, 1):
            subset = [r for r in rows if ((r[0]>>pb)&1) == ph_val]
            if not subset:
                continue
            kept = sum(1 for r in subset if ((r[1]>>nb)&1) == n_val)
            keep = kept / len(subset)
            if keep > best_keep:
                best_keep = keep
    return {"skip_ratio": 0.50, "winner_keep": best_keep, "false_neg": 1 - best_keep}


# ── Main ───────────────────────────────────────────────────────────────────────

# Generate fixed random prev_hashes (same for all hash functions = fair comparison)
prev_hashes_bytes = [random.randbytes(8) for _ in range(N_PREV_HASHES)]

print(f"Weak Hash Validator")
print(f"{'='*65}")
print(f"Prev hashes: {N_PREV_HASHES}  |  Nonce scan: {SCAN_RANGE:,}  |  "
      f"Difficulty: {DIFFICULTY_BITS}-bit  |  Perms: {N_PERMS}")
print(f"Expected winners/block: ~{SCAN_RANGE/2**DIFFICULTY_BITS:.1f}\n")

results = []

for name, hash_fn in HASH_FUNCTIONS:
    print(f"── {name} ──────────────────────────────────────────")

    # Collect winners
    t0 = time.perf_counter()
    all_winners = []
    rows = []
    for pb in prev_hashes_bytes:
        ws = scan_winners(pb, hash_fn, SCAN_RANGE)
        all_winners.append(ws)
        ph_int = int.from_bytes(pb, 'big')
        for w in ws:
            rows.append((ph_int, w))
    scan_time = time.perf_counter() - t0

    total_pairs = len(rows)
    print(f"  Pairs collected : {total_pairs:,}  ({scan_time:.1f}s)")

    if total_pairs == 0:
        print("  No winners found — check hash function\n")
        continue

    # Observed correlation
    obs_corr, obs_pb, obs_nb = max_bit_corr(rows)
    print(f"  Max correlation : {obs_corr:.6f}  "
          f"(prev_bit {obs_pb} ↔ nonce_bit {obs_nb})")

    # Permutation test
    t0 = time.perf_counter()
    pval = permutation_pvalue(rows, obs_corr, N_PERMS)
    perm_time = time.perf_counter() - t0

    if pval < 0.01:
        sig = "SIGNIFICANT p<0.01  ✓ SIGNAL DETECTED"
    elif pval < 0.05:
        sig = "MARGINAL    p<0.05  ~ WEAK SIGNAL"
    else:
        sig = "NOT SIGNIFICANT     ✗ NOISE"

    print(f"  p-value         : {pval:.3f}  →  {sig}  ({perm_time:.0f}s)")

    # Skip potential
    sp = skip_potential(rows, obs_pb, obs_nb)
    print(f"  Skip potential  : skip {sp['skip_ratio']:.0%} nonces, "
          f"keep {sp['winner_keep']:.1%} winners, "
          f"miss {sp['false_neg']:.1%}")

    # For xor_shift: show the theoretical prediction
    if "xor_shift" in name:
        print(f"  Theory check    : prev_bit[{obs_pb}] should predict nonce_bit[{obs_nb}]")
        expected_nb = obs_pb - 16
        if obs_nb == expected_nb:
            print(f"                    ✓ Matches theory (pb-16 = {expected_nb})")
        else:
            print(f"                    ? Theory predicts nonce_bit {expected_nb}, got {obs_nb}")

    results.append({
        "name": name.strip(),
        "pairs": total_pairs,
        "obs_corr": obs_corr,
        "pval": pval,
        "sig": sig,
        "winner_keep": sp["winner_keep"],
        "false_neg": sp["false_neg"],
    })
    print()

# ── Summary table ──────────────────────────────────────────────────────────────
print("═" * 65)
print("SUMMARY — Can our test detect known patterns?")
print(f"  {'Hash function':<28} {'corr':>8}  {'p-val':>6}  "
      f"{'keep%':>6}  {'miss%':>6}")
print("  " + "-" * 60)
for r in results:
    print(f"  {r['name']:<28} {r['obs_corr']:>8.6f}  {r['pval']:>6.3f}  "
          f"{r['winner_keep']:>6.1%}  {r['false_neg']:>6.1%}")

print()
print("VERDICT:")
weak_found   = any(("xor_shift" in r["name"] or "single_mix" in r["name"]) and r["pval"] < 0.05 for r in results)
xxh_found    = any("xxhash" in r["name"] and r["pval"] < 0.05 for r in results)

if weak_found and not xxh_found:
    print("  ✓ Methodology WORKS — detects weak hashes, finds nothing on xxHash64.")
    print("  → xxHash64 has no exploitable single-bit pattern at this data scale.")
    print("  → Next step: multi-hash window features (K previous blocks).")
elif not weak_found:
    print("  ✗ Methodology BLIND — failed to detect any provably leaky hash.")
    print("  → Test design needs revision before scaling up.")
elif weak_found and xxh_found:
    print("  ! xxHash64 signal detected — investigate further with more data.")
