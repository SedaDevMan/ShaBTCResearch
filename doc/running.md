# Running the NiceHash Dashboard — Operations Guide

## Environment

| Component  | Version / Path                                                  |
|------------|-----------------------------------------------------------------|
| Node.js    | v24.10.0 (`/home/adminweb/.nvm/versions/node/v24.10.0/bin/node`) |
| npm        | 11.6.1                                                          |
| Python     | 3.10.12 (research scripts only)                                 |
| nvm        | used — activate with `nvm use 24` if needed                     |
| Port       | **55560**                                                       |
| Working dir| `/var/www/jeer.currenciary.com/shabtc/`                         |

---

## File Structure

```
shabtc/
├── nh_server.js          ← Node.js backend (Express + ws)
├── package.json          ← dependencies: express, ws
├── package-lock.json
├── nh_config.json        ← credentials (chmod 600, git-ignored, created on first /api/config POST)
├── node_modules/
├── public/
│   ├── favicon.svg
│   ├── index.html        ← landing page (module cards)
│   └── nicehash/
│       └── index.html    ← full NiceHash dashboard (self-contained)
└── doc/
    ├── running.md        ← this file
    └── nicehash_arbitrage_strategy.md
```

---

## First-Time Setup

```bash
cd /var/www/jeer.currenciary.com/shabtc

# Install Node dependencies
npm install

# Start the server
node nh_server.js
```

The server starts immediately, fetches live data in the background, and is accessible at:

| URL                                  | Description              |
|--------------------------------------|--------------------------|
| http://31.56.232.147:55560/          | Landing page             |
| http://31.56.232.147:55560/nicehash/ | NiceHash arb dashboard   |
| http://31.56.232.147:55560/api/live  | Live data JSON           |

---

## Start / Stop / Restart

The server runs under **PM2** (id: `shabtc-dashboard`) for automatic crash recovery.
PM2 restarts it after 5 s, with exponential backoff up to 10 retries.

```bash
# Status
pm2 status shabtc-dashboard

# Start (if stopped)
pm2 start shabtc-dashboard

# Stop
pm2 stop shabtc-dashboard

# Restart
pm2 restart shabtc-dashboard

# Live logs
pm2 logs shabtc-dashboard --lines 50

# Full log file
tail -f /tmp/nh_server.log
```

### First-time PM2 registration (already done — for reference)

```bash
cd /var/www/jeer.currenciary.com/shabtc
pm2 start nh_server.js \
  --name shabtc-dashboard \
  --interpreter /home/adminweb/.nvm/versions/node/v24.10.0/bin/node \
  --restart-delay 5000 \
  --exp-backoff-restart-delay 100 \
  --max-restarts 10 \
  --log /tmp/nh_server.log
pm2 save
```

---

## Run as a systemd Service (recommended for production)

Create the service file:

```bash
sudo nano /etc/systemd/system/nh-dashboard.service
```

Paste:

```ini
[Unit]
Description=NiceHash Arbitrage Dashboard
After=network.target

[Service]
Type=simple
User=adminweb
WorkingDirectory=/var/www/jeer.currenciary.com/shabtc
ExecStart=/home/adminweb/.nvm/versions/node/v24.10.0/bin/node nh_server.js
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nh-dashboard

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nh-dashboard
sudo systemctl start nh-dashboard

# Check status
sudo systemctl status nh-dashboard

# Follow logs
sudo journalctl -u nh-dashboard -f
```

---

## Configure NiceHash Credentials

Credentials are stored server-side in `nh_config.json` (chmod 600, never in browser).

**Via the dashboard UI:**
1. Open http://31.56.232.147:55560/nicehash/
2. Click ⚙ Settings
3. Enter Organisation ID, API Key, API Secret
4. Enter pool details for ZEC and XMR
5. Click Save Settings → written to `nh_config.json`

**Via curl (headless):**

