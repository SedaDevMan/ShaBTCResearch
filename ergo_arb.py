"""
ergo_arb.py — Ergo (AUTOLYKOS) difficulty lag & NiceHash arbitrage analysis

ERGO DIFFICULTY ALGORITHM:
  - Adjusts every EPOCH = 1024 blocks (~34 hours at 2-min target)
  - Uses Linear Least Squares over last 8 epochs (~11 days of history)
  - Within an epoch: difficulty is COMPLETELY FIXED
  - After a hashrate shock, full correction takes 8 epochs (~11 days)

EXPLOIT:
  When hashrate drops mid-epoch → remainder of epoch has fixed (too-high) difficulty
  → blocks come slower → remaining miners earn more per real-time hash
  More importantly: at next epoch boundary, difficulty corrects only partially
  (weighted average of 8 epochs, so new data diluted by 7 old epochs)
  → profitable window can persist for MULTIPLE EPOCHS (days)

STRATEGY:
  1. Monitor Ergo epoch block times
  2. When avg block time > 1.2× target → hashrate dropped → join Ergo
  3. Mine for 1-3 epochs before difficulty catches up
  4. Exit when avg block time returns to normal
"""

import time, json
import numpy as np
from scipy import stats
from scipy.signal import correlate
from datetime import datetime, timezone
import urllib.request

ERG_API       = 'https://api.ergoplatform.com/api/v1'
TARGET_BT     = 120    # seconds (2 min target)
EPOCH_SIZE    = 1024   # blocks per epoch
ERG_REWARD    = 6.0    # ERG per block (current)
N_FETCH_BLOCKS = 8000  # ~8 epochs of recent data
DAILY_SAMPLES  = 365   # days for long-term correlation

def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries-1: raise
            time.sleep(1)

print("=" * 70)
print("  Ergo (AUTOLYKOS) Difficulty Lag & NiceHash Arbitrage Analysis")
print("=" * 70)
print()

# ── 1. Fetch recent blocks ──────────────────────────────────────────────────���─
print("  [1/5] Fetching %d recent Ergo blocks ..." % N_FETCH_BLOCKS)
blocks = []
offset = 0
limit  = 500
t0 = time.perf_counter()

while len(blocks) < N_FETCH_BLOCKS:
    url = '%s/blocks?limit=%d&sortBy=height&sortDirection=desc&offset=%d' % (ERG_API, limit, offset)
    d   = fetch(url)
    items = d['items']
    if not items:
        break
    blocks.extend(items)
    offset += limit
    if len(blocks) % 2000 == 0:
        print("    ... %d/%d  (%.0fs)" % (len(blocks), N_FETCH_BLOCKS, time.perf_counter()-t0))
    if len(blocks) >= N_FETCH_BLOCKS:
        break

blocks = blocks[:N_FETCH_BLOCKS]
blocks.sort(key=lambda x: x['height'])
print("    Got %d blocks in %.0fs" % (len(blocks), time.perf_counter()-t0))

# Extract arrays (timestamps in ms → seconds)
heights    = np.array([b['height']        for b in blocks])
timestamps = np.array([b['timestamp']/1000 for b in blocks])
diffs      = np.array([b['difficulty']    for b in blocks], dtype=np.float64)
epochs     = np.array([b['epoch']         for b in blocks])

# Block times
block_times = np.diff(timestamps)
mid_h       = heights[1:]
mid_epoch   = epochs[1:]
mid_diff    = diffs[1:]

# Remove outliers
valid      = (block_times > 0) & (block_times < 1200)
bt         = block_times[valid]
bh         = mid_h[valid]
be         = mid_epoch[valid]
bd         = mid_diff[valid]

print()

# ── 2. Per-epoch statistics ───────────────────────────────────────────────────
print("=" * 70)
print("  ANALYSIS A: Per-epoch block time analysis")
print("=" * 70)
print()
print("  Ergo adjusts difficulty every 1024 blocks (~34h).")
print("  Within an epoch difficulty is FIXED — hashrate changes create lag.")
print()

unique_epochs = sorted(set(be))
epoch_stats = {}

