# NiceHash Hashrate Rental Arbitrage — Strategy Document

## Overview

This document describes the arbitrage strategy implemented in the `shabtc` NiceHash dashboard.
The goal is to **rent hashrate on NiceHash below the mining break-even cost**, direct it to a
mining pool, and pocket the spread between mining revenue and rental cost.

This is not a prediction or speculation strategy. It is a **pure arbitrage**: if the rental price
is below the fair mining value, profit is structurally guaranteed (modulo execution risks described
below).

---

## Core Concept

NiceHash operates a **hashpower marketplace** where:
- **Sellers** (miners with rigs) list their hashrate for rent at a bid price (BTC per speed unit per day)
- **Buyers** (us) rent that hashrate and point it at any mining pool of our choice

As the buyer, we earn block rewards from the pool proportional to the hashrate we rented.
If the block rewards earned exceed the rental cost, we profit.

```
Profit = Mining Revenue - Rental Cost - Fees
```

The opportunity exists because NiceHash sellers often underprice their hashrate relative to
what that hashrate actually produces in block rewards. This mispricing is persistent and measurable
in real time.

---

## Algorithms Targeted

### ZEC — Equihash (EQUIHASH)

| Parameter         | Value                     |
|-------------------|---------------------------|
| Block time        | 75 seconds                |
| Blocks per day    | 1,152                     |
| Block reward      | 1.375 ZEC                 |
| Speed unit        | GSol/s (giga-solutions/s) |
| Next halving      | Block 3,360,000           |
| Pool fee assumed  | 1%                        |

### XMR — RandomX (RANDOMXMONERO)

| Parameter         | Value                     |
|-------------------|---------------------------|
| Block time        | 120 seconds               |
| Blocks per day    | 720                       |
| Block reward      | ~0.6 XMR (tail emission)  |
| Speed unit        | GH/s (giga-hashes/s)      |
| Halving           | None (tail emission)      |
| Pool fee assumed  | 1%                        |

---

## The Math

### Mining Value (what 1 unit of hashrate produces per day)

```
actual_usd_per_unit = (1 / network_hashrate) × blocks_per_day × block_reward × coin_price × 0.99
```

- `network_hashrate`: total network hashrate in the same unit as the rental (GSol/s or GH/s)
- `0.99`: deducts 1% pool fee
- Result: USD value produced per unit of rented hashrate per day

This is the **fair market value** of 1 unit of hashrate.

### Break-Even Bid

```
breakeven_bid_usd = actual_usd_per_unit / 1.03
```

NiceHash charges buyers a **3% fee** on top of the bid price. So the maximum we can bid while
still breaking even is the mining value divided by 1.03.

Any STANDARD order in the order book with a bid price **below** `breakeven_bid_usd` is profitable
to take.

### Arbitrage Ratio

```
arb_ratio = actual_usd_per_unit / nh_market_price_usd
```

- `arb_ratio > 1.0`: opportunity exists — mining value exceeds rental price
- `arb_ratio > 1.3`: strong opportunity (30%+ margin)
- `arb_ratio < 1.0`: market is overpriced — do not rent

### Per-Order Profit (displayed in the order book)

```
revenue_per_day = (rig_speed / network_hashrate) × blocks_per_day × block_reward × coin_price × 0.99
cost_per_day    = bid_usd × rig_speed × 1.03
profit_per_day  = revenue_per_day - cost_per_day
```

### Budget Duration

```
duration_days = deposit_usd / cost_per_day
```

The deposited BTC is consumed at the rate `cost_per_day`. When the deposit runs out, the order stops.
This is the key insight for total revenue projection: **a rig may not run for 24 hours**
if the budget is small relative to the cost rate.

### Total Actual Revenue (for the funded period)

```
total_revenue = revenue_per_day × duration_days
total_profit  = profit_per_day  × duration_days
             = total_revenue - deposit_usd
```

This is the most honest profitability figure: not "profit per day" (which assumes infinite runtime)
but "total profit given this exact budget".

---

## Order Types on NiceHash

| Type     | Description                                                              |
|----------|--------------------------------------------------------------------------|
| STANDARD | Price moves with market. Rigs fill automatically if your bid is competitive. |
| FIXED    | Locked price. More predictable but typically priced at a premium.         |

**We exclusively target STANDARD orders.** Fixed orders are already priced to capture the arb
premium, so they are never profitable to take as a buyer.

---

## Fee Structure

| Fee            | Who pays | Rate |
|----------------|----------|------|
| Pool fee       | Buyer    | ~1%  |
| NiceHash buyer fee | Buyer | 3%  |
| NiceHash seller fee | Seller | 2% |

Total cost efficiency from the buyer's perspective:
- We pay 3% on top of what we bid
- We lose 1% to pool fees on revenues
- Net: our effective mining revenue yield is `revenue × 0.99`, and we pay `bid × 1.03`

---

## Execution Flow

```
1. Monitor:  Fetch live coin prices + network hashrate every 30s
2. Detect:   Compute arb_ratio for ZEC and XMR
3. Evaluate: Scan order book for STANDARD orders below break-even
4. Select:   Choose the cheapest (most profitable) order
5. Place:    Deposit BTC, set bid below break-even, point to our pool
6. Earn:     Pool pays us block rewards proportional to our rented hashrate
7. Exit:     Order ends when deposit is consumed
```

---

## Pool Strategy

The rented hashrate is directed to **our own mining pool account**. We keep 100% of block rewards.

Recommended pools:
- **ZEC**: 2Miners (`zec.2miners.com:2020`) — 1% fee, PPLNS, reliable payouts
- **XMR**: MoneroOcean (`gulf.moneroocean.stream:10128`) — 0% fee, auto-algo

