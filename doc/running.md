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

### Start in background (persist after terminal close)

```bash
cd /var/www/jeer.currenciary.com/shabtc
nohup node nh_server.js >> /tmp/nh_server.log 2>&1 &
echo "PID: $!"
```

### Check if running

```bash
ps aux | grep nh_server | grep -v grep
# or
curl -s http://localhost:55560/api/live | python3 -m json.tool | head -10
```

### View live logs

```bash
tail -f /tmp/nh_server.log
```

### Stop

```bash
# Find the PID
pgrep -f nh_server.js

# Kill it
kill $(pgrep -f nh_server.js)
```

### Restart (stop + start)

```bash
kill $(pgrep -f nh_server.js) 2>/dev/null; sleep 1
cd /var/www/jeer.currenciary.com/shabtc
nohup node nh_server.js >> /tmp/nh_server.log 2>&1 &
echo "Restarted, PID: $!"
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

## Security Notes

- `nh_config.json` is chmod 600 — only readable by `adminweb`
- The API secret is never returned by `/api/config` (stripped before response)
- The dashboard has no authentication — it is intended for single-user local/VPN access only
- Do not expose port 55560 on a public interface without adding auth (nginx basic-auth or similar)
