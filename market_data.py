"""SOL-USD candles and spot price from Coinbase's public API (no key, works in the US)."""
import time
import requests

BASE = "https://api.exchange.coinbase.com/products/SOL-USD"
HEADERS = {"User-Agent": "sol-ai-bot/1.0"}


def fetch_candles(granularity=900):
    """Returns closed candles, oldest first: [{time, open, high, low, close, volume}, ...]"""
    r = requests.get(f"{BASE}/candles", params={"granularity": granularity},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    rows = sorted(r.json(), key=lambda x: x[0])
    now = time.time()
    rows = [x for x in rows if x[0] + granularity <= now]  # drop the still-forming candle
    return [{"time": int(t), "low": lo, "high": hi, "open": o, "close": c, "volume": v}
            for t, lo, hi, o, c, v in rows]


def fetch_price():
    r = requests.get(f"{BASE}/ticker", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])
