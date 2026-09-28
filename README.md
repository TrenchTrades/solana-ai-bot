# Solana AI Trading Bot (Phantom + Jupiter + Claude)

Trades **SOL, JUP, JTO and PYTH against USDC** on Solana, holding up to 3 positions at once. Technical indicators generate signals, Claude approves or vetoes them, and Jupiter executes the swap using a keypair exported from Phantom.

## How it decides

1. Every `POLL_SECONDS`, it pulls candles for each token from Coinbase and computes EMA 9/21 and RSI 14.
2. **Entry:** EMA9 crosses above EMA21 while RSI is below 70 → Claude reviews a market snapshot → buys only if Claude says APPROVE with confidence ≥ 0.6.
3. **Exit:** per-position stop-loss (−5%) and take-profit (+10%) fire immediately without asking Claude. An EMA cross-down or RSI ≥ 75 triggers a Claude-reviewed sell.
4. Each trade uses 25% of your total balance (capped at `MAX_TRADE_USD`), with at most 3 positions open and a 10% daily loss limit.
5. Claude can only veto a signal. It can never start a trade on its own, and if the API fails, buys are skipped and sells go ahead.

Each signal is evaluated once per candle, so a vetoed signal doesn't cost repeated API calls.

## Setup

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # then edit .env
```

Put your Anthropic API key (console.anthropic.com) in `.env`. That's all paper mode needs.

```bash
python bot.py --once     # one check, to confirm everything works
python bot.py            # run continuously
python bot.py --status   # balances and open position
```

Paper mode uses real Jupiter quotes and real prices but sends nothing. Trades are written to `trades.csv`, and everything else to `bot.log`.

## Going live

1. In Phantom, **create a new account** just for the bot. Never use your main account.
2. Send it a small amount of USDC plus about 0.05 SOL for fees.
3. Export that account's private key from its settings in Phantom ("Show Private Key") and paste it into `SOLANA_PRIVATE_KEY` in `.env`.
4. Get a free RPC URL from Helius or QuickNode and set `SOLANA_RPC_URL`. The public RPC will rate-limit you.
5. Set `LIVE_TRADING=true`.

**Pause anytime:** create an empty file named `STOP` in the folder. Delete it to resume.

## Safety notes

- `.env` holds a key that can drain that wallet. Never commit it, share it, or paste it into a website or chat, including with support staff.
- Limits in `.env` include max trade size, trades per day, daily loss cap, slippage, and a check that blocks trades when Jupiter's price differs from Coinbase's by more than 1%.
- Live fills can differ slightly from the quote, within `SLIPPAGE_BPS`. Check trades on solscan.io.
- If you stop the bot while it holds SOL, `state.json` remembers the position. Delete `state.json` only if you've closed the position manually.

## Telegram alerts

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token it gives you.
2. Open your new bot's chat, press **Start**, and send it any message.
3. Add `TELEGRAM_BOT_TOKEN=...` to `.env`, then run `python notify.py --chat-id` and add the `TELEGRAM_CHAT_ID=...` line it prints.
4. Run `python notify.py` to send a test message.

You'll get alerts for startup, every buy and sell, Claude vetoes (set `NOTIFY_VETOES=false` to silence), blocked trades, errors, and a daily summary at midnight UTC. Alerts are send-only; nobody can control the trading bot through Telegram.
