"""
nicehash_scan.py — Find non-dead NiceHash markets

For each GPU-mineable NiceHash algorithm:
  1. NiceHash price (correct marketFactor unit)
  2. Actual coin profitability from blockchain data
  3. Arb ratio = actual / NiceHash
     ~1.0 = fair market  | >100 = ghost market
"""

import json, time, urllib.request
import numpy as np

def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries-1: return None
            time.sleep(2)

print("=" * 72)
print("  NiceHash Market Scan — Finding non-dead GPU-mineable markets")
print("=" * 72)
print()

# ── 1. NiceHash: algo metadata + current stats ────────────────────────────────
algo_meta = fetch('https://api2.nicehash.com/main/api/v2/mining/algorithms')
stats_now = fetch('https://api2.nicehash.com/main/api/v2/public/stats/global/current')
algo_map  = {a['order']: a for a in algo_meta['miningAlgorithms']}

nh = {}
for a in stats_now['algos']:
    aid = a['a']
    if aid not in algo_map: continue
    info = algo_map[aid]
    mf   = float(info['marketFactor'])
    name = info['algorithm']
    speed_u = a['s'] / mf               # speed in display units
    nh[name] = {
        'speed_u':    speed_u,
        'price_btc':  a['p'],            # BTC / (display unit) / day
        'mf':         mf,
        'dmf':        info['displayMarketFactor'],
        'market_btc': speed_u * a['p'],
    }

# ── 2. Prices: one batched CoinGecko call ─────────────────────────────────────
price_ids = 'zcash,monero,bitcoin-gold,ravencoin,litecoin,bitcoin,beam'
price_data = fetch('https://api.coingecko.com/api/v3/simple/price?ids=%s&vs_currencies=usd' % price_ids)
if not price_data:
    print("CoinGecko failed, using fallbacks")
    price_data = {}

def price(cg_id, fallback):
    return (price_data or {}).get(cg_id, {}).get('usd', fallback)

BTC_USD  = price('bitcoin', 67000)
ZEC_USD  = price('zcash', 35)
XMR_USD  = price('monero', 165)
BTG_USD  = price('bitcoin-gold', 18)
RVN_USD  = price('ravencoin', 0.02)
LTC_USD  = price('litecoin', 80)
BEAM_USD = price('beam', 0.05)

print("  Prices: BTC=$%.0f  ZEC=$%.2f  XMR=$%.2f  BTG=$%.2f  RVN=$%.4f  LTC=$%.2f  BEAM=$%.4f" % (
    BTC_USD, ZEC_USD, XMR_USD, BTG_USD, RVN_USD, LTC_USD, BEAM_USD))

# ── 3. Blockchain data ─────────────────────────────────────────────────────────
print()
print("  Fetching blockchain stats ...")

def bc_stats(coin):
    d = fetch('https://api.blockchair.com/%s/stats' % coin)
    return d['data'] if d else {}

zec_bc = bc_stats('zcash')
xmr_bc = bc_stats('monero')
ltc_bc = bc_stats('litecoin')

# ZEC — Equihash, NiceHash unit: GSol/s (mf=1e9)
# blockchair hashrate_24h is in Sol/s
zec_hr_gsol = float(zec_bc.get('hashrate_24h', 13e9)) / 1e9   # GSol/s
zec_reward  = 1.5625                                            # post-2024-halving
zec_bt      = 75

# XMR — RandomX, NiceHash unit: GH/s (mf=1e9)
xmr_hr_gh   = float(xmr_bc.get('hashrate_24h', 5.5e9)) / 1e9  # GH/s
xmr_infl    = float(xmr_bc.get('inflation_24h', 0) or 0)
xmr_blk24   = float(xmr_bc.get('blocks_24h', 720) or 720)
xmr_reward  = xmr_infl / xmr_blk24 / 1e12 if xmr_infl > 0 else 0.60
xmr_bt      = 120