```bash
curl -s -X POST http://localhost:55560/api/config \
  -H 'Content-Type: application/json' \
  -d '{
    "org_id":     "your-org-uuid",
    "api_key":    "your-api-key",
    "api_secret": "your-api-secret",
    "pools": {
      "EQUIHASH": {
        "host": "zec.2miners.com",
        "port": "2020",
        "user": "YOUR_ZEC_WALLET.rig1",
        "pass": "x"
      },
      "RANDOMXMONERO": {
        "host": "gulf.moneroocean.stream",
        "port": "10128",
        "user": "YOUR_XMR_WALLET.rig1",
        "pass": "x"
      }
    }
  }'
```

**Check saved config (secret is never returned):**

```bash
curl -s http://localhost:55560/api/config | python3 -m json.tool
```

---

## API Reference (quick)

All endpoints are on `http://localhost:55560`.

```bash
# Live prices, arb ratios, halving countdown
curl -s http://localhost:55560/api/live | python3 -m json.tool

# Order book (algo = EQUIHASH or RANDOMXMONERO)
curl -s "http://localhost:55560/api/orderbook?algo=EQUIHASH" | python3 -m json.tool

# My active orders (requires credentials in nh_config.json)
curl -s "http://localhost:55560/api/myorders?algo=EQUIHASH" | python3 -m json.tool

# Place a STANDARD order (requires credentials)
curl -s -X POST http://localhost:55560/api/order \
  -H 'Content-Type: application/json' \
  -d '{
    "market":       "EU",
    "algorithm":    { "algorithm": "EQUIHASH" },
    "amount":       "0.01",
    "price":        "0.00050000",
    "limit":        "0.003",
    "type":         "STANDARD",
    "poolHostname": "zec.2miners.com",
    "poolPort":     2020,
    "username":     "YOUR_ZEC_WALLET.rig1",
    "password":     "x"
  }'

# Cancel an order
curl -s -X DELETE http://localhost:55560/api/order/ORDER-UUID-HERE
```

> **Note on bid price**: NiceHash expects `price` in **BTC per speed unit per day**.
> Convert from USD: `price_btc = bid_usd / btc_price`.
> Example: $400/GSol/day at BTC=$67,000 → price = `0.00597 BTC/GSol/day`

---

## WebSocket

The server pushes a full data update every 30 seconds to all connected browsers.

```
ws://31.56.232.147:55560/ws
```

Message format:
```json
{ "type": "live", "data": { ...same as /api/live... } }
```

Test from terminal:
```bash
# Install wscat if needed: npm install -g wscat
wscat -c ws://localhost:55560/ws
```

---

## Refresh Intervals

| Data          | Interval | Mechanism               |
|---------------|----------|-------------------------|
| Live prices + arb | 30s  | Server background loop  |
| Order books   | 15s      | Browser `setInterval`   |
| WS push       | 30s      | Broadcast after refresh |

---

## Logs

```bash
# Server stdout (background start)
tail -f /tmp/nh_server.log

# Example log lines:
# [refresh] fetching...
# [refresh] BTC=$66798 ZEC=$236.79 XMR=$327.97 ZEC_arb=9.48x XMR_arb=9.62x
```

---

## Dependency Update

```bash
cd /var/www/jeer.currenciary.com/shabtc
npm update
npm audit fix
```

---

## Mining Bot Setup

The server includes an automated ZEC mining bot that rents EQUIHASH hashrate on NiceHash,
routes ZEC profits through Binance, and auto-funds NiceHash operations.

**Money flow:**
```
2Miners pool ─ ZEC payout ─▶ Binance ZEC wallet
                                    │
                         Bot splits ZEC balance:
                         ├─ "ops" share ──▶ withdraw ZEC to NiceHash ZEC addr (~$0.12 fee)
                         │                      └─ NH Exchange: ZEC→BTC → fund EQUIHASH orders
                         └─ "profit" share ──▶ Binance ZEC→USDC market sell (stays at Binance)
```

### Step 1: Configure Binance API credentials

