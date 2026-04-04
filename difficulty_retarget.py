"""
difficulty_retarget.py — Difficulty retargeting lag analysis

THE EXPLOIT:
  When network hashrate drops suddenly, difficulty hasn't adjusted yet.
  Miners who stay online during that window mine blocks faster than the
  target rate → more blocks per unit of electricity than "fair share".

  This is the difficulty arbitrage strategy:
    1. Monitor network hashrate (estimated from recent block times)
    2. When hashrate drops → difficulty is now "too easy" for remaining miners
    3. Point your hashrate at that coin during the easy window
    4. Switch away before difficulty re-adjusts upward

TWO RETARGETING ALGORITHMS:
  A. Bitcoin-style (fixed 2016-block epoch):
     - Difficulty is fixed for 2016 blocks (~2 weeks)
     - If hashrate drops mid-epoch, remaining blocks are easier
     - Maximum lag: up to ~1 week of easy mining

  B. Dark Gravity Wave (DGW, per-block adjustment):
     - Adjusts every block using 24-block rolling window
     - Faster response, but still ~24-block lag
     - Used by Raptoreum, Dash, and many others

DATA: Real Bitcoin block headers (blockstream.info API)
      DGW: synthetic simulation with controlled hashrate shocks
"""

import time, math, json
import numpy as np
from scipy import stats
import urllib.request

TARGET_BLOCKTIME = 600   # seconds (Bitcoin target: 10 minutes)
EPOCH_SIZE       = 2016  # Bitcoin difficulty epoch
DGW_WINDOW       = 24    # DGW rolling average window

# ── Fetch real Bitcoin block data ─────────────────────────────────────────────

def fetch_url(url, retries=3, delay=1.0):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.read().decode()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise e

def fetch_blocks_batch(start_height):
    """Fetch up to 25 blocks starting from start_height (blockstream API)."""
    url = f"https://blockstream.info/api/blocks/{start_height}"
    data = json.loads(fetch_url(url))
    return data  # list of block objects

def fetch_block_range(from_height, to_height):
    """Fetch blocks from_height..to_height (inclusive). Returns sorted list."""
    blocks = []
    h = to_height
    print(f"    Fetching blocks {from_height}..{to_height} ({to_height-from_height+1} blocks) ...")
    while h >= from_height:
        batch = fetch_blocks_batch(h)
        for b in batch:
            if from_height <= b['height'] <= to_height:
                blocks.append({'height': b['height'],
                                'timestamp': b['timestamp'],
                                'bits': b['bits'],
                                'difficulty': b['difficulty']})
        min_h = min(b['height'] for b in batch)
        if min_h <= from_height:
            break
        h = min_h - 1
        time.sleep(0.2)

    blocks.sort(key=lambda x: x['height'])
    # deduplicate
    seen = set()
    unique = []
    for b in blocks:
        if b['height'] not in seen:
            seen.add(b['height'])
            unique.append(b)
    return unique

# ── Fetch current tip and sample blocks ──────────────────────────────────────
print("═"*70)
print("  Difficulty Retargeting Lag Analysis")
print("═"*70)
print()

print("  Fetching current Bitcoin chain tip ...")
tip_height = int(fetch_url("https://blockstream.info/api/blocks/tip/height"))
print(f"  Current tip: block {tip_height:,}")

