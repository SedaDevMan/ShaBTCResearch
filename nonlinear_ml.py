"""
nonlinear_ml.py — Non-linear pattern detection on nonce → winner

Our previous tests were all LINEAR (Pearson, chi-squared, Friedman).
A decision tree finds conjunctions like:
  "nonce bits 3,7,15,31 are ALL set → 8× more likely to win"
which are completely invisible to linear tests.

Validation: VerusHash N=1 MUST show signal (known algebraic structure).
            If the tree misses it, the test is broken.
Test:       SHA256d, VerusHash N=4, VerusHash N=10 should show nothing.

Features per nonce:
  - 32 individual bits (bit_0 .. bit_31)
  - 4 byte values (byte_0 .. byte_3)
  - 8 nibble values
  Total: 44 features
"""

import time, json
import numpy as np
from scipy import stats
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import cross_val_score
from sklearn.dummy import DummyClassifier
import verus_aes, pot_skip

NONCES_PER_BLOCK = 200_000
N_BLOCKS         = 10
TARGET           = b'\x01' + b'\x00' * 31   # 8-bit difficulty

import hashlib, struct
def make_chain(n):
    h = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000face")
    chain = []
    for _ in range(n):
        chain.append(h)
        h = hashlib.sha256(hashlib.sha256(h).digest()).digest()
    return chain

CHAIN = make_chain(N_BLOCKS)

def nonce_features(nonces):
    """Extract 44 features from array of nonce integers."""
    n = len(nonces)
    feats = np.zeros((n, 44), dtype=np.float32)
    arr = np.array(nonces, dtype=np.uint32)
    # 32 bits
    for b in range(32):
        feats[:, b] = (arr >> b) & 1
    # 4 bytes
    for b in range(4):
        feats[:, 32+b] = (arr >> (b*8)) & 0xFF
    # 8 nibbles
    for b in range(8):
        feats[:, 36+b] = (arr >> (b*4)) & 0xF
    return feats

def collect_pairs_verus(nrounds, n_blocks=N_BLOCKS):
    """Collect (nonce, is_winner) pairs for VerusHash-N."""
    nonces_all, labels_all = [], []
    for prev in CHAIN[:n_blocks]:
        winners = set(verus_aes.scan_winners(prev, NONCES_PER_BLOCK, TARGET, nrounds))
        for n in range(NONCES_PER_BLOCK):
            nonces_all.append(n)
            labels_all.append(1 if n in winners else 0)
    return np.array(nonces_all), np.array(labels_all)

def collect_pairs_sha256d(n_blocks=N_BLOCKS):
    """Collect (nonce, is_winner) pairs for real SHA256d using hashlib."""
    import struct
    nonces_all, labels_all = [], []
    target_int = int.from_bytes(TARGET, 'big')
    for prev in CHAIN[:n_blocks]:
        for n in range(NONCES_PER_BLOCK):
            inp = prev + struct.pack('<I', n)
            h1  = hashlib.sha256(inp).digest()
            h2  = hashlib.sha256(h1).digest()
            nonces_all.append(n)
            labels_all.append(1 if int.from_bytes(h2, 'big') < target_int else 0)
    return np.array(nonces_all), np.array(labels_all)

