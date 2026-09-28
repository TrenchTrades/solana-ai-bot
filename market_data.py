"""Price candles from Coinbase's public API (no key, works in the US) + on-chain decimals lookup."""
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.exchange.coinbase.com/products"
HEADERS = {"User-Agent": "sol-ai-bot/2.0"}


def fetch_candles(product, granularity=900, chunks=1):
    """Closed candles, oldest first: [{time, open, high, low, close, volume}, ...]
    Each chunk is up to 300 candles (Coinbase's limit per request)."""
    now = time.time()
    end = int(now)
    seen = {}
    for _ in range(chunks):
        start = end - granularity * 299
        params = {"granularity": granularity}
        if chunks > 1:
            params.update(start=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                          end=datetime.fromtimestamp(end, timezone.utc).isoformat())
        r = requests.get(f"{BASE}/{product}/candles", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        for row in r.json():
            seen[row[0]] = row
        end = start
    rows = sorted(seen.values(), key=lambda x: x[0])
    rows = [x for x in rows if x[0] + granularity <= now]  # drop the still-forming candle
    return [{"time": int(t), "low": lo, "high": hi, "open": o, "close": c, "volume": v}
            for t, lo, hi, o, c, v in rows]


def fetch_price(product):
    r = requests.get(f"{BASE}/{product}/ticker", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def token_decimals(rpc_url, mint):
    r = requests.post(rpc_url, timeout=15, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]})
    r.raise_for_status()
    return int(r.json()["result"]["value"]["decimals"])
