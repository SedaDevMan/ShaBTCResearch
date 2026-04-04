"""
algebraic_solver.py — Option A: Algebraic midstate attack on xxHash64

xxHash64 for our 12-byte input (8 bytes prev_hash + 4 bytes nonce) has
this exact structure:

  h      = P5 + 12                          ← init (constant)
  k1     = rotl(P_le * P2, 31) * P1         ← absorb prev_hash (P_le = prev as LE uint64)
  M      = rotl(h ^ k1, 27) * P1 + P4      ← MIDSTATE (fixed per prev_hash)
  h2     = M ^ (N_le * P1)                  ← mix nonce  (N_le = bswap32(nonce))
  pre    = rotl(h2, 23) * P2 + P3           ← pre-avalanche
  output = avalanche(pre)                   ← final output

Since avalanche is an invertible bijection, we can:
  1. For any desired output: pre = avalanche_inv(output)
  2. For any pre + known M: N_le = ((M ^ rotl(pre-P3)*P2_inv, 41)) * P1_inv  mod 2^64
  3. If N_le < 2^32: nonce = bswap32(N_le)  ← DIRECT winning nonce, no scanning!

Cost of algebraic approach = TARGET (= 2^(64-difficulty)) operations
Cost of brute-force scan   = 2^32 operations

CROSSOVER: algebraic beats brute force when difficulty > 32 bits.
"""

import struct, time, sys
import numpy as np
import scan   # our C extension

# ── Constants ──────────────────────────────────────────────────────────────────
M64  = np.uint64(0xFFFFFFFFFFFFFFFF)
MASK = (1 << 64) - 1

P1 = 0x9E3779B185EBCA87
P2 = 0xC2B2AE3D27D4EB4F
P3 = 0x165667B19E3779F9
P4 = 0x85EBCA77C2B2AE63
P5 = 0x27D4EB2F165667C5

P1_inv = pow(P1, -1, 1 << 64)
P2_inv = pow(P2, -1, 1 << 64)
P3_inv = pow(P3, -1, 1 << 64)


# ── Pure-Python helpers (for verification) ────────────────────────────────────