Pool payout scheme matters:
- **PPLNS** (Pay Per Last N Shares): rewards are proportional to shares contributed in the last N shares window. Better for larger/longer rentals.
- **PPS** (Pay Per Share): pays per share regardless of block luck. Better for short-duration rentals where we may not stay long enough to see a block.

For short rentals (< 6 hours), a PPS pool is strictly better since PPLNS payouts depend on the
pool finding a block during our rental window.

---

## Risk Factors

### 1. Network Hashrate Volatility
The `actual_usd_per_unit` is calculated using the 24-hour average network hashrate from Blockchair.
If the real-time hashrate spikes (e.g., large miner comes online), our yield drops below the
projection. Always add a safety margin (e.g., only enter when `arb_ratio > 1.1`).

### 2. Coin Price Movement
Between order placement and payout, the coin price may drop. The arb is only locked in at the
moment the pool pays out. For short rentals (hours), this risk is minimal. For multi-day orders,
hedge exposure.

### 3. Rig Fill Rate
A STANDARD order may not fill immediately, or may fill partially. If the network hashrate is
large relative to our rented speed, there is variance in actual blocks found. The revenue
projection is an **expected value**, not a guarantee.

### 4. Block Luck
Mining is probabilistic. Even with correct pricing, a rental may produce fewer blocks than
the statistical expectation (bad luck variance). This averages out over many orders.

### 5. Pool Payout Threshold
Pools have minimum payout thresholds. Short rentals may earn below the threshold and the
payout is delayed until the next rental builds on the balance.

### 6. NiceHash Order Cancellation
NiceHash can cancel orders in extreme market conditions. Funds are returned, but the opportunity
window is lost.

---

## Optimal Execution Parameters

Based on current market conditions (ZEC arb ~9.7× at time of writing, XMR ~9.6×):

| Parameter      | Recommendation                                                   |
|----------------|------------------------------------------------------------------|
| Min arb ratio  | > 1.10 (10% margin after all fees and variance)                  |
| Order type     | STANDARD only                                                    |
| Bid strategy   | Bid at or just below the cheapest profitable STANDARD order      |
| Budget size    | Large enough for ≥ 6 hours of runtime (see Duration column)      |
| Algo selection | Both ZEC and XMR when both are above threshold                   |
| Pool           | PPS for short rentals, PPLNS for > 12h                           |

---

## Automation Potential (Future Bot Logic)

The dashboard currently supports **manual** order placement. A bot would automate:

```
loop every 30s:
  fetch live data
  for each algo in [EQUIHASH, RANDOMXMONERO]:
    if arb_ratio > MIN_THRESHOLD:
      best_order = top of profitable STANDARD orders (sorted by profit_day desc)
      if no active order for this algo:
        place_order(
          algo       = algo,
          bid        = best_order.bid_usd (converted to BTC),
          speed      = best_order.speed,
          amount_btc = BUDGET_PER_ALGO,
          pool       = configured pool
        )
    else:
      # market overpriced, do nothing
      pass
```

Key decisions for the bot:
1. **Re-entry**: after a budget is consumed, re-evaluate and place again if still profitable
2. **Bid adjustment**: if our order doesn't fill in N minutes, raise bid slightly
3. **Multi-order**: split budget across multiple cheap orders for diversification
4. **Stop condition**: if arb_ratio drops below 1.0 mid-order, cancel and redeploy

---

## Current Dashboard Metrics Explained

| Metric              | Formula                                             | Meaning                                    |
|---------------------|-----------------------------------------------------|--------------------------------------------|
| `arb_ratio`         | `actual_usd_per_unit / nh_market_usd`               | How many times above market the arb is     |
| `actual_usd_per_unit` | `(1/netHR) × bpd × reward × price × 0.99`        | Fair value of 1 unit hashrate/day          |
| `breakeven_bid_usd` | `actual_usd_per_unit / 1.03`                        | Max bid to stay profitable after NH fee    |
| `profit_per_day`    | `revenue_per_day - cost_per_day`                    | Daily P&L for a specific rig               |
| `duration`          | `deposit_usd / cost_per_day`                        | How long the budget lasts on a specific rig|
| `total_revenue`     | `revenue_per_day × duration_days`                   | Actual coins earned for the funded period  |
| `total_profit`      | `profit_per_day × duration_days`                    | Net gain after subtracting full deposit    |

---

## Data Sources

| Data            | Source                                      | Refresh |
|-----------------|---------------------------------------------|---------|
| BTC/ZEC/XMR prices | CoinGecko simple price API               | 30s     |
| ZEC network hashrate | Blockchair `/zcash/stats` (hashrate_24h) | 30s   |
| XMR network hashrate | Blockchair `/monero/stats` (hashrate_24h)| 30s   |
| NH market price  | NiceHash `/public/simplemultialgo/info`    | 30s     |
| Order book       | NiceHash `/hashpower/orderBook`            | 15s     |
| My active orders | NiceHash authenticated API                 | Manual  |

---

## Questions for Kimi AI

1. Is the arb_ratio formula correct, or are we missing any cost/fee components?
2. Should we use real-time network hashrate instead of 24h average for a tighter arb signal?
3. For very short rentals (< 2h), is PPLNS expected value actually lower than PPS by a calculable amount?
4. Is there a correlation between NiceHash STANDARD order book depth and the arb ratio that could predict window duration?
5. Is there a smarter bid strategy than "bid just below cheapest profitable STANDARD order"? (e.g., bid at a fixed % below break-even to maximise fill speed vs. margin)
6. How does block luck variance affect minimum viable budget size for ZEC vs XMR?
7. Is simultaneous ZEC + XMR exposure better than focusing all budget on the higher arb_ratio algo?