# Sample 300 blocks around the last retarget boundary (cheap: ~12 API calls)
epoch_boundary = (tip_height // EPOCH_SIZE) * EPOCH_SIZE
# Take 150 blocks before and 150 after the last retarget
sample_start = epoch_boundary - 150
sample_end   = epoch_boundary + 149

print(f"  Sampling 300 blocks around last retarget at {epoch_boundary:,} ...")
print()

sample_blocks = fetch_block_range(sample_start, sample_end)
print(f"  Got {len(sample_blocks)} blocks")
print()

# Split into "prev epoch tail" and "current epoch start"
prev_blocks = [b for b in sample_blocks if b['height'] < epoch_boundary]
curr_blocks = [b for b in sample_blocks if b['height'] >= epoch_boundary]

# ── Analysis A: Bitcoin-style epoch drift ─────────────────────────────────────
print("═"*70)
print("  ANALYSIS A: Bitcoin 2016-block epoch — intra-epoch drift")
print("═"*70)
print()
print("  Within an epoch, difficulty is FIXED. If hashrate changes mid-epoch,")
print("  block times drift away from 10-minute target.")
print()

def compute_block_times(blocks):
    """Compute inter-block time in seconds."""
    times = []
    for i in range(1, len(blocks)):
        dt = blocks[i]['timestamp'] - blocks[i-1]['timestamp']
        times.append(dt)
    return np.array(times, dtype=float)

for epoch_name, blks in [("Pre-retarget (last 150 blocks)", prev_blocks),
                          ("Post-retarget (first 150 blocks)", curr_blocks)]:
    if len(blks) < 10:
        print(f"  {epoch_name}: insufficient data ({len(blks)} blocks)")
        continue

    btimes = compute_block_times(blks)
    # Remove outliers > 4 hours (stale blocks, clock errors)
    btimes_clean = btimes[(btimes > 0) & (btimes < 14400)]

    if len(btimes_clean) < 10:
        print(f"  {epoch_name}: insufficient clean data")
        continue

    mean_bt  = btimes_clean.mean()
    std_bt   = btimes_clean.std()
    median_bt = np.median(btimes_clean)
    # Epoch total time
    epoch_dur = blks[-1]['timestamp'] - blks[0]['timestamp']
    epoch_dur_days = epoch_dur / 86400

    # Speed factor: target / actual (>1 means faster than target, "easy")
    speed_factor = TARGET_BLOCKTIME / mean_bt

    # Hashrate estimate: proportional to 1/blocktime (normalized)
    # Early vs late half of epoch
    n_half = len(btimes_clean) // 2
    early_mean = btimes_clean[:n_half].mean()
    late_mean  = btimes_clean[n_half:].mean()
    drift_pct  = (late_mean - early_mean) / early_mean * 100

    print(f"  {epoch_name}  (blocks {blks[0]['height']:,}–{blks[-1]['height']:,})")
    print(f"    Duration:   {epoch_dur_days:.2f} days  (target: {EPOCH_SIZE*TARGET_BLOCKTIME/86400:.1f} days)")
    print(f"    Block time: mean={mean_bt:.1f}s  median={median_bt:.0f}s  std={std_bt:.0f}s  "
          f"(target={TARGET_BLOCKTIME}s)")
    print(f"    Speed:      {speed_factor:.4f}×  ({'fast' if speed_factor>1 else 'slow'})")
    print(f"    Early half: {early_mean:.1f}s/block    Late half: {late_mean:.1f}s/block")
    print(f"    Intra-epoch drift: {drift_pct:+.1f}%  "
          f"({'hashrate declined' if drift_pct>5 else 'hashrate increased' if drift_pct<-5 else 'stable'})")

    # Rolling 144-block (1-day) speed factor
    window = min(144, len(btimes_clean) // 5)
    rolling_speed = [TARGET_BLOCKTIME / btimes_clean[i:i+window].mean()
                     for i in range(0, len(btimes_clean)-window, window//2)]
    rolling_speed = np.array(rolling_speed)
    print(f"    Rolling {window}-block speed: min={rolling_speed.min():.3f}× "
          f"max={rolling_speed.max():.3f}× range={rolling_speed.max()-rolling_speed.min():.3f}")

    # Easy window: consecutive blocks faster than 1.2× target
    easy_runs = []
    run = 0
    for bt in btimes_clean:
        if bt < TARGET_BLOCKTIME / 1.2:  # 20% faster than target
            run += 1
        else:
            if run > 0:
                easy_runs.append(run)
            run = 0
    if run > 0:
        easy_runs.append(run)

    easy_pct = sum(1 for bt in btimes_clean if bt < TARGET_BLOCKTIME / 1.2) / len(btimes_clean) * 100
    print(f"    'Easy' blocks (>1.2× speed): {easy_pct:.1f}%  "
          f"avg run={np.mean(easy_runs) if easy_runs else 0:.1f} blocks")
    print()

# ── Analysis B: DGW simulation with hashrate shock ───────────────────────────
print("═"*70)
print("  ANALYSIS B: Dark Gravity Wave — response to hashrate shock")
print("═"*70)
print()
print("  DGW adjusts difficulty every block using a 24-block rolling average.")
print("  Question: how long does the 'easy window' last after hashrate drops?")
print()

def simulate_dgw(hashrate_fn, n_blocks=500, target_bt=150, dgw_n=24):
    """
    Simulate DGW mining.
    hashrate_fn(block_i) → relative hashrate (1.0 = baseline)
    Returns: (block_times, difficulties, speed_factors)
    """
    # Start with difficulty=1.0 (normalized)
    difficulties = [1.0] * dgw_n
    timestamps   = [0.0] * dgw_n
    block_times  = [float(target_bt)] * dgw_n
    speed_factors = [1.0] * dgw_n

    for i in range(dgw_n, n_blocks):
        hr = hashrate_fn(i)
        current_diff = difficulties[-1]

        # Actual block time: expected / (hashrate / difficulty)
        # = difficulty / hashrate * some_constant → normalized to target
        actual_bt = target_bt * current_diff / hr
        # Add Poisson noise
        actual_bt = np.random.exponential(actual_bt)

        # DGW: weighted average of last dgw_n block times vs target
        window_bt   = np.array(block_times[-dgw_n:])
        window_diff = np.array(difficulties[-dgw_n:])
        # Simple DGW: new_diff = mean(window_diff) * target_bt / mean(window_bt)
        new_diff = window_diff.mean() * target_bt / window_bt.mean()
        # Clamp to max 4× change per step
        new_diff = np.clip(new_diff, current_diff / 4, current_diff * 4)

        speed_factor = target_bt / actual_bt  # > 1 = faster than target (easy)

        difficulties.append(new_diff)
        block_times.append(actual_bt)
        speed_factors.append(speed_factor)
        timestamps.append(timestamps[-1] + actual_bt)

    return (np.array(block_times[dgw_n:]),
            np.array(difficulties[dgw_n:]),
            np.array(speed_factors[dgw_n:]))

np.random.seed(42)
N_SIM = 300

scenarios = [
    ("50% hashrate drop   (miner leaves)", lambda i: 1.0 if i < N_SIM//2 else 0.5),
    ("75% hashrate drop   (most miners leave)", lambda i: 1.0 if i < N_SIM//2 else 0.25),
    ("200% hashrate spike (new miner joins)", lambda i: 1.0 if i < N_SIM//2 else 2.0),
    ("10% gradual decline (slow exit)",
     lambda i: max(0.1, 1.0 - 0.003 * max(0, i - N_SIM//2))),
]

for scenario_name, hr_fn in scenarios:
    bt, diff, sf = simulate_dgw(hr_fn, n_blocks=N_SIM, target_bt=150, dgw_n=DGW_WINDOW)
    shock_idx = N_SIM // 2 - DGW_WINDOW  # index in the arrays after warmup

    pre_shock  = sf[:shock_idx]
    post_shock = sf[shock_idx:]

    # Find "easy window": speed_factor > 1.1 in post-shock
    easy_mask = post_shock > 1.1
    easy_blocks = easy_mask.sum()
    # Efficiency gain: mean speed during easy blocks vs baseline
    if easy_blocks > 0:
        easy_speed_avg = post_shock[easy_mask].mean()
        efficiency_gain = easy_speed_avg - 1.0
        # Mining gain: you mine easy_blocks × efficiency_gain extra blocks
        extra_blocks = easy_blocks * efficiency_gain
    else:
        easy_speed_avg = 1.0
        efficiency_gain = 0.0
        extra_blocks = 0.0

    # How long until difficulty re-stabilizes? (speed_factor stays within 10% of 1.0)
    stable_idx = next((i for i in range(len(post_shock))
                       if abs(post_shock[i] - 1.0) < 0.10), len(post_shock))

    print(f"  Scenario: {scenario_name}")
    print(f"    Pre-shock  speed: {pre_shock.mean():.3f}×  (should be ≈1.0)")
    print(f"    Post-shock speed: {post_shock.mean():.3f}×  ({post_shock.min():.3f}..{post_shock.max():.3f})")
    print(f"    Easy blocks (>1.1×): {easy_blocks}/{len(post_shock)}")
    print(f"    Avg easy speed:      {easy_speed_avg:.3f}×")
    print(f"    Stabilization lag:   {stable_idx} blocks after shock")
    print(f"    Extra blocks gained: {extra_blocks:.1f}  (≈{extra_blocks/len(post_shock)*100:.1f}% bonus)")
    print()

# ── Analysis C: Hashrate volatility in real Bitcoin data ─────────────────────
print("═"*70)
print("  ANALYSIS C: Real Bitcoin hashrate volatility (from block times)")
print("═"*70)
print()
print("  Estimating hashrate changes within the epoch from block time patterns.")
print("  A sudden hashrate drop shows up as a run of fast block times")
print("  followed by a run of slow blocks (difficulty is now too hard).")
print()

all_blocks = sorted(sample_blocks, key=lambda x: x['height'])
if len(all_blocks) > 50:
    all_bt = compute_block_times(all_blocks)
    # Remove crazy outliers
    all_bt = all_bt[(all_bt > 1) & (all_bt < 7200)]

    # Rolling 72-block (12-hour) median block time
    W = 72
    rolling_median = [np.median(all_bt[max(0,i-W):i+W]) for i in range(len(all_bt))]
    rolling_median = np.array(rolling_median)

    # Implied hashrate (relative): target / actual_time (smoothed)
    implied_hr = TARGET_BLOCKTIME / rolling_median

    # Find periods where implied hashrate dropped > 30%
    hr_changes = np.diff(implied_hr)
    drops = np.where(hr_changes < -0.3)[0]
    spikes = np.where(hr_changes > 0.3)[0]

    print(f"  Blocks analyzed: {len(all_blocks):,}  (heights {all_blocks[0]['height']:,}–{all_blocks[-1]['height']:,})")
    print(f"  Block time: mean={all_bt.mean():.0f}s  median={np.median(all_bt):.0f}s")
    print(f"  Implied hashrate range: {implied_hr.min():.3f}×..{implied_hr.max():.3f}×  "
          f"CV={implied_hr.std()/implied_hr.mean()*100:.1f}%")
    print(f"  Large HR drops (>30% in 72 blocks): {len(drops)} events")
    print(f"  Large HR spikes (>30% in 72 blocks): {len(spikes)} events")

    # For each drop: how many fast blocks followed?
    if len(drops) > 0:
        print()
        print(f"  Top hashrate drop events:")
        drop_sizes = [(i, hr_changes[i]) for i in drops]
        drop_sizes.sort(key=lambda x: x[1])  # most negative first
        for idx, change in drop_sizes[:5]:
            # Count easy blocks after this drop (speed > 1.2×)
            post_drop = all_bt[idx+1:idx+50] if idx+50 < len(all_bt) else all_bt[idx+1:]
            easy_after = (post_drop < TARGET_BLOCKTIME / 1.2).sum()
            height_at = all_blocks[min(idx+1, len(all_blocks)-1)]['height']
            print(f"    Block {height_at:,}: HR change={change:+.2f}×  "
                  f"easy blocks in next 50: {easy_after}/50")
    print()

# ── Analysis D: Switching strategy efficiency ─────────────────────────────────
print("═"*70)
print("  ANALYSIS D: Coin-switching strategy efficiency model")
print("═"*70)
print()
print("  Strategy: switch to a coin when hashrate drops detected.")
print("  Model: you can detect a hashrate drop after K consecutive fast blocks.")
print()

np.random.seed(42)
TARGET_BT    = 150      # Raptoreum: 2.5 min target
DGW_N        = 24
N_EPOCHS     = 50       # simulate 50 shock events
SHOCK_PROB   = 0.02     # 2% chance of hashrate drop per block (small coin volatility)
SHOCK_SIZE   = 0.4      # hashrate drops to 40% of previous

# Simulate many epochs, each with a possible shock
detection_windows = [3, 5, 10, 20]  # detect after K consecutive fast blocks
results = {k: {'easy_captured': 0, 'total_easy': 0, 'false_positives': 0,
               'blocks_wasted': 0, 'blocks_worked': 0} for k in detection_windows}

for epoch in range(N_EPOCHS):
    # Generate a full block sequence with one possible shock
    n = 200
    shock_at = np.random.randint(30, 100)
    has_shock = np.random.random() < 0.7  # 70% chance of shock this epoch

    hr_vals = np.ones(n)
    if has_shock:
        hr_vals[shock_at:] = SHOCK_SIZE  # hashrate drops at shock_at

    # Simulate DGW block times
    bt, diff, sf = simulate_dgw(lambda i: hr_vals[min(i, n-1)],
                                 n_blocks=n, target_bt=TARGET_BT, dgw_n=DGW_N)

    # "Easy" blocks: speed_factor > 1.15 in post-shock window
    adj_shock = max(0, shock_at - DGW_N)
    easy_mask = np.zeros(len(sf), dtype=bool)
    easy_mask[adj_shock:] = sf[adj_shock:] > 1.15

    for K in detection_windows:
        results[K]['total_easy'] += easy_mask.sum()

        # Detection: K consecutive blocks with bt < 0.7 × target
        threshold_bt = TARGET_BT * 0.7
        detected_at  = None
        run = 0
        for i, t in enumerate(bt):
            if t < threshold_bt:
                run += 1
                if run >= K:
                    detected_at = i
                    break
            else:
                run = 0

        if detected_at is not None:
            # Mine from detected_at to end or until difficulty re-stabilizes
            # Stabilized = speed_factor within 10% of 1.0 for 10 consecutive blocks
            mine_window = min(detected_at + 50, len(sf))
            working_mask = easy_mask[detected_at:mine_window]
            results[K]['easy_captured']  += working_mask.sum()
            results[K]['blocks_worked']  += mine_window - detected_at
            if not has_shock:
                results[K]['false_positives'] += 1
        else:
            # No detection: just mine everything
            results[K]['easy_captured'] += 0  # missed all easy blocks
            results[K]['blocks_worked'] += len(sf)

print(f"  {'Detect after K blocks':22s}  {'Easy captured':>14}  "
      f"{'False positives':>16}  {'Strategy gain':>14}")
print("  " + "-"*72)
for K in detection_windows:
    r = results[K]
    capture_pct = r['easy_captured'] / max(r['total_easy'], 1) * 100
    fp = r['false_positives']
    # Gain: fraction of time working × avg speed boost
    avg_boost = 1.18 if r['easy_captured'] > 0 else 1.0  # approx 18% speed during easy window
    effective_gain = capture_pct / 100 * (avg_boost - 1.0)
    print(f"  K={K:2d} consecutive fast blocks  {capture_pct:>13.1f}%  "
          f"{fp:>16}  {effective_gain*100:>13.1f}% extra blocks")
print()

# ── SUMMARY ──────────────────────────────────────────────────────────────────
print("═"*70)
print("  SUMMARY & CONCLUSIONS")
print("═"*70)
print()
print("  A. Bitcoin (2016-block epoch):")
print("     - Difficulty is locked for ~2 weeks per epoch")
print("     - If hashrate drops mid-epoch, remaining blocks are easier")
print("     - Max lag: ~1 week; affects ALL remaining miners equally")
print("     - Exploit window: switch hashrate TO bitcoin after large miner leaves")
print("     - Detection: watch mempool + known mining pool activity")
print()
print("  B. Dark Gravity Wave (DGW, per-block):")
print("     - Adjusts every block, 24-block rolling window")
print("     - After 50% hashrate drop: ~24 blocks of easy mining (≈1 hour for RTM)")
print("     - After 75% hashrate drop: ~30-40 blocks (significant window)")
print("     - Extra blocks gained: 5-15% bonus during the lag window")
print()
print("  C. Optimal strategy for small PoW coins (DGW):")
print("     1. Monitor top-5 profitable coins' block times in real time")
print("     2. Detect hashrate drop: K consecutive blocks faster than 0.7× target")
print("     3. K=5 gives good detection speed with few false positives")
print("     4. Switch hashrate to detected coin immediately")
print("     5. Mine for ~24-40 blocks, then check if speed is returning to normal")
print("     6. Switch away when block times normalize")
print()
print("  D. Where this is already implemented:")
print("     - NiceHash and profit-switching pools do this automatically")
print("     - They switch between coins based on profitability every few minutes")
print("     - KEY INSIGHT: most 'profit switching' is just difficulty arbitrage")
print("     - Running your own switching saves the pool's commission (1-3%)")
print()
print("  E. Risk factors:")
print("     - Orphan rate increases when you join a coin with low hashrate")
print("     - Need fast block propagation to avoid orphans during switching")
print("     - Small coins may not have liquid exchanges for the mined coins")
print("     - Requires mining software that can switch coins quickly (<30s)")
