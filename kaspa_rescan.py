"""
kaspa_rescan.py — For each block in kaspa_blocks.json, scan 500K nonces
and collect all winners. Produces kaspa_winners.json.

At 14-bit difficulty + 500K scan: ~30 winners/block × 100 blocks = ~3,000 pairs.
"""

import json, time, struct
import heavyhash

SCAN_RANGE = 500_000
TARGET_BYTES = b'\x00\x04' + b'\x00' * 30  # 14-bit difficulty

def matrix_to_bytes(mat):
    return bytes(mat[r][c] for r in range(64) for c in range(64))

with open("kaspa_blocks.json") as f:
    data = json.load(f)

blocks = data["blocks"]
print(f"Rescanning {len(blocks)} blocks × {SCAN_RANGE:,} nonces each")
print(f"Target: {TARGET_BYTES.hex()[:16]}...  (14-bit difficulty)")
print(f"Expected: ~{SCAN_RANGE // 2**14:.0f} winners/block\n")

results     = []
total_pairs = 0
t0          = time.perf_counter()

for i, block in enumerate(blocks):
    prev_bytes = bytes.fromhex(block["prev"])
    mat_list   = heavyhash.generate_matrix(prev_bytes)
    mat_bytes  = matrix_to_bytes(mat_list)

    winners = heavyhash.scan_winners(mat_bytes, prev_bytes, SCAN_RANGE, TARGET_BYTES)
    total_pairs += len(winners)
    results.append({
        "prev":    block["prev"],
        "block":   block["block"],
        "winners": winners,
    })

    if (i + 1) % 10 == 0:
        elapsed = time.perf_counter() - t0
        eta     = elapsed / (i + 1) * (len(blocks) - i - 1)
        print(f"  {i+1}/{len(blocks)}  pairs={total_pairs:,}  eta={eta:.1f}s")

elapsed = time.perf_counter() - t0
avg = total_pairs / len(blocks)
print(f"\nDone in {elapsed:.1f}s  |  {total_pairs:,} pairs  |  avg {avg:.1f} winners/block")

out = {
    "config": {
        "difficulty_bits": 14,
        "scan_range":      SCAN_RANGE,
        "target":          TARGET_BYTES.hex(),
    },
    "blocks_scanned":  len(blocks),
    "total_pairs":     total_pairs,
    "winners_per_block": results,
}
with open("kaspa_winners.json", "w") as f:
    json.dump(out, f)
print("Saved → kaspa_winners.json")