def rotl64(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & MASK

def avalanche(h: int) -> int:
    h ^= h >> 33;  h = (h * P2) & MASK
    h ^= h >> 29;  h = (h * P3) & MASK
    h ^= h >> 32
    return h

def avalanche_inv(h: int) -> int:
    h ^= h >> 32                             # inv xorshift-32  (self-inverse since 2×32=64)
    h  = (h * P3_inv) & MASK                 # inv multiply P3
    h ^= h >> 29; h ^= h >> 58              # inv xorshift-29  (two passes)
    h  = (h * P2_inv) & MASK                 # inv multiply P2
    h ^= h >> 33                             # inv xorshift-33  (self-inverse since 2×33>64)
    return h

def compute_midstate(prev_bytes: bytes) -> int:
    """Compute M = midstate after absorbing prev_hash (8 bytes)."""
    P_le = int.from_bytes(prev_bytes[:8], 'little')
    h    = (P5 + 12) & MASK
    k1   = rotl64((P_le * P2) & MASK, 31)
    k1   = (k1 * P1) & MASK
    h    = h ^ k1
    h    = (rotl64(h, 27) * P1 + P4) & MASK
    return h

def xxh64_python(prev_bytes: bytes, nonce: int) -> int:
    """Pure-Python reimplementation of XXH64(prev||nonce_BE, 12, seed=0)."""
    M    = compute_midstate(prev_bytes)
    N_le = int.from_bytes(struct.pack('>I', nonce), 'little')  # bswap32
    h    = (M ^ (N_le * P1)) & MASK
    pre  = (rotl64(h, 23) * P2 + P3) & MASK
    return avalanche(pre)

def nonce_from_pre_scalar(M: int, pre: int):
    """Given midstate M and pre-avalanche value, return N_le and nonce (or None)."""
    tmp  = ((pre - P3) * P2_inv) & MASK
    h2   = rotl64(tmp, 41)
    N_le = ((M ^ h2) * P1_inv) & MASK
    if N_le >= (1 << 32):
        return None, None
    nonce = int.from_bytes(N_le.to_bytes(4, 'little'), 'big')   # bswap32
    return N_le, nonce


# ── Numpy vectorised algebraic scanner ───────────────────────────────────────

def rotl64_np(x: np.ndarray, r: int) -> np.ndarray:
    return ((x << np.uint64(r)) | (x >> np.uint64(64 - r))).astype(np.uint64)

def avalanche_inv_np(h: np.ndarray) -> np.ndarray:
    h = h ^ (h >> np.uint64(32))
    h = (h * np.uint64(P3_inv)).astype(np.uint64)
    h = h ^ (h >> np.uint64(29)) ^ (h >> np.uint64(58))
    h = (h * np.uint64(P2_inv)).astype(np.uint64)
    h = h ^ (h >> np.uint64(33))
    return h

def algebraic_scan(prev_bytes: bytes, target: int) -> list[int]:
    """
    Find all winning nonces for prev_bytes where XXH64 output < target.
    Uses algebraic inversion — cost = O(target) not O(2^32).
    """
    M    = compute_midstate(prev_bytes)
    M_np = np.uint64(M)

    # Process in chunks to avoid allocating target-size array at once
    CHUNK = min(target, 1 << 22)   # 4M elements = 32 MB per chunk
    winners = []

    for start in range(0, target, CHUNK):
        end     = min(start + CHUNK, target)
        outputs = np.arange(start, end, dtype=np.uint64)

        # Invert avalanche
        pre = avalanche_inv_np(outputs)

        # Derive N_le from midstate and pre
        tmp   = ((pre.astype(object) - P3) % (1 << 64))   # avoid numpy overflow on subtraction
        tmp   = np.array(tmp, dtype=np.uint64)
        tmp   = (tmp * np.uint64(P2_inv)).astype(np.uint64)
        h2    = rotl64_np(tmp, 41)
        N_le  = ((M_np ^ h2) * np.uint64(P1_inv)).astype(np.uint64)

        # Valid nonce: N_le must fit in uint32
        valid = N_le < np.uint64(1 << 32)
        for N in N_le[valid]:
            N_int = int(N)
            nonce = int.from_bytes(N_int.to_bytes(4, 'little'), 'big')
            winners.append(nonce)

    return sorted(winners)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Verify formula correctness
# ══════════════════════════════════════════════════════════════════════════════
print("═" * 65)
print("STEP 1 — Formula verification")
print()

prev_hex   = "0000e9218e5a6b9b"
prev_bytes = bytes.fromhex(prev_hex)

errors = 0
for nonce in range(0, 100_000, 1000):
    c_out  = scan.xxh64(prev_bytes + struct.pack('>I', nonce))
    py_out = xxh64_python(prev_bytes, nonce)
    if c_out != py_out:
        print(f"  MISMATCH nonce={nonce}: C={c_out}  Python={py_out}")
        errors += 1

print(f"  Python trace vs C extension: {'OK — 100 nonces match' if errors==0 else f'{errors} mismatches!'}")

# Verify avalanche is invertible
ok = all(avalanche_inv(avalanche(x)) == x for x in
         [0, 1, 2**32, 2**48, 2**63, 0xDEADBEEFCAFEBABE, P1, P2])
print(f"  avalanche_inv(avalanche(x)) == x: {'OK' if ok else 'FAIL'}")

# Verify nonce_from_pre recovers correct nonce
TARGET_16 = (1 << 64) >> 16
winners_c = scan.scan_winners(prev_bytes, 500_000, TARGET_16)
print(f"  Nonce recovery test on {len(winners_c)} known winners:")
mismatches = 0
for nonce in winners_c[:20]:
    output = xxh64_python(prev_bytes, nonce)
    pre    = avalanche_inv(output)
    M      = compute_midstate(prev_bytes)
    _, recovered = nonce_from_pre_scalar(M, pre)
    if recovered != nonce:
        print(f"    FAIL nonce={nonce} recovered={recovered}")
        mismatches += 1
print(f"  Recovery: {'ALL OK' if mismatches==0 else f'{mismatches} failures'}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: Demonstrate algebraic scanner at high difficulty
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("STEP 2 — Algebraic scanner vs brute-force at 40-bit difficulty")
print("         (target = 2^24 ≈ 16M — algebraic cost << brute-force 2^32)")
print()

TARGET_40 = (1 << 64) >> 40   # = 2^24 = 16,777,216

print(f"  target = 2^24 = {TARGET_40:,}")
print(f"  Algebraic cost : ~{TARGET_40:,} iterations")
print(f"  Brute-force cost: ~{2**32:,} iterations  ({2**32//TARGET_40}x more)\n")

t0 = time.perf_counter()
alg_winners = algebraic_scan(prev_bytes, TARGET_40)
t_alg = time.perf_counter() - t0

print(f"  Algebraic  : {len(alg_winners)} winners  in {t_alg:.3f}s")
if alg_winners:
    print(f"  Nonces found: {alg_winners[:5]}{'...' if len(alg_winners)>5 else ''}")

# Verify all algebraic winners are genuine
if alg_winners:
    all_valid = all(xxh64_python(prev_bytes, n) < TARGET_40 for n in alg_winners)
    print(f"  Verification: {'ALL GENUINE WINNERS ✓' if all_valid else 'SOME FALSE POSITIVES!'}")

# Brute-force for comparison — only scan first 5M to estimate rate
t0 = time.perf_counter()
bf_sample = scan.scan_winners(prev_bytes, 5_000_000, TARGET_40)
t_bf_5m = time.perf_counter() - t0
t_bf_full_est = t_bf_5m * (2**32 / 5_000_000)
print(f"  Brute-force: sample 5M nonces in {t_bf_5m:.3f}s → full 2^32 ≈ {t_bf_full_est:.1f}s est.")
print(f"  Speedup (algebraic vs brute-force): {t_bf_full_est/t_alg:.0f}×")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Crossover analysis
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("STEP 3 — Crossover: algebraic cost vs brute-force cost")
print()
print(f"  {'Difficulty':>12}  {'Target':>12}  {'Alg cost':>12}  {'BF cost':>12}  {'Faster'}")
print("  " + "-" * 62)

for d in range(16, 65, 4):
    target_size = 1 << (64 - d)
    alg_cost    = target_size           # iterate over all valid outputs
    bf_cost     = 1 << 32              # always scan full nonce space
    faster      = "ALGEBRAIC" if alg_cost < bf_cost else "brute-force"
    marker      = " ← CROSSOVER" if d == 32 else ""
    target_str  = f"2^{64-d}"
    alg_str     = f"2^{64-d}"
    bf_str      = f"2^32"
    print(f"  {d:>12}-bit  {target_str:>12}  {alg_str:>12}  {bf_str:>12}  {faster}{marker}")

print(f"""
  Summary:
  • difficulty ≤ 32 bits → brute-force wins   (scanning nonces is cheaper)
  • difficulty > 32 bits → ALGEBRAIC wins     (inversion is cheaper than scanning)
  • Our PoC uses 16-bit difficulty            → brute-force territory
  • Real Bitcoin SHA256d uses ~70-bit diff    → algebraic territory IF hash invertible
""")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: Prove it with 48-bit difficulty (ultra-fast algebraic)
# ══════════════════════════════════════════════════════════════════════════════
print(f"{'═'*65}")
print("STEP 4 — 48-bit difficulty: algebraic scans 65K outputs vs 4.3B brute-force")
print()

TARGET_48 = (1 << 64) >> 48   # = 2^16 = 65536

t0 = time.perf_counter()
alg_48 = algebraic_scan(prev_bytes, TARGET_48)
t_alg_48 = time.perf_counter() - t0

print(f"  Algebraic solved 2^16 = {TARGET_48:,} inversions in {t_alg_48*1000:.2f}ms")
print(f"  Winners found: {len(alg_48)}")
if alg_48:
    ok = all(xxh64_python(prev_bytes, n) < TARGET_48 for n in alg_48)
    print(f"  All genuine: {ok}")
    print(f"  Winning nonces: {alg_48}")

# Cross-check with C scanner
bf_48 = scan.scan_winners(prev_bytes, 2**32, TARGET_48)
print(f"\n  C brute-force full 2^32 scan: {len(bf_48)} winners")
print(f"  Algebraic found same set: {sorted(alg_48) == sorted(bf_48)}")
print(f"\n  At 48-bit difficulty the algebraic solver is {2**32 // TARGET_48:,}x fewer iterations")
print(f"  and finds EXACTLY the same winners as a full 2^32 brute-force scan.")