for ep in unique_epochs:
    mask  = be == ep
    ep_bt = bt[mask]
    if len(ep_bt) < 50:
        continue
    avg_bt    = ep_bt.mean()
    speed_fac = TARGET_BT / avg_bt          # >1 = faster = more profitable
    n_blocks  = len(ep_bt)
    start_h   = bh[mask].min()
    epoch_stats[ep] = {
        'avg_bt': avg_bt, 'speed': speed_fac,
        'n': n_blocks, 'start_h': start_h,
    }

ep_list   = sorted(epoch_stats.keys())
speeds    = np.array([epoch_stats[e]['speed'] for e in ep_list])
avg_bts   = np.array([epoch_stats[e]['avg_bt'] for e in ep_list])

print("  Block time per epoch (last %d epochs):" % len(ep_list))
print("    Mean speed factor : %.3f×" % speeds.mean())
print("    Std               : %.3f×" % speeds.std())
print("    Min (slowest)     : %.3f×  (epoch %d)" % (speeds.min(), ep_list[np.argmin(speeds)]))
print("    Max (fastest)     : %.3f×  (epoch %d)" % (speeds.max(), ep_list[np.argmax(speeds)]))
print("    CV                : %.1f%%" % (speeds.std()/speeds.mean()*100))
print()

# Epochs significantly above/below normal
fast_thresh = 1.15   # 15% faster → profitable window
slow_thresh = 0.85   # 15% slower → difficulty too high
fast_epochs = [(e, epoch_stats[e]) for e in ep_list if epoch_stats[e]['speed'] > fast_thresh]
slow_epochs = [(e, epoch_stats[e]) for e in ep_list if epoch_stats[e]['speed'] < slow_thresh]

print("  Fast epochs (>1.15×, profitable to join): %d/%d (%.0f%%)" % (
    len(fast_epochs), len(ep_list), len(fast_epochs)/len(ep_list)*100))
print("  Slow epochs (<0.85×, unprofitable):       %d/%d (%.0f%%)" % (
    len(slow_epochs), len(ep_list), len(slow_epochs)/len(ep_list)*100))
print()

# ── 3. Hashrate shock detection & lag measurement ────────────────────────────
print("=" * 70)
print("  ANALYSIS B: Hashrate shock events & difficulty lag")
print("=" * 70)
print()

# Detect shocks: speed_factor changes by >25% between consecutive epochs
shocks = []
for i in range(1, len(ep_list)):
    prev_s = epoch_stats[ep_list[i-1]]['speed']
    curr_s = epoch_stats[ep_list[i]]['speed']
    change = (curr_s - prev_s) / prev_s
    if abs(change) > 0.20:
        shocks.append({
            'epoch': ep_list[i],
            'change': change,
            'before': prev_s,
            'after': curr_s,
        })

print("  Hashrate shock events (>20%% change between epochs): %d" % len(shocks))
print()

if shocks:
    print("  %-8s  %-10s  %-10s  %-10s  Type" % ("Epoch", "Change", "Before", "After"))
    print("  " + "-"*50)
    for s in shocks[:15]:
        direction = "SPIKE ^" if s['change'] > 0 else "DROP  v"
        print("  %-8d  %+9.1f%%  %10.3f×  %10.3f×  %s" % (
            s['epoch'], s['change']*100, s['before'], s['after'], direction))
    print()

    # For each drop: measure how many SUBSEQUENT epochs remain fast
    drops = [s for s in shocks if s['change'] < -0.20]
    print("  Lag analysis for drops >=20%%:")
    print()
    lag_data = []
    for drop in drops[:8]:
        ep_idx = ep_list.index(drop['epoch'])
        # Collect speed factors for next 8 epochs
        future_speeds = []
        for j in range(0, min(10, len(ep_list)-ep_idx)):
            future_speeds.append(epoch_stats[ep_list[ep_idx+j]]['speed'])
        future_speeds = np.array(future_speeds)

        # How many epochs stay above 1.10× (profitable)?
        profitable_epochs = (future_speeds > 1.10).sum()
        recovery_epoch = next((j for j, s in enumerate(future_speeds) if s < 1.05), len(future_speeds))

        lag_data.append({
            'epoch': drop['epoch'],
            'change': drop['change'],
            'profitable_epochs': profitable_epochs,
            'recovery_epochs': recovery_epoch,
            'peak_speed': future_speeds.max(),
            'avg_speed': future_speeds[:recovery_epoch].mean() if recovery_epoch > 0 else 1.0,
        })

        print("  Drop at epoch %d (%+.0f%%):" % (drop['epoch'], drop['change']*100))
        print("    Speed per epoch: " + "  ".join("%.2fx" % s for s in future_speeds[:8]))
        print("    Profitable epochs (>1.10x): %d  |  Recovery: %d epochs (~%.0fh)" % (
            profitable_epochs, recovery_epoch, recovery_epoch * EPOCH_SIZE * TARGET_BT / 3600))
        print()

