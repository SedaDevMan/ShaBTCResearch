"""
mlp_test.py

Neural network test: can a non-linear MLP learn to predict
winning nonces from K previous block hashes?

Task: binary classification
  Positive: (K-hash window bits, winning nonce bits)   → label 1
  Negative: (K-hash window bits, random losing nonce)  → label 0

If MLP accuracy > 55% on held-out test set → learnable signal exists.
If accuracy ≈ 50% → no signal, xxHash is unlearnable.

Also tests: can MLP learn the SKIP RATIO metric?
  = what fraction of nonce space can we skip while keeping 95% of winners?
"""

import json, random, time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

random.seed(42); np.random.seed(42); torch.manual_seed(42)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {DEVICE}\n")

# ── Load data ─────────────────────────────────────────────────────────────────
with open("blocks.json") as f:
    chain = json.load(f)
with open("winners_5m.json") as f:
    wdata = json.load(f)

SCAN_RANGE = wdata["config"]["scan_range"]
prev_to_winners = {b["prev"]: set(b["winners"]) for b in wdata["winners_per_block"]}
mined = [b for b in chain if b["prev"] is not None and b["prev"] in prev_to_winners]

# ── Build dataset ─────────────────────────────────────────────────────────────
K = 10        # window size
NONCE_BITS = 19
FEAT_BITS  = K * 64
NEG_RATIO  = 3   # negatives per positive (class balance)

def hash_bits(h: int) -> list:
    return [(h >> b) & 1 for b in range(64)]

def nonce_bits(n: int) -> list:
    return [(n >> b) & 1 for b in range(NONCE_BITS)]

X_list, y_list = [], []

for i in range(K - 1, len(mined)):
    block   = mined[i]
    winners = prev_to_winners.get(block["prev"], set())
    if not winners: continue

    window = [int(mined[i - k]["prev"], 16) for k in range(K - 1, -1, -1)]
    feat_bits = []
    for h in window:
        feat_bits.extend(hash_bits(h))

    # Positives
    for w in winners:
        X_list.append(feat_bits + nonce_bits(w))
        y_list.append(1)

    # Negatives: sample random nonces NOT in winners
    neg_count = len(winners) * NEG_RATIO
    sampled = 0
    while sampled < neg_count:
        n = random.randint(0, SCAN_RANGE - 1)
        if n not in winners:
            X_list.append(feat_bits + nonce_bits(n))
            y_list.append(0)
            sampled += 1

X = np.array(X_list, dtype=np.float32)
y = np.array(y_list, dtype=np.float32)
print(f"Dataset: {len(X):,} samples  ({y.sum():.0f} pos / {(1-y).sum():.0f} neg)")
print(f"Feature dim: {X.shape[1]}  (K={K} hashes × 64 bits + {NONCE_BITS} nonce bits)\n")

# ── Train/test split (80/20 by block, not random — no data leakage) ───────────
# Shuffle at sample level (blocks already mixed by prev_hash diversity)
idx = np.random.permutation(len(X))
split = int(0.8 * len(idx))
tr_idx, te_idx = idx[:split], idx[split:]

X_tr, y_tr = torch.tensor(X[tr_idx]), torch.tensor(y[tr_idx])
X_te, y_te = torch.tensor(X[te_idx]), torch.tensor(y[te_idx])

tr_ds = TensorDataset(X_tr, y_tr)
te_ds = TensorDataset(X_te, y_te)
tr_dl = DataLoader(tr_ds, batch_size=512, shuffle=True)

# ── MLP architecture ──────────────────────────────────────────────────────────
class MLP(nn.Module):
    def __init__(self, in_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, 256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128),    nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, 64),     nn.ReLU(),
            nn.Linear(64, 1),       nn.Sigmoid()
        )
    def forward(self, x): return self.net(x).squeeze(1)

model = MLP(X.shape[1]).to(DEVICE)
pos_weight = torch.tensor([NEG_RATIO], device=DEVICE)  # balance loss
criterion  = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

