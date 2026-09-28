"""Loads settings from .env. Anything not set in .env uses the defaults below."""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _float(name, default):
    return float(os.getenv(name) or default)


def _int(name, default):
    return int(os.getenv(name) or default)


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Tokens the bot knows how to trade. "coinbase" is the price feed used for signals.
# Decimals are re-checked on-chain at startup.
TOKEN_REGISTRY = {
    "SOL":  {"mint": SOL_MINT, "decimals": 9, "coinbase": "SOL-USD"},
    "JUP":  {"mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6, "coinbase": "JUP-USD"},
    "JTO":  {"mint": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL", "decimals": 9, "coinbase": "JTO-USD"},
    "PYTH": {"mint": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3", "decimals": 6, "coinbase": "PYTH-USD"},
}
TOKENS = [t.strip().upper() for t in os.getenv("TOKENS", "SOL,JUP,JTO,PYTH").split(",") if t.strip()]

LIVE_TRADING = _bool("LIVE_TRADING")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-sonnet-5"
JUPITER_BASE_URL = (os.getenv("JUPITER_BASE_URL") or "https://lite-api.jup.ag/swap/v1").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()

# Strategy
CANDLE_GRANULARITY = _int("CANDLE_GRANULARITY", 900)
EMA_FAST = _int("EMA_FAST", 9)
EMA_SLOW = _int("EMA_SLOW", 21)
RSI_PERIOD = _int("RSI_PERIOD", 14)
RSI_MAX_ENTRY = _float("RSI_MAX_ENTRY", 70)
RSI_EXIT = _float("RSI_EXIT", 75)
POLL_SECONDS = _int("POLL_SECONDS", 60)
MIN_LLM_CONFIDENCE = _float("MIN_LLM_CONFIDENCE", 0.6)

# Risk ("medium" defaults)
MAX_OPEN_POSITIONS = _int("MAX_OPEN_POSITIONS", 3)
POSITION_PCT = _float("POSITION_PCT", 0.25)        # fraction of total balance per trade
MAX_TRADE_USD = _float("MAX_TRADE_USD", 50)        # hard cap per trade
STOP_LOSS_PCT = _float("STOP_LOSS_PCT", 5)
TAKE_PROFIT_PCT = _float("TAKE_PROFIT_PCT", 10)
MAX_TRADES_PER_DAY = _int("MAX_TRADES_PER_DAY", 12)
MAX_DAILY_LOSS_PCT = _float("MAX_DAILY_LOSS_PCT", 10)
SLIPPAGE_BPS = _int("SLIPPAGE_BPS", 50)
MAX_PRICE_DEVIATION_PCT = _float("MAX_PRICE_DEVIATION_PCT", 1.0)
SOL_FEE_RESERVE = _float("SOL_FEE_RESERVE", 0.02)
MAX_PRIORITY_FEE_LAMPORTS = _int("MAX_PRIORITY_FEE_LAMPORTS", 1_000_000)

PAPER_START_USDC = _float("PAPER_START_USDC", 1000)

# Telegram alerts (optional; leave blank to disable)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_VETOES = _bool("NOTIFY_VETOES", "true")
