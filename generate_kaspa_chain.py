"""
generate_kaspa_chain.py — Mine 100 blocks using HeavyHash (Kaspa-style).

For each block:
  - pre_pow_hash = previous block's final hash (32 bytes)
  - matrix = generate_matrix(pre_pow_hash)
  - Mine: find nonce where heavyhash(matrix, pre_pow_hash + nonce_LE8) < target
  - block_hash = that winning hash

Difficulty: 14-bit  →  P(win) = 1/2^14 ≈ 1/16384
Expected: ~16K hashes per block, ~3s total at 521 KH/s
"""

import sys, time, struct
import heavyhash

DIFFICULTY_BITS = 14
# target: top 14 bits = 0  →  first byte 0x00, second byte < 0x04
# 2^(256-14) = 0x0004000...000
TARGET_BYTES = b'\x00\x04' + b'\x00' * 30

NBLOCKS = 100

# Genesis pre_pow_hash (fixed seed, 32 bytes)
genesis_pre = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000cafe")

def matrix_to_bytes(mat):
    """Flatten 64x64 list-of-lists to 4096 bytes."""
    return bytes(mat[r][c] for r in range(64) for c in range(64))

def mine_block(pre_pow_hash: bytes):
    """Return (nonce, block_hash_bytes) for the first nonce that wins."""
    mat_list  = heavyhash.generate_matrix(pre_pow_hash)
    mat_bytes = matrix_to_bytes(mat_list)
    prefix    = pre_pow_hash          # 32-byte header prefix

    # Scan in batches of 100K
    BATCH = 100_000
    nonce = 0
    while True:
        winners = heavyhash.scan_winners(mat_bytes, prefix, BATCH, TARGET_BYTES)
        if winners:
            w = min(winners)  # take lowest winning nonce
            # Compute actual hash to store as block hash
            nonce_bytes = struct.pack('<Q', w)
            full_header = prefix + nonce_bytes
            block_hash  = heavyhash.heavyhash(mat_bytes, full_header)
            return w + nonce, block_hash   # absolute nonce
        nonce += BATCH
        # Re-issue scan starting from 'nonce' — rebuild prefix with offset?
        # Easier: just pass an offset. Since scan_winners starts from 0 each call,
        # build a wrapper that shifts the base nonce via a different prefix trick.
        # Actually: let's just iterate ourselves.
        if nonce > 2**32:
            raise RuntimeError("No winner found in 2^32 nonces")

def mine_block_v2(pre_pow_hash: bytes):
    """Mine by scanning with increasing base nonces."""
    mat_list  = heavyhash.generate_matrix(pre_pow_hash)
    mat_bytes = matrix_to_bytes(mat_list)
    prefix    = pre_pow_hash

    BATCH = 200_000
    base  = 0
    while base < 2**32:
        # Embed base nonce into prefix to shift the scan window
        # heavyhash.scan_winners iterates 0..scan_range-1 appended as LE64
        # We can't shift without modifying C, so just scan large range once
        winners = heavyhash.scan_winners(mat_bytes, prefix, BATCH, TARGET_BYTES)
        if winners:
            w = min(winners)
            nonce_bytes = struct.pack('<Q', w)
            block_hash  = heavyhash.heavyhash(mat_bytes, prefix + nonce_bytes)
            return w, block_hash
        base += BATCH
        if base >= 2**20:  # cap at 1M tries max per block (safety)
            # Lower difficulty threshold for this block
            break
    # fallback: scan up to 2M
    winners = heavyhash.scan_winners(mat_bytes, prefix, 2_000_000, TARGET_BYTES)
    if winners:
        w = min(winners)
        nonce_bytes = struct.pack('<Q', w)
        block_hash  = heavyhash.heavyhash(mat_bytes, prefix + nonce_bytes)
        return w, block_hash
    raise RuntimeError(f"No winner in 2M nonces for pre_pow={pre_pow_hash.hex()}")


print(f"Mining {NBLOCKS} HeavyHash blocks at {DIFFICULTY_BITS}-bit difficulty")
print(f"Target: {TARGET_BYTES.hex()[:16]}...")
print(f"Expected hashes/block: ~{2**DIFFICULTY_BITS:,}  ({2**DIFFICULTY_BITS/521000:.2f}s per block at 521KH/s)\n")

chain = []
prev_hash = genesis_pre
t0 = time.perf_counter()

for i in range(NBLOCKS):
    nonce, block_hash = mine_block_v2(prev_hash)
    chain.append({
        "block":    i + 1,
        "prev":     prev_hash.hex(),
        "nonce":    nonce,
        "hash":     block_hash.hex(),
    })
    prev_hash = block_hash

    if (i + 1) % 10 == 0:
        elapsed = time.perf_counter() - t0
        rate    = (i + 1) / elapsed
        eta     = (NBLOCKS - i - 1) / rate
        print(f"  Block {i+1:>3}/{NBLOCKS}  elapsed={elapsed:.1f}s  rate={rate:.1f} blk/s  eta={eta:.1f}s")

elapsed = time.perf_counter() - t0
print(f"\nDone: {NBLOCKS} blocks in {elapsed:.1f}s  ({elapsed/NBLOCKS*1000:.0f}ms/block)")

import json
out = {
    "config": {"difficulty_bits": DIFFICULTY_BITS, "target": TARGET_BYTES.hex()},
    "blocks": chain,
}
with open("kaspa_blocks.json", "w") as f:
    json.dump(out, f)
print("Saved → kaspa_blocks.json")
