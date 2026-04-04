"""
analyze_nonces.py

For each prev_hash in blocks.json, scans SCAN_RANGE nonces and collects
every winner where xxhash64(prev_hash || nonce) < TARGET.

Then runs statistical tests to answer:
  Q: Is there any correlation between prev_hash bits and winning nonce bits?
  Q: Are winning nonces uniformly distributed or clustered?

If patterns exist → the "index" idea has merit.
If not → avalanche effect holds and the search cannot be shortened.
"""

import xxhash
import struct
import json
import time
import math
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
DIFFICULTY_BITS = 16
TARGET          = (2**64) >> DIFFICULTY_BITS
SCAN_RANGE      = 500_000   # nonces to scan per prev_hash (expect ~7-8 winners each)
# ──────────────────────────────────────────────────────────────────────────────


def scan_winners(prev_hex: str, scan_range: int) -> list[int]:
    prev_bytes = bytes.fromhex(prev_hex)
    winners = []
    for n in range(scan_range):
        if xxhash.xxh64(prev_bytes + struct.pack(">I", n)).intdigest() < TARGET:
            winners.append(n)
    return winners


def chi_square_uniformity(values: list[int], max_val: int, bins: int = 16) -> tuple[float, float]:
    """Chi-square test: are values uniform across [0, max_val]?"""
    counts = [0] * bins
    for v in values:
        bucket = min(int(v / max_val * bins), bins - 1)
        counts[bucket] += 1
    expected = len(values) / bins
    if expected == 0:
        return 0.0, 1.0
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    # p-value approximation via chi2 CDF (degrees of freedom = bins-1)
    # Simple: compare against critical value at p=0.05 for df=15 → 24.996
    p_approx = "UNIFORM (p>0.05)" if chi2 < 25.0 else "NON-UNIFORM (p<0.05)"
    return chi2, p_approx


def bit_correlation(prev_hashes: list[str], all_winners: list[list[int]]) -> dict:
    """
    For each bit position in prev_hash (0-63) and each nonce bit (0-18),
    compute point-biserial correlation: does prev_hash bit predict nonce bit?
    Returns the max absolute correlation found and its location.
    """
    # Flatten: one row per (prev_hash, winning_nonce) pair
    prev_ints = [int(h, 16) for h in prev_hashes]
    rows = []
    for ph_int, winners in zip(prev_ints, all_winners):
        for w in winners:
            rows.append((ph_int, w))

    if not rows:
        return {"max_corr": 0, "at_bits": (0, 0)}

    max_corr = 0.0
    max_loc  = (0, 0)
    n = len(rows)

    # Check each prev_hash bit vs each nonce bit
    for pb in range(64):          # prev_hash bit
        ph_bits = [(r[0] >> pb) & 1 for r in rows]
        ph_mean = sum(ph_bits) / n
        if ph_mean in (0.0, 1.0):    # constant bit — skip
            continue
        for nb in range(19):      # nonce bit (0..500K fits in 19 bits)
            n_bits = [(r[1] >> nb) & 1 for r in rows]
            n_mean = sum(n_bits) / n
            if n_mean in (0.0, 1.0):
                continue
            # Pearson correlation on binary sequences
            cov = sum((ph_bits[i] - ph_mean) * (n_bits[i] - n_mean) for i in range(n)) / n
            std_ph = math.sqrt(ph_mean * (1 - ph_mean))
            std_n  = math.sqrt(n_mean  * (1 - n_mean))
            if std_ph == 0 or std_n == 0:
                continue
            corr = abs(cov / (std_ph * std_n))
            if corr > max_corr:
                max_corr = corr
                max_loc  = (pb, nb)

    return {"max_corr": max_corr, "at_bits": max_loc, "n_pairs": n}


