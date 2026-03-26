"""
kalshi_backend.py
─────────────────────────────────────────────────────────────────────────────
FastAPI backend — Kalshi REST v2 + Binance vol engine
─────────────────────────────────────────────────────────────────────────────
"""

import os
import math
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

KALSHI_KEY_ID   = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
KALSHI_DEMO     = os.getenv("KALSHI_DEMO", "true").lower() == "true"
KRAKEN_BASE     = os.getenv("KRAKEN_BASE", "https://api.kraken.com")

# Kraken uses different ticker symbols than Binance
# BTC → XBTUSD,  ETH → ETHUSD
KRAKEN_SYMBOLS  = {"BTC": "XBTUSD", "ETH": "ETHUSD"}

KALSHI_BASE = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if KALSHI_DEMO else
    "https://api.kalshi.com/trade-api/v2"
)

EDGE_THRESHOLD = 0.05
SIGMA_FLOOR    = 0.05
HOURS_PER_YEAR = 365 * 24
VOL_CACHE_TTL  = 300
MAX_CONTRACTS  = 10
MAX_TRADES_LOG = 50

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("kalshi_bot")


# ── Black-Scholes ─────────────────────────────────────────────────────────────
def norm_cdf(x):
    if x < -10: return 0.0
    if x > 10:  return 1.0
    sign = 1 if x >= 0 else -1
    t    = 1 / (1 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
           t * (-1.821255978 + t * 1.330274429))))
    phi  = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    cdf  = 1 - phi * poly
    return cdf if sign > 0 else 1 - cdf


def calc_probability(S, K, T_hours, sigma):
    sigma = max(sigma, SIGMA_FLOOR)
    T_yr  = max(T_hours / HOURS_PER_YEAR, 1 / HOURS_PER_YEAR / 60)
    d2    = (math.log(S / K) - (sigma ** 2 / 2) * T_yr) / (sigma * math.sqrt(T_yr))
    return {"prob": norm_cdf(d2), "d2": d2}


def ewma_vol(closes, lam=0.94):
    if len(closes) < 2: return SIGMA_FLOOR
    rets  = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    seed  = rets[:10]
    mean  = sum(seed) / len(seed)
    var_t = sum((r - mean) ** 2 for r in seed) / max(len(seed) - 1, 1)
    for r in rets[10:]:
        var_t = lam * var_t + (1 - lam) * r * r
    return max(math.sqrt(var_t) * math.sqrt(365 * 24 * 60), SIGMA_FLOOR)


