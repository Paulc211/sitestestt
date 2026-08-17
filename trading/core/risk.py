"""The risk manager. Every order the trading agent wants to place goes through
RiskManager.evaluate_entry(); there is no code path that reaches a broker
without passing it. Pure logic + injected state so it is fully unit-testable
— tests/test_risk.py covers every rule.

The watchdog holds its OWN RiskLimits instance built from config and never
consumes anything computed here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import config


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = config.MAX_POSITION_PCT
    max_open_positions: int = config.MAX_OPEN_POSITIONS
    stop_loss_pct: float = config.STOP_LOSS_PCT
    take_profit_pct: float = config.TAKE_PROFIT_PCT
    daily_loss_cap_pct: float = config.DAILY_LOSS_CAP_PCT
    min_crypto_notional: float = config.MIN_CRYPTO_NOTIONAL_USD


@dataclass
class RiskContext:
    """Everything the rules need, gathered by the caller each cycle."""
    equity: float
    day_start_equity: float
    open_position_count: int
    open_symbols: set[str] = field(default_factory=set)
    halt_flag_active: bool = False
    day_halted: bool = False           # daily loss cap already tripped today
    regime_multiplier: float = 1.0     # 1.0 calm / 0.5 elevated / 0.0 halt
    avoid_symbols: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    qty: float = 0.0
    notional: float = 0.0
    stop_price: float = 0.0
    tp_price: float = 0.0


def daily_loss_breached(day_start_equity: float, current_equity: float,
                        cap_pct: float) -> bool:
    """True when equity has fallen cap_pct or more from the day's start.
    A zero/negative/NaN day-start is treated as breached — if we cannot
    establish the baseline we do not trade. Fail closed."""
    if not _is_finite_positive(day_start_equity) or not _is_finite_positive(current_equity):
        return True
    return (current_equity - day_start_equity) / day_start_equity <= -abs(cap_pct)


def _is_finite_positive(x: float) -> bool:
    try:
        return math.isfinite(x) and x > 0
    except TypeError:
        return False


class RiskManager:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    # -- sizing ---------------------------------------------------------

    def position_size(self, equity: float, price: float,
                      regime_multiplier: float, is_crypto: bool) -> tuple[float, float]:
        """Returns (qty, notional). qty == 0 means 'too small to trade'."""
        if not _is_finite_positive(equity) or not _is_finite_positive(price):
            return 0.0, 0.0
        mult = min(max(regime_multiplier, 0.0), 1.0)  # regime can only shrink
        notional = equity * self.limits.max_position_pct * mult
        if is_crypto:
            if notional < self.limits.min_crypto_notional:
                return 0.0, 0.0
            qty = round(notional / price, 6)
            return qty, qty * price
        qty = math.floor(notional / price) if config.STOCK_WHOLE_SHARES \
            else notional / price
        if qty < 1 and config.STOCK_WHOLE_SHARES:
            return 0.0, 0.0
        return float(qty), qty * price

    def protective_prices(self, entry_price: float) -> tuple[float, float]:
        """(stop, take_profit) for a long entry."""
        stop = round(entry_price * (1 - self.limits.stop_loss_pct), 2)
        tp = round(entry_price * (1 + self.limits.take_profit_pct), 2)
        return stop, tp

    # -- the gate -------------------------------------------------------

    def evaluate_entry(self, symbol: str, price: float, ctx: RiskContext,
                       is_crypto: bool) -> RiskDecision:
        """Single gate for every new entry. Ordered so the cheapest and most
        safety-critical checks run first. Fail closed on anything odd."""
        if ctx.halt_flag_active:
            return RiskDecision(False, "halt.flag present — all trading halted")
        if ctx.day_halted:
            return RiskDecision(False, "daily loss cap tripped — no new trades today")
        if daily_loss_breached(ctx.day_start_equity, ctx.equity,
                               self.limits.daily_loss_cap_pct):
            return RiskDecision(
                False,
                f"daily loss cap: equity {ctx.equity:.2f} vs day start "
                f"{ctx.day_start_equity:.2f}",
            )
        if ctx.regime_multiplier <= 0:
            return RiskDecision(False, "regime agent: market too turbulent, sizing to zero")
        if symbol in ctx.avoid_symbols:
            return RiskDecision(False, f"{symbol} on news avoid list")
        if symbol in ctx.open_symbols:
            return RiskDecision(False, f"already holding {symbol}")
        if ctx.open_position_count >= self.limits.max_open_positions:
            return RiskDecision(
                False,
                f"max open positions reached ({ctx.open_position_count}"
                f"/{self.limits.max_open_positions})",
            )
        if not _is_finite_positive(price):
            return RiskDecision(False, f"bad price for {symbol}: {price!r}")

        qty, notional = self.position_size(
            ctx.equity, price, ctx.regime_multiplier, is_crypto
        )
        if qty <= 0:
            return RiskDecision(
                False, f"position size for {symbol} too small at equity "
                       f"{ctx.equity:.2f} (regime mult {ctx.regime_multiplier})"
            )
        stop, tp = self.protective_prices(price)
        if not (stop < price < tp):
            return RiskDecision(False, f"degenerate stop/tp for {symbol}")
        return RiskDecision(True, "all risk checks passed", qty=qty,
                            notional=notional, stop_price=stop, tp_price=tp)

    # -- watchdog-side checks ------------------------------------------

    def position_breaches_limit(self, market_value: float, equity: float,
                                tolerance: float) -> bool:
        """Used by the watchdog against REAL broker positions."""
        if not _is_finite_positive(equity):
            return True
        return abs(market_value) > equity * self.limits.max_position_pct * tolerance
