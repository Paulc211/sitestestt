"""Watchdog: trusts NOTHING the trading agent writes.

Every minute it queries the brokers directly for real equity and real
positions, holds its own copy of the risk limits (built straight from
config), and compares reality against them.

On a hard breach: cancel all open orders -> flatten all positions ->
write halt.flag -> SIGKILL the trading agent -> Telegram alert.

Also detects: stale trading heartbeat (agent went silent) and crash-looping
(PID churn), and it enforces the daily loss cap with its OWN baseline —
recorded from the broker, not from the trading agent's numbers.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from brokers.router import all_brokers, equity_broker
from core import db
from core.alerts import send_alert
from core.halt import halt_active, set_halt
from core.logging_setup import setup_logging
from core.risk import RiskLimits, RiskManager, daily_loss_breached

AGENT = "watchdog"
NY = ZoneInfo("America/New_York")
log = logging.getLogger(AGENT)


class Watchdog:
    def __init__(self) -> None:
        # own limits, own risk manager — independent of the trading agent
        self.risk = RiskManager(RiskLimits())

    def run_forever(self) -> None:
        log.info("Watchdog starting")
        db.log_reasoning(AGENT, "Watchdog online — independently verifying "
                                "broker state every %ds" % config.WATCHDOG_INTERVAL_SEC)
        while True:
            started = time.time()
            try:
                self.cycle()
                db.heartbeat(AGENT, os.getpid(), "ok")
            except Exception as e:
                log.exception("Watchdog cycle failed")
                db.heartbeat(AGENT, os.getpid(), f"error: {type(e).__name__}")
                db.log_reasoning(AGENT, f"cycle failed: {type(e).__name__}: {e}")
                # a watchdog that cannot see the broker repeatedly is itself
                # an alert-worthy condition
                fails = int(db.kv_get("wd_consecutive_failures", "0")) + 1
                db.kv_set("wd_consecutive_failures", str(fails))
                if fails >= 5:
                    send_alert(f"⚠️ Watchdog cannot reach broker "
                               f"({fails} consecutive failures): "
                               f"{type(e).__name__}", dedup_key="wd_blind")
            else:
                db.kv_set("wd_consecutive_failures", "0")
            time.sleep(max(5.0, config.WATCHDOG_INTERVAL_SEC - (time.time() - started)))

    # ------------------------------------------------------------------

    def cycle(self) -> None:
        equity, positions = self._real_state()
        day_start = self._own_day_start(equity)
        db.record_equity(equity, AGENT)

        # 1. daily loss cap — the hard version. Trading agent stops entering
        #    at -2%; if reality is at breach AND the trading agent somehow
        #    still holds risk, we flatten everything.
        if daily_loss_breached(day_start, equity,
                               self.risk.limits.daily_loss_cap_pct):
            if not halt_active():
                self.emergency_stop(
                    f"daily loss cap breached at broker: equity {equity:.2f} "
                    f"vs day start {day_start:.2f}")
            return

        # 2. per-position size breach vs REAL market values
        for p in positions:
            if self.risk.position_breaches_limit(
                    p["market_value"] or 0, equity,
                    config.WATCHDOG_POSITION_TOLERANCE):
                self.emergency_stop(
                    f"position limit breach: {p['symbol']} market value "
                    f"{p['market_value']:.2f} vs equity {equity:.2f} "
                    f"(cap {self.risk.limits.max_position_pct:.0%} "
                    f"x{config.WATCHDOG_POSITION_TOLERANCE})")
                return

        # 3. too many open positions (allow +1 for in-flight fills)
        if len(positions) > self.risk.limits.max_open_positions + 1:
            self.emergency_stop(
                f"{len(positions)} open positions exceeds limit "
                f"{self.risk.limits.max_open_positions}")
            return

        # 4. every position must have a working protective sell order
        self._verify_protective_orders(positions)

        # 5. liveness of the trading agent
        self._check_liveness()

        db.log_reasoning(
            AGENT,
            f"OK: broker equity {equity:.2f} (day start {day_start:.2f}), "
            f"{len(positions)} positions within limits",
        )

    # ------------------------------------------------------------------

    def _real_state(self) -> tuple[float, list[dict]]:
        equity = 0.0
        positions: list[dict] = []
        for b in all_brokers():
            acct = b.get_account()
            equity += acct["equity"]
            positions.extend(b.get_positions())
        return equity, positions

    def _own_day_start(self, equity: float) -> float:
        day = datetime.now(NY).strftime("%Y-%m-%d")
        key = f"wd_day_start_equity:{day}"
        stored = db.kv_get(key)
        if stored is not None:
            return float(stored)
        db.kv_set(key, str(equity))
        db.log_reasoning(AGENT, f"Watchdog day baseline (from broker): {equity:.2f}")
        return equity

    def _verify_protective_orders(self, positions: list[dict]) -> None:
        """A position with no working sell order has no broker-side stop —
        that's exactly the failure mode broker-side stops exist to prevent."""
        if not positions:
            return
        protected: set[str] = set()
        for b in all_brokers():
            for o in b.get_open_orders():
                if o["side"] == "sell":
                    protected.add(o["symbol"])
        naked = [p["symbol"] for p in positions if p["symbol"] not in protected]
        if naked:
            msg = f"⚠️ Positions with NO protective sell order at broker: {naked}"
            db.log_reasoning(AGENT, msg)
            send_alert(msg, dedup_key="naked_positions")

    def _check_liveness(self) -> None:
        hbs = {h["agent"]: h for h in db.get_heartbeats()}
        tr = hbs.get("trading")
        now = time.time()

        if tr is None or now - tr["ts"] > config.WATCHDOG_STALE_HEARTBEAT_SEC:
            age = "never" if tr is None else f"{now - tr['ts']:.0f}s ago"
            if not halt_active():
                set_halt(f"trading agent silent (last heartbeat {age})", AGENT)
                msg = (f"🔇 Trading agent SILENT (last heartbeat {age}). "
                       f"halt.flag written. Broker-side stops remain active.")
                db.log_reasoning(AGENT, msg)
                send_alert(msg, dedup_key="trading_silent")
                if config.WATCHDOG_FLATTEN_ON_SILENCE:
                    self._cancel_and_flatten()
            return

        pids = db.recent_pids("trading", config.WATCHDOG_CRASH_LOOP_WINDOW_SEC)
        if len(pids) >= config.WATCHDOG_CRASH_LOOP_RESTARTS:
            if not halt_active():
                set_halt(f"trading agent crash-looping ({len(pids)} PIDs in "
                         f"{config.WATCHDOG_CRASH_LOOP_WINDOW_SEC}s)", AGENT)
                msg = (f"💥 Trading agent CRASH-LOOPING: {len(pids)} restarts in "
                       f"{config.WATCHDOG_CRASH_LOOP_WINDOW_SEC // 60} min. "
                       f"halt.flag written.")
                db.log_reasoning(AGENT, msg)
                send_alert(msg, dedup_key="crash_loop")

    # ------------------------------------------------------------------

    def emergency_stop(self, reason: str) -> None:
        """Breach response, in strict order: stop the bleeding first."""
        log.critical("EMERGENCY STOP: %s", reason)
        db.log_reasoning(AGENT, f"🛑 EMERGENCY STOP: {reason}")

        # 1. halt flag FIRST so the trading agent stops placing orders even
        #    while we're still cancelling
        set_halt(reason, AGENT)

        # 2+3. cancel everything, then flatten everything, on every broker
        self._cancel_and_flatten()

        # 4. kill the trading agent process
        self._kill_trading_agent()

        # 5. tell the human
        send_alert(f"🛑 WATCHDOG EMERGENCY STOP\n{reason}\n"
                   f"Orders cancelled, positions flattened, halt.flag set, "
                   f"trading agent killed.\n"
                   f"Remove {config.HALT_FLAG_PATH} to resume.",
                   dedup_key="emergency_stop")

    def _cancel_and_flatten(self) -> None:
        for b in all_brokers():
            try:
                b.cancel_all_orders()
            except Exception:
                log.exception("cancel_all_orders failed on %s", b.name)
            try:
                b.close_all_positions()
            except Exception:
                log.exception("close_all_positions failed on %s", b.name)

    def _kill_trading_agent(self) -> None:
        hbs = {h["agent"]: h for h in db.get_heartbeats()}
        tr = hbs.get("trading")
        if not tr:
            return
        pid = tr["pid"]
        if pid == os.getpid():
            return
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(3)
            os.kill(pid, signal.SIGKILL)  # raises if already gone
            log.warning("Trading agent pid %d SIGKILLed", pid)
        except ProcessLookupError:
            log.info("Trading agent pid %d already gone", pid)
        except PermissionError:
            log.error("No permission to kill pid %d", pid)
        # systemd will restart it, but it will refuse to trade: halt.flag is set


def main() -> None:
    setup_logging(AGENT)
    db.init_db()
    Watchdog().run_forever()


if __name__ == "__main__":
    main()
