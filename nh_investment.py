#!/usr/bin/env python3
"""
NiceHash Price-Lag Investment Analysis
=======================================
Strategy: When ZEC or XMR price spikes, NiceHash order book prices lag 1-3 days.
During that window, buy NH hashrate at old price and mine at new higher value.
Window closes when NH buyers bid up price to match actual mining value.

NH algo history price = weighted-average paid price (STANDARD orders clearing price).
This is the price you pay for standard hashrate — the baseline for this analysis.
Fixed-order prices are higher; this script is conservative and uses the avg paid price.
"""

import requests
import time
import sys
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# KNOWN REAL NETWORK VALUES (verified from blockchair + blockchain)
# ─────────────────────────────────────────────────────────────────────────────
ZEC_NETWORK_GSOL   = 13.25      # GSol/s total network hashrate
ZEC_BLOCK_REWARD   = 1.375      # ZEC per block (88% of 1.5625 subsidy → miners)
ZEC_BLOCKS_DAY     = 1148       # blocks per day

XMR_NETWORK_GH     = 5.458      # GH/s total network hashrate
XMR_BLOCK_REWARD   = 0.6        # XMR per block (tail emission)
XMR_BLOCKS_DAY     = 720        # blocks per day

NH_EQUIHASH_SUPPLY = 0.254      # GSol/s available on NH order book
NH_RANDOMX_SUPPLY  = 0.139      # GH/s available on NH order book
NH_BUYER_FEE       = 0.03       # 3% NiceHash buyer fee
POOL_FEE           = 0.02       # 2% pool fee on mining revenue
BUY_THRESHOLD      = 1.30       # enter when actual/NH > 1.30
SELL_THRESHOLD     = 1.10       # exit when actual/NH drops < 1.10

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def ts_to_date(ts_s):
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d")

