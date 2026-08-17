"""Pure-Python indicators. No pandas/numpy — keeps the resident footprint tiny
on a 1GB droplet and makes the math auditable line by line.
"""
from __future__ import annotations

import math


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average. Seeds with the SMA of the first `period`
    values; entries before the seed are None-equivalent (math.nan)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float] = [math.nan] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float] = [math.nan] * len(values)
    if len(values) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def realized_vol_annualized(closes: list[float], bars_per_year: float) -> float:
    """Annualized realized volatility from bar-close log returns."""
    if len(closes) < 3:
        return 0.0
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(bars_per_year)
