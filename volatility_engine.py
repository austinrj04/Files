"""
volatility_engine.py
────────────────────────────────────────────────────────────────────────────
Volatility-based probability engine for BTC/ETH hourly markets.
────────────────────────────────────────────────────────────────────────────
"""

import math
import time
import logging
import requests
from typing import Optional

USE_SCIPY = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vol_engine")

MINUTES_PER_YEAR  = 365 * 24 * 60
HOURS_PER_YEAR    = 365 * 24
SIGMA_FLOOR       = 0.05
T_MIN             = 1 / HOURS_PER_YEAR / 60
EDGE_BUY_YES      = 0.05
EDGE_BUY_NO       = -0.05
VOL_CACHE_TTL     = 300


def _norm_cdf(x: float) -> float:
    if x < -10: return 0.0
    if x > 10:  return 1.0
    sign = 1.0 if x >= 0 else -1.0
    t    = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    phi  = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    cdf  = 1.0 - phi * poly
    return cdf if sign > 0 else 1.0 - cdf


def calculate_probability(S: float, K: float, T: float, sigma: float) -> float:
    if S <= 0 or K <= 0:
        raise ValueError(f"Prices must be positive (S={S}, K={K})")
    if T < 0:
        raise ValueError(f"Time to expiry cannot be negative (T={T}h)")
    sigma = max(sigma, SIGMA_FLOOR)
    T_yr  = max(T / HOURS_PER_YEAR, T_MIN)
    d2    = (math.log(S / K) - (sigma ** 2 / 2) * T_yr) / (sigma * math.sqrt(T_yr))
    probability = _norm_cdf(d2)
    log.info("BS model | S=%.2f K=%.2f T=%.4fh σ=%.4f d2=%.4f P(>K)=%.4f",
             S, K, T, sigma, d2, probability)
    return probability


def compute_edge(fair_probability: float, market_probability: float) -> float:
    return fair_probability - market_probability


def get_trade_signal(edge: float) -> str:
    if edge > EDGE_BUY_YES:  return "BUY_YES"
    if edge < EDGE_BUY_NO:   return "BUY_NO"
    return "NO_TRADE"


class VolatilityCache:
    def __init__(self, symbol: str = "BTCUSDT", interval: str = "1m",
                 lookback: int = 60, method: str = "ewma"):
        self.symbol   = symbol
        self.interval = interval
        self.lookback = lookback
        self.method   = method
        self._sigma:      Optional[float] = None
        self._last_fetch: float           = 0.0

    def get_sigma(self) -> float:
        if self._sigma is None or (time.time() - self._last_fetch) > VOL_CACHE_TTL:
            self._refresh()
        return self._sigma

    def force_refresh(self) -> float:
        self._refresh()
        return self._sigma

    def _refresh(self) -> None:
        closes = self._fetch_closes()
        if len(closes) < 2:
            log.warning("Not enough candles; using floor.")
            self._sigma = SIGMA_FLOOR
        else:
            if self.method == "ewma":
                self._sigma = self._ewma_vol(closes)
            else:
                self._sigma = self._simple_vol(closes)
        self._sigma      = max(self._sigma, SIGMA_FLOOR)
        self._last_fetch = time.time()
        log.info("Volatility updated | σ=%.4f", self._sigma)

    def _fetch_closes(self) -> list:
        url    = "https://api.binance.com/api/v3/klines"
        params = {"symbol": self.symbol, "interval": self.interval,
                  "limit": self.lookback + 1}
        try:
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            return [float(c[4]) for c in resp.json()]
        except requests.RequestException as exc:
            log.error("Failed to fetch klines: %s", exc)
            return []

    @staticmethod
    def _log_returns(closes: list) -> list:
        return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    def _simple_vol(self, closes: list) -> float:
        returns = self._log_returns(closes)
        n       = len(returns)
        mean    = sum(returns) / n
        var     = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std_per = math.sqrt(var)
        ppy     = MINUTES_PER_YEAR if self.interval == "1m" else MINUTES_PER_YEAR / 5
        return std_per * math.sqrt(ppy)

    def _ewma_vol(self, closes: list, lam: float = 0.94) -> float:
        returns = self._log_returns(closes)
        if not returns:
            return SIGMA_FLOOR
        seed   = returns[:10]
        mean_s = sum(seed) / len(seed)
        var_t  = sum((r - mean_s) ** 2 for r in seed) / max(len(seed) - 1, 1)
        for r in returns[10:]:
            var_t = lam * var_t + (1 - lam) * r * r
        ppy = MINUTES_PER_YEAR if self.interval == "1m" else MINUTES_PER_YEAR / 5
        return math.sqrt(var_t) * math.sqrt(ppy)


def evaluate_market(S, K, T, market_probability, vol_cache):
    sigma     = vol_cache.get_sigma()
    fair_prob = calculate_probability(S, K, T, sigma)
    edge      = compute_edge(fair_prob, market_probability)
    signal    = get_trade_signal(edge)
    log.info("SIGNAL %-9s | fair=%.4f market=%.4f edge=%+.4f",
             signal, fair_prob, market_probability, edge)
    return {"sigma": sigma, "fair_prob": fair_prob,
            "market_prob": market_probability, "edge": edge, "signal": signal}
