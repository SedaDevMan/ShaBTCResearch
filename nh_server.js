'use strict';

const express = require('express');
const { WebSocketServer } = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const https = require('https');

// ── Constants ──────────────────────────────────────────────────────────────
const PORT = 55560;
const PUBLIC_DIR = path.join(__dirname, 'public');
const CONFIG_FILE = path.join(__dirname, 'nh_config.json');
const BOT_CONFIG_FILE = path.join(__dirname, 'bot_config.json');
const REFRESH_INTERVAL = 30_000;
const BOT_CYCLE_INTERVAL = 20_000;

const BINANCE_API = 'https://api.binance.com';

const NH_API = 'https://api2.nicehash.com';

const ALGO_CONFIG = {
  EQUIHASH: {
    coin: 'zec',
    blocks_per_day: 86400 / 75,   // 1152
    reward: 1.375,                // verified on-chain via Blockchair (137,500,000 zatoshis)
    halving_block: 4_406_400,     // 3rd halving ~Nov 2028 (ZIP-208: 1st=1,046,400 2nd=2,726,400 3rd=4,406,400)
  },
  RANDOMXMONERO: {
    coin: 'xmr',
    blocks_per_day: 86400 / 120,  // 720
    reward: 0.6,
    halving_block: null,
  },
};

// ── State ─────────────────────────────────────────────────────────────────
let liveCache = null;
let orderbooks = {};
let config = loadConfig();
let botConfig = loadBotConfig();
let botLog = [];       // last 200 entries
let botStatus = { enabled: false, slots_active: 0, last_cycle: null, nh_btc: null, binance_zec: null, binance_usdc: null };
let botTimer = null;
let pendingNhDeposit = null; // { expectedBtc, sentAt } — set after Binance→NH withdrawal, cleared when NH available balance confirms
let botOrderMeta = {};       // orderId → { placed_arb, placed_market_btc, placed_at }
let deadOrderStreak = 0;     // consecutive dead orders (0 miners); resets when an order fills
let deadOrderCooldownUntil = 0; // epoch ms — don't place new orders until this time

// ── Config I/O ────────────────────────────────────────────────────────────
function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_FILE))
      return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
  } catch {}
  return { org_id: '', api_key: '', api_secret: '', pools: {} };
}

function saveConfig(data) {
  const merged = { ...config, ...data };
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(merged, null, 2));
  fs.chmodSync(CONFIG_FILE, 0o600);
  config = merged;
}

// ── Bot Config I/O ────────────────────────────────────────────────────────
function loadBotConfig() {
  const defaults = {
    enabled: false, max_slots: 3, min_arb_ratio: 1.15, wait_for_arb: true,
    bid_strategy: 'cheapest_profitable', max_bid_usd: 26000,
    order_amount_btc: '0.001', order_limit_gsol: 0.003, max_limit_gsol: 0, zec_ops_pct: 30, nh_btc_threshold: 0.005,
    reprice_scale: 0.25,
    binance: { api_key: '', api_secret: '', zec_address: '' },
  };
  // NH platform minimums (from GET /main/api/v2/public/buy/info)
  // minAmount = 0.001 BTC, minLimit = 0.003 GSol/s for EQUIHASH
  try {
    if (fs.existsSync(BOT_CONFIG_FILE)) {
      const saved = JSON.parse(fs.readFileSync(BOT_CONFIG_FILE, 'utf8'));
      return { ...defaults, ...saved, binance: { ...defaults.binance, ...saved.binance } };
    }
  } catch {}
  return defaults;
}

function saveBotConfig(data) {
  const merged = {
    ...botConfig, ...data,
    binance: { ...botConfig.binance, ...(data.binance || {}) },
  };
  fs.writeFileSync(BOT_CONFIG_FILE, JSON.stringify(merged, null, 2));
  fs.chmodSync(BOT_CONFIG_FILE, 0o600);
  botConfig = merged;
}

function safeBotConfig() {
  const c = { ...botConfig, binance: { ...botConfig.binance } };
  delete c.binance.api_secret;
  return c;
}

// ── Bot logger ────────────────────────────────────────────────────────────
function botLogEntry(msg) {
  const entry = { ts: new Date().toISOString(), msg };
  botLog.push(entry);
  if (botLog.length > 200) botLog.shift();
  console.log(`[bot] ${msg}`);
  broadcast({ type: 'bot_log', entry });
}

