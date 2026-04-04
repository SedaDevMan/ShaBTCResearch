"""
rescan.py — rescan all blocks with 5M nonces using the C extension.
Produces winners_5m.json with ~87 winners per block (~19K pairs total).
"""
import scan, json, time

DIFFICULTY_BITS = 16
TARGET          = (2**64) >> DIFFICULTY_BITS
SCAN_RANGE      = 5_000_000

with open("blocks.json") as f:
    chain = json.load(f)

mined = [b for b in chain if b["prev"] is not None]
print(f"Rescanning {len(mined)} blocks × {SCAN_RANGE:,} nonces  (target<{TARGET:#018x})\n")

results = []
total_winners = 0
t0 = time.perf_counter()

for i, block in enumerate(mined):
    prev_bytes = bytes.fromhex(block["prev"])
    winners    = scan.scan_winners(prev_bytes, SCAN_RANGE, TARGET)
    total_winners += len(winners)
    results.append({"prev": block["prev"], "winners": winners})

    if (i + 1) % 44 == 0:
        elapsed = time.perf_counter() - t0
        eta = elapsed / (i + 1) * (len(mined) - i - 1)
        print(f"  {i+1}/{len(mined)}  pairs={total_winners:,}  eta={eta:.1f}s")

elapsed = time.perf_counter() - t0
avg = total_winners / len(mined)
print(f"\nDone in {elapsed:.1f}s  |  {total_winners:,} pairs  |  avg {avg:.1f} winners/block")

out = {
    "config": {"difficulty_bits": DIFFICULTY_BITS, "scan_range": SCAN_RANGE},
    "blocks_scanned": len(mined),
    "total_pairs": total_winners,
    "winners_per_block": results
}
with open("winners_5m.json", "w") as f:
    json.dump(out, f)
print("Saved → winners_5m.json")