# ── Kalshi auth ───────────────────────────────────────────────────────────────
class KalshiAuth:
    def __init__(self):
        self._private_key = None
        # 1. Try env var KALSHI_PRIVATE_KEY (Railway / cloud deployments)
        # 2. Fall back to file path KALSHI_PRIVATE_KEY_PATH (local)
        pem_env = os.getenv("KALSHI_PRIVATE_KEY", "")
        try:
            if pem_env:
                pem_bytes = pem_env.replace("\n", "\n").encode()
                self._private_key = serialization.load_pem_private_key(
                    pem_bytes, password=None)
                log.info("RSA key loaded from environment variable")
            else:
                with open(KALSHI_KEY_PATH, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(
                        f.read(), password=None)
                log.info("RSA key loaded from %s", KALSHI_KEY_PATH)
        except (FileNotFoundError, ValueError) as e:
            log.warning("Could not load private key: %s", e)

    def _sign(self, method, path, ts):
        import base64
        msg = f"{ts}{method}{path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def headers(self, method, path):
        ts  = str(int(time.time() * 1000))
        sig = self._sign(method.upper(), path, ts) if self._private_key else ""
        return {
            "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type":            "application/json",
        }


auth           = KalshiAuth()
kalshi_client  = httpx.AsyncClient(base_url=KALSHI_BASE, timeout=10)
kraken_client  = httpx.AsyncClient(base_url=KRAKEN_BASE, timeout=5)


# ── Kraken ────────────────────────────────────────────────────────────────────
# Ticker:  GET /0/public/Ticker?pair=XBTUSD
# OHLC:    GET /0/public/OHLC?pair=XBTUSD&interval=1
# Note: Kraken uses XBT for Bitcoin (not BTC).
# OHLC candle format: [time, open, high, low, CLOSE, vwap, volume, count]
# Close price is index [4] — same position as Binance.
_vol_cache = {}


async def get_spot_price(asset):
    pair = KRAKEN_SYMBOLS[asset]           # BTC -> XBTUSD, ETH -> ETHUSD
    r    = await kraken_client.get(f"/0/public/Ticker?pair={pair}")
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError(f"Kraken ticker error: {data['error']}")
    # Kraken returns the pair under its canonical name (e.g. XXBTZUSD)
    result = data["result"]
    ticker = next(iter(result.values()))
    # "c" = last trade closed: [price, lot volume]
    return float(ticker["c"][0])


async def get_vol(asset):
    now = time.time()
    if asset in _vol_cache and now - _vol_cache[asset][1] < VOL_CACHE_TTL:
        return _vol_cache[asset][0]
    pair = KRAKEN_SYMBOLS[asset]
    # interval=1 -> 1-minute candles; Kraken returns up to 720
    r = await kraken_client.get(f"/0/public/OHLC?pair={pair}&interval=1")
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError(f"Kraken OHLC error: {data['error']}")
    result  = data["result"]
    candles = result[next(k for k in result if k != "last")]
    # Most recent 61 complete candles (drop last — it is still open)
    closes  = [float(c[4]) for c in candles[-62:-1]]
    sigma   = ewma_vol(closes)
    _vol_cache[asset] = (sigma, now)
    log.info("Vol updated (Kraken) | %s sigma=%.4f", asset, sigma)
    return sigma


# ── Kalshi market helpers ─────────────────────────────────────────────────────
async def find_crypto_markets():
    markets = []
    for series in ["KXBTCD", "KXETHD"]:
        path = f"/markets?series_ticker={series}&status=open&limit=10"
        try:
            r = await kalshi_client.get(path, headers=auth.headers("GET", path))
            r.raise_for_status()
            markets.extend(r.json().get("markets", []))
        except Exception as e:
            log.warning("Market fetch error %s: %s", series, e)
    return markets


async def get_orderbook(ticker):
    path = f"/markets/{ticker}/orderbook"
    r    = await kalshi_client.get(path, headers=auth.headers("GET", path))
    r.raise_for_status()
    return r.json().get("orderbook", {})


async def place_order(ticker, side, contracts, limit_price):
    path    = "/portfolio/orders"
    payload = {
        "ticker":    ticker,
        "action":    "buy",
        "side":      side,
        "type":      "limit",
        "count":     contracts,
        "yes_price": limit_price if side == "yes" else 100 - limit_price,
    }
    r = await kalshi_client.post(
        path, json=payload, headers=auth.headers("POST", path))
    r.raise_for_status()
    return r.json()


async def get_positions():
    path = "/portfolio/positions"
    r    = await kalshi_client.get(path, headers=auth.headers("GET", path))
    r.raise_for_status()
    return r.json().get("market_positions", [])


# ── Session state ─────────────────────────────────────────────────────────────
session_start = time.time()
state = {
    "trades":        [],
    "cum_pnl":       0.0,
    "pnl_history":   [{"t": 0, "v": 0.0}],
    "wins":          0,
    "total_trades":  0,
    "total_edge":    0.0,
    "last_markets":  [],
    "conn_kalshi":   False,
    "conn_kraken":   False,
    "errors":        [],
}


def record_trade(ticker, asset, signal, fair, mkt, fill, edge, pnl):
    state["total_trades"] += 1
    state["total_edge"]   += abs(edge)
    state["cum_pnl"]      += pnl
    if pnl > 0:
        state["wins"] += 1
    elapsed = (time.time() - session_start) / 60
    state["pnl_history"].append(
        {"t": round(elapsed, 1), "v": round(state["cum_pnl"], 2)})
    state["pnl_history"] = state["pnl_history"][-80:]
    state["trades"] = [{
        "id":     state["total_trades"],
        "ts":     datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "ticker": ticker,
        "asset":  asset,
        "signal": signal,
        "fair":   round(fair, 4),
        "mkt":    round(mkt, 4),
        "fill":   round(fill, 4),
        "edge":   round(edge, 4),
        "pnl":    round(pnl, 2),
    }] + state["trades"][:MAX_TRADES_LOG - 1]
    log.info("TRADE %s | %s edge=%.3f pnl=$%.2f", signal, ticker, edge, pnl)


# ── Bot loop ──────────────────────────────────────────────────────────────────
async def bot_loop():
    log.info("Bot loop started | demo=%s edge_threshold=%.2f",
             KALSHI_DEMO, EDGE_THRESHOLD)
    while True:
        try:
            await run_tick()
        except Exception as e:
            log.error("Tick error: %s", e)
            state["errors"] = ([str(e)] + state["errors"])[:10]
        await asyncio.sleep(10)


async def run_tick():
    markets = await find_crypto_markets()
    state["conn_kalshi"] = True
    enriched = []

    for mkt in markets:
        ticker = mkt.get("ticker", "")
        asset  = "BTC" if "BTC" in ticker.upper() else "ETH"
        strike = mkt.get("floor_strike") or mkt.get("cap_strike")
        if not strike:
            continue
        strike = float(strike)

        close_time = mkt.get("close_time")
        if close_time:
            close_ts = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            T_hours  = max(
                (close_ts - datetime.now(timezone.utc)).total_seconds() / 3600,
                0.01)
        else:
            T_hours = 1.0

        try:
            spot  = await get_spot_price(asset)
            sigma = await get_vol(asset)
            state["conn_kraken"] = True
        except Exception as e:
            log.warning("Kraken error: %s", e)
            state["conn_kraken"] = False
            continue

        try:
            ob       = await get_orderbook(ticker)
            yes_book = ob.get("yes", [])
            best_ask = yes_book[0][-1]  / 100 if yes_book else None
            best_bid = yes_book[-1][-1] / 100 if yes_book else None
            mkt_prob = ((best_ask or 0) + (best_bid or 0)) / 2 \
                       if best_ask and best_bid \
                       else mkt.get("last_price", 50) / 100
        except Exception:
            mkt_prob = mkt.get("last_price", 50) / 100

        bs     = calc_probability(spot, strike, T_hours, sigma)
        fair   = bs["prob"]
        edge   = fair - mkt_prob
        signal = ("BUY_YES" if edge >  EDGE_THRESHOLD else
                  "BUY_NO"  if edge < -EDGE_THRESHOLD else "NO_TRADE")

        enriched.append({
            "ticker":    ticker,
            "asset":     asset,
            "spot":      round(spot, 2),
            "strike":    strike,
            "T_hours":   round(T_hours, 3),
            "sigma":     round(sigma, 4),
            "d2":        round(bs["d2"], 4),
            "fair_prob": round(fair, 4),
            "mkt_prob":  round(mkt_prob, 4),
            "edge":      round(edge, 4),
            "signal":    signal,
        })

        if signal != "NO_TRADE" and KALSHI_KEY_ID:
            try:
                side       = "yes" if signal == "BUY_YES" else "no"
                limit_cens = max(1, min(99,
                    int(fair * 100) + (1 if side == "yes" else -1)))
                contracts  = min(MAX_CONTRACTS, max(1, int(abs(edge) * 100)))
                result     = await place_order(ticker, side, contracts, limit_cens)
                fill_price = result.get("order", {}).get("yes_price",
                             limit_cens) / 100
                pnl_est    = (fair - fill_price) * contracts * 100
                record_trade(ticker, asset, signal, fair, mkt_prob,
                             fill_price, edge, pnl_est)
            except Exception as e:
                log.warning("Order failed %s: %s", ticker, e)

    state["last_markets"] = enriched
    log.info("Tick complete | %d markets evaluated", len(enriched))


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Vol Engine Bot API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "vol-engine running", "docs": "/docs", "health": "/api/status"}


