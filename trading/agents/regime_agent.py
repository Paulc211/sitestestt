"""Market-regime agent: watches VIX (best-effort, free Stooq feed) and
realized volatility of SPY and BTC computed from the same 15-min bars the
strategy trades on. Publishes a single number the trading agent reads:

    regime_multiplier  1.0 = calm, trade normal size
                       0.5 = elevated, halve position sizes
                       0.0 = extreme, no new entries at all
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time

import config
from brokers.router import broker_for
from core import db
from core.alerts import send_alert
from core.http import retry_request
from core.indicators import realized_vol_annualized
from core.logging_setup import setup_logging

AGENT = "regime"
log = logging.getLogger(AGENT)

# 15-min bars: equities ~26/day * 252 days; crypto 96/day * 365
EQUITY_BARS_PER_YEAR = 26 * 252
CRYPTO_BARS_PER_YEAR = 96 * 365

STOOQ_VIX_URL = "https://stooq.com/q/l/?s=^vix&f=sd2t2ohlcv&h&e=csv"


def fetch_vix() -> float | None:
    """Best-effort. The regime call never depends on this succeeding."""
    try:
        r = retry_request("GET", STOOQ_VIX_URL, timeout=10, max_retries=2)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if rows:
            return float(rows[0]["Close"])
    except Exception:
        log.warning("VIX fetch failed; falling back to realized vol only")
    return None


class RegimeAgent:
    def run_forever(self) -> None:
        log.info("Regime agent starting")
        db.log_reasoning(AGENT, "Regime agent online")
        while True:
            started = time.time()
            try:
                self.cycle()
                db.heartbeat(AGENT, os.getpid(), "ok")
            except Exception as e:
                log.exception("Regime cycle failed")
                db.heartbeat(AGENT, os.getpid(), f"error: {type(e).__name__}")
                db.log_reasoning(AGENT, f"cycle failed: {type(e).__name__}: {e}")
            time.sleep(max(10.0, config.REGIME_INTERVAL_SEC - (time.time() - started)))

    def cycle(self) -> None:
        spy_vol = self._realized_vol("SPY", EQUITY_BARS_PER_YEAR)
        btc_vol = self._realized_vol("BTC-USD", CRYPTO_BARS_PER_YEAR)
        vix = fetch_vix()

        level = 0  # 0 calm / 1 elevated / 2 extreme
        reasons = []
        if vix is not None:
            if vix >= config.VIX_EXTREME:
                level = max(level, 2); reasons.append(f"VIX {vix:.1f} extreme")
            elif vix >= config.VIX_ELEVATED:
                level = max(level, 1); reasons.append(f"VIX {vix:.1f} elevated")
            else:
                reasons.append(f"VIX {vix:.1f} calm")
        if spy_vol is not None:
            if spy_vol >= config.REGIME_EQUITY_VOL_EXTREME:
                level = max(level, 2); reasons.append(f"SPY rvol {spy_vol:.0%} extreme")
            elif spy_vol >= config.REGIME_EQUITY_VOL_ELEVATED:
                level = max(level, 1); reasons.append(f"SPY rvol {spy_vol:.0%} elevated")
            else:
                reasons.append(f"SPY rvol {spy_vol:.0%} calm")
        if btc_vol is not None:
            if btc_vol >= config.REGIME_CRYPTO_VOL_EXTREME:
                level = max(level, 2); reasons.append(f"BTC rvol {btc_vol:.0%} extreme")
            elif btc_vol >= config.REGIME_CRYPTO_VOL_ELEVATED:
                level = max(level, 1); reasons.append(f"BTC rvol {btc_vol:.0%} elevated")
            else:
                reasons.append(f"BTC rvol {btc_vol:.0%} calm")

        if not reasons:
            # zero data sources -> do not silently keep full size
            level = 1
            reasons.append("no vol data available — defaulting to reduced size")

        mult = {0: 1.0, 1: config.REGIME_REDUCED_MULT, 2: 0.0}[level]
        label = {0: "CALM", 1: "ELEVATED", 2: "EXTREME"}[level]

        prev = db.kv_get("regime_label")
        db.kv_set("regime_multiplier", str(mult))
        db.kv_set("regime_label", label)
        summary = f"Regime {label} (mult x{mult}): " + "; ".join(reasons)
        db.log_reasoning(AGENT, summary)

        if prev is not None and prev != label:
            icon = {"CALM": "🟢", "ELEVATED": "🟡", "EXTREME": "🔴"}[label]
            send_alert(f"{icon} Regime change {prev} → {label}\n{summary}",
                       dedup_key=f"regime:{label}")

    def _realized_vol(self, symbol: str, bars_per_year: float) -> float | None:
        try:
            bars = broker_for(symbol).get_bars(symbol, config.TIMEFRAME, 64)
            if len(bars) < 10:
                return None
            return realized_vol_annualized([b["c"] for b in bars], bars_per_year)
        except Exception:
            log.warning("realized vol fetch failed for %s", symbol)
            return None


def main() -> None:
    setup_logging(AGENT)
    db.init_db()
    RegimeAgent().run_forever()


if __name__ == "__main__":
    main()
