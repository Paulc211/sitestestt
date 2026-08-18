"""Trading agent: pull bars -> signal -> risk gate -> protected order.

Safety order of operations, every cycle and again before EVERY order:
  1. halt.flag check
  2. daily loss cap check (also enforced independently by the watchdog)
  3. regime multiplier / news avoid list
  4. risk manager sizing
Every order carries a broker-side stop; stocks also carry a broker-side TP
(bracket). Crypto TP is enforced in software here, checked each cycle.
"""
from __future__ import annotations

import importlib
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from brokers.alpaca import is_crypto
from brokers.router import broker_for, equity_broker
from core import db
from core.alerts import send_alert
from core.halt import halt_active, halt_reason
from core.logging_setup import setup_logging
from core.risk import RiskContext, RiskManager, daily_loss_breached
from core.settings import get_mode
from strategies.base import Signal

AGENT = "trading"
NY = ZoneInfo("America/New_York")

log = logging.getLogger(AGENT)


def load_strategy():
    mod = importlib.import_module(config.STRATEGY_MODULE)
    return mod.build()


def trading_day() -> str:
    return datetime.now(NY).strftime("%Y-%m-%d")


def get_day_start_equity(current_equity: float) -> float:
    """First reading of the day becomes the baseline for the -2% kill switch."""
    day = trading_day()
    key = f"day_start_equity:{day}"
    stored = db.kv_get(key)
    if stored is not None:
        return float(stored)
    db.kv_set(key, str(current_equity))
    db.log_reasoning(AGENT, f"Day {day} baseline equity set: {current_equity:.2f}")
    return current_equity


def day_halted() -> bool:
    return db.kv_get(f"day_halt:{trading_day()}") is not None


def set_day_halt(reason: str) -> None:
    db.kv_set(f"day_halt:{trading_day()}", reason)


def regime_multiplier() -> float:
    raw = db.kv_get("regime_multiplier", "1.0")
    try:
        return min(max(float(raw), 0.0), 1.0)
    except (TypeError, ValueError):
        return 1.0  # regime agent down -> trade at normal size; watchdog
                    # still enforces hard limits