# ── 4. NiceHash price vs actual Ergo profitability ───────────────────────────
print("=" * 70)
print("  ANALYSIS C: NiceHash AUTOLYKOS price vs actual ERG profitability")
print("=" * 70)
print()

print("  [2/5] Loading NiceHash AUTOLYKOS history ...")
nh_data  = fetch('https://api2.nicehash.com/main/api/v2/public/algo/history?algorithm=AUTOLYKOS')
nh_ts    = np.array([r[0] for r in nh_data])
nh_price = np.array([r[2] for r in nh_data])   # BTC / (marketFactor H/s) / day
nh_hr    = np.array([r[1] for r in nh_data])
print("    %d days: %s → %s" % (
    len(nh_ts),
    datetime.utcfromtimestamp(nh_ts[0]).strftime('%Y-%m-%d'),
    datetime.utcfromtimestamp(nh_ts[-1]).strftime('%Y-%m-%d')))

print("  [3/5] Fetching ERG/BTC price history ...")
erg_cg = fetch('https://api.coingecko.com/api/v3/coins/ergo/market_chart'
               '?vs_currency=usd&days=365&interval=daily')
btc_cg = fetch('https://api.coingecko.com/api/v3/coins/bitcoin/market_chart'
               '?vs_currency=usd&days=365&interval=daily')
erg_prices = {datetime.utcfromtimestamp(p[0]/1000).strftime('%Y-%m-%d'): p[1]
              for p in erg_cg['prices']}
btc_prices = {datetime.utcfromtimestamp(p[0]/1000).strftime('%Y-%m-%d'): p[1]
              for p in btc_cg['prices']}
print("    ERG: %d days  BTC: %d days" % (len(erg_prices), len(btc_prices)))

print("  [4/5] Sampling per-epoch Ergo difficulty (last %d days) ..." % DAILY_SAMPLES)
tip_height = blocks[-1]['height']
# Sample every epoch boundary (one per ~34h epoch) going back DAILY_SAMPLES days
# Use toHeight + desc to reliably get a block at or before the target height
EPOCHS_BACK = int(DAILY_SAMPLES * 86400 / TARGET_BT / EPOCH_SIZE) + 2
tip_epoch   = tip_height // EPOCH_SIZE
difficulties_daily = {}   # keyed by date string
t0 = time.perf_counter()
sampled = 0

for ep in range(max(0, tip_epoch - EPOCHS_BACK), tip_epoch + 1):
    target_h = ep * EPOCH_SIZE
    if target_h < 1:
        continue
    off = tip_height - target_h
    url = '%s/blocks?limit=1&sortBy=height&sortDirection=desc&offset=%d' % (ERG_API, off)
    b = fetch(url)
    if b and b.get('items'):
        blk  = b['items'][0]
        ts   = blk['timestamp'] / 1000
        diff = blk['difficulty']
        date = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
        difficulties_daily[date] = diff
        sampled += 1
    if sampled % 60 == 0 and sampled > 0:
        print("    ... %d/%d  (%.0fs)" % (sampled, EPOCHS_BACK, time.perf_counter()-t0))

print("    Got %d per-epoch difficulty points" % len(difficulties_daily))
print()