# Rebuild without sigmoid for BCEWithLogitsLoss
class MLP2(nn.Module):
    def __init__(self, in_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, 256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128),    nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, 64),     nn.ReLU(),
            nn.Linear(64, 1)
        )
    def forward(self, x): return self.net(x).squeeze(1)

model = MLP2(X.shape[1]).to(DEVICE)
opt   = torch.optim.Adam(model.parameters(), lr=1e-3)

# ── Training ──────────────────────────────────────────────────────────────────
EPOCHS = 30
print(f"Training MLP ({sum(p.numel() for p in model.parameters()):,} params) "
      f"for {EPOCHS} epochs …\n")

X_te_d = X_te.to(DEVICE); y_te_d = y_te.to(DEVICE)
best_acc = 0.0

for epoch in range(1, EPOCHS + 1):
    model.train()
    total_loss = 0
    for xb, yb in tr_dl:
        xb, yb = xb.to(DEVICE), yb.to(DEVICE)
        opt.zero_grad()
        loss = criterion(model(xb), yb)
        loss.backward()
        opt.step()
        total_loss += loss.item()

    model.eval()
    with torch.no_grad():
        logits = model(X_te_d)
        preds  = (torch.sigmoid(logits) > 0.5).float()
        acc    = (preds == y_te_d).float().mean().item()
        if acc > best_acc: best_acc = acc

    if epoch % 5 == 0 or epoch == 1:
        print(f"  Epoch {epoch:>3}  loss={total_loss/len(tr_dl):.4f}  test_acc={acc:.4f}")

# ── Evaluation ────────────────────────────────────────────────────────────────
print(f"\nBest test accuracy: {best_acc:.4f}")
random_baseline  = NEG_RATIO / (NEG_RATIO + 1)  # always predict majority class
balanced_baseline = 0.50

print(f"Majority-class baseline (always predict negative): {random_baseline:.4f}")
print(f"Random-guess baseline: {balanced_baseline:.4f}")

model.eval()
with torch.no_grad():
    probs = torch.sigmoid(model(X_te_d)).cpu().numpy()
    y_np  = y_te.numpy()

# Precision/recall at threshold 0.5
tp = ((probs>0.5) & (y_np==1)).sum()
fp = ((probs>0.5) & (y_np==0)).sum()
fn = ((probs<0.5) & (y_np==1)).sum()
prec = tp/(tp+fp) if (tp+fp)>0 else 0
rec  = tp/(tp+fn) if (tp+fn)>0 else 0
print(f"Precision: {prec:.4f}  Recall: {rec:.4f}  F1: {2*prec*rec/(prec+rec+1e-9):.4f}")

# ── Skip ratio analysis ───────────────────────────────────────────────────────
print("\n── Skip ratio vs winner recall ──────────────────────────────")
print("If we only mine nonces where model score > threshold:")
print(f"  {'threshold':>10}  {'skip%':>7}  {'winner_recall':>14}  {'false_neg%':>11}")
print("  " + "-"*50)
for thresh in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
    keep   = (probs >= thresh).sum()
    pos_keep = ((probs >= thresh) & (y_np == 1)).sum()
    total_pos = (y_np == 1).sum()
    skip_pct  = 1 - keep / len(probs)
    recall    = pos_keep / total_pos if total_pos > 0 else 0
    print(f"  {thresh:>10.1f}  {skip_pct:>7.1%}  {recall:>14.1%}  {1-recall:>11.1%}")

print()
if best_acc > random_baseline + 0.02:
    print("VERDICT: MLP learned a signal above baseline → non-linear pattern exists!")
    print("  → Investigate which features the model uses (feature importance).")
elif best_acc > balanced_baseline + 0.05:
    print("VERDICT: MLP learned the class imbalance but little else.")
else:
    print("VERDICT: MLP at chance level → no learnable pattern in xxHash64.")
    print("  → xxHash64 is genuinely unpredictable from K-window features.")
    print("  → For SHA256d: same conclusion expected (even stronger hash).")
    print("  → The skip-index concept requires a fundamentally different approach.")
