#!/usr/bin/env python3
"""
Backtester: replays the bot's exact rules over past price data. Trades nothing.

  python backtest.py                       last 90 days, using your .env settings
  python backtest.py --days 180            longer history
  python backtest.py --sweep               compare stop-loss / take-profit combinations
  python backtest.py --start-usdc 50 --max-trade 50

Notes
- Claude's approve/veto step is NOT simulated (it would cost API calls, and Claude may
  "remember" past prices, which would make the result look better than reality).
  This tests the indicator strategy that Claude filters. Claude can only remove trades.
- Stop-loss / take-profit are checked against each candle's high and low. If both are hit
  in the same candle, the stop-loss is assumed to happen first (the pessimistic choice).
- Costs: COST_PCT per side (swap fees + slippage) plus a fixed network fee per swap.
- Past performance doesn't predict future results.
"""
import argparse
import csv
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import requests

import config as C
from indicators import ema

CB = "https://api.exchange.coinbase.com/products"
HEADERS = {"User-Agent": "sol-ai-bot-backtest/1.0"}
CACHE_DIR = "backtest_data"


# ---------------------------------------------------------------- data
def fetch_history(product, gran, days):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{product}_{gran}_{days}d.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        with open(path) as f:
            return json.load(f)

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_limit = end - timedelta(days=days)
    chunk = timedelta(seconds=gran * 299)
    rows, cur_end, n = {}, end, 0
    while cur_end > start_limit:
        cur_start = max(start_limit, cur_end - chunk)
        r = requests.get(f"{CB}/{product}/candles", headers=HEADERS, timeout=20, params={
            "granularity": gran, "start": cur_start.isoformat(), "end": cur_end.isoformat()})
        if r.status_code == 429:
            time.sleep(2)
            continue
        r.raise_for_status()
        for t, lo, hi, o, c, v in r.json():
            rows[int(t)] = (o, hi, lo, c)
        cur_end = cur_start
        n += 1
        print(f"\r  downloading {product}: {n} chunks, {len(rows)} candles", end="", flush=True)
        time.sleep(0.25)
    print()
    now = time.time()
    ts = sorted(t for t in rows if t + gran <= now)
    data = {"t": ts, "o": [rows[t][0] for t in ts], "h": [rows[t][1] for t in ts],
            "l": [rows[t][2] for t in ts], "c": [rows[t][3] for t in ts]}
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def rsi_series(closes, period):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


# ---------------------------------------------------------------- simulation
@dataclass
class Params:
    start_usdc: float
    max_trade: float
    position_pct: float
    max_open: int
    sl: float
    tp: float
    rsi_max_entry: float
    rsi_exit: float
    max_trades_day: int
    max_daily_loss_pct: float
    cost_pct: float
    fixed_fee: float
    ema_fast: int
    ema_slow: int
    rsi_period: int


def prepare(raw, p):
    out = {}
    for sym, d in raw.items():
        c = d["c"]
        out[sym] = {**d, "ef": ema(c, p.ema_fast), "es": ema(c, p.ema_slow),
                    "r": rsi_series(c, p.rsi_period),
                    "idx": {t: i for i, t in enumerate(d["t"])}}
    return out