print("  [5/5] Computing arbitrage ratio ...")
dates = sorted(set(difficulties_daily) & set(erg_prices) & set(btc_prices))

actual_prof = []
nh_prof_usd = []
date_list   = []

# NiceHash AUTOLYKOS marketFactor — check the algo spec
# From mining algorithms API: AUTOLYKOS miningFactor/marketFactor
# Need to verify units — use same approach as ETC
# For now: profitability = ERG_REWARD * ERG_price * miner_hashrate * 86400 / difficulty
# For 1 GH/s = 1e9 H/s: P_USD = ERG_REWARD * ERG_price * 1e9 * 86400 / difficulty

for d in dates:
    diff    = difficulties_daily[d]
    erg_usd = erg_prices[d]
    btc_usd = btc_prices[d]

    # Blocks per day at 1 GH/s = 86400 * 1e9 / diff
    # Revenue (USD/GH/day) = blocks_per_day * ERG_REWARD * erg_usd
    p_actual_ghs = 86400 * 1e9 * ERG_REWARD * erg_usd / diff

    # NiceHash price: find closest day
    date_ts  = datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
    nh_idx   = np.argmin(np.abs(nh_ts - date_ts))
    p_nh_btc = nh_price[nh_idx]
    p_nh_usd = p_nh_btc * btc_usd  # same unit: BTC per (market unit) * USD/BTC

    actual_prof.append(p_actual_ghs)
    nh_prof_usd.append(p_nh_usd)
    date_list.append(d)

actual_prof = np.array(actual_prof)
nh_prof_usd = np.array(nh_prof_usd)

# Normalize to median to remove unit offset (compare RELATIVE movements)
actual_norm = actual_prof / np.median(actual_prof)
nh_norm     = nh_prof_usd / np.median(nh_prof_usd)
arb_ratio   = actual_norm / (nh_norm + 1e-9)

print("  Actual ERG profitability (normalized to median):")
print("    Mean=%.3f  Std=%.3f  CV=%.1f%%" % (
    actual_norm.mean(), actual_norm.std(), actual_norm.std()/actual_norm.mean()*100))
print("  NiceHash AUTOLYKOS price (normalized):")
print("    Mean=%.3f  Std=%.3f  CV=%.1f%%" % (
    nh_norm.mean(), nh_norm.std(), nh_norm.std()/nh_norm.mean()*100))
print()

r, p = stats.pearsonr(actual_norm, nh_norm)
print("  Pearson correlation: r=%.4f  p=%.4f" % (r, p))
print()

# ── Cross-correlation ─────────────────────────────────────────────────────────
print("=" * 70)
print("  ANALYSIS D: Cross-correlation — NiceHash lag behind actual ERG profit")
print("=" * 70)
print()

xcorr = correlate(nh_norm, actual_norm, mode='full')
lags  = np.arange(-(len(actual_norm)-1), len(actual_norm))
xcorr_norm = xcorr / len(actual_norm)

best_lag = lags[np.argmax(xcorr_norm)]
best_corr = xcorr_norm[np.argmax(xcorr_norm)]

print("  Cross-correlation (positive lag = NiceHash follows ERG with delay):")
print("  %-12s  %-12s  %s" % ("Lag (days)", "Correlation", ""))
print("  " + "-"*40)
for lag in [0, 1, 2, 3, 5, 7, 10, 14]:
    idx = np.where(lags == lag)[0]
    if len(idx):
        c = xcorr_norm[idx[0]]
        marker = " <-- best" if lag == best_lag else ""
        print("  %-12d  %-12.4f%s" % (lag, c, marker))

print()
print("  Best lag: %d day(s)  (NiceHash adjusts to ERG profitability with ~%d day delay)" % (
    best_lag, best_lag))
print()

# ── Arbitrage spike events ────────────────────────────────────────────────────
print("=" * 70)
print("  ANALYSIS E: Historical arbitrage windows")
print("=" * 70)
print()
print("  'Arbitrage window' = actual ERG profit > 1.3× NiceHash price")
print("  (i.e., you earn 30%% more mining ERG than NiceHash rental costs)")
print()

