"""
nicehash_arb.py — NiceHash ETChash rental price vs ETC actual profitability

Measures the lag between:
  Signal A: ETC actual profitability (block_reward × ETC_price / difficulty)
  Signal B: NiceHash ETChash rental price

When A > B: renting hash on NiceHash to mine ETC is profitable.
The lag = how long after A spikes before B catches up = arbitrage window.
"""

import time, json
import numpy as np
from scipy import stats
from scipy.signal import correlate
from datetime import datetime, timedelta, timezone
import urllib.request

ETC_RPC       = 'https://etc.etcdesktop.com'
ETC_BLOCK_TIME = 13      # seconds
ETC_REWARD    = 2.048    # ETC per block (current era, blocks 20M-25M)
BLOCKS_PER_DAY = 86400 / ETC_BLOCK_TIME  # ~6646

def rpc(method, params):
    payload = json.dumps({'jsonrpc':'2.0','method':method,'params':params,'id':1}).encode()
    req = urllib.request.Request(ETC_RPC, data=payload,
                                  headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())['result']

def fetch_url(url):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

print("═"*70)
print("  NiceHash ETChash Arbitrage Analysis")
print("═"*70)
print()

# ── 1. NiceHash historical prices ─────────────────────────────────────────────
print("  [1/4] Loading NiceHash ETChash history ...")
nh_data = fetch_url('https://api2.nicehash.com/main/api/v2/public/algo/history?algorithm=ETCHASH')
# Format: [ts_unix, hashrate_H/s, price_BTC/TH/day, 0]
nh_ts    = np.array([r[0] for r in nh_data])
nh_price = np.array([r[2] for r in nh_data])  # BTC/TH/day
nh_hr    = np.array([r[1] for r in nh_data])   # H/s on NiceHash platform
print(f"     {len(nh_ts)} days  {datetime.utcfromtimestamp(nh_ts[0]).strftime('%Y-%m-%d')} "
      f"→ {datetime.utcfromtimestamp(nh_ts[-1]).strftime('%Y-%m-%d')}")

# ── 2. ETC and BTC price history (CoinGecko, last 365 days) ──────────────────
print("  [2/4] Fetching ETC/BTC price history (CoinGecko) ...")
etc_cg = fetch_url('https://api.coingecko.com/api/v3/coins/ethereum-classic/market_chart'
                   '?vs_currency=usd&days=365&interval=daily')
btc_cg = fetch_url('https://api.coingecko.com/api/v3/coins/bitcoin/market_chart'
                   '?vs_currency=usd&days=365&interval=daily')

# Convert to daily dicts keyed by date string
etc_prices = {datetime.utcfromtimestamp(p[0]/1000).strftime('%Y-%m-%d'): p[1]
              for p in etc_cg['prices']}
btc_prices = {datetime.utcfromtimestamp(p[0]/1000).strftime('%Y-%m-%d'): p[1]
              for p in btc_cg['prices']}
print(f"     ETC: {len(etc_prices)} days  BTC: {len(btc_prices)} days")

# ── 3. ETC daily difficulty (sample 1 block per day for past 365 days) ────────
print("  [3/4] Fetching ETC daily difficulty (sampling ~1 block/day) ...")
tip_hex    = rpc('eth_blockNumber', [])
tip_height = int(tip_hex, 16)
N_DAYS     = 365

difficulties = {}
t0 = time.perf_counter()
for day_offset in range(N_DAYS, 0, -1):
    target_height = tip_height - int(day_offset * BLOCKS_PER_DAY)
    if target_height < 0:
        continue
    b = rpc('eth_getBlockByNumber', [hex(target_height), False])
    if b:
        ts   = int(b['timestamp'], 16)
        diff = int(b['difficulty'], 16)
        date = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
        difficulties[date] = diff
    if day_offset % 50 == 0:
        elapsed = time.perf_counter() - t0
        print(f"     ... {N_DAYS - day_offset}/{N_DAYS}  ({elapsed:.0f}s)")