class TradingAgent:
    def __init__(self) -> None:
        self.mode = get_mode()
        self.risk = RiskManager()
        self.strategy = load_strategy()

    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        log.info("Trading agent starting — MODE=%s strategy=%s", self.mode,
                 self.strategy.name)
        db.log_reasoning(AGENT, f"Started in {self.mode.upper()} mode, "
                                f"strategy {self.strategy.name}")
        if self.mode == "live":
            send_alert("⚠️ Trading agent started in LIVE mode",
                       dedup_key="live_start")
        while True:
            started = time.time()
            try:
                self.cycle()
                db.heartbeat(AGENT, os.getpid(), "ok")
            except Exception as e:
                log.exception("Cycle failed")
                # the error-reporting writes must never be able to kill the
                # process themselves (e.g. transient sqlite lock)
                try:
                    db.heartbeat(AGENT, os.getpid(), f"error: {type(e).__name__}")
                    db.log_reasoning(AGENT, f"Cycle failed: {type(e).__name__}: {e}")
                except Exception:
                    log.exception("Could not record cycle error in DB")
            elapsed = time.time() - started
            time.sleep(max(5.0, config.LOOP_INTERVAL_SEC - elapsed))

    # ------------------------------------------------------------------

    def cycle(self) -> None:
        if halt_active():
            db.log_reasoning(AGENT, f"HALTED — halt.flag present: {halt_reason()}")
            return

        broker = equity_broker()
        account = broker.get_account()
        equity = account["equity"]
        day_start = get_day_start_equity(equity)
        db.record_equity(equity, AGENT)

        positions = broker.get_positions()
        db.snapshot_positions(positions)
        open_symbols = {p["symbol"] for p in positions}

        # daily loss kill switch (agent-side; watchdog enforces it independently)
        if not day_halted() and daily_loss_breached(
                day_start, equity, self.risk.limits.daily_loss_cap_pct):
            set_day_halt(f"equity {equity:.2f} vs day start {day_start:.2f}")
            msg = (f"🛑 DAILY LOSS CAP HIT: equity {equity:.2f} vs day start "
                   f"{day_start:.2f}. No new trades until tomorrow.")
            db.log_reasoning(AGENT, msg)
            send_alert(msg, dedup_key="daily_loss_cap")

        ctx = RiskContext(
            equity=equity,
            day_start_equity=day_start,
            open_position_count=len(positions),
            open_symbols=open_symbols,
            halt_flag_active=halt_active(),
            day_halted=day_halted(),
            regime_multiplier=regime_multiplier(),
            avoid_symbols=db.avoided_symbols(),
        )
        db.log_reasoning(
            AGENT,
            f"Cycle: equity {equity:.2f}, {len(positions)} open, "
            f"regime x{ctx.regime_multiplier}, avoid={sorted(ctx.avoid_symbols) or '—'}, "
            f"day_halted={ctx.day_halted}",
        )

        self._check_crypto_take_profits(positions)

        for symbol in config.ALL_SYMBOLS:
            try:
                self._handle_symbol(symbol, ctx)
            except Exception as e:
                log.exception("Symbol %s failed", symbol)
                db.log_reasoning(AGENT, f"error: {type(e).__name__}: {e}", symbol)

    # ------------------------------------------------------------------

    def _handle_symbol(self, symbol: str, ctx: RiskContext) -> None:
        broker = broker_for(symbol)
        if not broker.market_open(symbol):
            db.log_reasoning(AGENT, "market closed, skipping", symbol)
            return
        bars = broker.get_bars(symbol, config.TIMEFRAME, config.BARS_LOOKBACK)
        if not bars:
            db.log_reasoning(AGENT, "no bars returned", symbol)
            return
        signal = self.strategy.generate(symbol, bars)
        db.log_reasoning(AGENT, signal.reason, symbol)

        last_close = bars[-1]["c"]
        holding = symbol in ctx.open_symbols

        if signal.action == Signal.BUY and not holding:
            # one entry attempt per signal bar: if the order fails, do NOT
            # re-buy on the same bar every cycle (that bleeds the spread)
            bar_key = f"entry_bar:{symbol}"
            bar_ts = str(bars[-1]["t"])
            if db.kv_get(bar_key) == bar_ts:
                db.log_reasoning(AGENT, "entry already attempted on this bar — "
                                        "waiting for next signal", symbol)
                return
            db.kv_set(bar_key, bar_ts)
            self._enter(symbol, last_close, ctx, broker)
        elif signal.action == Signal.SELL and holding:
            self._exit(symbol, last_close, signal.reason, broker, ctx)

    def _enter(self, symbol: str, price: float, ctx: RiskContext, broker) -> None:
        # re-check halt RIGHT before ordering — any agent may have tripped it
        # since the cycle started
        ctx.halt_flag_active = halt_active()
        ctx.day_halted = day_halted()
        decision = self.risk.evaluate_entry(symbol, price, ctx,
                                            is_crypto(symbol))
        if not decision.allowed:
            db.log_reasoning(AGENT, f"entry blocked: {decision.reason}", symbol)
            return
        order = broker.place_protected_buy(symbol, decision.qty,
                                           decision.stop_price,
                                           decision.tp_price)
        db.set_position_targets(symbol, decision.stop_price, decision.tp_price,
                                decision.qty)
        db.record_trade(symbol, "buy", decision.qty, price,
                        order.get("id") or order.get("order_id"),
                        f"entry: {decision.reason}", self.mode)
        db.log_reasoning(
            AGENT,
            f"BUY {decision.qty} @ ~{price:.2f} (notional {decision.notional:.2f}, "
            f"stop {decision.stop_price:.2f}, tp {decision.tp_price:.2f})",
            symbol,
        )
        ctx.open_symbols.add(symbol)
        ctx.open_position_count += 1

    def _exit(self, symbol: str, price: float, reason: str, broker,
              ctx: RiskContext) -> None:
        if halt_active():
            db.log_reasoning(AGENT, "halt.flag present — not placing exit order "
                                    "(watchdog owns cleanup)", symbol)
            return
        broker.close_position(symbol)
        db.clear_position_targets(symbol)
        db.record_trade(symbol, "sell", 0, price, None, f"exit: {reason}",
                        self.mode)
        db.log_reasoning(AGENT, f"SELL (close) @ ~{price:.2f}: {reason}", symbol)
        ctx.open_symbols.discard(symbol)
        ctx.open_position_count = max(0, ctx.open_position_count - 1)

    # ------------------------------------------------------------------

    def _check_crypto_take_profits(self, positions: list[dict]) -> None:
        """Crypto venues here don't support OCO with our stop, so TP is
        enforced in software (the STOP is broker-side, which is the leg that
        matters if this server dies)."""
        for p in positions:
            if not is_crypto(p["symbol"]):
                continue
            targets = db.get_position_targets(p["symbol"])
            if not targets:
                continue
            cur = p.get("current_price") or 0
            if cur and cur >= targets["tp_price"]:
                if halt_active():
                    return
                broker = broker_for(p["symbol"])
                broker.close_position(p["symbol"])
                db.clear_position_targets(p["symbol"])
                db.record_trade(p["symbol"], "sell", p["qty"], cur, None,
                                f"take profit hit ({cur:.2f} >= "
                                f"{targets['tp_price']:.2f})", self.mode)
                msg = f"💰 Take profit: {p['symbol']} @ {cur:.2f}"
                db.log_reasoning(AGENT, msg, p["symbol"])
                send_alert(msg, dedup_key=f"tp:{p['symbol']}")


def main() -> None:
    setup_logging(AGENT)
    db.init_db()
    TradingAgent().run_forever()


if __name__ == "__main__":
    main()