threshold = 1.3
in_spike  = False
spike_start = 0
spikes    = []

for i, (ratio, date) in enumerate(zip(arb_ratio, date_list)):
    if ratio > threshold and not in_spike:
        in_spike    = True
        spike_start = i
    elif ratio <= threshold and in_spike:
        in_spike = False
        dur      = i - spike_start
        peak     = arb_ratio[spike_start:i].max()
        avg_r    = arb_ratio[spike_start:i].mean()
        spikes.append({'start': date_list[spike_start], 'dur': dur,
                        'peak': peak, 'avg': avg_r})

if in_spike:
    dur  = len(arb_ratio) - spike_start
    peak = arb_ratio[spike_start:].max()
    avg_r= arb_ratio[spike_start:].mean()
    spikes.append({'start': date_list[spike_start], 'dur': dur,
                    'peak': peak, 'avg': avg_r, 'ongoing': True})

print("  %-12s  %-8s  %-10s  %-10s" % ("Start date", "Days", "Avg ratio", "Peak ratio"))
print("  " + "-"*46)
for s in spikes:
    flag = " (ongoing)" if s.get('ongoing') else ""
    print("  %-12s  %-8d  %-10.2f×  %-10.2f×%s" % (
        s['start'], s['dur'], s['avg'], s['peak'], flag))

print()
n_spike_days = sum(s['dur'] for s in spikes)
print("  Total spike days: %d / %d  (%.1f%% of time)" % (
    n_spike_days, len(date_list), n_spike_days/len(date_list)*100))
if spikes:
    print("  Avg spike duration: %.1f days" % np.mean([s['dur'] for s in spikes]))
    print("  Avg peak ratio:     %.2f×" % np.mean([s['peak'] for s in spikes]))
print()

# ── Current state ─────────────────────────────────────────────────────────────
print("=" * 70)
print("  CURRENT STATE & STRATEGY")
print("=" * 70)
print()

# Current Ergo stats
tip_diff  = blocks[-1]['difficulty']
tip_ts    = blocks[-1]['timestamp']/1000
erg_now   = list(erg_prices.values())[-1]
btc_now   = list(btc_prices.values())[-1]

net_hr_ghs  = tip_diff / TARGET_BT / 1e9
p_act_now   = 86400 * 1e9 * ERG_REWARD * erg_now / tip_diff
nh_price_now = nh_price[-1]
nh_usd_now   = nh_price_now * btc_now

print("  Ergo network:    %.0f GH/s  |  diff=%.3e  |  ERG=$%.2f" % (
    net_hr_ghs, tip_diff, erg_now))
print("  NiceHash supply: %.0f GH/s  (%.1f%% of Ergo network)" % (
    nh_hr[-1]/1e9, nh_hr[-1]/1e9/net_hr_ghs*100 if net_hr_ghs > 0 else 0))
print()
print("  Actual ERG profit  : $%.4f / GH/day" % p_act_now)
print("  NiceHash AUTOLYKOS : $%.4f / GH/day  (%.4e BTC/GH/day × $%.0f)" % (
    nh_usd_now, nh_price_now, btc_now))
print()

if nh_usd_now > 0:
    ratio_now = p_act_now / nh_usd_now
    print("  Current arb ratio: %.2f×" % ratio_now)
    if ratio_now > 1.3:
        print("  ARBITRAGE WINDOW OPEN NOW! Rent AUTOLYKOS, mine ERG.")
    elif ratio_now > 1.0:
        print("  Slight edge (%.2f×) — marginal, not worth transaction costs." % ratio_now)
    else:
        print("  No arbitrage — NiceHash rental costs more than ERG mining earns.")

print()
print("  If NiceHash lags by %d day(s) as measured:" % best_lag)
print("  → You have a %d-day window to exploit each ERG profitability spike" % best_lag)
print("  → Optimal: monitor daily ERG profitability (RPC + price feed)")
print("  → Trigger: actual_profit > NiceHash_price × 1.3")
print("  → Action: buy AUTOLYKOS hashrate on NiceHash, point to ERG pool")
print("  → Exit: when actual_profit drops back to NiceHash_price × 1.1")