print(f"     Got {len(difficulties)} daily difficulty points in {time.perf_counter()-t0:.0f}s")

# ── 4. Build aligned daily series ─────────────────────────────────────────────
print("  [4/4] Aligning data series ...")

dates_common = sorted(set(difficulties.keys()) & set(etc_prices.keys()) & set(btc_prices.keys()))
print(f"     {len(dates_common)} days of aligned data")
print()

# Compute actual ETC profitability per TH/s per day (USD)
# P_actual = block_reward * ETC_USD * (1TH/s) * 86400 / difficulty
#   where difficulty is in H (so 1TH/s = 1e12 H/s)
# P_actual(USD/TH/day) = ETC_REWARD * ETC_price_USD * 1e12 * 86400 / difficulty / 86400
#                      = ETC_REWARD * ETC_price_USD * 1e12 / difficulty
# Wait: blocks_found_per_day_at_1TH = 86400 / block_time * (1e12 / network_hashrate)
#   network_hashrate ≈ difficulty / block_time (approximately)
#   blocks_per_day_at_1TH = 86400 / block_time * 1e12 * block_time / difficulty
#                         = 86400 * 1e12 / difficulty
# Revenue = blocks_per_day_at_1TH * ETC_REWARD * ETC_price_USD
#         = 86400 * 1e12 * ETC_REWARD * ETC_price_USD / difficulty

actual_prof = []   # USD/TH/day
nh_prof_usd = []   # USD/TH/day (NiceHash rental price converted)
date_list   = []

for d in dates_common:
    diff     = difficulties[d]
    etc_usd  = etc_prices[d]
    btc_usd  = btc_prices[d]

    p_actual = 86400 * 1e12 * ETC_REWARD * etc_usd / diff  # USD/TH/day
    # NiceHash price: find closest NiceHash day
    date_ts  = datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
    nh_idx   = np.argmin(np.abs(nh_ts - date_ts))
    p_nh_btc = nh_price[nh_idx]                           # BTC/TH/day
    p_nh_usd = p_nh_btc * btc_usd                         # USD/TH/day

    actual_prof.append(p_actual)
    nh_prof_usd.append(p_nh_usd)
    date_list.append(d)

actual_prof = np.array(actual_prof)
nh_prof_usd = np.array(nh_prof_usd)
arb_ratio   = actual_prof / (nh_prof_usd + 1e-12)  # actual / NiceHash price

# ── ANALYSIS A: Basic correlation ─────────────────────────────────────────────
print("═"*70)
print("  ANALYSIS A: Actual ETC profitability vs NiceHash price")
print("═"*70)
print()
print(f"  {'Date range':20s}: {date_list[0]} → {date_list[-1]}")
print(f"  {'ETC profitability':20s}: mean=${actual_prof.mean():.4f}  std=${actual_prof.std():.4f}")
print(f"  {'NiceHash price':20s}: mean=${nh_prof_usd.mean():.4f}  std=${nh_prof_usd.std():.4f}")
print()

r, p = stats.pearsonr(actual_prof, nh_prof_usd)
print(f"  Pearson correlation: r={r:.4f}  p={p:.4f}")
print()

# Arbitrage ratio distribution
print(f"  Arbitrage ratio (actual / NiceHash):")
print(f"    Mean   = {arb_ratio.mean():.3f}×")
print(f"    Std    = {arb_ratio.std():.3f}×")
print(f"    Min    = {arb_ratio.min():.3f}×  (NiceHash overpriced)")
print(f"    Max    = {arb_ratio.max():.3f}×  (NiceHash underpriced = ARBITRAGE)")
print()

