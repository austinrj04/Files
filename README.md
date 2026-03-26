# Vol Engine Bot — Kalshi Setup

## Files in this repo

| File | Purpose |
|------|---------|
| `requirements.txt` | Python dependencies |
| `runtime.txt` | Python version pin |
| `railway.toml` | Railway deploy config |
| `volatility_engine.py` | Black-Scholes + EWMA vol engine |
| `kalshi_backend.py` | FastAPI bot backend |
| `kalshi_private_key.pem` | YOUR private key (add manually, never commit) |

---

## Deploy to Railway

1. Push all files to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variables in Railway dashboard:

```
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_DEMO=true
KRAKEN_BASE=https://api.kraken.com
```

4. Railway will build and deploy automatically
5. Copy your Railway URL and connect the dashboard

---

## API endpoints

| Endpoint | Description |
|----------|-------------|
| GET /api/status | Connection health |
| GET /api/markets | Live BTC/ETH markets + BS model |
| GET /api/trades | Execution history |
| GET /api/stats | Session P&L, win rate, uptime |
| GET /api/positions | Kalshi portfolio positions |

---

## Key parameters (kalshi_backend.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| EDGE_THRESHOLD | 0.05 | Min edge to fire a trade |
| MAX_CONTRACTS | 10 | Max contracts per order |
| VOL_CACHE_TTL | 300 | Seconds between vol recalculations |

---

## IMPORTANT

- Never commit `kalshi_private_key.pem` to GitHub
- Add it via Railway's file system or environment variable
- Start with KALSHI_DEMO=true before going live