def simulate(data, p):
    usdc = p.start_usdc
    positions, trades, curve = {}, [], []
    last_close = {}
    day, trades_today, pnl_today, day_start_eq = None, 0, 0.0, p.start_usdc
    skipped = {"max_open": 0, "daily_trades": 0, "daily_loss": 0, "too_small": 0}
    warm = max(p.ema_slow, p.rsi_period) * 3
    all_ts = sorted(set().union(*[d["t"] for d in data.values()]))

    def equity():
        return usdc + sum(pos["amount"] * last_close.get(s, pos["entry"]) for s, pos in positions.items())

    def close(sym, px, ts, reason):
        nonlocal usdc, pnl_today, trades_today
        pos = positions.pop(sym)
        proceeds = pos["amount"] * px * (1 - p.cost_pct / 100) - p.fixed_fee
        pnl = proceeds - pos["cost"]
        usdc += proceeds
        pnl_today += pnl
        trades_today += 1
        trades.append({"token": sym, "entry_time": pos["time"], "exit_time": ts, "entry": pos["entry"],
                       "exit": px, "cost": pos["cost"], "proceeds": proceeds, "pnl": pnl,
                       "pnl_pct": pnl / pos["cost"] * 100, "reason": reason})

    for ts in all_ts:
        dstr = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        if dstr != day:
            day, trades_today, pnl_today, day_start_eq = dstr, 0, 0.0, equity()
        for sym, d in data.items():
            i = d["idx"].get(ts)
            if i is None:
                continue
            last_close[sym] = d["c"][i]
            if i < warm or d["r"][i] is None:
                continue
            ef, es, r = d["ef"], d["es"], d["r"][i]
            cross_up = ef[i - 1] <= es[i - 1] and ef[i] > es[i]
            cross_dn = ef[i - 1] >= es[i - 1] and ef[i] < es[i]

            pos = positions.get(sym)
            if pos:
                sl_px = pos["entry"] * (1 - p.sl / 100)
                tp_px = pos["entry"] * (1 + p.tp / 100)
                if d["l"][i] <= sl_px:
                    close(sym, min(sl_px, d["o"][i]), ts, "stop_loss")
                elif d["h"][i] >= tp_px:
                    close(sym, max(tp_px, d["o"][i]), ts, "take_profit")
                elif cross_dn:
                    close(sym, d["c"][i], ts, "ema_cross_down")
                elif r >= p.rsi_exit:
                    close(sym, d["c"][i], ts, "rsi_exit")
                continue

            if not (cross_up and r < p.rsi_max_entry):
                continue
            if len(positions) >= p.max_open:
                skipped["max_open"] += 1
                continue
            if trades_today >= p.max_trades_day:
                skipped["daily_trades"] += 1
                continue
            if pnl_today <= -day_start_eq * p.max_daily_loss_pct / 100:
                skipped["daily_loss"] += 1
                continue
            size = min(p.max_trade, equity() * p.position_pct, usdc)
            if size < 5:
                skipped["too_small"] += 1
                continue
            fill = d["c"][i] * (1 + p.cost_pct / 100)
            usdc -= size
            positions[sym] = {"amount": (size - p.fixed_fee) / fill, "entry": fill,
                              "cost": size, "time": ts}
            trades_today += 1
        curve.append((ts, equity()))

    end_ts = all_ts[-1]
    for sym in list(positions):
        close(sym, last_close[sym], end_ts, "end_of_test")
    curve.append((end_ts, usdc))
    return trades, curve, skipped


# ---------------------------------------------------------------- reporting
def stats(trades, curve, start):
    final = curve[-1][1]
    peak, max_dd = start, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        worst = max(worst, streak)
    return {
        "final": final, "return_pct": (final / start - 1) * 100, "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "max_dd": max_dd, "worst_streak": worst,
    }


def buy_and_hold(data, warm):
    rets = []
    for sym, d in data.items():
        if len(d["c"]) > warm:
            rets.append((d["c"][-1] / d["c"][warm] - 1) * 100)
    return sum(rets) / len(rets) if rets else 0.0


