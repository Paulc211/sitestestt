"""Reality-check agent: compares live/paper results against the most recent
backtest expectations stored by backtest.py --save. Alerts when live
performance diverges meaningfully — the failure mode where a strategy quietly
stops working and nothing else notices.
"""
from __future__ import annotations

import logging
import os
import time

import config
from core import db
from core.alerts import send_alert
from core.logging_setup import setup_logging

AGENT = "reality"
log = logging.getLogger(AGENT)


def live_metrics(lookback_days: int) -> dict | None:
    """Win rate + expectancy from completed round trips in the trades table.
    A round trip = a sell whose pnl is recorded, or matched buy->sell pairs."""
    since = time.time() - lookback_days * 86400
    trades = db.get_trades(limit=2000, since_ts=since)
    # pair FIFO per symbol
    buys: dict[str, list[dict]] = {}
    round_trips: list[float] = []
    for t in sorted(trades, key=lambda x: x["ts"]):
        if t["side"] == "buy":
            buys.setdefault(t["symbol"], []).append(t)
        elif t["side"] == "sell":
            if t.get("pnl") is not None:
                round_trips.append(t["pnl"])
                continue
            stack = buys.get(t["symbol"])
            if stack and t.get("price") and stack[0].get("price"):
                b = stack.pop(0)
                qty = b["qty"] or t["qty"] or 0
                round_trips.append((t["price"] - b["price"]) * qty)
    if len(round_trips) < config.REALITY_MIN_TRADES:
        return None
    wins = [p for p in round_trips if p > 0]
    return {
        "trades": len(round_trips),
        "win_rate": len(wins) / len(round_trips),
        "expectancy": sum(round_trips) / len(round_trips),
    }


def drawdown_from_equity(lookback_days: int) -> float | None:
    curve = db.get_equity_curve(points=5000)
    since = time.time() - lookback_days * 86400
    eq = [p["equity"] for p in curve if p["ts"] >= since]
    if len(eq) < 10:
        return None
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    return max_dd


class RealityCheckAgent:
    def run_forever(self) -> None:
        log.info("Reality-check agent starting")
        db.log_reasoning(AGENT, "Reality-check agent online")
        while True:
            started = time.time()
            try:
                self.cycle()
                db.heartbeat(AGENT, os.getpid(), "ok")
            except Exception as e:
                log.exception("Reality-check cycle failed")
                db.heartbeat(AGENT, os.getpid(), f"error: {type(e).__name__}")
                db.log_reasoning(AGENT, f"cycle failed: {type(e).__name__}: {e}")
            time.sleep(max(30.0, config.REALITY_INTERVAL_SEC - (time.time() - started)))

    def cycle(self) -> None:
        strategy = config.STRATEGY_MODULE.rsplit(".", 1)[-1]
        expected = db.latest_backtest_stats(strategy)
        if not expected:
            db.log_reasoning(
                AGENT, "no backtest baseline saved yet — run "
                       "`python backtest.py --save` to enable divergence checks")
            return

        live = live_metrics(config.REALITY_LOOKBACK_DAYS)
        dd = drawdown_from_equity(config.REALITY_LOOKBACK_DAYS)
        problems = []

        if live is None:
            db.log_reasoning(
                AGENT, f"fewer than {config.REALITY_MIN_TRADES} completed "
                       f"round trips in {config.REALITY_LOOKBACK_DAYS}d — "
                       f"not enough data to compare yet")
        else:
            if (expected["win_rate"] or 0) - live["win_rate"] > \
                    config.REALITY_WIN_RATE_DELTA:
                problems.append(
                    f"win rate {live['win_rate']:.0%} vs backtest "
                    f"{expected['win_rate']:.0%}")
            if live["expectancy"] < 0 and (expected["expectancy"] or 0) > 0:
                problems.append(
                    f"live expectancy {live['expectancy']:.2f}/trade is "
                    f"negative (backtest {expected['expectancy']:.2f})")
            db.log_reasoning(
                AGENT,
                f"live {live['trades']} trades, win rate {live['win_rate']:.0%} "
                f"(backtest {expected['win_rate']:.0%}), expectancy "
                f"{live['expectancy']:.2f} (backtest {expected['expectancy']:.2f})")

        if dd is not None and expected["max_drawdown"]:
            if dd > expected["max_drawdown"] * config.REALITY_DRAWDOWN_FACTOR:
                problems.append(
                    f"drawdown {dd:.1%} vs backtest max "
                    f"{expected['max_drawdown']:.1%}")

        if problems:
            msg = ("📉 REALITY CHECK: live performance diverging from "
                   "backtest:\n- " + "\n- ".join(problems) +
                   "\nConsider halting (touch halt.flag) and re-evaluating.")
            db.log_reasoning(AGENT, msg)
            send_alert(msg, dedup_key="reality_divergence")


def main() -> None:
    setup_logging(AGENT)
    db.init_db()
    RealityCheckAgent().run_forever()


if __name__ == "__main__":
    main()