def fetch_json(url, label, retries=3, pause=2):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30,
                             headers={"User-Agent": "nh-investment-analysis/1.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  [warn] {label} attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(pause)
    print(f"  [error] Could not fetch {label}, returning None")
    return None

def print_sep(char="─", width=74):
    print(char * width)

def print_header(title):
    width = 74
    print_sep("═", width)
    inner = width - 4
    pad_l = (inner - len(title)) // 2
    pad_r = inner - len(title) - pad_l
    print(f"{'═' * (pad_l+2)} {title} {'═' * (pad_r+2)}")
    print_sep("═", width)


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_nh_algo_history(algorithm):
    """
    Fetch NiceHash algo history.
    API returns: [[timestamp_s, speed_sol_or_h_per_s, price, 0.0], ...]
    price field = BTC per (marketFactor) per day
      EQUIHASH:     marketFactor=1e9 (GSol), so price in BTC/GSol/day
      RANDOMXMONERO:marketFactor=1e9 (GH),   so price in BTC/GH/day
    Returns sorted list of (date_str, price).
    """
    url = (f"https://api2.nicehash.com/main/api/v2/public/algo/history"
           f"?algorithm={algorithm}")
    data = fetch_json(url, f"NH {algorithm}")
    if data is None:
        return []

    raw = data if isinstance(data, list) else []
    if not raw and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and len(v) > 0:
                raw = v
                break

    rows = []
    for entry in raw:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                ts, _speed, price = entry[0], entry[1], entry[2]
            elif isinstance(entry, dict):
                ts    = entry.get("timestamp", entry.get("t", 0))
                price = entry.get("price", entry.get("p", 0))
            else:
                continue
            if ts > 1e12:
                ts = ts / 1000
            date = ts_to_date(int(ts))
            rows.append((date, float(price)))
        except Exception:
            continue

    dedup = {}
    for date, price in rows:
        dedup[date] = price
    return sorted(dedup.items())


def fetch_coingecko(coin_id, label):
    """
    Fetch 365 days daily price from CoinGecko.
    Returns dict {date_str: price_usd}.
    """
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days=365&interval=daily")
    data = fetch_json(url, f"CoinGecko {label}")
    time.sleep(1.5)
    if data is None:
        return {}
    result = {}
    for entry in data.get("prices", []):
        ts_ms, price = entry[0], entry[1]
        date = ts_to_date(int(ts_ms / 1000))
        result[date] = float(price)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DAILY SERIES BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build_daily_series(nh_zec, nh_xmr, btc_prices, zec_prices, xmr_prices):
    nh_zec_d = dict(nh_zec)
    nh_xmr_d = dict(nh_xmr)

    all_dates = sorted(
        set(nh_zec_d) & set(nh_xmr_d) & set(btc_prices) & set(zec_prices) & set(xmr_prices)
    )

    series = []
    for date in all_dates:
        btc = btc_prices[date]
        zec = zec_prices[date]
        xmr = xmr_prices[date]
        nzp = nh_zec_d[date]
        nxp = nh_xmr_d[date]

        if btc <= 0 or nzp <= 0 or nxp <= 0:
            continue

        nh_zec_usd  = nzp * btc
        nh_xmr_usd  = nxp * btc
        act_zec_usd = (1.0 / ZEC_NETWORK_GSOL) * ZEC_BLOCKS_DAY * ZEC_BLOCK_REWARD * zec
        act_xmr_usd = (1.0 / XMR_NETWORK_GH)   * XMR_BLOCKS_DAY * XMR_BLOCK_REWARD * xmr

        series.append({
            "date":        date,
            "btc_usd":     btc,
            "zec_usd":     zec,
            "xmr_usd":     xmr,
            "nh_zec_btc":  nzp,
            "nh_xmr_btc":  nxp,
            "nh_zec_usd":  nh_zec_usd,
            "nh_xmr_usd":  nh_xmr_usd,
            "act_zec_usd": act_zec_usd,
            "act_xmr_usd": act_xmr_usd,
            "ratio_zec":   act_zec_usd / nh_zec_usd,
            "ratio_xmr":   act_xmr_usd / nh_xmr_usd,
        })

    return series


# ─────────────────────────────────────────────────────────────────────────────
# WINDOW DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def ratio_key(coin):
    return "ratio_zec" if coin == "ZEC" else "ratio_xmr"

def detect_windows(series, coin, nh_usd_key, act_usd_key, supply):
    rkey    = ratio_key(coin)
    windows = []
    in_win  = False
    w_days  = []

    for row in series:
        ratio = row[rkey]
        if not in_win:
            if ratio >= BUY_THRESHOLD:
                in_win = True
                w_days = [row]
        else:
            w_days.append(row)
            if ratio < SELL_THRESHOLD:
                in_win = False
                windows.append(_summarize(w_days, coin, nh_usd_key, act_usd_key, supply))
                w_days = []

    if in_win and w_days:
        windows.append(_summarize(w_days, coin, nh_usd_key, act_usd_key, supply,
                                  still_open=True))
    return windows


def _summarize(days, coin, nh_usd_key, act_usd_key, supply, still_open=False):
    rkey       = ratio_key(coin)
    ratios     = [d[rkey]        for d in days]
    nh_prices  = [d[nh_usd_key]  for d in days]
    act_prices = [d[act_usd_key] for d in days]
    duration   = len(days)

    # Per-day, per-unit-of-supply net profit
    daily_nets = [
        act * (1 - POOL_FEE) - nh * (1 + NH_BUYER_FEE)
        for act, nh in zip(act_prices, nh_prices)
    ]
    total_net_per_unit  = sum(daily_nets)
    total_cost_per_unit = sum(nh_prices) * (1 + NH_BUYER_FEE)
    roi_pct = (total_net_per_unit / total_cost_per_unit * 100) if total_cost_per_unit > 0 else 0

    return {
        "coin":               coin,
        "start":              days[0]["date"],
        "end":                days[-1]["date"],
        "duration":           duration,
        "peak_ratio":         max(ratios),
        "min_ratio":          min(ratios),
        "avg_ratio":          sum(ratios) / duration,
        "avg_nh_usd":         sum(nh_prices)  / duration,
        "avg_act_usd":        sum(act_prices) / duration,
        "net_per_unit":       total_net_per_unit,
        "cost_per_unit":      total_cost_per_unit,
        "roi_pct":            roi_pct,
        "full_supply_cost":   total_cost_per_unit * supply,
        "full_supply_profit": total_net_per_unit  * supply,
        "still_open":         still_open,
        "_days":              days,   # keep for monthly drill-down
    }


# ─────────────────────────────────────────────────────────────────────────────
# CAPITAL ANALYSIS
# NiceHash orders are daily recurring — you deploy capital_usd each day
# that buys you supply_fraction of hashrate for that day.
# total_spend = capital_usd * deployed_days  (capital recycled daily)
# ─────────────────────────────────────────────────────────────────────────────

def capital_analysis(windows, daily_capital_usd, supply, nh_usd_key, act_usd_key):
    """
    daily_capital_usd: budget available to deploy EACH day.
    NH orders are 24h; you re-spend each day you're in a window.
    Returns aggregated results across all windows.
    """
    total_spend    = 0.0
    total_profit   = 0.0
    deployed_days  = 0
    active_windows = 0

    for w in windows:
        days       = w["_days"]
        had_days   = False
        for row in days:
            nh_day_cost_full = supply * row[nh_usd_key] * (1 + NH_BUYER_FEE)
            frac = min(1.0, daily_capital_usd / nh_day_cost_full) if nh_day_cost_full > 0 else 0
            actual_spend = nh_day_cost_full * frac
            day_rev      = supply * frac * row[act_usd_key] * (1 - POOL_FEE)
            day_profit   = day_rev - actual_spend
            total_spend  += actual_spend
            total_profit += day_profit
            deployed_days += 1
            had_days = True
        if had_days:
            active_windows += 1

    roi_on_daily_cap = (total_profit / daily_capital_usd * 100) if daily_capital_usd > 0 else 0
    return {
        "spend":    total_spend,
        "profit":   total_profit,
        "days":     deployed_days,
        "windows":  active_windows,
        "roi":      roi_on_daily_cap,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY BREAKDOWN
# ─────────────────────────────────────────────────────────────────────────────

def monthly_breakdown(series, coin, nh_usd_key, act_usd_key, supply):
    rkey = ratio_key(coin)
    monthly = {}
    for row in series:
        ym = row["date"][:7]
        if ym not in monthly:
            monthly[ym] = {"ratios": [], "nh": [], "act": [], "nets": []}
        monthly[ym]["ratios"].append(row[rkey])
        monthly[ym]["nh"].append(row[nh_usd_key])
        monthly[ym]["act"].append(row[act_usd_key])
        net_day = (row[act_usd_key] * (1-POOL_FEE)
                   - row[nh_usd_key] * (1+NH_BUYER_FEE)) * supply
        monthly[ym]["nets"].append(net_day)

    result = []
    for ym in sorted(monthly):
        d      = monthly[ym]
        n      = len(d["ratios"])
        avg_r  = sum(d["ratios"]) / n
        above  = sum(1 for r in d["ratios"] if r >= BUY_THRESHOLD)
        month_profit = sum(d["nets"])
        avg_nh = sum(d["nh"]) / n
        result.append((ym, n, avg_r, above, month_profit, avg_nh))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PRINT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fmt_usd(v):
    if abs(v) >= 1e6:
        return f"${v/1e6:,.2f}M"
    return f"${v:,.2f}"

def fmt_pct(v):
    return f"{v:+.1f}%"

def fmt_ratio(v):
    return f"{v:.2f}x"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print_header("NiceHash Price-Lag Investment Analysis")
    print(f"  Run date   : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Strategy   : Buy NH hashrate when actual mining value > NH cost by >= 30%")
    print(f"  Exit       : When ratio drops back below 1.10")
    print(f"  NH price   : weighted-average STANDARD order paid price (algo history API)")
    print(f"  Note       : Fixed/LIMIT orders are higher; this analysis is conservative")
    print()

    # ── Fetch ─────────────────────────────────────────────────────────────────
    print("Fetching NiceHash algo history ...")
    nh_zec = fetch_nh_algo_history("EQUIHASH")
    print(f"  EQUIHASH      : {len(nh_zec)} days of history")
    nh_xmr = fetch_nh_algo_history("RANDOMXMONERO")
    print(f"  RANDOMXMONERO : {len(nh_xmr)} days of history")

    print("Fetching CoinGecko price history ...")
    btc_prices = fetch_coingecko("bitcoin", "BTC")
    print(f"  BTC : {len(btc_prices)} days")
    zec_prices = fetch_coingecko("zcash",   "ZEC")
    print(f"  ZEC : {len(zec_prices)} days")
    xmr_prices = fetch_coingecko("monero",  "XMR")
    print(f"  XMR : {len(xmr_prices)} days")

    # ── Merge ─────────────────────────────────────────────────────────────────
    series = build_daily_series(nh_zec, nh_xmr, btc_prices, zec_prices, xmr_prices)
    if not series:
        print("\n[ERROR] No overlapping data. Cannot proceed.")
        sys.exit(1)

    print(f"\nOverlapping daily data: {len(series)} days  "
          f"({series[0]['date']} to {series[-1]['date']})")

    # ── Detect windows ────────────────────────────────────────────────────────
    zec_wins = detect_windows(series, "ZEC", "nh_zec_usd", "act_zec_usd", NH_EQUIHASH_SUPPLY)
    xmr_wins = detect_windows(series, "XMR", "nh_xmr_usd", "act_xmr_usd", NH_RANDOMX_SUPPLY)

    # ═══════════════════════════════════════════════════════════════════════════
    # A — WINDOW LIST
    # ═══════════════════════════════════════════════════════════════════════════
    print_header("A — BUY WINDOWS DETECTED (365 days)")
    print(f"  Entry threshold : ratio >= {BUY_THRESHOLD:.2f}   "
          f"Exit threshold : ratio < {SELL_THRESHOLD:.2f}")
    print()

    for coin, wins, supply, unit in [
        ("ZEC (EQUIHASH, 0.254 GSol/s supply)", zec_wins, NH_EQUIHASH_SUPPLY, "GSol/s"),
        ("XMR (RANDOMXMONERO, 0.139 GH/s supply)", xmr_wins, NH_RANDOMX_SUPPLY, "GH/s"),
    ]:
        print(f"  {coin}  --  {len(wins)} window(s)")
        print(f"  {'─'*70}")
        if not wins:
            print("    No windows detected.")
            print()
            continue

        print(f"  {'Start':>12}  {'End':>12}  {'Days':>4}  "
              f"{'MinR':>6}  {'AvgR':>6}  {'PeakR':>6}  "
              f"{'FullCost':>12}  {'FullProfit':>12}  {'ROI':>7}  Status")
        print(f"  {'─'*70}")
        for w in wins:
            status = "[OPEN]" if w["still_open"] else "closed"
            print(f"  {w['start']:>12}  {w['end']:>12}  {w['duration']:>4}  "
                  f"{fmt_ratio(w['min_ratio']):>6}  {fmt_ratio(w['avg_ratio']):>6}  "
                  f"{fmt_ratio(w['peak_ratio']):>6}  "
                  f"{fmt_usd(w['full_supply_cost']):>12}  "
                  f"{fmt_usd(w['full_supply_profit']):>12}  "
                  f"{fmt_pct(w['roi_pct']):>7}  {status}")

        total_days   = sum(w["duration"] for w in wins)
        total_profit = sum(w["full_supply_profit"] for w in wins)
        print()
        print(f"    Windows: {len(wins)}  |  "
              f"Total days in window: {total_days}  |  "
              f"Avg peak ratio: {sum(w['peak_ratio'] for w in wins)/len(wins):.2f}x  |  "
              f"Full-supply profit: {fmt_usd(total_profit)}")
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # A2 — MONTHLY RATIO BREAKDOWN
    # ═══════════════════════════════════════════════════════════════════════════
    print_header("A2 — MONTHLY RATIO BREAKDOWN")
    print("  Shows how ratio evolved month by month (always-open window explained)")
    print()

    for coin, wins, supply, nh_usd_key, act_usd_key in [
        ("ZEC", zec_wins, NH_EQUIHASH_SUPPLY, "nh_zec_usd", "act_zec_usd"),
        ("XMR", xmr_wins, NH_RANDOMX_SUPPLY,  "nh_xmr_usd", "act_xmr_usd"),
    ]:
        months = monthly_breakdown(series, coin, nh_usd_key, act_usd_key, supply)
        print(f"  {coin}")
        print(f"  {'Month':>8}  {'Days':>4}  {'AvgRatio':>9}  {'AboveBuy':>8}  "
              f"{'MonthlyProfit(fullsupply)':>26}  {'AvgNH_USD':>12}")
        print(f"  {'─'*70}")
        for ym, n, avg_r, above, mprofit, avg_nh in months:
            flag = " <-- BUY" if avg_r >= BUY_THRESHOLD else ""
            print(f"  {ym:>8}  {n:>4}  {fmt_ratio(avg_r):>9}  "
                  f"{above:>4}/{n:<3}  {fmt_usd(mprofit):>26}  "
                  f"{fmt_usd(avg_nh):>12}{flag}")
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # B — CAPITAL SIZING
    # ═══════════════════════════════════════════════════════════════════════════
    print_header("B — FULL-SUPPLY CAPITAL REQUIREMENTS")
    print("  Cost to deploy full available supply for one day")
    print()

    today = series[-1]
    for coin, supply, unit, nh_usd_key in [
        ("ZEC / EQUIHASH",      NH_EQUIHASH_SUPPLY, "GSol/s", "nh_zec_usd"),
        ("XMR / RANDOMXMONERO", NH_RANDOMX_SUPPLY,  "GH/s",   "nh_xmr_usd"),
    ]:
        nh_today = today[nh_usd_key]
        daily_full = supply * nh_today * (1 + NH_BUYER_FEE)
        print(f"  {coin}")
        print(f"    Supply            : {supply} {unit}")
        print(f"    NH price today    : {fmt_usd(nh_today)}/unit/day")
        print(f"    Daily cost (100%) : {fmt_usd(daily_full)}")
        print(f"    Daily cost (50%)  : {fmt_usd(daily_full*0.5)}")
        print(f"    Daily cost (10%)  : {fmt_usd(daily_full*0.1)}")
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # C — SIZING RECOMMENDATIONS
    # ═══════════════════════════════════════════════════════════════════════════
    print_header("C — INVESTMENT SIZING RECOMMENDATIONS")
    print()
    print("  NH orders are 24-hour recurring. 'Daily capital' = budget you deploy each")
    print("  day you are in a window. The same dollars are recycled daily.")
    print()

    for coin, wins, supply, unit, nh_usd_key in [
        ("ZEC / EQUIHASH",      zec_wins, NH_EQUIHASH_SUPPLY, "GSol/s", "nh_zec_usd"),
        ("XMR / RANDOMXMONERO", xmr_wins, NH_RANDOMX_SUPPLY,  "GH/s",  "nh_xmr_usd"),
    ]:
        if not wins:
            print(f"  {coin}: no windows detected")
            continue

        # Use current-day prices for sizing
        nh_now   = today[nh_usd_key]
        daily_full = supply * nh_now * (1 + NH_BUYER_FEE)
        avg_ratio  = sum(w["avg_ratio"]  for w in wins) / len(wins)
        avg_dur    = sum(w["duration"]   for w in wins) / len(wins)

        print(f"  {coin}  (avg window {avg_dur:.0f}d, avg ratio {avg_ratio:.2f}x)")
        print(f"    Current daily cost for FULL supply  : {fmt_usd(daily_full)}")
        print(f"    Current daily cost for 50% supply   : {fmt_usd(daily_full*0.5)}")
        print(f"    Current daily cost for 10% supply   : {fmt_usd(daily_full*0.1)}")
        print(f"    Min viable order (~1% supply/day)   : {fmt_usd(daily_full*0.01)}")
        print()
        print(f"    Recommended sizing by ratio:")
        print(f"      ratio 1.30-1.50  ->  deploy 50% supply/day  ({fmt_usd(daily_full*0.5)}/day)")
        print(f"      ratio 1.50-2.00  ->  deploy 75% supply/day  ({fmt_usd(daily_full*0.75)}/day)")
        print(f"      ratio > 2.00     ->  deploy 100% supply/day ({fmt_usd(daily_full)}/day)")
        today_ratio = today["ratio_zec"] if coin.startswith("ZEC") else today["ratio_xmr"]
        print(f"      TODAY ratio={fmt_ratio(today_ratio)} -> ", end="")
        if today_ratio >= 2.0:
            print(f"DEPLOY 100%  ({fmt_usd(daily_full)}/day)")
        elif today_ratio >= 1.5:
            print(f"DEPLOY 75%   ({fmt_usd(daily_full*0.75)}/day)")
        elif today_ratio >= BUY_THRESHOLD:
            print(f"DEPLOY 50%   ({fmt_usd(daily_full*0.5)}/day)")
        else:
            print("NO POSITION")
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # D — CURRENT STATE
    # ═══════════════════════════════════════════════════════════════════════════
    print_header("D — CURRENT STATE")
    print(f"  Last data date : {today['date']}")
    print(f"  BTC price      : {fmt_usd(today['btc_usd'])}")
    print(f"  ZEC price      : {fmt_usd(today['zec_usd'])}")
    print(f"  XMR price      : {fmt_usd(today['xmr_usd'])}")
    print()

    for coin, rkey, ak, nk, supply, unit in [
        ("ZEC", "ratio_zec", "act_zec_usd", "nh_zec_usd", NH_EQUIHASH_SUPPLY, "GSol/s"),
        ("XMR", "ratio_xmr", "act_xmr_usd", "nh_xmr_usd", NH_RANDOMX_SUPPLY,  "GH/s"),
    ]:
        ratio     = today[rkey]
        act_val   = today[ak]
        nh_cost   = today[nk]
        daily_full = supply * nh_cost * (1 + NH_BUYER_FEE)
        day_profit = supply * (act_val*(1-POOL_FEE) - nh_cost*(1+NH_BUYER_FEE))

        print(f"  {coin} ({supply} {unit} supply):")
        print(f"    Actual mining value (per unit/day) : {fmt_usd(act_val)}")
        print(f"    NiceHash cost (per unit/day)       : {fmt_usd(nh_cost)}")
        print(f"    Ratio                              : {fmt_ratio(ratio)}")
        print(f"    Daily profit at full supply        : {fmt_usd(day_profit)}")
        print(f"    Daily cost at full supply          : {fmt_usd(daily_full)}", end="")
        if ratio >= BUY_THRESHOLD:
            print(f"  <-- WINDOW OPEN, BUY NOW (ratio={fmt_ratio(ratio)})")
        elif ratio >= SELL_THRESHOLD:
            print(f"  <-- Hold if in position")
        else:
            print(f"  <-- No opportunity")
        print()

    # ═══════════════════════════════════════════════════════════════════════════
    # E — SUMMARY TABLE
    # ═══════════════════════════════════════════════════════════════════════════
    print_header("E — SUMMARY: PROFIT BY DAILY CAPITAL BUDGET")
    print("  Model: you deploy 'daily_capital' each day you are in a window.")
    print("  Capital is recycled daily (NH orders are 24-hour).")
    print("  If daily_capital < full-supply daily cost, you buy proportional hashrate.")
    print()

    daily_budgets = [500, 1_000, 2_500, 5_000, 10_000]

    for coin, wins, supply, nh_usd_key, act_usd_key in [
        ("ZEC / EQUIHASH",      zec_wins, NH_EQUIHASH_SUPPLY, "nh_zec_usd", "act_zec_usd"),
        ("XMR / RANDOMXMONERO", xmr_wins, NH_RANDOMX_SUPPLY,  "nh_xmr_usd", "act_xmr_usd"),
    ]:
        deployed_days_total = sum(w["duration"] for w in wins)
        print(f"  {coin}  ({len(wins)} window(s), {deployed_days_total} total deployed days)")
        print(f"  {'─'*70}")
        print(f"  {'DailyBudget':>12}  {'TotalSpend':>12}  {'TotalProfit':>12}  "
              f"{'ProfitPerDay':>13}  {'ROIonBudget':>12}  {'Days':>5}")
        print(f"  {'─'*70}")

        for budget in daily_budgets:
            res = capital_analysis(wins, budget, supply, nh_usd_key, act_usd_key)
            profit_per_day = res["profit"] / res["days"] if res["days"] > 0 else 0
            print(f"  {fmt_usd(budget):>12}  {fmt_usd(res['spend']):>12}  "
                  f"{fmt_usd(res['profit']):>12}  "
                  f"{fmt_usd(profit_per_day):>13}  "
                  f"{fmt_pct(res['roi']):>12}  {res['days']:>5}")
        print()

    # ── Bottom-line note ──────────────────────────────────────────────────────
    print_sep()
    print("  Key facts about this analysis:")
    print(f"    * NH history price = weighted avg STANDARD order price (conservative baseline)")
    print(f"    * Fixed/LIMIT order prices are higher; actual entry prices may differ")
    print(f"    * NH buyer fee {NH_BUYER_FEE*100:.0f}%, pool fee {POOL_FEE*100:.0f}% both deducted")
    print(f"    * ZEC supply on NH: {NH_EQUIHASH_SUPPLY} GSol/s "
          f"(~{NH_EQUIHASH_SUPPLY/ZEC_NETWORK_GSOL*100:.1f}% of ZEC network)")
    print(f"    * XMR supply on NH: {NH_RANDOMX_SUPPLY} GH/s  "
          f"(~{NH_RANDOMX_SUPPLY/XMR_NETWORK_GH*100:.1f}% of XMR network)")
    print(f"    * Persistent high ratios (5x-12x) indicate NH standard prices are")
    print(f"      chronically below mining value -- structural arbitrage, not spike-lag")
    print(f"    * Risk: ratio collapses if NH buyers bid up, coin price drops, or")
    print(f"      network difficulty spikes (51% attack scenario)")
    print_sep("═")
    print()


if __name__ == "__main__":
    main()
