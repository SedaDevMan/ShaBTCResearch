#!/usr/bin/env python3
"""
mining_score.py — Multi-coin PoW profitability variance scanner

Core idea: score(coin, time) = price(t) × block_reward / difficulty(t)
           = USD revenue per unit of work done

If this score has HIGH variance, you can wait for spikes and mine only then.
Result: capture most revenue while doing fraction of total work.

Data sources:
  Prices:     CoinGecko (90-day daily history, free)
  Difficulty: blockchain.info (BTC), blockchair.com (multi-coin)
  Fallback:   estimate difficulty ∝ hashrate ∝ price (lagged proxy)
"""

import requests, json, os, time, math, sys
import numpy as np
from scipy import stats
from datetime import datetime, timedelta, timezone

CACHE_DIR = "/tmp/mscore_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_TTL = 3600  # 1 hour

# ── Coins to analyze ─────────────────────────────────────────────────────
# block_reward in coin units, block_time_s = target seconds/block
COINS = {
    "BTC":  {"name": "Bitcoin",          "cg": "bitcoin",          "bc": "bitcoin",          "reward": 3.125,   "block_s": 600,  "diff_unit": 1e12},
    "LTC":  {"name": "Litecoin",         "cg": "litecoin",         "bc": "litecoin",         "reward": 6.25,    "block_s": 150,  "diff_unit": 1e6},
    "XMR":  {"name": "Monero",           "cg": "monero",           "bc": "monero",           "reward": 0.60,    "block_s": 120,  "diff_unit": 1e11},
    "ZEC":  {"name": "Zcash",            "cg": "zcash",            "bc": "zcash",            "reward": 1.5625,  "block_s": 75,   "diff_unit": 1e9},
    "ETC":  {"name": "Ethereum Classic", "cg": "ethereum-classic", "bc": "ethereum-classic", "reward": 2.56,    "block_s": 13,   "diff_unit": 1e15},
    "RVN":  {"name": "Ravencoin",        "cg": "ravencoin",        "bc": "ravencoin",        "reward": 2500,    "block_s": 60,   "diff_unit": 1e9},
    "DOGE": {"name": "Dogecoin",         "cg": "dogecoin",         "bc": "dogecoin",         "reward": 10000,   "block_s": 60,   "diff_unit": 1e6},
    "ERG":  {"name": "Ergo",             "cg": "ergo",             "bc": None,               "reward": 3.0,     "block_s": 120,  "diff_unit": 1e15},
    "KAS":  {"name": "Kaspa",            "cg": "kaspa",            "bc": None,               "reward": 146,     "block_s": 1,    "diff_unit": 1e18},
    "FLUX": {"name": "Flux",             "cg": "zelcash",          "bc": None,               "reward": 37.5,    "block_s": 150,  "diff_unit": 1e6},
}

# ── Cache helpers ─────────────────────────────────────────────────────────
def cache_get(key):
    path = f"{CACHE_DIR}/{key}.json"
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < CACHE_TTL:
        try:
            with open(path) as f: return json.load(f)
        except: pass
    return None

def cache_set(key, data):
    with open(f"{CACHE_DIR}/{key}.json", "w") as f:
        json.dump(data, f)