def run_ml_test(label, nonces, labels):
    """
    Train decision tree + gradient boosting on nonce features.
    Compare to dummy baseline (always predict majority class).
    If real model >> dummy → non-linear signal found.
    """
    print(f"  {label}")
    n_winners = labels.sum()
    n_total   = len(labels)
    base_rate = n_winners / n_total

    if n_winners < 50:
        print(f"    Too few winners ({n_winners}) — skip\n")
        return None

    print(f"    Winners: {n_winners:,} / {n_total:,}  ({base_rate*100:.2f}%)")

    X = nonce_features(nonces)
    y = labels

    # Subsample if too large (keep balance)
    if n_total > 100_000:
        win_idx  = np.where(y==1)[0]
        lose_idx = np.where(y==0)[0]
        # Keep all winners + same number of losers
        n_keep   = min(len(win_idx)*10, len(lose_idx))
        lose_samp= np.random.choice(lose_idx, n_keep, replace=False)
        idx      = np.concatenate([win_idx, lose_samp])
        np.random.shuffle(idx)
        X, y = X[idx], y[idx]
        print(f"    Subsampled to {len(y):,} ({y.sum()} winners)")

    # Baseline
    dummy  = DummyClassifier(strategy='most_frequent')
    d_scores = cross_val_score(dummy, X, y, cv=5, scoring='roc_auc')

    # Decision tree
    tree   = DecisionTreeClassifier(max_depth=8, min_samples_leaf=20)
    t_scores = cross_val_score(tree, X, y, cv=5, scoring='roc_auc')

    # Gradient boosting (stronger)
    gb     = GradientBoostingClassifier(n_estimators=50, max_depth=4,
                                         learning_rate=0.1, subsample=0.8)
    g_scores = cross_val_score(gb, X, y, cv=5, scoring='roc_auc')

    d_auc = d_scores.mean()
    t_auc = t_scores.mean()
    g_auc = g_scores.mean()

    # Signal: AUC significantly above 0.5 (random = 0.5)
    t_signal = t_auc > 0.55 and t_auc > d_auc + 0.03
    g_signal = g_auc > 0.55 and g_auc > d_auc + 0.03

    print(f"    Dummy AUC:    {d_auc:.4f}")
    print(f"    DecTree AUC:  {t_auc:.4f}  {'◄ SIGNAL' if t_signal else ''}")
    print(f"    GradBoost AUC:{g_auc:.4f}  {'◄ SIGNAL' if g_signal else ''}")

    if t_signal or g_signal:
        # Find most important feature
        tree.fit(X, y)
        top_feat = np.argmax(tree.feature_importances_)
        feat_names = ([f"bit_{i}" for i in range(32)] +
                      [f"byte_{i}" for i in range(4)] +
                      [f"nibble_{i}" for i in range(8)])
        print(f"    Top feature: {feat_names[top_feat]}"
              f"  (importance={tree.feature_importances_[top_feat]:.3f})")
        print(f"    ► NON-LINEAR STRUCTURE FOUND in {label}!")
    else:
        print(f"    No signal — nonce bits do not predict winners")
    print()

    return {"label": label, "tree_auc": float(t_auc), "gb_auc": float(g_auc),
            "signal": bool(t_signal or g_signal)}


np.random.seed(42)
print("═"*60)
print("  Non-linear ML nonce pattern test")
print("═"*60)
print(f"  {NONCES_PER_BLOCK:,} nonces × {N_BLOCKS} blocks per algorithm\n")

results = []

# ── Validation: VerusHash N=1 (MUST show signal) ─────────────────────────
print("── Validation (must find signal) ──────────────────────────")
print("  Collecting VerusHash N=1 pairs ...")
t0 = time.perf_counter()
nonces, labels = collect_pairs_verus(1)
print(f"  Done: {time.perf_counter()-t0:.1f}s")
r = run_ml_test("VerusHash N=1 (known weak)", nonces, labels)
if r: results.append(r)

# ── VerusHash N=4 (should be noise) ──────────────────────────────────────
print("── Real targets ────────────────────────────────────────────")
print("  Collecting VerusHash N=4 pairs ...")
t0 = time.perf_counter()
nonces, labels = collect_pairs_verus(4)
print(f"  Done: {time.perf_counter()-t0:.1f}s")
r = run_ml_test("VerusHash N=4", nonces, labels)
if r: results.append(r)

# ── VerusHash N=10 ────────────────────────────────────────────────────────
print("  Collecting VerusHash N=10 pairs ...")
t0 = time.perf_counter()
nonces, labels = collect_pairs_verus(10)
print(f"  Done: {time.perf_counter()-t0:.1f}s")
r = run_ml_test("VerusHash N=10", nonces, labels)
if r: results.append(r)

# ── SHA256d ───────────────────────────────────────────────────────────────
print("  Collecting SHA256d pairs ...")
t0 = time.perf_counter()
nonces, labels = collect_pairs_sha256d()
print(f"  Done: {time.perf_counter()-t0:.1f}s")
r = run_ml_test("SHA256d (full)", nonces, labels)
if r: results.append(r)


# ── Summary ───────────────────────────────────────────────────────────────
print("═"*60)
print("  SUMMARY")
print("═"*60)
print(f"  {'Algorithm':30s}  {'Tree AUC':>10}  {'GB AUC':>8}  Result")
print("  " + "-"*56)
for r in results:
    verdict = "SIGNAL ◄" if r["signal"] else "noise"
    print(f"  {r['label']:30s}  {r['tree_auc']:>10.4f}  {r['gb_auc']:>8.4f}  {verdict}")

print()
if any(r["signal"] for r in results if "N=1" not in r["label"]):
    print("  NON-LINEAR STRUCTURE FOUND beyond known weak case!")
else:
    validated = any(r["signal"] for r in results if "N=1" in r["label"])
    print(f"  Validation {'PASSED ✓' if validated else 'FAILED ✗'} (N=1 detected)")
    print("  No non-linear structure in SHA256d or VerusHash N≥4")
    print("  Linear tests were sufficient — no hidden conjunctions")