# LTC — Scrypt, NiceHash unit: TH/s (mf=1e12)
ltc_diff    = float(ltc_bc.get('difficulty', 1e8))
ltc_hr_ths  = ltc_diff * 4294967296 / 150 / 1e12       # TH/s (from diff)
ltc_infl    = float(ltc_bc.get('inflation_24h', 0) or 0)
ltc_blk24   = float(ltc_bc.get('blocks_24h', 576) or 576)
ltc_reward  = ltc_infl / ltc_blk24 / 1e8 if ltc_infl > 0 else 6.25
ltc_bt      = 150

# BTG — ZHASH Equihash-144,5, NiceHash unit: MSol/s (mf=1e6)
# BTG not on blockchair, use approximate known values
# BTG network: ~5 MSol/s, reward 6.25 BTG, 10 min blocks
btg_hr_msol = 5.0
btg_reward  = 6.25
btg_bt      = 600

# BEAM — BeamHashIII, NiceHash unit: MSol/s (mf=1e6)
# BEAM network hashrate ~40-60 kSol/s = 0.04-0.06 MSol/s
# Fetch from BEAM explorer
beam_exp = fetch('https://explorer.beam.mw/api/v1/status')
if beam_exp:
    beam_hr_raw  = beam_exp.get('hashrate', beam_exp.get('hash_rate', 0))
    beam_hr_msol = float(beam_hr_raw or 0) / 1e6    # MSol/s
    beam_reward  = float(beam_exp.get('reward', 0) or 0)
    if beam_reward > 1e6: beam_reward /= 1e8        # groth → BEAM
    if beam_hr_msol == 0: beam_hr_msol = 0.05
    if beam_reward == 0:  beam_reward  = 3.0
else:
    beam_hr_msol = 0.05
    beam_reward  = 3.0
beam_bt = 60

# RVN — KawPoW, NiceHash unit: TH/s (mf=1e12)
# Try a Ravencoin API
rvn_api = fetch('https://rvn.cryptoscope.io/api/getnetworkhashps/?nbblocks=144')
if rvn_api is None:
    rvn_api = fetch('https://api.whattomine.com/coins/234.json')  # RVN on WTM
if rvn_api and isinstance(rvn_api, dict):
    if 'network_hashrate' in rvn_api:
        rvn_hr_ths = float(rvn_api['network_hashrate']) / 1e12
    elif 'nethash' in rvn_api:
        rvn_hr_ths = float(rvn_api['nethash']) / 1e12
    else:
        rvn_hr_ths = 5e-3   # ~5 TH/s fallback
elif isinstance(rvn_api, (int, float)):
    rvn_hr_ths = float(rvn_api) / 1e12
else:
    rvn_hr_ths = 5e-3
rvn_reward = 2500
rvn_bt     = 60

print("  ZEC: %.2f GSol/s  reward=%.4f ZEC" % (zec_hr_gsol, zec_reward))
print("  XMR: %.2f GH/s   reward=%.4f XMR" % (xmr_hr_gh, xmr_reward))
print("  LTC: %.2f TH/s   reward=%.4f LTC" % (ltc_hr_ths, ltc_reward))
print("  BTG: %.2f MSol/s reward=%.4f BTG (fallback)" % (btg_hr_msol, btg_reward))
print("  BEAM:%.4f MSol/s reward=%.4f BEAM" % (beam_hr_msol, beam_reward))
print("  RVN: %.4f TH/s  reward=%.0f RVN" % (rvn_hr_ths, rvn_reward))
print()

# ── 4. Compute arb ratios ──────────────────────────────────────────────────────
def arb(nh_algo, hr_units, block_reward, block_time_s, coin_usd):
    """
    hr_units: network hashrate already in the NiceHash market factor unit
    Returns (actual_usd, nh_usd, ratio) all per 1 display unit per day.
    """
    if nh_algo not in nh: return None
    info      = nh[nh_algo]
    nh_usd    = info['price_btc'] * BTC_USD           # USD / unit / day
    # blocks earned per unit per day: 1/hr_units × blocks_per_day
    bpd       = 86400.0 / block_time_s
    actual_usd = (bpd / hr_units) * block_reward * coin_usd  if hr_units > 0 else 0
    ratio     = actual_usd / nh_usd if nh_usd > 0 else float('inf')
    return {
        'algo':       nh_algo,
        'dmf':        info['dmf'],
        'supply_u':   info['speed_u'],
        'market_usd': info['market_btc'] * BTC_USD,
        'nh_usd':     nh_usd,
        'actual_usd': actual_usd,
        'ratio':      ratio,
        'net_hr':     hr_units,
    }

