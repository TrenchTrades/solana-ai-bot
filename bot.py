#!/usr/bin/env python3
"""
Multi-token Solana trading bot
  tokens:    SOL, JUP, JTO, PYTH vs USDC (set TOKENS in .env)
  signals:   EMA crossover + RSI on Coinbase candles, per token
  filter:    Claude approves or vetoes each signal
  execution: Jupiter aggregator, signed with your wallet's keypair
  risk:      up to MAX_OPEN_POSITIONS at once, each with its own stop-loss / take-profit

Usage:
  python bot.py            run forever
  python bot.py --once     run a single check and exit
  python bot.py --status   show balances / open positions
Create a file named STOP in this folder to pause the bot without killing it.
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import config as C
from indicators import ema, rsi
from jupiter import Jupiter
from llm import review_signal
from market_data import fetch_candles, fetch_price, token_decimals

log = logging.getLogger("bot")

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"
KILL_FILE = "STOP"
MIN_TRADE_USD = 5
TRADE_FIELDS = ["time", "mode", "token", "side", "reason", "amount", "usdc", "price",
                "pnl_usd", "llm", "tx"]


def utc_now():
    return datetime.now(timezone.utc)


def today():
    return utc_now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- state
def load_state():
    s = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
    # Upgrade from the single-SOL version
    if "position" in s:
        old = s.pop("position")
        s["positions"] = {}
        if old:
            s["positions"]["SOL"] = {"amount": old["sol"], "usdc_cost": old["usdc_cost"],
                                     "entry_price": old["entry_price"], "opened_at": old["opened_at"]}
    if not isinstance(s.get("last_signal_candle"), dict):
        s["last_signal_candle"] = {}
    paper = s.get("paper") or {"USDC": C.PAPER_START_USDC}
    if "usdc" in paper:
        paper = {"USDC": paper["usdc"], "SOL": paper.get("sol", 0.0)}
    s["paper"] = paper
    s.setdefault("positions", {})
    s.setdefault("day", None)
    s.setdefault("trades_today", 0)
    s.setdefault("pnl_today", 0.0)
    s.setdefault("day_start_equity", None)
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE_FILE)


def log_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.mode = "LIVE" if C.LIVE_TRADING else "PAPER"
        self.jup = Jupiter(C.JUPITER_BASE_URL, C.JUPITER_API_KEY)
        self.wallet = None
        if C.LIVE_TRADING:
            from wallet import Wallet
            self.wallet = Wallet(C.SOLANA_RPC_URL, C.SOLANA_PRIVATE_KEY)
        self.state = load_state()
        self.tokens = self.load_tokens()

    def load_tokens(self):
        """Checks each token has a price feed and correct decimals. Held tokens are always kept."""
        wanted = list(dict.fromkeys(C.TOKENS + list(self.state["positions"])))
        active = {}
        for sym in wanted:
            info = C.TOKEN_REGISTRY.get(sym)
            if not info:
                log.warning("%s is not in TOKEN_REGISTRY (config.py) — skipped", sym)
                continue
            try:
                fetch_price(info["coinbase"])
            except Exception as e:
                log.warning("%s: Coinbase feed %s unavailable (%s) — skipped", sym, info["coinbase"], e)
                continue
            try:
                d = token_decimals(C.SOLANA_RPC_URL, info["mint"])
                if d != info["decimals"]:
                    log.warning("%s: on-chain decimals %d differ from config %d; using %d",
                                sym, d, info["decimals"], d)
                    info = {**info, "decimals": d}
            except Exception as e:
                log.warning("%s: couldn't verify decimals (%s); using %d", sym, e, info["decimals"])
            active[sym] = info
        if not active:
            sys.exit("No tradable tokens available.")
        log.info("Watching: %s", ", ".join(active))
        return active

    # ---------- balances
    def usdc_balance(self):
        if self.wallet:
            return self.wallet.token_balance(C.USDC_MINT)
        return self.state["paper"].get("USDC", 0.0)

    def token_balance(self, sym):
        if self.wallet:
            if sym == "SOL":
                return self.wallet.sol_balance()
            return self.wallet.token_balance(self.tokens[sym]["mint"])
        return self.state["paper"].get(sym, 0.0)

    def equity(self, prices):
        total = self.usdc_balance()
        for sym, pos in self.state["positions"].items():
            total += pos["amount"] * prices.get(sym, pos["entry_price"])
        return total

    def roll_day(self, equity):
        s = self.state
        if s["day"] != today():
            s.update(day=today(), trades_today=0, pnl_today=0.0, day_start_equity=equity)
            save_state(s)

    # ---------- execution
    def execute(self, sym, side, amount_in, ref_price, forced=False):
        """BUY: spend amount_in USDC on sym. SELL: sell amount_in of sym for USDC.
        Returns (token_amount, usdc, price, tx) or None if blocked."""
        info = self.tokens[sym]
        scale = 10 ** info["decimals"]
        if side == "BUY":
            q = self.jup.quote(C.USDC_MINT, info["mint"], amount_in * 1e6, C.SLIPPAGE_BPS)
            amount, usdc = int(q["outAmount"]) / scale, amount_in
        else:
            q = self.jup.quote(info["mint"], C.USDC_MINT, amount_in * scale, C.SLIPPAGE_BPS)
            amount, usdc = amount_in, int(q["outAmount"]) / 1e6
        price = usdc / amount

        # Jupiter's executable price must match Coinbase (looser for forced exits).
        max_dev = C.MAX_PRICE_DEVIATION_PCT * (3 if forced else 1)
        dev = abs(price - ref_price) / ref_price * 100
        if dev > max_dev:
            log.warning("Blocked %s %s: Jupiter %.6f is %.2f%% off Coinbase %.6f (limit %.2f%%)",
                        side, sym, price, dev, ref_price, max_dev)
            return None

        if self.wallet:
            tx_b64 = self.jup.swap_transaction(q, self.wallet.pubkey)
            sig = self.wallet.sign_and_send(tx_b64)
            log.info("Sent %s %s tx %s — waiting for confirmation", side, sym, sig)
            self.wallet.confirm(sig)
            tx = sig
        else:
            p = self.state["paper"]
            sign = 1 if side == "BUY" else -1
            p["USDC"] = p.get("USDC", 0.0) - sign * usdc
            p[sym] = p.get(sym, 0.0) + sign * amount
            tx = "paper"
        return amount, usdc, price, tx

    def open_position(self, sym, ref_price, equity, verdict):
        if self.wallet and self.wallet.sol_balance() < C.SOL_FEE_RESERVE:
            log.warning("SOL balance below fee reserve %.3f — add SOL for fees", C.SOL_FEE_RESERVE)
            return
        usdc_bal = self.usdc_balance()
        size = round(min(C.MAX_TRADE_USD, equity * C.POSITION_PCT, usdc_bal), 2)
        if size < MIN_TRADE_USD:
            log.info("BUY %s skipped: trade size $%.2f below $%d minimum (USDC %.2f, balance %.2f)",
                     sym, size, MIN_TRADE_USD, usdc_bal, equity)
            return
        result = self.execute(sym, "BUY", size, ref_price)
        if not result:
            return
        amount, usdc, price, tx = result
        self.state["positions"][sym] = {"amount": amount, "usdc_cost": usdc, "entry_price": price,
                                        "opened_at": utc_now().isoformat()}
        self.state["trades_today"] += 1
        save_state(self.state)
        log_trade({"time": utc_now().isoformat(), "mode": self.mode, "token": sym, "side": "BUY",
                   "reason": "ema_cross_up", "amount": f"{amount:.6f}", "usdc": f"{usdc:.2f}",
                   "price": f"{price:.6f}", "pnl_usd": "", "llm": verdict, "tx": tx})
        log.info("BOUGHT %.4f %s for %.2f USDC @ %.6f (%s)", amount, sym, usdc, price, tx)

    def close_position(self, sym, ref_price, reason, verdict="", forced=False):
        pos = self.state["positions"][sym]
        amount = pos["amount"]
        if self.wallet:
            bal = self.token_balance(sym)
            if sym == "SOL":
                bal -= C.SOL_FEE_RESERVE
            amount = min(amount, bal)
            if amount <= 0:
                log.error("Wallet holds no %s to sell (was it moved?). Clearing the position.", sym)
                del self.state["positions"][sym]
                save_state(self.state)
                return
        result = self.execute(sym, "SELL", amount, ref_price, forced=forced)
        if not result:
            return
        sold, usdc, price, tx = result
        pnl = usdc - pos["usdc_cost"] * (sold / pos["amount"])
        del self.state["positions"][sym]
        self.state["trades_today"] += 1
        self.state["pnl_today"] += pnl
        save_state(self.state)
        log_trade({"time": utc_now().isoformat(), "mode": self.mode, "token": sym, "side": "SELL",
                   "reason": reason, "amount": f"{sold:.6f}", "usdc": f"{usdc:.2f}",
                   "price": f"{price:.6f}", "pnl_usd": f"{pnl:.2f}", "llm": verdict, "tx": tx})
        log.info("SOLD %.4f %s for %.2f USDC @ %.6f | PnL %+.2f (%s)", sold, sym, usdc, price, pnl, reason)

    # ---------- decision loop
    def snapshot(self, sym, candles, ef, es, r, price, prices):
        closes = [c["close"] for c in candles]
        per_day = max(1, 86400 // C.CANDLE_GRANULARITY)
        window = candles[-per_day:]
        vols = [c["volume"] for c in candles[-50:]]
        avg_vol = sum(vols) / len(vols) or 1
        snap = {
            "token": sym, "pair": f"{sym}/USDC", "candle_seconds": C.CANDLE_GRANULARITY,
            "spot_price": price,
            f"ema{C.EMA_FAST}": ef[-1], f"ema{C.EMA_SLOW}": es[-1],
            f"ema{C.EMA_FAST}_prev": ef[-2], f"ema{C.EMA_SLOW}_prev": es[-2],
            "rsi": round(r, 2),
            "change_24h_pct": round((closes[-1] / window[0]["open"] - 1) * 100, 2),
            "high_24h": max(c["high"] for c in window),
            "low_24h": min(c["low"] for c in window),
            "last_volume_vs_avg50": round(candles[-1]["volume"] / avg_vol, 2),
            "last_30_closes": [float(f"{x:.6g}") for x in closes[-30:]],
        }
        pos = self.state["positions"].get(sym)
        if pos:
            snap["this_position"] = {"entry_price": pos["entry_price"],
                                     "unrealized_pct": round((price / pos["entry_price"] - 1) * 100, 2),
                                     "opened_at": pos["opened_at"]}
        others = {s: round((prices.get(s, p["entry_price"]) / p["entry_price"] - 1) * 100, 2)
                  for s, p in self.state["positions"].items() if s != sym}
        snap["other_open_positions_unrealized_pct"] = others
        return snap

    def tick(self):
        if os.path.exists(KILL_FILE):
            log.warning("STOP file present — paused (delete it to resume)")
            return
        data = {}
        for sym, info in self.tokens.items():
            try:
                data[sym] = (fetch_candles(info["coinbase"], C.CANDLE_GRANULARITY),
                             fetch_price(info["coinbase"]))
            except Exception as e:
                log.warning("%s: market data failed (%s)", sym, e)
        prices = {s: d[1] for s, d in data.items()}
        equity = self.equity(prices)
        self.roll_day(equity)
        s = self.state
        log.info("[%s] Balance ~$%.2f | open: %s | today: %d trades, PnL %+.2f",
                 self.mode, equity, ", ".join(s["positions"]) or "none",
                 s["trades_today"], s["pnl_today"])
        for sym, (candles, price) in data.items():
            try:
                self.evaluate(sym, candles, price, equity, prices)
            except Exception:
                log.exception("%s: evaluation failed", sym)

    def evaluate(self, sym, candles, price, equity, prices):
        s = self.state
        if len(candles) < max(C.EMA_SLOW, C.RSI_PERIOD) * 3:
            log.warning("%s: not enough candle data (%d)", sym, len(candles))
            return
        closes = [c["close"] for c in candles]
        ef, es = ema(closes, C.EMA_FAST), ema(closes, C.EMA_SLOW)
        r = rsi(closes, C.RSI_PERIOD)
        cid = candles[-1]["time"]
        cross_up = ef[-2] <= es[-2] and ef[-1] > es[-1]
        cross_dn = ef[-2] >= es[-2] and ef[-1] < es[-1]
        pos = s["positions"].get(sym)
        log.info("  %-4s %.6g | EMA%d %.6g EMA%d %.6g | RSI %.1f | %s", sym, price,
                 C.EMA_FAST, ef[-1], C.EMA_SLOW, es[-1], r,
                 f"holding, {(price / pos['entry_price'] - 1) * 100:+.2f}%" if pos else "flat")

        if pos:
            chg = (price / pos["entry_price"] - 1) * 100
            if chg <= -C.STOP_LOSS_PCT:
                self.close_position(sym, price, f"stop_loss {chg:.2f}%", "bypassed", forced=True)
                return
            if chg >= C.TAKE_PROFIT_PCT:
                self.close_position(sym, price, f"take_profit {chg:.2f}%", "bypassed", forced=True)
                return
            if (cross_dn or r >= C.RSI_EXIT) and s["last_signal_candle"].get(sym) != cid:
                s["last_signal_candle"][sym] = cid
                save_state(s)
                why = "ema_cross_down" if cross_dn else f"rsi_{r:.0f}"
                v = review_signal(f"SELL {sym}", self.snapshot(sym, candles, ef, es, r, price, prices))
                log.info("Claude on SELL %s: %s (%.2f) — %s", sym, v["decision"], v["confidence"], v["reason"])
                if v["approve"] or v["error"]:
                    self.close_position(sym, price, why, f"{v['decision']} {v['confidence']:.2f}")
            return

        if not (cross_up and r < C.RSI_MAX_ENTRY) or s["last_signal_candle"].get(sym) == cid:
            return
        s["last_signal_candle"][sym] = cid
        save_state(s)
        if len(s["positions"]) >= C.MAX_OPEN_POSITIONS:
            log.info("BUY %s ignored: already at %d open positions", sym, C.MAX_OPEN_POSITIONS)
            return
        if s["trades_today"] >= C.MAX_TRADES_PER_DAY:
            log.info("BUY %s ignored: daily trade limit reached", sym)
            return
        loss_limit = (s["day_start_equity"] or equity) * C.MAX_DAILY_LOSS_PCT / 100
        if s["pnl_today"] <= -loss_limit:
            log.info("BUY %s ignored: daily loss limit reached (%.2f)", sym, s["pnl_today"])
            return
        v = review_signal(f"BUY {sym}", self.snapshot(sym, candles, ef, es, r, price, prices))
        log.info("Claude on BUY %s: %s (%.2f) — %s", sym, v["decision"], v["confidence"], v["reason"])
        if v["approve"]:
            self.open_position(sym, price, equity, f"{v['decision']} {v['confidence']:.2f}")

    def status(self):
        prices = {}
        for sym, info in self.tokens.items():
            try:
                prices[sym] = fetch_price(info["coinbase"])
            except Exception:
                pass
        usdc = self.usdc_balance()
        print(f"Mode:        {self.mode}")
        if self.wallet:
            print(f"Wallet:      {self.wallet.pubkey}")
            print(f"SOL (fees):  {self.wallet.sol_balance():.4f}")
        print(f"USDC:        {usdc:,.2f}")
        print(f"Watching:    {', '.join(self.tokens)}  (max {C.MAX_OPEN_POSITIONS} open)")
        if self.state["positions"]:
            for sym, pos in self.state["positions"].items():
                px = prices.get(sym, pos["entry_price"])
                print(f"Position:    {pos['amount']:.4f} {sym} @ {pos['entry_price']:.6g} "
                      f"(now {px:.6g}, {(px / pos['entry_price'] - 1) * 100:+.2f}%, ~${pos['amount'] * px:,.2f})")
        else:
            print("Positions:   none")
        print(f"Total:       ~${self.equity(prices):,.2f}")
        print(f"Today:       {self.state['trades_today']} trades, PnL {self.state['pnl_today']:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if C.LIVE_TRADING and not C.SOLANA_PRIVATE_KEY:
        sys.exit("LIVE_TRADING=true but SOLANA_PRIVATE_KEY is empty.")
    if not C.ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY is empty — every BUY will be vetoed.")

    bot = Bot()
    if args.status:
        bot.status()
        return

    if C.LIVE_TRADING:
        log.warning("LIVE TRADING with wallet %s. Up to %d positions, max $%.2f each. "
                    "Starting in 10s (Ctrl+C to abort)...",
                    bot.wallet.pubkey, C.MAX_OPEN_POSITIONS, C.MAX_TRADE_USD)
        time.sleep(10)
    else:
        log.info("PAPER mode — real prices and quotes, no transactions sent.")

    try:
        while True:
            try:
                bot.tick()
            except Exception:
                log.exception("Tick failed; will retry next cycle")
            if args.once:
                break
            time.sleep(C.POLL_SECONDS)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
