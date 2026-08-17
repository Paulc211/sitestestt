"""Backtester — evaluate a strategy on historical 15-min bars before it ever
touches money, using the SAME strategy module, sizing rules, and stop/TP
percentages as live.

Usage:
    python backtest.py                     # 60 days, all config symbols
    python backtest.py --days 90 --symbols SPY,BTC-USD
    python backtest.py --save              # store results as the baseline the
                                           # reality-check agent compares against

Fills are simulated conservatively: entries at next bar OPEN after a signal
(we trade on closed bars with delayed data — never assume we get the close),
stops/TPs intrabar via the bar's low/high, stop checked before TP.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import config
from brokers.alpaca import is_crypto, to_alpaca
from core import db
from core.http import retry_request
from core.logging_setup import setup_logging
from core.risk import RiskManager
from core.settings import ALPACA_DATA_HOST, alpaca_keys
from strategies.base import Signal


def fetch_history(symbol: str, days: int) -> list[dict]:
    key, secret = alpaca_keys()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=days)
    bars: list[dict] = []
    page_token = None
    while True:
        params = {"timeframe": config.TIMEFRAME, "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 10000, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        if is_crypto(symbol):
            params["symbols"] = to_alpaca(symbol)
            r = retry_request("GET", f"{ALPACA_DATA_HOST}/v1beta3/crypto/us/bars",
                              headers=headers, params=params)
            data = r.json()
            raw = data.get("bars", {}).get(to_alpaca(symbol), [])
        else:
            params["feed"] = config.ALPACA_STOCK_FEED
            params["adjustment"] = "split"
            r = retry_request("GET", f"{ALPACA_DATA_HOST}/v2/stocks/{symbol}/bars",
                              headers=headers, params=params)
            data = r.json()
            raw = data.get("bars") or []
        for b in raw:
            bars.append({
                "t": datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp(),
                "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                "c": float(b["c"]), "v": float(b["v"]),
            })
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return bars


@dataclass
class OpenTrade:
    symbol: str
    qty: float
    entry: float
    stop: float
    tp: float


@dataclass
class Result:
    pnls: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def run_backtest(symbols: list[str], days: int, start_equity: float = 100_000.0,
                 verbose: bool = True) -> dict:
    import importlib
    strategy = importlib.import_module(config.STRATEGY_MODULE).build()
    risk = RiskManager()

    history = {}
    for s in symbols:
        bars = fetch_history(s, days)
        if len(bars) > strategy.min_bars + 5:
            history[s] = bars
            if verbose:
                print(f"  {s}: {len(bars)} bars")
        else:
            print(f"  {s}: only {len(bars)} bars — skipped", file=sys.stderr)
    if not history:
        raise SystemExit("No usable history fetched — check API keys / symbols.")

    # merge timeline of all bar timestamps
    timeline = sorted({b["t"] for bars in history.values() for b in bars})
    index = {s: 0 for s in history}
    equity = start_equity
    day_start_equity = start_equity
    current_day = None
    day_halted = False
    open_trades: dict[str, OpenTrade] = {}
    res = Result()

    for t in timeline:
        day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        if day != current_day:
            current_day = day
            day_start_equity = equity
            day_halted = False

        for sym, bars in history.items():
            i = index[sym]
            while i < len(bars) and bars[i]["t"] < t:
                i += 1
            index[sym] = i
            if i >= len(bars) or bars[i]["t"] != t:
                continue
            bar = bars[i]

            # manage open position intrabar: stop first (conservative)
            if sym in open_trades:
                ot = open_trades[sym]
                exit_price = None
                if bar["l"] <= ot.stop:
                    exit_price = ot.stop
                elif bar["h"] >= ot.tp:
                    exit_price = ot.tp
                if exit_price is not None:
                    pnl = (exit_price - ot.entry) * ot.qty
                    equity += pnl
                    res.pnls.append(pnl)
                    del open_trades[sym]

            # signals on bars up to and including this closed bar
            window = bars[max(0, i - config.BARS_LOOKBACK):i + 1]
            sig = strategy.generate(sym, window)

            if sig.action == Signal.SELL and sym in open_trades:
                ot = open_trades.pop(sym)
                # exit at next bar open if available, else this close
                nxt = bars[i + 1]["o"] if i + 1 < len(bars) else bar["c"]
                pnl = (nxt - ot.entry) * ot.qty
                equity += pnl
                res.pnls.append(pnl)

            elif sig.action == Signal.BUY and sym not in open_trades \
                    and not day_halted and len(open_trades) < config.MAX_OPEN_POSITIONS:
                nxt = bars[i + 1]["o"] if i + 1 < len(bars) else None
                if nxt is None:
                    continue
                qty, _ = risk.position_size(equity, nxt, 1.0, is_crypto(sym))
                if qty <= 0:
                    continue
                stop, tp = risk.protective_prices(nxt)
                open_trades[sym] = OpenTrade(sym, qty, nxt, stop, tp)

        # daily loss cap in-sim
        if not day_halted and day_start_equity > 0 and \
                (equity - day_start_equity) / day_start_equity <= -config.DAILY_LOSS_CAP_PCT:
            day_halted = True

        res.equity_curve.append(equity)

    # liquidate leftovers at last close
    for sym, ot in list(open_trades.items()):
        last = history[sym][-1]["c"]
        pnl = (last - ot.entry) * ot.qty
        equity += pnl
        res.pnls.append(pnl)
    res.equity_curve.append(equity)

    wins = [p for p in res.pnls if p > 0]
    peak = res.equity_curve[0]
    max_dd = 0.0
    for v in res.equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak > 0 else 0)

    return {
        "trades": len(res.pnls),
        "win_rate": len(wins) / len(res.pnls) if res.pnls else 0.0,
        "expectancy": sum(res.pnls) / len(res.pnls) if res.pnls else 0.0,
        "total_return": (equity - start_equity) / start_equity,
        "max_drawdown": max_dd,
        "final_equity": equity,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default=",".join(config.ALL_SYMBOLS))
    ap.add_argument("--save", action="store_true",
                    help="save as the reality-check baseline")
    args = ap.parse_args()

    setup_logging("backtest")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"Backtesting {config.STRATEGY_MODULE} over {args.days}d on {symbols}")
    stats = run_backtest(symbols, args.days)

    print("\n=== Backtest results ===")
    print(f"  trades:        {stats['trades']}")
    print(f"  win rate:      {stats['win_rate']:.1%}")
    print(f"  expectancy:    ${stats['expectancy']:.2f}/trade")
    print(f"  total return:  {stats['total_return']:.2%}")
    print(f"  max drawdown:  {stats['max_drawdown']:.2%}")
    print(f"  final equity:  ${stats['final_equity']:,.2f}")

    if args.save:
        db.init_db()
        strategy_name = config.STRATEGY_MODULE.rsplit(".", 1)[-1]
        db.save_backtest_stats(
            strategy_name, args.days, stats["trades"], stats["win_rate"],
            stats["expectancy"], stats["max_drawdown"], stats["total_return"],
            notes=f"symbols={','.join(symbols)}",
        )
        print(f"\nSaved as baseline for '{strategy_name}' — the reality-check "
              f"agent will now compare live results against it.")


if __name__ == "__main__":
    main()