results = list(filter(None, [
    arb('EQUIHASH',      zec_hr_gsol,  zec_reward,  zec_bt,  ZEC_USD),
    arb('RANDOMXMONERO', xmr_hr_gh,    xmr_reward,  xmr_bt,  XMR_USD),
    arb('SCRYPT',        ltc_hr_ths,   ltc_reward,  ltc_bt,  LTC_USD),
    arb('ZHASH',         btg_hr_msol,  btg_reward,  btg_bt,  BTG_USD),
    arb('BEAMV3',        beam_hr_msol, beam_reward, beam_bt, BEAM_USD),
    arb('KAWPOW',        rvn_hr_ths,   rvn_reward,  rvn_bt,  RVN_USD),
]))
results.sort(key=lambda x: x['market_usd'], reverse=True)

# ── 5. Print ───────────────────────────────────────────────────────────────────
print("=" * 72)
print("  RESULTS: Arb ratio = actual mining / NiceHash rental price")
print("  ~1.0 = real fair market  |  <3 = opportunity  |  >100 = dead/ghost")
print("=" * 72)
print()
print("  %-14s %4s  %10s  %10s  %9s  %10s" % (
    "Algo(Coin)", "Unit", "NH $/unit/d", "Act$/unit/d", "Ratio", "Mkt $k/day"))
print("  " + "-"*68)
for c in results:
    if c['ratio'] < 2.5:
        flag = "  *** REAL MARKET"
    elif c['ratio'] < 5:
        flag = "  ** near-market"
    elif c['ratio'] < 15:
        flag = "  * inefficient"
    elif c['ratio'] > 500:
        flag = "  GHOST"
    else:
        flag = ""
    coin_tag = {'EQUIHASH':'ZEC','RANDOMXMONERO':'XMR','SCRYPT':'LTC',
                'ZHASH':'BTG','BEAMV3':'BEAM','KAWPOW':'RVN'}.get(c['algo'],'?')
    print("  %-14s %4s  %10.2f  %10.2f  %9.2fx  %10.2f%s" % (
        "%s(%s)"%(c['algo'][:8],coin_tag), c['dmf'],
        c['nh_usd'], c['actual_usd'], c['ratio'],
        c['market_usd']/1000, flag))

print()
print("  Legend: NH = NiceHash rental price  |  Act = actual coin mining revenue")
print("  Ratio = Act/NH: buy hashrate from NiceHash, mine coin, pocket the difference")
print()

# ── 6. Best candidates summary ────────────────────────────────────────────────
print("=" * 72)
print("  BEST CANDIDATES for rental arbitrage")
print("=" * 72)
print()
ranked = sorted(results, key=lambda x: x['ratio'])
for i, c in enumerate(ranked, 1):
    verdict = ("REAL MARKET — excellent" if c['ratio'] < 1.5 else
               "GOOD — profitable rental" if c['ratio'] < 3 else
               "modest gap" if c['ratio'] < 10 else
               "large gap — likely ghost market")
    coin_tag = {'EQUIHASH':'ZEC','RANDOMXMONERO':'XMR','SCRYPT':'LTC',
                'ZHASH':'BTG','BEAMV3':'BEAM','KAWPOW':'RVN'}.get(c['algo'],'?')
    print("  #%d %-20s ratio=%.2fx  mkt=$%.0f/d  [%s]" % (
        i, "%s→%s"%(c['algo'],coin_tag), c['ratio'], c['market_usd'], verdict))
print()
print("  Bottom line:")
print("  - ratio < 2×  → NiceHash price is close to actual mining value → REAL liquid market")
print("  - ratio 2-10× → NiceHash is somewhat underpriced vs actual → some arbitrage")
print("  - ratio > 100× → ghost market (like ETChash/AUTOLYKOS)")