// ── Binance REST helper ────────────────────────────────────────────────────
function binanceRequest(method, path, params = {}, signed = true) {
  const key    = botConfig.binance.api_key;
  const secret = botConfig.binance.api_secret;
  if (signed && (!key || !secret)) return Promise.reject(new Error('No Binance credentials'));

  const p = { ...params };
  if (signed) p.timestamp = Date.now();

  const qs = new URLSearchParams(p).toString();
  let fullQs = qs;
  if (signed) {
    const sig = crypto.createHmac('sha256', secret).update(qs).digest('hex');
    fullQs = `${qs}&signature=${sig}`;
  }

  const isPost = method.toUpperCase() === 'POST';
  const urlPath = isPost ? path : `${path}?${fullQs}`;
  const parsed = new URL(`${BINANCE_API}${urlPath}`);

  const options = {
    hostname: parsed.hostname,
    path: parsed.pathname + parsed.search,
    method: method.toUpperCase(),
    headers: {
      'User-Agent': 'shabtc-bot/1.0',
      'X-MBX-APIKEY': key,
      ...(isPost ? { 'Content-Type': 'application/x-www-form-urlencoded' } : {}),
    },
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, res => {
      let data = '';
      res.on('data', d => data += d);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data) }); }
        catch (e) { reject(new Error(`Binance parse error: ${e.message} body=${data.slice(0,200)}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(15_000, () => { req.destroy(); reject(new Error('Binance timeout')); });
    if (isPost) req.write(fullQs);
    req.end();
  });
}

// ── NH BTC deposit address ─────────────────────────────────────────────────
async function getNHBtcDepositAddress() {
  const result = await nhRequest('GET', '/main/api/v2/accounting/depositAddresses', 'currency=BTC');
  if (result.status !== 200) throw new Error(`NH BTC deposit addr error: ${JSON.stringify(result.body)}`);
  const list = result.body.list || [];
  const entry = list.find(a => a.currency === 'BTC');
  if (!entry) throw new Error('BTC deposit address not found in NH response');
  return entry.address;
}

// ── Bot order helpers ─────────────────────────────────────────────────────
// myOrders requires ts= (cursor-based pagination); without it NH returns 400.
function myOrdersQS() {
  return `algorithm=EQUIHASH&op=LT&limit=100&ts=${Date.now()}`;
}
// NH does not return the 'note' field in myOrders responses.
// Identify bot orders by pool ID (cached after first ensureNhPool() call)
// AND status=ACTIVE (status is an object {code,description}, not a string).
function isBotOrder(o) {
  const poolId = config.pools?.EQUIHASH?.pool_id;
  const active = o.status?.code === 'ACTIVE' || o.status === 'ACTIVE';
  return active && (!poolId || o.pool?.id === poolId);
}

// ── Bot cycle ─────────────────────────────────────────────────────────────
async function runBotCycle() {
  botLogEntry('cycle start');
  try {
    const cfg = botConfig;
    if (!liveCache) { botLogEntry('cycle skip: no live data yet'); return; }

    // Fetch NH BTC balance, NH active bot orders, Binance ZEC balance in parallel
    const [nhBtcRes, nhOrdersRes, bnAccRes] = await Promise.allSettled([
      nhRequest('GET', '/main/api/v2/accounting/accounts2'),
      nhRequest('GET', '/main/api/v2/hashpower/myOrders', myOrdersQS()),
      binanceRequest('GET', '/api/v3/account', {}),
    ]);

    const nhBtcTotal = nhBtcRes.status === 'fulfilled' && nhBtcRes.value.status === 200
      ? parseFloat(nhBtcRes.value.body.total?.totalBalance || 0)
      : null;
    const nhBtcAvail = nhBtcRes.status === 'fulfilled' && nhBtcRes.value.status === 200
      ? parseFloat(nhBtcRes.value.body.total?.available || 0)
      : null;
    const nhBtc = nhBtcTotal; // alias used for threshold checks and display

    const allNhOrders = nhOrdersRes.status === 'fulfilled' && nhOrdersRes.value.status === 200
      ? (nhOrdersRes.value.body.list || [])
      : [];
    const botOrders = allNhOrders.filter(isBotOrder);

    let bnZec = 0, bnUsdc = 0;
    if (bnAccRes.status === 'fulfilled' && bnAccRes.value.status === 200) {
      const balances = bnAccRes.value.body.balances || [];
      const zecAsset  = balances.find(b => b.asset === 'ZEC');
      const usdcAsset = balances.find(b => b.asset === 'USDC');
      bnZec  = parseFloat(zecAsset?.free  || 0);
      bnUsdc = parseFloat(usdcAsset?.free || 0);
    } else if (bnAccRes.status === 'rejected') {
      botLogEntry(`Binance account fetch error: ${bnAccRes.reason?.message || bnAccRes.reason}`);
    }

    botStatus = {
      enabled: cfg.enabled,
      slots_active: botOrders.length,
      last_cycle: new Date().toISOString(),
      nh_btc:      nhBtc,
      binance_zec:  bnZec,
      binance_usdc: bnUsdc,
    };

    // ── Pending NH deposit check ──
    // Normal blockchain confirmation: ~30 min. Compliance hold: hours/days (manual).
    const DEPOSIT_WARN_MS    = 60 * 60 * 1000;  // 1 hour — warn, may need manual action on NH
    const DEPOSIT_TIMEOUT_MS = 24 * 60 * 60 * 1000; // 24 hours — give up, don't block forever
    if (pendingNhDeposit !== null) {
      const elapsedMin = Math.round((Date.now() - pendingNhDeposit.sentAt) / 60000);
      if (nhBtcAvail !== null && nhBtcAvail >= pendingNhDeposit.expectedBtc * 0.90) {
        botLogEntry(`NH deposit confirmed: ${nhBtcAvail} BTC now available (waited ${elapsedMin}min)`);
        pendingNhDeposit = null;
      } else if (Date.now() - pendingNhDeposit.sentAt > DEPOSIT_TIMEOUT_MS) {
        botLogEntry(`⚠ NH deposit not confirmed after 24h — resuming anyway (avail=${nhBtcAvail}). Check NiceHash manually.`);
        pendingNhDeposit = null;
      } else if (Date.now() - pendingNhDeposit.sentAt > DEPOSIT_WARN_MS && !pendingNhDeposit.warnedKyc) {
        pendingNhDeposit.warnedKyc = true;
        botLogEntry(`⚠ NH deposit still pending after ${elapsedMin}min — NiceHash may require manual action (source-of-funds / KYC check). Log in to NiceHash and check your deposit status.`);
      } else {
        botLogEntry(`waiting for NH deposit: expecting ${pendingNhDeposit.expectedBtc} BTC, avail=${nhBtcAvail ?? 'n/a'} (${elapsedMin}min elapsed)`);
      }
    }

    const nhBtcPendingStr = nhBtcAvail !== null && nhBtcTotal !== null && nhBtcTotal > nhBtcAvail
      ? ` (${(nhBtcTotal - nhBtcAvail).toFixed(8)} pending)`
      : '';
    botLogEntry(`status: NH BTC=${nhBtc ?? 'n/a'}${nhBtcPendingStr} Binance ZEC=${bnZec} USDC=${bnUsdc} slots=${botOrders.length}/${cfg.max_slots}`);

    const arb = liveCache.zec_arb?.arb_ratio || 0;

    // ── Order management — skip if NH deposit still pending ──
    if (pendingNhDeposit !== null) {
      botLogEntry('order placement deferred — waiting for NH deposit to clear');
    } else if (arb < 1.0 && botOrders.length > 0) {
      botLogEntry(`arb=${arb.toFixed(4)} < 1.0 — cancelling ${botOrders.length} bot order(s)`);
      for (const o of botOrders) {
        try {
          await nhRequest('DELETE', `/main/api/v2/hashpower/order/${o.id}`);
          botLogEntry(`cancelled order ${o.id}`);
        } catch (e) {
          botLogEntry(`cancel error ${o.id}: ${e.message}`);
        }
      }
    } else if (arb < cfg.min_arb_ratio && cfg.wait_for_arb) {
      botLogEntry(`waiting for arb ≥ ${cfg.min_arb_ratio}× (current ${arb.toFixed(4)}×)`);
    } else {
      // ── Dead-order check: cancel if 0 miners for > 2 minutes since placement ──
      // Each order costs ~$2.50 to place, so only cancel truly dead orders (no miners at all).
      // Reprice-on-arb-improvement was removed — the $2.50 placement fee makes it not worth it.
      let cancelledDead = 0;
      for (const o of botOrders) {
        const meta   = botOrderMeta[o.id];
        const placedAt = meta?.placed_at || (o.startTs ? new Date(o.startTs).getTime() : 0);
        const ageMs  = placedAt ? Date.now() - placedAt : 0;
        const noMiners = parseInt(o.rigsCount || 0) === 0 && parseFloat(o.acceptedCurrentSpeed || 0) === 0;
        if (noMiners && ageMs > 2 * 60 * 1000) {
          try {
            await nhRequest('DELETE', `/main/api/v2/hashpower/order/${o.id}`);
            delete botOrderMeta[o.id];
            cancelledDead++;
            deadOrderStreak++;
            // Exponential back-off: 15min × streak (15, 30, 45 …) capped at 2h
            const cooldownMs = Math.min(deadOrderStreak * 15 * 60 * 1000, 2 * 60 * 60 * 1000);
            deadOrderCooldownUntil = Date.now() + cooldownMs;
            botLogEntry(`dead order ${o.id}: 0 miners for ${Math.round(ageMs/60000)}min — cancelled (streak=${deadOrderStreak}, pausing ${Math.round(cooldownMs/60000)}min before next bid)`);
          } catch (e) {
            botLogEntry(`dead order cancel error ${o.id}: ${e.message}`);
          }
        } else if (!noMiners) {
          // Order has miners — reset streak
          deadOrderStreak = 0;
        }
      }

      // ── Place new orders to fill empty slots ──
      const activeAfterReprice = botOrders.length - cancelledDead;
      if (deadOrderCooldownUntil > Date.now()) {
        const waitMin = Math.round((deadOrderCooldownUntil - Date.now()) / 60000);
        botLogEntry(`market too thin — pausing placement for ${waitMin}min more (streak=${deadOrderStreak})`);
      } else if (arb >= cfg.min_arb_ratio && activeAfterReprice < cfg.max_slots) {
        const slots_needed = cfg.max_slots - activeAfterReprice;
        for (let i = 0; i < slots_needed; i++) {
          try {
            await placeBotOrder(cfg, arb);
          } catch (e) {
            botLogEntry(`order placement error: ${e.message}`);
            break;
          }
        }
      }
    }

    // ── Funding check (NH BTC low) → sell ops% ZEC→BTC on Binance, withdraw to NH ──
    // Skip if a deposit is already in-flight (don't double-fund)
    let zecAfterFunding = bnZec;
    if (pendingNhDeposit === null && nhBtc !== null && nhBtc < cfg.nh_btc_threshold && bnZec > 0.01) {
      const opsZec = +(bnZec * (cfg.zec_ops_pct / 100)).toFixed(8);
      await fundNiceHash(opsZec);
      zecAfterFunding = +(bnZec - opsZec).toFixed(8);
    }

    // ── Profit sweep (remaining ZEC → USDC) ──
    if (zecAfterFunding > 0.005) {
      await sweepProfitZec(zecAfterFunding);
    }

  } catch (e) {
    botLogEntry(`cycle error: ${e.message}`);
  }
  botLogEntry('cycle end');
  broadcastBotStatus();
}

// ── Get or create NiceHash pool entry, return its UUID ────────────────────
// NH orders require a poolId (UUID), not inline pool credentials.
// We cache the ID in nh_config.json to avoid re-creating on every cycle.
async function ensureNhPool() {
  const pool = config.pools?.EQUIHASH;
  if (!pool) throw new Error('No EQUIHASH pool configured in nh_config.json');
  if (pool.pool_id) return pool.pool_id;

  // Check if a matching pool already exists on NH
  const listRes = await nhRequest('GET', '/main/api/v2/pools', 'size=100&page=0&algorithm=EQUIHASH');
  if (listRes.status === 200) {
    const existing = (listRes.body.list || []).find(p =>
      p.stratumHostname === pool.host &&
      p.stratumPort === parseInt(pool.port) &&
      p.username === pool.user
    );
    if (existing) {
      config.pools.EQUIHASH.pool_id = existing.id;
      saveConfig({ pools: config.pools });
      botLogEntry(`NH pool found: ${existing.id}`);
      return existing.id;
    }
  }

  // Create it
  const createRes = await nhRequest('POST', '/main/api/v2/pools', '', JSON.stringify({
    algorithm:       'EQUIHASH',
    name:            '2miners-zec',
    stratumHostname: pool.host,
    stratumPort:     parseInt(pool.port),
    username:        pool.user,
    password:        pool.pass || 'x',
  }));
  if (createRes.status === 200 || createRes.status === 201) {
    const poolId = createRes.body.id;
    config.pools.EQUIHASH.pool_id = poolId;
    saveConfig({ pools: config.pools });
    botLogEntry(`NH pool created: ${poolId}`);
    return poolId;
  }
  throw new Error(`NH pool create failed ${createRes.status}: ${JSON.stringify(createRes.body)}`);
}

async function placeBotOrder(cfg, arb) {
  const pool = config.pools && config.pools.EQUIHASH;
  if (!pool) throw new Error('No EQUIHASH pool configured in nh_config.json');

  const btcPrice = liveCache.prices.btc;
  const eqMkt   = liveCache.nh_market.EQUIHASH;

  // Bid = min fill price + $50 premium.
  // Bidding below min_fill_btc gets zero hashrate (all miners already allocated to higher bids).
  // A $50 premium above the cheapest paying order is enough to attract those miners to us.
  const premiumBtc  = 50 / btcPrice;
  const minFillBtc  = eqMkt.min_fill_btc || eqMkt.btc;
  // NH requires price precision ≤ 4 decimal places (orderbook prices all follow this pattern)
  const bidBtc      = +(Math.round((minFillBtc + premiumBtc) * 10000) / 10000).toFixed(8);

  if (bidBtc * btcPrice > cfg.max_bid_usd) {
    botLogEntry(`bid $${(bidBtc * btcPrice).toFixed(0)} > max_bid_usd $${cfg.max_bid_usd} — skipping`);
    return;
  }

  // ── Edge check: verify arb at our specific bid before committing ──
  // Global arb uses cheapest alive order (market price), but we may bid lower.
  // Recalculate with our actual bid to be sure we have positive edge.
  const actualUsdPerUnit = liveCache.zec_arb?.actual_usd_per_unit || 0;
  const NH_FEE = 1.03;
  const arbAtBid = actualUsdPerUnit > 0 ? actualUsdPerUnit / (bidBtc * btcPrice * NH_FEE) : 0;
  if (arbAtBid < cfg.min_arb_ratio) {
    botLogEntry(`no edge at bid ${bidBtc.toFixed(8)} BTC: arb=${arbAtBid.toFixed(4)}× < min ${cfg.min_arb_ratio}× — pausing, will retry next cycle`);
    return;
  }

  // Configurable hashrate limit — higher = faster cycle, lower timing risk.
  // Estimated cycle hours = (order_amount_btc / (limit_gsol × bid_btc)) / 24h
  // NH platform minimums: minAmount=0.001 BTC, minLimit=0.003 GSol/s (EQUIHASH)
  const NH_MIN_AMOUNT = 0.001;
  const NH_MIN_LIMIT  = 0.003;

  // Auto-limit: use available market speed, capped by max_limit_gsol config.
  // Leave a 10% buffer so we don't grab the entire pool.
  const availableGsol = eqMkt.available_gsol || 0;
  const maxLimitCap   = cfg.max_limit_gsol > 0 ? cfg.max_limit_gsol : Infinity;
  const autoLimit     = +(availableGsol * 0.90).toFixed(6);
  const limitGsol     = Math.max(NH_MIN_LIMIT, Math.min(autoLimit, maxLimitCap));

  const amountBtc   = Math.max(NH_MIN_AMOUNT, parseFloat(cfg.order_amount_btc));
  const costPerDay  = limitGsol * bidBtc;  // BTC/day at full limit
  const cycleHours  = costPerDay > 0 ? +(amountBtc / costPerDay * 24).toFixed(1) : '?';

  const poolId = await ensureNhPool();

  const body = JSON.stringify({
    market:               'EU',
    algorithm:            'EQUIHASH',
    amount:               amountBtc.toFixed(8),
    price:                bidBtc.toFixed(8),
    limit:                limitGsol.toFixed(8),
    poolId,
    type:                 'STANDARD',
    marketFactor:         '1000000000.00000000',
    displayMarketFactor:  'GSol',
    priceFactor:          '1000000000.00000000',
    displayPriceFactor:   'GSol',
    note:                 'shabtc-bot',
  });

  const result = await nhRequest('POST', '/main/api/v2/hashpower/order', '', body);
  if (result.status === 200 || result.status === 201) {
    const id = result.body.id || '?';
    const finishTs = new Date(Date.now() + cycleHours * 3600_000);
    const finishStr = finishTs.toISOString().slice(0,16).replace('T',' ');
    botOrderMeta[id] = { placed_arb: arb, placed_market_btc: eqMkt.btc, placed_at: Date.now() };
    botLogEntry(`placed order ${id} bid=${bidBtc.toFixed(8)} BTC ($${(bidBtc*btcPrice).toFixed(0)}/GSol/day) edge=${arbAtBid.toFixed(4)}× limit=${limitGsol} GSol/s amount=${cfg.order_amount_btc} BTC — duration: ${cycleHours}h finished: ${finishStr}`);
  } else {
    throw new Error(`NH order failed ${result.status}: ${JSON.stringify(result.body)}`);
  }
}

async function fundNiceHash(opsZec) {
  try {
    botLogEntry(`NH BTC low — selling ${opsZec} ZEC→BTC on Binance then withdrawing to NiceHash`);

    // Step 1: sell ZEC→BTC on Binance
    const sellResult = await binanceRequest('POST', '/api/v3/order', {
      symbol: 'ZECBTC', side: 'SELL', type: 'MARKET', quantity: opsZec.toFixed(8),
    });
    if (sellResult.status !== 200 && sellResult.status !== 201) {
      botLogEntry(`ZEC→BTC sell error: ${JSON.stringify(sellResult.body)}`);
      return;
    }
    const fills = sellResult.body.fills || [];
    const btcGross = +fills.reduce((s, f) => s + parseFloat(f.price) * parseFloat(f.qty), 0).toFixed(8);
    botLogEntry(`Sold ${opsZec} ZEC → ${btcGross} BTC`);

    // Step 2: withdraw BTC to NiceHash
    // Binance auto-deducts fee from withdrawal amount; we just specify btcGross.
    // Real Binance BTC fee = 0.000015 BTC; minimum withdrawal = 0.00012 BTC.
    const BTC_WITHDRAW_FEE = 0.000015;
    const BTC_MIN_WITHDRAW  = 0.00012;
    if (btcGross < BTC_MIN_WITHDRAW) {
      botLogEntry(`BTC too small to withdraw (${btcGross} < min ${BTC_MIN_WITHDRAW}) — skipping`);
      return;
    }
    const nhBtcAddr = await getNHBtcDepositAddress();
    const nhReceives = +(btcGross - BTC_WITHDRAW_FEE).toFixed(8);
    const feeDollar  = (BTC_WITHDRAW_FEE * (liveCache?.prices?.btc || 80000)).toFixed(0);
    const wdResult = await binanceRequest('POST', '/sapi/v1/capital/withdraw/apply', {
      coin: 'BTC', network: 'BTC', address: nhBtcAddr, amount: btcGross.toFixed(8),
    });
    if (wdResult.status === 200) {
      botLogEntry(`Sent ${btcGross} BTC → NH receives ${nhReceives} BTC (fee ~$${feeDollar}) id=${wdResult.body.id} — waiting for confirmation`);
      pendingNhDeposit = { expectedBtc: nhReceives, sentAt: Date.now() };
    } else {
      botLogEntry(`BTC withdrawal error: ${JSON.stringify(wdResult.body)}`);
    }
  } catch (e) {
    botLogEntry(`fundNiceHash error: ${e.message}`);
  }
}

async function sweepProfitZec(profitZec) {
  try {
    const result = await binanceRequest('POST', '/api/v3/order', {
      symbol: 'ZECUSDC', side: 'SELL', type: 'MARKET', quantity: profitZec.toFixed(8),
    });
    if (result.status === 200 || result.status === 201) {
      const fills = result.body.fills || [];
      const totalUsdc = fills.reduce((s, f) => s + parseFloat(f.price || 0) * parseFloat(f.qty || 0), 0);
      botLogEntry(`Converted ${profitZec.toFixed(4)} ZEC → $${totalUsdc.toFixed(2)} USDC (profit sweep)`);
    } else {
      botLogEntry(`ZEC→USDC sweep error: ${JSON.stringify(result.body)}`);
    }
  } catch (e) {
    botLogEntry(`sweepProfitZec error: ${e.message}`);
  }
}

function broadcastBotStatus() {
  broadcast({ type: 'bot_status', data: botStatus });
}

function startBot() {
  if (botTimer) clearInterval(botTimer);
  botConfig.enabled = true;
  saveBotConfig({ enabled: true });
  botLogEntry('Bot started');
  runBotCycle();
  botTimer = setInterval(runBotCycle, BOT_CYCLE_INTERVAL);
}

function stopBot() {
  botConfig.enabled = false;
  saveBotConfig({ enabled: false });
  if (botTimer) { clearInterval(botTimer); botTimer = null; }
  botStatus.enabled = false;
  botLogEntry('Bot stopped');
  broadcastBotStatus();
}

// Auto-start deferred to after wss is initialized (see server.listen)

// ── HTTP helpers ──────────────────────────────────────────────────────────
function fetchJSON(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': 'shabtc-dashboard/1.0' } }, res => {
      let body = '';
      res.on('data', d => body += d);
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(new Error(`JSON parse failed for ${url}: ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(10_000, () => { req.destroy(); reject(new Error(`Timeout: ${url}`)); });
  });
}

