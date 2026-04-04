"""
generate_chain.py

Simulates a blockchain using xxHash64.
Each block is "mined" by finding a nonce where:
    xxhash64(prev_hash_bytes || nonce_bytes) < target

The resulting chain of block hashes is saved to blocks.json.
This is the input corpus for the nonce-pattern analysis PoC.
"""

import xxhash
import struct
import json
import time

# ── Config ────────────────────────────────────────────────────────────────────
CHAIN_LENGTH   = 220          # number of blocks to mine
DIFFICULTY_BITS = 16          # leading zero bits required  (16 → ~1/65536 nonces win)
TARGET         = (2**64) >> DIFFICULTY_BITS   # uint64 target
GENESIS_SEED   = b"shabtc-genesis-block-v1"
# ──────────────────────────────────────────────────────────────────────────────


def xxh64_int(data: bytes) -> int:
    """Return xxhash64 digest as a Python int."""
    return xxhash.xxh64(data).intdigest()


def mine_block(prev_hash_hex: str) -> dict:
    """
    Search nonces 0..2^32 until xxh64(prev_hash_bytes || nonce) < TARGET.
    Returns a dict with the winning nonce and the new block hash.
    """
    prev_bytes = bytes.fromhex(prev_hash_hex)
    for nonce in range(2**32):
        nonce_bytes = struct.pack(">I", nonce)   # 4-byte big-endian
        digest = xxh64_int(prev_bytes + nonce_bytes)
        if digest < TARGET:
            new_hash = format(digest, "016x")
            return {"nonce": nonce, "hash": new_hash, "prev": prev_hash_hex}
    raise RuntimeError("No nonce found in full 2^32 range — lower difficulty")


def generate_chain(length: int) -> list[dict]:
    # Genesis block: hash of the seed, no nonce needed
    genesis_hash = format(xxh64_int(GENESIS_SEED), "016x")
    chain = [{"block": 0, "nonce": None, "hash": genesis_hash, "prev": None}]
    print(f"Genesis  block #0  hash={genesis_hash}")

    for i in range(1, length + 1):
        t0 = time.perf_counter()
        result = mine_block(chain[-1]["hash"])
        elapsed = time.perf_counter() - t0
        block = {"block": i, **result}
        chain.append(block)
        print(f"  Mined  block #{i:<4} nonce={result['nonce']:<12,}  "
              f"hash={result['hash']}  ({elapsed:.3f}s)")

    return chain


if __name__ == "__main__":
    print(f"Mining {CHAIN_LENGTH} blocks  |  difficulty={DIFFICULTY_BITS} bits  |  target<{TARGET:#018x}\n")
    t_start = time.perf_counter()
    chain = generate_chain(CHAIN_LENGTH)
    total = time.perf_counter() - t_start

    with open("blocks.json", "w") as f:
        json.dump(chain, f, indent=2)

    print(f"\nDone — {len(chain)} blocks saved to blocks.json  ({total:.1f}s total)")
    print(f"Nonce range seen: "
          f"min={min(b['nonce'] for b in chain if b['nonce'] is not None):,}  "
          f"max={max(b['nonce'] for b in chain if b['nonce'] is not None):,}")
