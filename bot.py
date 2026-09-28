#!/usr/bin/env python3
"""
SOL/USDC trading bot
  signals:   EMA crossover + RSI on Coinbase candles
  filter:    Claude approves or vetoes each signal
  execution: Jupiter aggregator, signed with your Phantom keypair
  default:   PAPER mode (real Jupiter quotes, no transactions sent)

Usage:
  python bot.py            run forever
  python bot.py --once     run a single check and exit
  python bot.py --status   show balances / open position
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
from market_data import fetch_candles, fetch_price

log = logging.getLogger("bot")

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"
KILL_FILE = "STOP"
MIN_TRADE_USD = 5


def utc_now():
    return datetime.now(timezone.utc)


def today():
    return utc_now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- state
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
    else:
        s = {"position": None,
             "paper": {"usdc": C.PAPER_START_USDC, "sol": 0.0},
             "day": today(), "trades_today": 0, "pnl_today": 0.0,
             "last_signal_candle": None}
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE_FILE)


def log_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "mode", "side", "reason", "sol", "usdc",
                                          "price", "pnl_usd", "llm", "tx"])
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

    def roll_day(self):
        if self.state.get("day") != today():
            self.state.update(day=today(), trades_today=0, pnl_today=0.0)
            save_state(self.state)

    def balances(self):
        """(usdc, sol)"""
        if self.wallet:
            return self.wallet.token_balance(C.USDC_MINT), self.wallet.sol_balance()
        p = self.state["paper"]
        return p["usdc"], p["sol"]

    # ---------- execution
    def execute(self, side, amount_in, ref_price, forced=False):
        """side BUY: spend amount_in USDC for SOL. side SELL: sell amount_in SOL for USDC.
        Returns (sol, usdc, price, tx) or None if blocked."""
        if side == "BUY":
            q = self.jup.quote(C.USDC_MINT, C.SOL_MINT, amount_in * 1e6, C.SLIPPAGE_BPS)
            sol, usdc = int(q["outAmount"]) / 1e9, amount_in
        else:
            q = self.jup.quote(C.SOL_MINT, C.USDC_MINT, amount_in * 1e9, C.SLIPPAGE_BPS)
            sol, usdc = amount_in, int(q["outAmount"]) / 1e6
        price = usdc / sol

        # Sanity check: Jupiter's executable price vs Coinbase spot.
        # Protects against thin liquidity, bad routes, or bad data. Looser for forced exits.
        max_dev = C.MAX_PRICE_DEVIATION_PCT * (3 if forced else 1)
        dev = abs(price - ref_price) / ref_price * 100
        if dev > max_dev:
            log.warning("Blocked %s: Jupiter price %.4f is %.2f%% off Coinbase %.4f (limit %.2f%%)",
                        side, price, dev, ref_price, max_dev)
            return None

        if self.wallet:
            tx_b64 = self.jup.swap_transaction(q, self.wallet.pubkey)
            sig = self.wallet.sign_and_send(tx_b64)
            log.info("Sent %s tx %s — waiting for confirmation", side, sig)
            self.wallet.confirm(sig)
            tx = sig
        else:
            p = self.state["paper"]
            if side == "BUY":
                p["usdc"] -= usdc
                p["sol"] += sol
            else:
                p["sol"] -= sol
                p["usdc"] += usdc
            tx = "paper"
        return sol, usdc, price, tx

    def open_position(self, ref_price, reason, verdict):
        usdc_bal, sol_bal = self.balances()
        if self.wallet and sol_bal < C.SOL_FEE_RESERVE:
            log.warning("SOL balance %.4f below fee reserve %.4f — add SOL for fees",
                        sol_bal, C.SOL_FEE_RESERVE)
            return
        size = round(min(C.MAX_TRADE_USD, usdc_bal * C.POSITION_PCT), 2)
        if size < MIN_TRADE_USD:
            log.info("Not enough USDC to trade (balance %.2f)", usdc_bal)
            return
        result = self.execute("BUY", size, ref_price)
        if not result:
            return
        sol, usdc, price, tx = result
        self.state["position"] = {"sol": sol, "usdc_cost": usdc, "entry_price": price,
                                  "opened_at": utc_now().isoformat()}
        self.state["trades_today"] += 1
        save_state(self.state)
        log_trade({"time": utc_now().isoformat(), "mode": self.mode, "side": "BUY",
                   "reason": reason, "sol": f"{sol:.6f}", "usdc": f"{usdc:.2f}",
                   "price": f"{price:.4f}", "pnl_usd": "", "llm": verdict, "tx": tx})
        log.info("BOUGHT %.4f SOL for %.2f USDC @ %.4f (%s)", sol, usdc, price, tx)

    def close_position(self, ref_price, reason, verdict="", forced=False):
        pos = self.state["position"]
        sol = pos["sol"]
        if self.wallet:
            _, sol_bal = self.balances()
            sol = min(sol, sol_bal - C.SOL_FEE_RESERVE)
            if sol <= 0:
                log.error("Wallet holds no SOL above the fee reserve; can't sell. Check the wallet.")
                return
        result = self.execute("SELL", sol, ref_price, forced=forced)
        if not result:
            return
        sold, usdc, price, tx = result
        cost = pos["usdc_cost"] * (sold / pos["sol"])
        pnl = usdc - cost
        self.state["position"] = None
        self.state["trades_today"] += 1
        self.state["pnl_today"] += pnl
        save_state(self.state)
        log_trade({"time": utc_now().isoformat(), "mode": self.mode, "side": "SELL",
                   "reason": reason, "sol": f"{sold:.6f}", "usdc": f"{usdc:.2f}",
                   "price": f"{price:.4f}", "pnl_usd": f"{pnl:.2f}", "llm": verdict, "tx": tx})
        log.info("SOLD %.4f SOL for %.2f USDC @ %.4f | PnL %+.2f (%s)", sold, usdc, price, pnl, reason)

    # ---------- decision loop
    def snapshot(self, candles, ef, es, r, price):
        closes = [c["close"] for c in candles]
        per_day = max(1, 86400 // C.CANDLE_GRANULARITY)
        window = candles[-per_day:]
        vols = [c["volume"] for c in candles[-50:]]
        snap = {
            "pair": "SOL/USDC",
            "candle_seconds": C.CANDLE_GRANULARITY,
            "spot_price": round(price, 4),
            f"ema{C.EMA_FAST}": round(ef[-1], 4),
            f"ema{C.EMA_SLOW}": round(es[-1], 4),
            f"ema{C.EMA_FAST}_prev": round(ef[-2], 4),
            f"ema{C.EMA_SLOW}_prev": round(es[-2], 4),
            "rsi": round(r, 2),
            "change_24h_pct": round((closes[-1] / window[0]["open"] - 1) * 100, 2),
            "high_24h": max(c["high"] for c in window),
            "low_24h": min(c["low"] for c in window),
            "last_volume_vs_avg50": round(candles[-1]["volume"] / (sum(vols) / len(vols)), 2),
            "last_30_closes": [round(x, 3) for x in closes[-30:]],
        }
        pos = self.state["position"]
        if pos:
            snap["open_position"] = {"entry_price": round(pos["entry_price"], 4),
                                     "unrealized_pct": round((price / pos["entry_price"] - 1) * 100, 2),
                                     "opened_at": pos["opened_at"]}
        return snap

    def tick(self):
        self.roll_day()
        s = self.state
        if os.path.exists(KILL_FILE):
            log.warning("STOP file present — paused (delete it to resume)")
            return

        candles = fetch_candles(C.CANDLE_GRANULARITY)
        if len(candles) < max(C.EMA_SLOW, C.RSI_PERIOD) * 3:
            log.warning("Not enough candle data yet (%d)", len(candles))
            return
        closes = [c["close"] for c in candles]
        ef, es = ema(closes, C.EMA_FAST), ema(closes, C.EMA_SLOW)
        r = rsi(closes, C.RSI_PERIOD)
        price = fetch_price()
        candle_id = candles[-1]["time"]
        cross_up = ef[-2] <= es[-2] and ef[-1] > es[-1]
        cross_dn = ef[-2] >= es[-2] and ef[-1] < es[-1]

        pos = s["position"]
        log.info("[%s] SOL %.3f | EMA%d %.3f EMA%d %.3f | RSI %.1f | %s",
                 self.mode, price, C.EMA_FAST, ef[-1], C.EMA_SLOW, es[-1], r,
                 f"holding since {pos['entry_price']:.3f}" if pos else "flat")

        if pos:
            chg = (price / pos["entry_price"] - 1) * 100
            # Hard exits never wait for Claude.
            if chg <= -C.STOP_LOSS_PCT:
                self.close_position(price, f"stop_loss {chg:.2f}%", "bypassed", forced=True)
                return
            if chg >= C.TAKE_PROFIT_PCT:
                self.close_position(price, f"take_profit {chg:.2f}%", "bypassed", forced=True)
                return
            if (cross_dn or r >= C.RSI_EXIT) and s["last_signal_candle"] != candle_id:
                s["last_signal_candle"] = candle_id
                save_state(s)
                why = "ema_cross_down" if cross_dn else f"rsi_{r:.0f}"
                v = review_signal("SELL", self.snapshot(candles, ef, es, r, price))
                log.info("Claude on SELL: %s (%.2f) — %s", v["decision"], v["confidence"], v["reason"])
                # If Claude is unreachable, exiting is the safer default.
                if v["approve"] or v["error"]:
                    self.close_position(price, why, f"{v['decision']} {v['confidence']:.2f}")
            return

        if not (cross_up and r < C.RSI_MAX_ENTRY) or s["last_signal_candle"] == candle_id:
            return
        s["last_signal_candle"] = candle_id
        save_state(s)
        if s["trades_today"] >= C.MAX_TRADES_PER_DAY:
            log.info("BUY signal ignored: daily trade limit reached")
            return
        if s["pnl_today"] <= -C.MAX_DAILY_LOSS_USD:
            log.info("BUY signal ignored: daily loss limit reached (%.2f)", s["pnl_today"])
            return
        v = review_signal("BUY", self.snapshot(candles, ef, es, r, price))
        log.info("Claude on BUY: %s (%.2f) — %s", v["decision"], v["confidence"], v["reason"])
        if v["approve"]:
            self.open_position(price, "ema_cross_up", f"{v['decision']} {v['confidence']:.2f}")

    def status(self):
        usdc, sol = self.balances()
        price = fetch_price()
        print(f"Mode:        {self.mode}")
        if self.wallet:
            print(f"Wallet:      {self.wallet.pubkey}")
        print(f"USDC:        {usdc:,.2f}")
        print(f"SOL:         {sol:,.4f}  (~${sol * price:,.2f})")
        print(f"Total:       ~${usdc + sol * price:,.2f}")
        print(f"Today:       {self.state['trades_today']} trades, PnL {self.state['pnl_today']:+.2f}")
        pos = self.state["position"]
        if pos:
            print(f"Position:    {pos['sol']:.4f} SOL @ {pos['entry_price']:.4f} "
                  f"({(price / pos['entry_price'] - 1) * 100:+.2f}%)")
        else:
            print("Position:    none")


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
        log.warning("LIVE TRADING with wallet %s. Max %.2f USDC per trade. Starting in 10s (Ctrl+C to abort)...",
                    bot.wallet.pubkey, C.MAX_TRADE_USD)
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