@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_loop())
    log.info("Backend started | KALSHI_DEMO=%s", KALSHI_DEMO)


@app.get("/api/status")
def get_status():
    e = int(time.time() - session_start)
    return {
        "ok":            state["conn_kalshi"] and state["conn_kraken"],
        "kalshi":        state["conn_kalshi"],
        "kraken_data":   state["conn_kraken"],
        "demo_mode":     KALSHI_DEMO,
        "uptime_s":      e,
        "uptime":        f"{e//3600:02d}:{(e%3600)//60:02d}:{e%60:02d}",
        "recent_errors": state["errors"][-3:],
    }


@app.get("/api/markets")
def get_markets():
    return {"markets": state["last_markets"]}


@app.get("/api/trades")
def get_trades(limit: int = 30):
    return state["trades"][:limit]


@app.get("/api/stats")
def get_stats():
    e = int(time.time() - session_start)
    n = state["total_trades"]
    return {
        "sessionPnl":  round(state["cum_pnl"], 2),
        "totalTrades": n,
        "winRate":     round(state["wins"] / n, 4) if n > 0 else None,
        "avgEdge":     round(state["total_edge"] / n, 4) if n > 0 else None,
        "uptime":      f"{e//3600:02d}:{(e%3600)//60:02d}:{e%60:02d}",
        "pnlHistory":  state["pnl_history"],
    }


@app.get("/api/positions")
async def get_pos():
    try:
        return {"positions": await get_positions()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("kalshi_backend:app", host="0.0.0.0", port=8000, reload=True)