def gap_analysis(all_winners: list[list[int]]) -> dict:
    """Are gaps between consecutive winners uniform (exponential) or structured?"""
    gaps = []
    for winners in all_winners:
        s = sorted(winners)
        for i in range(1, len(s)):
            gaps.append(s[i] - s[i-1])
    if not gaps:
        return {}
    mean_gap  = sum(gaps) / len(gaps)
    expected  = SCAN_RANGE / (SCAN_RANGE / (2**16))   # ≈ 65536
    variance  = sum((g - mean_gap)**2 for g in gaps) / len(gaps)
    cv        = math.sqrt(variance) / mean_gap   # coefficient of variation
    # For exponential distribution (random), CV ≈ 1.0
    return {
        "count":       len(gaps),
        "mean_gap":    round(mean_gap, 1),
        "expected_gap": round(expected, 1),
        "cv":          round(cv, 4),
        "cv_verdict":  "RANDOM (≈exponential)" if 0.7 < cv < 1.3 else "STRUCTURED (CV far from 1.0)"
    }


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with open("blocks.json") as f:
        chain = json.load(f)

    # Use blocks 1..220 (skip genesis which has no prev)
    blocks = [b for b in chain if b["prev"] is not None]
    prev_hashes = [b["prev"] for b in blocks]

    print(f"Scanning {len(prev_hashes)} prev_hashes × {SCAN_RANGE:,} nonces each …\n")

    all_winners   = []
    total_winners = 0
    t0 = time.perf_counter()

    for i, ph in enumerate(prev_hashes):
        w = scan_winners(ph, SCAN_RANGE)
        all_winners.append(w)
        total_winners += len(w)
        if (i + 1) % 20 == 0:
            elapsed = time.perf_counter() - t0
            eta = elapsed / (i + 1) * (len(prev_hashes) - i - 1)
            print(f"  {i+1:>3}/{len(prev_hashes)}  winners so far={total_winners:,}  "
                  f"eta={eta:.0f}s")

    elapsed = time.perf_counter() - t0
    print(f"\nScan complete in {elapsed:.1f}s")
    print(f"Total (prev_hash, nonce) pairs collected: {total_winners:,}")
    avg_per_block = total_winners / len(prev_hashes)
    print(f"Average winners per block: {avg_per_block:.1f}  "
          f"(expected ≈ {SCAN_RANGE / 2**16:.1f})\n")

    # ── Test 1: Uniformity ─────────────────────────────────────────────────────
    flat_winners = [w for ws in all_winners for w in ws]
    chi2, verdict = chi_square_uniformity(flat_winners, SCAN_RANGE, bins=16)
    print("═" * 60)
    print("TEST 1 — Nonce Uniformity (chi-square)")
    print(f"  chi² = {chi2:.3f}   →  {verdict}")
    print("  Meaning: are winning nonces spread uniformly across nonce space?")

    # ── Test 2: Gap analysis ───────────────────────────────────────────────────
    print("\nTEST 2 — Inter-winner Gap Analysis")
    gaps = gap_analysis(all_winners)
    for k, v in gaps.items():
        print(f"  {k}: {v}")
    print("  Meaning: random hashes produce exponential gaps (CV≈1); "
          "clusters would show CV<1.")

    # ── Test 3: Bit-level correlation ──────────────────────────────────────────
    print("\nTEST 3 — Bit Correlation (prev_hash bits vs nonce bits)")
    print("  Running …")
    corr = bit_correlation(prev_hashes, all_winners)
    print(f"  Max |correlation| found: {corr['max_corr']:.6f}")
    print(f"  At: prev_hash bit {corr['at_bits'][0]}  ↔  nonce bit {corr['at_bits'][1]}")
    print(f"  Over {corr['n_pairs']:,} pairs")
    threshold = 0.05
    if corr["max_corr"] < threshold:
        verdict2 = f"NO MEANINGFUL CORRELATION (all < {threshold})"
    else:
        verdict2 = f"POTENTIAL CORRELATION DETECTED (>{threshold}) — investigate!"
    print(f"  Verdict: {verdict2}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("SUMMARY")
    print(f"  Nonce distribution : {chi_square_uniformity(flat_winners, SCAN_RANGE)[1]}")
    print(f"  Gap structure      : {gaps.get('cv_verdict', 'N/A')}")
    print(f"  Bit correlation    : {verdict2}")

    # Save raw data for further analysis
    results = {
        "config": {"difficulty_bits": DIFFICULTY_BITS, "scan_range": SCAN_RANGE},
        "blocks_scanned": len(prev_hashes),
        "total_pairs": total_winners,
        "winners_per_block": [
            {"prev": ph, "winners": ws}
            for ph, ws in zip(prev_hashes, all_winners)
        ]
    }
    with open("winners.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nRaw winners saved to winners.json")