# Days where arbitrage is significant
for threshold in [1.2, 1.5, 2.0, 3.0]:
    n_days = (arb_ratio > threshold).sum()
    pct    = n_days / len(arb_ratio) * 100
    if n_days > 0:
        # Avg excess profit during those days
        excess = actual_prof[arb_ratio > threshold] - nh_prof_usd[arb_ratio > threshold]
        print(f"  Days with ratio > {threshold:.1f}×:  {n_days:3d} days ({pct:.1f}%)  "
              f"avg excess profit: ${excess.mean():.4f}/TH/day")

print()

# ── ANALYSIS B: Cross-correlation (lag measurement) ──────────────────────────
print("═"*70)
print("  ANALYSIS B: Cross-correlation — does NiceHash price lag ETC profitability?")
print("═"*70)
print()

# Normalize both to z-scores before cross-correlation
a_norm = (actual_prof - actual_prof.mean()) / actual_prof.std()
n_norm = (nh_prof_usd - nh_prof_usd.mean()) / nh_prof_usd.std()

xcorr = correlate(n_norm, a_norm, mode='full')
lags  = np.arange(-(len(a_norm)-1), len(a_norm))
xcorr_norm = xcorr / len(a_norm)

# Find the lag with maximum cross-correlation
# Positive lag = NiceHash price follows ETC profitability with that many days delay
best_lag_idx = np.argmax(xcorr_norm)
best_lag     = lags[best_lag_idx]
best_corr    = xcorr_norm[best_lag_idx]

print(f"  Best lag: {best_lag} days  (correlation = {best_corr:.4f})")
print(f"  Interpretation: NiceHash price lags ETC profitability by {best_lag} day(s)")
print()

# Show cross-correlation at lag 0, 1, 2, 3, 5 days
print(f"  {'Lag (days)':>12}  {'Correlation':>12}  Meaning")
print("  " + "-"*50)
for lag in [0, 1, 2, 3, 5, 7, 14]:
    idx = np.where(lags == lag)[0]
    if len(idx):
        c = xcorr_norm[idx[0]]
        meaning = ""
        if lag == 0:   meaning = "simultaneous adjustment"
        elif lag == 1: meaning = "1-day lag → buy today, profit tomorrow"
        elif lag == 2: meaning = "2-day lag"
        print(f"  {lag:>12}  {c:>12.4f}  {meaning}")
print()

# ── ANALYSIS C: Spike detection in historical data ────────────────────────────
print("═"*70)
print("  ANALYSIS C: Historical arbitrage spike events")
print("═"*70)
print()

# Find periods where arb_ratio stayed > 1.5 for ≥2 consecutive days
print(f"  {'Date':12s}  {'Duration':>8}  {'Avg arb ratio':>14}  "
      f"{'Profit/TH excess':>18}  Peak ratio")
print("  " + "-"*68)

in_spike = False
spike_start = 0
spike_days = []
total_arb_profit = 0.0

for i, (ratio, date) in enumerate(zip(arb_ratio, date_list)):
    if ratio > 1.5 and not in_spike:
        in_spike = True
        spike_start = i
    elif ratio <= 1.5 and in_spike:
        in_spike = False
        spike_days_range = list(range(spike_start, i))
        if len(spike_days_range) >= 1:
            dur     = len(spike_days_range)
            avg_r   = arb_ratio[spike_days_range].mean()
            peak_r  = arb_ratio[spike_days_range].max()
            excess  = (actual_prof[spike_days_range] - nh_prof_usd[spike_days_range]).sum()
            total_arb_profit += excess
            spike_days.append({'start': date_list[spike_start], 'dur': dur,
                                'avg_r': avg_r, 'peak_r': peak_r, 'excess': excess})
            print(f"  {date_list[spike_start]:12s}  {dur:>6}d  {avg_r:>14.2f}×  "
                  f"${excess:>16.4f}/TH  {peak_r:.2f}×")