function nhRequest(method, endpoint, query = '', body = '') {
  const key = config.api_key;
  const secret = config.api_secret;
  if (!key || !secret) return Promise.reject(new Error('No NH credentials'));

  const ts    = Date.now().toString();
  const nonce = crypto.randomUUID();
  const reqId = crypto.randomUUID();  // X-Request-Id must be different from X-Nonce
  // Many NH endpoints require ts as a query param (cursor); append if not already present
  const finalQuery = query
    ? (query.includes('ts=') ? query : `${query}&ts=${ts}`)
    : `ts=${ts}`;
  // Official NiceHash HMAC format: key\0time\0nonce\0\0org_id\0\0METHOD\0path\0query[\0body]
  let msg = [key, ts, nonce, '', config.org_id || '', '', method.toUpperCase(), endpoint, finalQuery].join('\0');
  if (body) msg += '\0' + body;
  const sig = crypto.createHmac('sha256', secret).update(msg).digest('hex');

  const qs = finalQuery ? `?${finalQuery}` : '';
  const parsed = new URL(`${NH_API}${endpoint}${qs}`);

  const options = {
    hostname: parsed.hostname,
    path: parsed.pathname + parsed.search,
    method: method.toUpperCase(),
    headers: {
      'X-Time': ts,
      'X-Nonce': nonce,
      'X-Request-Id': reqId,
      'X-Auth': `${key}:${sig}`,
      'X-Organization-Id': config.org_id || '',
      'Content-Type': 'application/json',
      'User-Agent': 'shabtc-dashboard/1.0',
    },
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, res => {
      let data = '';
      res.on('data', d => data += d);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data) }); }
        catch (e) { reject(new Error(`NH parse error: ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(15_000, () => { req.destroy(); reject(new Error('NH timeout')); });
    if (body) req.write(body);
    req.end();
  });
}

// ── NiceHash public API ───────────────────────────────────────────────────
async function fetchNHAlgoStats() {
  try {
    const simple = await fetchJSON(`${NH_API}/main/api/v2/public/simplemultialgo/info`);
    const result = {};
    if (simple && simple.miningAlgorithms) {
      for (const a of simple.miningAlgorithms)
        result[a.algorithm] = parseFloat(a.paying || 0);
    }
    return result;
  } catch (e) {
    console.error('NH algo stats error:', e.message);
    return {};
  }
}

async function fetchNHOrderbook(algo) {
  try {
    const url = `${NH_API}/main/api/v2/hashpower/orderBook?algorithm=${algo}&size=100&page=0`;
    const data = await fetchJSON(url);
    if (!data) return [];
    let raw = [];
    if (data.orders) {
      raw = data.orders;
    } else if (data.stats) {
      for (const loc of Object.values(data.stats))
        if (loc.orders) raw.push(...loc.orders);
    }
    return raw;
  } catch (e) {
    console.error(`NH orderbook ${algo} error:`, e.message);
    return [];
  }
}

// ── Blockchair stats ──────────────────────────────────────────────────────
async function fetchBlockchairStats(coin) {
  try {
    const data = await fetchJSON(`https://api.blockchair.com/${coin}/stats`);
    return data && data.data ? data.data : null;
  } catch (e) {
    console.error(`Blockchair ${coin} error:`, e.message);
    return null;
  }
}

// ── Available hashrate at a given price point ─────────────────────────────
// Returns GSol/s of miners we can attract by bidding AT targetPrice.
// = paying speed of all orders priced BELOW targetPrice (those miners will switch to us).
function availableSpeedAtPrice(rawOrders, targetPrice) {
  return rawOrders
    .filter(o => o.alive && parseFloat(o.price || 0) < targetPrice)
    .reduce((sum, o) => sum + parseFloat(o.payingSpeed || o.acceptedCurrentSpeed || 0), 0);
}

// ── Minimum price that actually attracts hashrate ─────────────────────────
// = cheapest order with meaningful paying speed (> 1 MSol/s).
// Bidding below this gets zero fill.
function minFillPrice(rawOrders) {
  const paying = rawOrders
    .filter(o => o.alive && parseFloat(o.payingSpeed || o.acceptedCurrentSpeed || 0) > 0.001)
    .sort((a, b) => parseFloat(a.price) - parseFloat(b.price));
  return paying.length > 0 ? parseFloat(paying[0].price) : 0;
}

// ── Market price: cheapest alive STANDARD order in orderbook ─────────────
// Using simplemultialgo/info is WRONG — its unit is ambiguous and it represents
// NiceHash's internal seller payout, not the actual buyer market price.
// Source of truth = the real order book.
function cheapestAlivePrice(rawOrders) {
  // Priority 1: orders actually being filled (acceptedCurrentSpeed > 0) — most reliable
  const filled = rawOrders
    .filter(o => o.alive && parseFloat(o.price) > 0 && parseFloat(o.acceptedCurrentSpeed) > 0)
    .sort((a, b) => parseFloat(a.price) - parseFloat(b.price));
  if (filled.length) return { price: parseFloat(filled[0].price), quality: 'ok' };

  // Priority 2: orders with rigs assigned (likely to fill soon)
  const withRigs = rawOrders
    .filter(o => o.alive && parseFloat(o.price) > 0 && parseInt(o.rigsCount || 0) > 0)
    .sort((a, b) => parseFloat(a.price) - parseFloat(b.price));
  if (withRigs.length) return { price: parseFloat(withRigs[0].price), quality: 'verify' };

  // Priority 3: median of all alive orders (avoids ghost-bid cheapest outlier)
  const alive = rawOrders
    .filter(o => o.alive && parseFloat(o.price) > 0)
    .map(o => parseFloat(o.price))
    .sort((a, b) => a - b);
  if (alive.length) {
    const med = alive[Math.floor(alive.length / 2)];
    return { price: med, quality: 'suspicious' };
  }

  return { price: 0, quality: 'suspicious' };
}

// ── Arb calculation ───────────────────────────────────────────────────────
function calcArb(algo, networkHR, coinPrice, btcPrice, marketBTC, halvingDays, priceQuality = 'ok') {
  const cfg = ALGO_CONFIG[algo];
  if (!networkHR || !coinPrice || !marketBTC) return null;

  const actual_usd_per_unit = (1 / networkHR) * cfg.blocks_per_day * cfg.reward * coinPrice * 0.99;
  const breakeven_bid_usd   = actual_usd_per_unit / 1.03;
  const nh_market_usd       = marketBTC * btcPrice;
  const arb_ratio           = actual_usd_per_unit / nh_market_usd;

  // Post-halving projection (ZEC only, shown when halving < 90 days away)
  let post_halving_ratio = null;
  if (cfg.halving_block && halvingDays !== null && halvingDays < 90) {
    const post_usd = (1 / networkHR) * cfg.blocks_per_day * (cfg.reward / 2) * coinPrice * 0.99;
    post_halving_ratio = +(post_usd / nh_market_usd).toFixed(4);
  }

  // data_quality: inherit from price source quality, or flag if ratio is extreme
  const data_quality = priceQuality === 'suspicious' || arb_ratio > 5
    ? 'suspicious'
    : priceQuality === 'verify' || arb_ratio > 2
    ? 'verify'
    : 'ok';

  return {
    actual_usd_per_unit: +actual_usd_per_unit.toFixed(2),
    breakeven_bid_usd:   +breakeven_bid_usd.toFixed(2),
    nh_market_usd:       +nh_market_usd.toFixed(2),
    nh_market_btc:       marketBTC,
    arb_ratio:           +arb_ratio.toFixed(4),
    post_halving_ratio,
    data_quality,
    opportunity:         arb_ratio > 1.0 && data_quality !== 'suspicious',
  };
}

function enrichOrders(orders, breakeven_usd, btcPrice, networkHR, coinPrice, algo) {
  const cfg = ALGO_CONFIG[algo];
  return orders.map(o => {
    const bid_btc         = parseFloat(o.price || 0);
    const bid_usd         = bid_btc * btcPrice;
    const activeSpeed     = parseFloat(o.acceptedCurrentSpeed || 0);
    const limitSpeed      = parseFloat(o.limit || 0);
    const rigsCount       = parseInt(o.rigsCount || 0);
    const type            = o.type || 'STANDARD';

    // "fillable" = has rigs assigned OR is actively hashing.
    // Ghost bids (alive=true, rigsCount=0, speed=0) will never fill — exclude from profit calc.
    const fillable = rigsCount > 0 || activeSpeed > 0;
    // Speed for profit calc: actual if running, limit as potential if rigs assigned
    const speed = activeSpeed > 0 ? activeSpeed : (fillable ? limitSpeed : 0);

    let profit_day = null;
    if (fillable && speed > 0 && coinPrice && networkHR) {
      const revenue = (speed / networkHR) * cfg.blocks_per_day * cfg.reward * coinPrice * 0.99;
      const cost    = bid_usd * speed * 1.03;
      profit_day    = +(revenue - cost).toFixed(2);
    }

    return {
      id: o.id, type,
      bid_btc: +bid_btc.toFixed(8),
      bid_usd: +bid_usd.toFixed(2),
      speed:   +speed.toFixed(6),
      rigs:    rigsCount,
      alive:   o.alive,
      profit_day,
      // Profitable only if fillable AND below break-even AND positive margin
      profitable: o.alive && fillable && profit_day !== null && profit_day > 0,
    };
  }).sort((a, b) => a.bid_usd - b.bid_usd);
}

// ── Main refresh ──────────────────────────────────────────────────────────
async function refresh() {
  try {
    console.log('[refresh] fetching...');

    const [geckoRes, zecRes, xmrRes] = await Promise.allSettled([
      fetchJSON('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,zcash,monero&vs_currencies=usd'),
      fetchBlockchairStats('zcash'),
      fetchBlockchairStats('monero'),
    ]);

    const prices   = geckoRes.status === 'fulfilled' ? geckoRes.value : {};
    const btcPrice = prices.bitcoin?.usd || 0;
    const zecPrice = prices.zcash?.usd   || 0;
    const xmrPrice = prices.monero?.usd  || 0;

    const zecData  = zecRes.status === 'fulfilled' ? zecRes.value : null;
    const xmrData  = xmrRes.status === 'fulfilled' ? xmrRes.value : null;

    const zecNetHR = zecData ? (zecData.hashrate_24h || 0) / 1e9 : 0; // GSol/s
    const xmrNetHR = xmrData ? (xmrData.hashrate_24h || 0) / 1e9 : 0; // GH/s

    const zecBlock     = zecData ? (zecData.best_block_height || 0) : 0;
    const halvingBlock = ALGO_CONFIG.EQUIHASH.halving_block;
    const blocksLeft   = Math.max(0, halvingBlock - zecBlock);
    const halving_days = +(blocksLeft / ALGO_CONFIG.EQUIHASH.blocks_per_day).toFixed(1);

    // Fetch order books first — market price is derived from real orders, not simplemultialgo
    const [eqRes, xmrObRes] = await Promise.allSettled([
      fetchNHOrderbook('EQUIHASH'),
      fetchNHOrderbook('RANDOMXMONERO'),
    ]);
    const rawEq  = eqRes.status    === 'fulfilled' ? eqRes.value    : [];
    const rawXmr = xmrObRes.status === 'fulfilled' ? xmrObRes.value : [];

    // Market price = cheapest alive STANDARD order in the real order book
    const eqMkt  = cheapestAlivePrice(rawEq);
    const xmrMkt = cheapestAlivePrice(rawXmr);

    const zecArb = calcArb('EQUIHASH',      zecNetHR, zecPrice, btcPrice, eqMkt.price,  halving_days, eqMkt.quality);
    const xmrArb = calcArb('RANDOMXMONERO', xmrNetHR, xmrPrice, btcPrice, xmrMkt.price, null,          xmrMkt.quality);

    orderbooks.EQUIHASH      = enrichOrders(rawEq,  zecArb?.breakeven_bid_usd || 0, btcPrice, zecNetHR, zecPrice, 'EQUIHASH');
    orderbooks.RANDOMXMONERO = enrichOrders(rawXmr, xmrArb?.breakeven_bid_usd || 0, btcPrice, xmrNetHR, xmrPrice, 'RANDOMXMONERO');

    liveCache = {
      ts: Date.now(),
      prices:  { btc: btcPrice, zec: zecPrice, xmr: xmrPrice },
      network: { zec_hr_gsol: +zecNetHR.toFixed(4), xmr_hr_gh: +xmrNetHR.toFixed(4), zec_block: zecBlock },
      halving: { block: halvingBlock, current_block: zecBlock, blocks_remaining: blocksLeft, days: halving_days },
      nh_market: {
        EQUIHASH:      { btc: eqMkt.price,  usd: +(eqMkt.price  * btcPrice).toFixed(2), source: eqMkt.quality, available_gsol: +availableSpeedAtPrice(rawEq, eqMkt.price + 50/btcPrice).toFixed(4), min_fill_btc: +minFillPrice(rawEq).toFixed(8) },
        RANDOMXMONERO: { btc: xmrMkt.price, usd: +(xmrMkt.price * btcPrice).toFixed(2), source: xmrMkt.quality },
      },
      zec_arb: zecArb,
      xmr_arb: xmrArb,
      order_counts: {
        EQUIHASH:      orderbooks.EQUIHASH.length,
        RANDOMXMONERO: orderbooks.RANDOMXMONERO.length,
        alive_eq:      rawEq.filter(o => o.alive).length,
        alive_xmr:     rawXmr.filter(o => o.alive).length,
      },
    };

    broadcast({ type: 'live', data: { ...liveCache, bot: { enabled: botStatus.enabled, slots_active: botStatus.slots_active, last_cycle: botStatus.last_cycle, nh_btc: botStatus.nh_btc, binance_zec: botStatus.binance_zec, binance_usdc: botStatus.binance_usdc } } });
    console.log(`[refresh] BTC=$${btcPrice} ZEC=$${zecPrice} XMR=$${xmrPrice} ZEC_arb=${zecArb?.arb_ratio} (${zecArb?.data_quality}) XMR_arb=${xmrArb?.arb_ratio} (${xmrArb?.data_quality})`);
  } catch (e) {
    console.error('[refresh] error:', e.message);
  }
}

// ── Express app ───────────────────────────────────────────────────────────
const app = express();
app.use(express.json());
app.use(express.static(PUBLIC_DIR, { etag: false, maxAge: 0 }));

app.get('/api/live', (req, res) => {
  if (!liveCache) return res.status(503).json({ error: 'Data not yet loaded, try again shortly' });
  res.json(liveCache);
});

app.get('/api/orderbook', (req, res) => {
  const algo = req.query.algo || 'EQUIHASH';
  const book = orderbooks[algo];
  if (!book) return res.status(404).json({ error: 'Unknown algo' });
  res.json({ algo, orders: book, count: book.length });
});

app.get('/api/myorders', async (req, res) => {
  const algo = req.query.algo || 'EQUIHASH';
  try {
    const result = await nhRequest('GET', '/main/api/v2/hashpower/myOrders', `algorithm=${algo}&op=LT&limit=100&ts=${Date.now()}`);
    res.status(result.status).json(result.body);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/order', async (req, res) => {
  try {
    const result = await nhRequest('POST', '/main/api/v2/hashpower/order', '', JSON.stringify(req.body));
    res.status(result.status).json(result.body);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.delete('/api/order/:id', async (req, res) => {
  try {
    const result = await nhRequest('DELETE', `/main/api/v2/hashpower/order/${req.params.id}`);
    res.status(result.status).json(result.body);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/config', (req, res) => {
  const safe = { ...config };
  delete safe.api_secret;
  res.json(safe);
});

app.post('/api/config', (req, res) => {
  try {
    saveConfig(req.body);
    res.json({ ok: true });
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

// ── Bot endpoints ─────────────────────────────────────────────────────────
app.get('/api/bot/status', async (req, res) => {
  // Also fetch live NH active bot orders for the UI
  try {
    const result = await nhRequest('GET', '/main/api/v2/hashpower/myOrders', myOrdersQS());
    const orders = result.status === 200
      ? (result.body.list || []).filter(isBotOrder)
      : [];
    const btcPrice = liveCache?.prices?.btc || 0;
    const enriched = orders.map(o => ({
      id:       o.id,
      market:   o.market || '?',
      bid_btc:  parseFloat(o.price || 0),
      bid_usd:  +(parseFloat(o.price || 0) * btcPrice).toFixed(2),
      speed:    parseFloat(o.acceptedCurrentSpeed || 0),
      active:   o.status?.code === 'ACTIVE',
    }));
    res.json({ ...botStatus, orders: enriched });
  } catch (e) {
    res.json({ ...botStatus, orders: [], error: e.message });
  }
});

app.get('/api/bot/config', (req, res) => {
  res.json(safeBotConfig());
});

app.post('/api/bot/config', (req, res) => {
  try {
    saveBotConfig(req.body);
    res.json({ ok: true });
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

app.post('/api/bot/start', (req, res) => {
  startBot();
  res.json({ ok: true, enabled: true });
});

app.post('/api/bot/stop', async (req, res) => {
  stopBot();
  if (req.body && req.body.cancel_orders) {
    try {
      const result = await nhRequest('GET', '/main/api/v2/hashpower/myOrders', myOrdersQS());
      const orders = result.status === 200 ? (result.body.list || []).filter(isBotOrder) : [];
      for (const o of orders) {
        await nhRequest('DELETE', `/main/api/v2/hashpower/order/${o.id}`).catch(() => {});
      }
      botLogEntry(`Cancelled ${orders.length} bot orders on stop`);
    } catch (e) {
      botLogEntry(`cancel on stop error: ${e.message}`);
    }
  }
  res.json({ ok: true, enabled: false });
});

app.get('/api/bot/log', (req, res) => {
  res.json({ entries: botLog.slice(-200) });
});

app.post('/api/bot/topup', async (req, res) => {
  try {
    const bnAccRes = await binanceRequest('GET', '/api/v3/account', {});
    const balances = bnAccRes.body.balances || [];
    const bnZec = parseFloat((balances.find(b => b.asset === 'ZEC') || {}).free || 0);
    const opsZec = +(bnZec * ((botConfig.zec_ops_pct || 30) / 100)).toFixed(8);
    await fundNiceHash(opsZec);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/bot/sweep', async (req, res) => {
  try {
    const bnAccRes = await binanceRequest('GET', '/api/v3/account', {});
    const balances = bnAccRes.body.balances || [];
    const bnZec = parseFloat((balances.find(b => b.asset === 'ZEC') || {}).free || 0);
    if (bnZec > 0.005) {
      await sweepProfitZec(bnZec);
      res.json({ ok: true, swept_zec: bnZec });
    } else {
      res.json({ ok: false, msg: 'Not enough ZEC to sweep', bnZec });
    }
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── HTTP + WebSocket server ───────────────────────────────────────────────
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });

function broadcast(msg) {
  const str = JSON.stringify(msg);
  wss.clients.forEach(ws => {
    if (ws.readyState === ws.OPEN) ws.send(str);
  });
}

wss.on('connection', ws => {
  if (liveCache) ws.send(JSON.stringify({ type: 'live', data: { ...liveCache, bot: { enabled: botStatus.enabled, slots_active: botStatus.slots_active, last_cycle: botStatus.last_cycle, nh_btc: botStatus.nh_btc, binance_zec: botStatus.binance_zec, binance_usdc: botStatus.binance_usdc } } }));
  ws.on('message', raw => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'ping') ws.send(JSON.stringify({ type: 'pong', ts: Date.now() }));
    } catch {}
  });
});

// ── Start ─────────────────────────────────────────────────────────────────
server.listen(PORT, () => {
  console.log(`shabtc NiceHash dashboard listening on http://0.0.0.0:${PORT}`);
  console.log(`  Landing:   http://localhost:${PORT}/`);
  console.log(`  Dashboard: http://localhost:${PORT}/nicehash/`);
  console.log(`  Bot:       http://localhost:${PORT}/bot/`);
  console.log(`  API live:  http://localhost:${PORT}/api/live`);
  // Auto-start bot after wss is initialized
  if (botConfig.enabled) startBot();
});

refresh();
setInterval(refresh, REFRESH_INTERVAL);