def report(trades, curve, skipped, p, data, days):
    s = stats(trades, curve, p.start_usdc)
    warm = max(p.ema_slow, p.rsi_period) * 3
    first = datetime.fromtimestamp(curve[0][0], timezone.utc).strftime("%Y-%m-%d")
    last = datetime.fromtimestamp(curve[-1][0], timezone.utc).strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print(f" BACKTEST  {first} → {last}  ({days} days, {C.CANDLE_GRANULARITY // 60}-min candles)")
    print(f" Tokens: {', '.join(data)}")
    print(f" Settings: SL {p.sl}% | TP {p.tp}% | {p.position_pct * 100:.0f}% per trade, cap ${p.max_trade:.0f} "
          f"| max {p.max_open} open | costs {p.cost_pct}%/side + ${p.fixed_fee}/swap")
    print("=" * 60)
    print(f" Start balance      ${p.start_usdc:,.2f}")
    print(f" End balance        ${s['final']:,.2f}   ({s['return_pct']:+.2f}%)")
    print(f" Buy & hold (equal) {buy_and_hold(data, warm):+.2f}%   (for comparison)")
    print(f" Max drawdown       {s['max_dd']:.2f}%")
    print(f" Trades             {s['trades']}   (win rate {s['win_rate']:.1f}%)")
    print(f" Avg win / loss     ${s['avg_win']:+.2f} / ${s['avg_loss']:+.2f}")
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    print(f" Profit factor      {pf}   (above 1.0 = made money; above 1.3 = decent)")
    print(f" Worst losing streak {s['worst_streak']} trades in a row")

    print("\n Exits:")
    reasons = {}
    for t in trades:
        reasons.setdefault(t["reason"], []).append(t["pnl"])
    for reason, pnls in sorted(reasons.items(), key=lambda x: -len(x[1])):
        print(f"   {reason:<16} {len(pnls):>4} trades   PnL {sum(pnls):+8.2f}")

    print("\n Per token:")
    for sym in data:
        tt = [t for t in trades if t["token"] == sym]
        if tt:
            w = sum(1 for t in tt if t["pnl"] > 0)
            print(f"   {sym:<5} {len(tt):>4} trades   win {w / len(tt) * 100:5.1f}%   "
                  f"PnL {sum(t['pnl'] for t in tt):+8.2f}")
        else:
            print(f"   {sym:<5}    0 trades")
    if any(skipped.values()):
        print("\n Signals skipped by risk limits: " +
              ", ".join(f"{k} {v}" for k, v in skipped.items() if v))

    with open("backtest_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0]) if trades else ["token"])
        w.writeheader()
        for t in trades:
            row = dict(t)
            for k in ("entry_time", "exit_time"):
                row[k] = datetime.fromtimestamp(row[k], timezone.utc).strftime("%Y-%m-%d %H:%M")
            w.writerow(row)
    print("\n Every simulated trade saved to backtest_trades.csv")
    print(" Claude's filter is not included. Past results don't predict future ones.")
    print("=" * 60)


def sweep(data, p, days):
    print("\n" + "=" * 72)
    print(f" STOP-LOSS / TAKE-PROFIT SWEEP  ({days} days, other settings unchanged)")
    print("=" * 72)
    print(f" {'SL%':>5} {'TP%':>5} {'Return':>9} {'MaxDD':>8} {'Trades':>7} {'Win%':>6} {'PF':>6}")
    rows = []
    for sl in (3, 5, 8):
        for tp in (6, 10, 15):
            q = replace(p, sl=sl, tp=tp)
            trades, curve, _ = simulate(data, q)
            s = stats(trades, curve, q.start_usdc)
            rows.append((sl, tp, s))
            mark = "  ← current" if (sl, tp) == (p.sl, p.tp) else ""
            pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
            print(f" {sl:>5} {tp:>5} {s['return_pct']:>+8.2f}% {s['max_dd']:>7.2f}% {s['trades']:>7} "
                  f"{s['win_rate']:>5.1f}% {pf:>6}{mark}")
    print("\n Be wary of picking the single best row: settings tuned to one period often")
    print(" disappoint in the next. Look for combinations that do reasonably well together.")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser(description="Backtest the trading bot's strategy")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--tokens", default=",".join(C.TOKENS))
    ap.add_argument("--start-usdc", type=float, default=100.0)
    ap.add_argument("--max-trade", type=float, default=C.MAX_TRADE_USD)
    ap.add_argument("--position-pct", type=float, default=C.POSITION_PCT)
    ap.add_argument("--max-open", type=int, default=C.MAX_OPEN_POSITIONS)
    ap.add_argument("--sl", type=float, default=C.STOP_LOSS_PCT)
    ap.add_argument("--tp", type=float, default=C.TAKE_PROFIT_PCT)
    ap.add_argument("--cost-pct", type=float, default=0.25, help="swap fee + slippage per side, percent")
    ap.add_argument("--fixed-fee", type=float, default=0.05, help="network/priority fee per swap, USD")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    p = Params(start_usdc=args.start_usdc, max_trade=args.max_trade, position_pct=args.position_pct,
               max_open=args.max_open, sl=args.sl, tp=args.tp, rsi_max_entry=C.RSI_MAX_ENTRY,
               rsi_exit=C.RSI_EXIT, max_trades_day=C.MAX_TRADES_PER_DAY,
               max_daily_loss_pct=C.MAX_DAILY_LOSS_PCT, cost_pct=args.cost_pct,
               fixed_fee=args.fixed_fee, ema_fast=C.EMA_FAST, ema_slow=C.EMA_SLOW,
               rsi_period=C.RSI_PERIOD)

    print(f"Loading {args.days} days of price history (cached for 6 hours after the first run)...")
    raw = {}
    for sym in [t.strip().upper() for t in args.tokens.split(",") if t.strip()]:
        info = C.TOKEN_REGISTRY.get(sym)
        if not info:
            print(f"  {sym}: unknown token, skipped")
            continue
        try:
            raw[sym] = fetch_history(info["coinbase"], C.CANDLE_GRANULARITY, args.days)
        except Exception as e:
            print(f"  {sym}: download failed ({e}), skipped")
    if not raw:
        raise SystemExit("No price data downloaded.")

    data = prepare(raw, p)
    if args.sweep:
        sweep(data, p, args.days)
    else:
        trades, curve, skipped = simulate(data, p)
        report(trades, curve, skipped, p, data, args.days)


if __name__ == "__main__":
    main()