if in_spike:
    spike_days_range = list(range(spike_start, len(arb_ratio)))
    if spike_days_range:
        dur    = len(spike_days_range)
        avg_r  = arb_ratio[spike_days_range].mean()
        peak_r = arb_ratio[spike_days_range].max()
        excess = (actual_prof[spike_days_range] - nh_prof_usd[spike_days_range]).sum()
        total_arb_profit += excess
        print(f"  {date_list[spike_start]:12s}  {dur:>6}d  {avg_r:>14.2f}×  "
              f"${excess:>16.4f}/TH  {peak_r:.2f}×  (ongoing)")

print()
print(f"  Total arb profit over {len(date_list)} days: ${total_arb_profit:.4f}/TH/day cumulative")
n_spike_days = sum(d['dur'] for d in spike_days)
print(f"  Spike days total: {n_spike_days}/{len(date_list)}  ({n_spike_days/len(date_list)*100:.1f}% of time)")

if spike_days:
    avg_dur  = np.mean([d['dur'] for d in spike_days])
    avg_peak = np.mean([d['peak_r'] for d in spike_days])
    print(f"  Avg spike duration: {avg_dur:.1f} days")
    print(f"  Avg peak ratio:     {avg_peak:.2f}×")
print()

# ── ANALYSIS D: Strategy value ─────────────────────────────────────────────────
print("═"*70)
print("  ANALYSIS D: Rental arbitrage strategy value")
print("═"*70)
print()
print("  Strategy: rent X TH/s on NiceHash when arb_ratio > 1.5")
print("  Cost:     X × NiceHash_price USD/day")
print("  Revenue:  X × actual_ETC_profitability USD/day")
print("  Profit:   X × (actual - NiceHash_price) USD/day")
print()

# For each 1 TH/s rented during spike days:
if spike_days:
    best_spike = max(spike_days, key=lambda x: x['excess'])
    print(f"  Best historical spike: {best_spike['start']}  "
          f"{best_spike['dur']} days  {best_spike['peak_r']:.2f}× peak")
    print(f"    Profit per TH/s rented over {best_spike['dur']} days: ${best_spike['excess']:.4f}")
    print(f"    For 100 TH/s rented: ${best_spike['excess']*100:.2f}")
    print(f"    For 1 PH/s rented:   ${best_spike['excess']*1000:.2f}")
    print()

# Current state
print(f"  Current state ({date_list[-1]}):")
print(f"    ETC actual profitability: ${actual_prof[-1]:.5f}/TH/day")
print(f"    NiceHash rental price:    ${nh_prof_usd[-1]:.5f}/TH/day")
print(f"    Current arb ratio:        {arb_ratio[-1]:.3f}×")
if arb_ratio[-1] > 1.2:
    print(f"    ► ARBITRAGE WINDOW OPEN NOW!")
else:
    print(f"    ► Market at equilibrium (no current arbitrage)")
print()

# ── ANALYSIS E: Real-time monitor value ───────────────────────────────────────
print("═"*70)
print("  ANALYSIS E: What a live monitor would be worth")
print("═"*70)
print()
print("  The daily data above shows multi-day windows.")
print("  The intra-day analysis (from etc_difficulty.py) showed:")
print("    - 7.2h fast-block window (2.5× speed) within a single day")
print("    - NiceHash daily price would show this as a partial-day spike")
print()
print("  A LIVE monitor (polling every 30s) captures intra-day spikes")
print("  that daily data misses. Estimated extra capture: 3-5× vs daily data.")
print()

if spike_days:
    daily_profit_per_th = total_arb_profit / len(date_list)
    live_est = daily_profit_per_th * 4  # 4× capture vs daily
    print(f"  Daily data avg arb profit: ${daily_profit_per_th:.5f}/TH/day")
    print(f"  Live monitor estimate:     ${live_est:.5f}/TH/day")
    print()
    for scale_th in [1, 100, 1000, 10000]:
        annual = live_est * 365 * scale_th
        print(f"  {scale_th:>6} TH/s rented annually: ~${annual:.2f}/year in arb profit")