def get(url, params=None, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": "mining-research/1.0"})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        pass
    return None

# ── Price history: CoinGecko ─────────────────────────────────────────────
def fetch_prices(cg_id, days=90):
    key = f"price_{cg_id}_{days}"
    c = cache_get(key)
    if c: return c
    print(f"    [API] CoinGecko prices: {cg_id} ...")
    data = get("https://api.coingecko.com/api/v3/coins/{}/market_chart".format(cg_id),
               {"vs_currency": "usd", "days": days, "interval": "daily"})
    if data and "prices" in data:
        result = [(int(p[0]/1000), float(p[1])) for p in data["prices"]]
        cache_set(key, result)
        return result
    return None

# ── Difficulty history ────────────────────────────────────────────────────
def fetch_difficulty_blockchain_info(timespan="90days"):
    """Bitcoin-only: blockchain.info difficulty chart."""
    key = f"diff_btc_{timespan}"
    c = cache_get(key)
    if c: return c
    print(f"    [API] blockchain.info difficulty ...")
    data = get("https://api.blockchain.info/charts/difficulty",
               {"timespan": timespan, "format": "json", "cors": "true"})
    if data and "values" in data:
        result = [(int(v["x"]), float(v["y"])) for v in data["values"]]
        cache_set(key, result)
        return result
    return None

def fetch_difficulty_blockchair(bc_id, days=90):
    """Blockchair: aggregated daily difficulty for multiple coins."""
    if not bc_id: return None
    key = f"diff_{bc_id}_{days}"
    c = cache_get(key)
    if c: return c
    print(f"    [API] blockchair difficulty: {bc_id} ...")
    # Get current stats first (always works)
    stats_data = get(f"https://api.blockchair.com/{bc_id}/stats")
    if not stats_data: return None
    s = stats_data.get("data", {})
    cur_diff = s.get("difficulty") or s.get("hashrate_mean")
    if not cur_diff: return None

    # Try to get historical block data (limited free tier)
    now_ts = int(time.time())
    start_ts = now_ts - days * 86400
    start_date = datetime.utcfromtimestamp(start_ts).strftime("%Y-%m-%d")
    end_date   = datetime.utcnow().strftime("%Y-%m-%d")

    hist = get(f"https://api.blockchair.com/{bc_id}/blocks",
               {"a": "date,avg(difficulty)", "q": f"time({start_date}..{end_date})",
                "limit": "100"})

    if hist and "data" in hist:
        rows = hist["data"]
        if rows:
            result = [(int(datetime.strptime(r["date"], "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp()),
                       float(r["avg(difficulty)"])) for r in rows if r.get("avg(difficulty)")]
            if result:
                cache_set(key, result)
                return result

    # Fallback: just current difficulty as a flat series
    result = [(now_ts - i * 86400, float(cur_diff)) for i in range(days, 0, -1)]
    cache_set(key, result)
    return result

def fetch_difficulty_xmr():
    """Monero: current network info from xmrchain."""
    key = "diff_xmr_current"
    c = cache_get(key)
    if c: return c
    print(f"    [API] Monero network info ...")
    data = get("https://xmrchain.net/api/networkinfo")
    if data and data.get("status") == "OK":
        diff = data["data"].get("difficulty")
        if diff:
            now_ts = int(time.time())
            result = [(now_ts - i * 86400, float(diff)) for i in range(90, 0, -1)]
            cache_set(key, result)
            return result
    return None

def fetch_difficulty(ticker, coin):
    """Route difficulty fetch to best available source."""
    if ticker == "BTC":
        return fetch_difficulty_blockchain_info()
    if ticker == "XMR":
        d = fetch_difficulty_xmr()
        if d: return d
    if coin["bc"]:
        return fetch_difficulty_blockchair(coin["bc"])
    return None

# ── Align price + difficulty to daily timestamps ─────────────────────────
def align_series(prices, difficulties):
    """
    Match price and difficulty by day.
    Returns aligned numpy arrays (timestamps, prices, difficulties).
    """
    # Build day→price dict
    price_by_day = {}
    for ts, p in prices:
        day = (ts // 86400) * 86400
        price_by_day[day] = p

    diff_by_day = {}
    for ts, d in difficulties:
        day = (ts // 86400) * 86400
        diff_by_day[day] = d

    # Intersect
    days = sorted(set(price_by_day) & set(diff_by_day))
    if len(days) < 14:
        return None, None, None

    ts_arr = np.array(days)
    p_arr  = np.array([price_by_day[d] for d in days])
    d_arr  = np.array([diff_by_day[d]  for d in days])
    return ts_arr, p_arr, d_arr

# ── Profitability score ───────────────────────────────────────────────────
def profitability_score(prices, difficulties, block_reward, block_time_s):
    """
    USD per unit_work per second = price × reward_per_second / difficulty
    Normalized: divide by mean so 1.0 = average day.
    """
    reward_per_s = block_reward / block_time_s
    raw = prices * reward_per_s / difficulties
    return raw / raw.mean()   # normalized: 1.0 = average

# ── Spike analysis ────────────────────────────────────────────────────────
def analyze_spikes(scores, label=""):
    """
    Find windows where score > threshold.
    Returns dict of stats.
    """
    n = len(scores)
    mean_s = scores.mean()
    std_s  = scores.std()
    z      = (scores - mean_s) / (std_s + 1e-12)

    results = {}
    for mult in [1.0, 1.5, 2.0, 3.0]:
        above = scores > mult
        n_days  = above.sum()
        rev_pct = scores[above].sum() / scores.sum() * 100 if scores.sum() > 0 else 0
        results[mult] = {"n_days": int(n_days), "rev_pct": float(rev_pct)}

    # Consecutive spike runs
    runs = []
    in_run = False
    run_len = 0
    for s in (scores > 1.5):
        if s:
            in_run = True; run_len += 1
        else:
            if in_run: runs.append(run_len)
            in_run = False; run_len = 0
    if in_run: runs.append(run_len)

    results["max_z"]     = float(z.max())
    results["std"]       = float(std_s)
    results["cv"]        = float(std_s / mean_s)   # coefficient of variation
    results["runs_1_5x"] = runs
    results["avg_run"]   = float(np.mean(runs)) if runs else 0.0
    results["n_days"]    = n
    return results

# ── Main ──────────────────────────────────────────────────────────────────
print("═"*65)
print("  Mining Profitability Variance Scanner — 90-day analysis")
print("═"*65)
print()

all_results = {}

for ticker, coin in COINS.items():
    print(f"  {ticker} ({coin['name']}) ...")

    prices_raw = fetch_prices(coin["cg"], days=90)
    if not prices_raw:
        print(f"    ✗ price data unavailable\n"); continue

    diffs_raw = fetch_difficulty(ticker, coin)
    if not diffs_raw:
        print(f"    ✗ difficulty data unavailable — using price proxy\n")
        # Proxy: assume difficulty ∝ price with a 7-day lag
        # This models "miners follow price, difficulty follows miners"
        ts_arr = np.array([t for t, p in prices_raw])
        p_arr  = np.array([p for t, p in prices_raw])
        # Lagged price as difficulty proxy (7-day rolling mean, shifted 7 days)
        if len(p_arr) > 14:
            lag = 7
            d_proxy = np.convolve(p_arr, np.ones(lag)/lag, mode='full')[:len(p_arr)]
            d_proxy[d_proxy < 1e-12] = p_arr.mean()
            ts_aligned, p_aligned, d_aligned = ts_arr, p_arr, d_proxy
        else:
            print(f"    ✗ not enough data\n"); continue
    else:
        ts_aligned, p_aligned, d_aligned = align_series(prices_raw, diffs_raw)
        if ts_aligned is None:
            print(f"    ✗ alignment failed (too few matching days)\n"); continue

    if d_aligned.std() < 1e-9:
        print(f"    ⚠  difficulty is flat (using current value only) — price proxy for variance")
        # Since difficulty is flat, score variance = price variance
        # This is still useful: shows revenue variance due to price alone

    scores = profitability_score(p_aligned, d_aligned, coin["reward"], coin["block_s"])
    sp = analyze_spikes(scores)
    all_results[ticker] = {"coin": coin["name"], "scores": scores.tolist(),
                           "timestamps": ts_aligned.tolist(), "spikes": sp,
                           "n_days": len(scores)}

    print(f"    Days: {len(scores)}  |  Score CV: {sp['cv']:.3f}"
          f"  |  Max spike: {sp['max_z']:.1f}σ")
    print(f"    Days score>1.5×avg: {sp[1.5]['n_days']} ({sp[1.5]['n_days']/len(scores)*100:.0f}%)"
          f"  contain {sp[1.5]['rev_pct']:.0f}% of revenue")
    print(f"    Days score>2.0×avg: {sp[2.0]['n_days']} ({sp[2.0]['n_days']/len(scores)*100:.0f}%)"
          f"  contain {sp[2.0]['rev_pct']:.0f}% of revenue")
    if sp["runs_1_5x"]:
        print(f"    Spike runs (>1.5×): avg {sp['avg_run']:.1f} days,  longest {max(sp['runs_1_5x'])} days")
    print()

    time.sleep(1.2)  # Rate limit: CoinGecko free tier = 10-30 req/min


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-COIN OPTIMAL SWITCHING ANALYSIS
# If you mine the BEST coin each day, how much better vs staying on one coin?
# ═══════════════════════════════════════════════════════════════════════════
print("═"*65)
print("  Optimal switching analysis (mine best coin each day)")
print("═"*65)

# Align all coins to same timestamps
all_ts = None
for t, v in all_results.items():
    ts = set(v["timestamps"])
    all_ts = ts if all_ts is None else all_ts & ts

if all_ts and len(all_ts) >= 14:
    common_ts = sorted(all_ts)
    n_days = len(common_ts)

    # Build score matrix [coins × days]
    tickers_avail = [t for t in all_results if t in COINS]
    score_matrix  = []
    for t in tickers_avail:
        ts_list  = all_results[t]["timestamps"]
        sc_list  = all_results[t]["scores"]
        ts_map   = dict(zip(ts_list, sc_list))
        row = [ts_map.get(ts, np.nan) for ts in common_ts]
        score_matrix.append(row)

    M = np.array(score_matrix)   # [n_coins × n_days]

    # Per-day best coin
    best_day_score = np.nanmax(M, axis=0)          # best score each day
    mean_day_score = np.nanmean(M, axis=0)         # average score each day
    best_coin_idx  = np.nanargmax(M, axis=0)       # which coin each day

    # How much better is optimal switching vs average?
    improvement = best_day_score / mean_day_score
    print(f"\n  Common days across {len(tickers_avail)} coins: {n_days}")
    print(f"  Switching to best coin daily:")
    print(f"    Mean daily improvement:  {improvement.mean():.2f}×")
    print(f"    Median daily improvement:{np.median(improvement):.2f}×")
    print(f"    Best single day:         {improvement.max():.2f}×")

    # Coin frequency as "best" coin
    from collections import Counter
    freq = Counter(tickers_avail[i] for i in best_coin_idx)
    print(f"\n  Days each coin was #1 (out of {n_days}):")
    for t, cnt in sorted(freq.items(), key=lambda x: -x[1]):
        print(f"    {t:>5}  {cnt:>3} days  ({cnt/n_days*100:.0f}%)")

    # Strategy: mine only top-K days per month
    print(f"\n  'Mine only the best N days per month' strategy:")
    print(f"  {'Days/month':>12}  {'Revenue captured':>18}  {'Efficiency':>12}")
    monthly = n_days / 3  # approximate months in data
    for days_mined in [5, 10, 15, 20, 25, 30]:
        # Top days_mined/month by score (across all coins)
        threshold = np.percentile(best_day_score, 100 * (1 - days_mined/30))
        top_mask  = best_day_score >= threshold
        rev_pct   = best_day_score[top_mask].sum() / best_day_score.sum() * 100
        efficiency = rev_pct / (top_mask.sum() / n_days * 100)
        print(f"  {days_mined:>12}  {rev_pct:>17.1f}%  {efficiency:>11.2f}×")
else:
    print("  Not enough common data for cross-coin analysis")
    print(f"  (Common timestamps: {len(all_ts) if all_ts else 0})")


# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*65}")
print("  SUMMARY — Profitability variance by coin")
print("═"*65)
print(f"  {'Coin':>5}  {'CV':>6}  {'MaxSpike':>9}  {'>1.5× days':>10}  {'Rev in spikes':>14}  Data")
print("  " + "-"*60)
for t, v in sorted(all_results.items(), key=lambda x: -x[1]["spikes"]["cv"]):
    sp = v["spikes"]
    pct_days = sp[1.5]["n_days"] / sp["n_days"] * 100
    print(f"  {t:>5}  {sp['cv']:>6.3f}  {sp['max_z']:>8.1f}σ  "
          f"{sp[1.5]['n_days']:>3}d ({pct_days:.0f}%)  "
          f"{sp[1.5]['rev_pct']:>13.1f}%  "
          f"{'real diff' if COINS[t]['bc'] else 'price proxy'}")

print()
print("  CV = coefficient of variation (std/mean). Higher = more volatile = more opportunity.")
print("  >1.5× days = days where profitability > 1.5× the 90-day average.")
print()

# Save results
summary = {t: {"name": v["coin"], "cv": v["spikes"]["cv"],
               "max_z": v["spikes"]["max_z"],
               "days_above_1_5x": v["spikes"][1.5]["n_days"],
               "rev_pct_in_spikes": v["spikes"][1.5]["rev_pct"],
               "n_days": v["spikes"]["n_days"]}
           for t, v in all_results.items()}
with open("mining_score_results.json", "w") as f:
    json.dump(summary, f, indent=2)
print("Results → mining_score_results.json")