1. In Binance, create an API key with permissions:
   - **Read** (for balance checks)
   - **Spot & Margin Trading** (for ZECUSDC sell orders)
   - **Withdrawals** (for ZEC withdrawal to NiceHash)
   - Whitelist your server IP in the API key settings

2. Find your Binance **ZEC deposit address** (Wallet → Deposit → ZEC → ZEC network)

3. Open the Bot UI: http://31.56.232.147:55560/bot/

4. In the **Binance Credentials** section, enter:
   - API Key
   - API Secret
   - Binance ZEC Deposit Address ← copy this to 2Miners as your payout address
   - Click **Save Binance Credentials**

### Step 2: Configure 2Miners payout

In your 2Miners account settings, set the payout address to the **Binance ZEC deposit address**
saved in step 1. This ensures mining rewards land directly in Binance.

### Step 3: Configure bot settings

| Setting | Default | Description |
|---|---|---|
| `max_slots` | 3 | Max simultaneous EQUIHASH orders on NiceHash |
| `min_arb_ratio` | 1.10 | Minimum arb ratio before placing orders (1.10 = 10% profit margin) |
| `wait_for_arb` | true | Hold when arb < threshold instead of placing at loss |
| `order_amount_btc` | 0.005 | BTC amount per NiceHash order |
| `max_bid_usd` | 26000 | Max bid price USD/GSol/day |
| `zec_ops_pct` | 30 | % of Binance ZEC balance to send to NiceHash for ops |
| `nh_btc_threshold` | 0.01 | Top up NiceHash when BTC balance drops below this |

Via UI: http://31.56.232.147:55560/bot/ → Controls & Config → Save Config

Via curl:
```bash
curl -s -X POST http://localhost:55560/api/bot/config \
  -H 'Content-Type: application/json' \
  -d '{
    "max_slots": 3,
    "min_arb_ratio": 1.10,
    "wait_for_arb": true,
    "order_amount_btc": "0.005",
    "zec_ops_pct": 30,
    "nh_btc_threshold": 0.01
  }'
```

### Step 4: Start the bot

**Via UI:** Click **▶ START BOT**

**Via curl:**
```bash
curl -s -X POST http://localhost:55560/api/bot/start
```

The bot runs a cycle every 60 seconds. Each cycle:
1. Checks NH BTC balance, active bot orders, Binance ZEC/USDC balances
2. If arb ≥ min_arb_ratio: places new EQUIHASH orders up to max_slots
3. If arb < 1.0: cancels all bot-managed orders
4. If NH BTC < threshold: withdraws ops_pct% of Binance ZEC to NH ZEC deposit address
5. Remaining Binance ZEC → USDC market sell (profit sweep)

### Bot API Reference

```bash
# Bot status (active orders, balances, last cycle)
curl -s http://localhost:55560/api/bot/status | python3 -m json.tool

# Bot config (secrets stripped)
curl -s http://localhost:55560/api/bot/config | python3 -m json.tool

# Start bot
curl -s -X POST http://localhost:55560/api/bot/start

# Stop bot (with optional order cancellation)
curl -s -X POST http://localhost:55560/api/bot/stop \
  -H 'Content-Type: application/json' \
  -d '{"cancel_orders": true}'

# Last 200 log entries
curl -s http://localhost:55560/api/bot/log | python3 -m json.tool

# Force ZEC→NH top-up now
curl -s -X POST http://localhost:55560/api/bot/topup

# Force ZEC→USDC profit sweep now
curl -s -X POST http://localhost:55560/api/bot/sweep
```

### Bot config file

`bot_config.json` is chmod 600. Binance API secret is never returned by `/api/bot/config`.

---

## Security Notes

- `nh_config.json` is chmod 600 — only readable by `adminweb`
- `bot_config.json` is chmod 600 — Binance API secret never returned via HTTP
- The API secret is never returned by `/api/config` or `/api/bot/config` (stripped before response)
- The dashboard has no authentication — it is intended for single-user local/VPN access only
- Do not expose port 55560 on a public interface without adding auth (nginx basic-auth or similar)
