"""Price candles from Coinbase's public API (no key, works in the US) + on-chain decimals lookup."""
import time
import requests

BASE = "https://api.exchange.coinbase.com/products"
HEADERS = {"User-Agent": "sol-ai-bot/2.0"}


def fetch_candles(product, granularity=900):
    """Closed candles, oldest first: [{time, open, high, low, close, volume}, ...]"""
    r = requests.get(f"{BASE}/{product}/candles", params={"granularity": granularity},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    rows = sorted(r.json(), key=lambda x: x[0])
    now = time.time()
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
