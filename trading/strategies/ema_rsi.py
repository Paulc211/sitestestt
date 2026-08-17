"""EMA(12/26) crossover filtered by RSI(14).

Buy : fast EMA crosses ABOVE slow EMA on the latest closed bar and RSI is
      not overbought.
Sell: fast EMA crosses BELOW slow EMA, unless RSI says the move is already
      exhausted to the downside.
Else: hold, with the reasoning spelled out.
"""
from __future__ import annotations

import math
from typing import Any

import config
from core.indicators import ema, rsi

from .base import Signal, Strategy


class EmaRsiStrategy(Strategy):
    name = "ema_rsi"

    def __init__(self, fast: int = config.EMA_FAST, slow: int = config.EMA_SLOW,
                 rsi_period: int = config.RSI_PERIOD,
                 overbought: float = config.RSI_OVERBOUGHT,
                 oversold: float = config.RSI_OVERSOLD):
        self.fast, self.slow = fast, slow
        self.rsi_period = rsi_period
        self.overbought, self.oversold = overbought, oversold
        self.min_bars = slow + rsi_period + 2

    def generate(self, symbol: str, bars: list[dict[str, Any]]) -> Signal:
        if len(bars) < self.min_bars:
            return Signal(Signal.HOLD,
                          f"only {len(bars)} bars, need {self.min_bars}")
        closes = [b["c"] for b in bars]
        f = ema(closes, self.fast)
        s = ema(closes, self.slow)
        r = rsi(closes, self.rsi_period)
        if any(math.isnan(x) for x in (f[-1], f[-2], s[-1], s[-2], r[-1])):
            return Signal(Signal.HOLD, "indicators not warmed up")

        cur_rsi = r[-1]
        crossed_up = f[-2] <= s[-2] and f[-1] > s[-1]
        crossed_down = f[-2] >= s[-2] and f[-1] < s[-1]
        spread = (f[-1] - s[-1]) / s[-1] * 100

        if crossed_up:
            if cur_rsi >= self.overbought:
                return Signal(Signal.HOLD,
                              f"bullish EMA cross but RSI {cur_rsi:.0f} >= "
                              f"{self.overbought:.0f} (overbought) — skipping")
            return Signal(Signal.BUY,
                          f"EMA{self.fast} crossed above EMA{self.slow} "
                          f"(spread {spread:+.2f}%), RSI {cur_rsi:.0f} ok")
        if crossed_down:
            if cur_rsi <= self.oversold:
                return Signal(Signal.HOLD,
                              f"bearish EMA cross but RSI {cur_rsi:.0f} <= "
                              f"{self.oversold:.0f} (oversold) — not chasing")
            return Signal(Signal.SELL,
                          f"EMA{self.fast} crossed below EMA{self.slow} "
                          f"(spread {spread:+.2f}%), RSI {cur_rsi:.0f}")
        trend = "above" if f[-1] > s[-1] else "below"
        return Signal(Signal.HOLD,
                      f"RSI {cur_rsi:.0f}, no crossover (fast {trend} slow, "
                      f"spread {spread:+.2f}%), holding")


def build() -> Strategy:
    return EmaRsiStrategy()
