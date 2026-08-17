"""Risk manager + mode-safety tests. These rules are the ones standing between
a bug and real money — every gate is exercised, including the fail-closed
paths for malformed inputs.

Run:  pytest tests/ -v   (from the trading/ directory)
"""
from __future__ import annotations

import math

import pytest

import config
from core.risk import (RiskContext, RiskLimits, RiskManager,
                       daily_loss_breached)
from core.settings import LIVE_CONFIRM_PHRASE, resolve_mode


def make_ctx(**overrides) -> RiskContext:
    base = dict(
        equity=100_000.0,
        day_start_equity=100_000.0,
        open_position_count=0,
        open_symbols=set(),
        halt_flag_active=False,
        day_halted=False,
        regime_multiplier=1.0,
        avoid_symbols=set(),
    )
    base.update(overrides)
    return RiskContext(**base)


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager(RiskLimits(
        max_position_pct=0.05, max_open_positions=4, stop_loss_pct=0.02,
        take_profit_pct=0.04, daily_loss_cap_pct=0.02, min_crypto_notional=10.0,
    ))


# ---------------------------------------------------------------------------
# MODE safety: it must be impossible to trade live by accident
# ---------------------------------------------------------------------------

class TestModeResolution:
    def test_missing_mode_is_paper(self):
        assert resolve_mode(None, None) == "paper"
        assert resolve_mode(None, LIVE_CONFIRM_PHRASE) == "paper"

    def test_blank_mode_is_paper(self):
        assert resolve_mode("", LIVE_CONFIRM_PHRASE) == "paper"
        assert resolve_mode("   ", LIVE_CONFIRM_PHRASE) == "paper"

    @pytest.mark.parametrize("garbage", [
        "prod", "production", "LIVE!", "live-trading", "true", "1", "yes",
        "l ive", "livee", "\x00live", "paper", "PAPER",
    ])
    def test_malformed_mode_is_paper(self, garbage):
        assert resolve_mode(garbage, LIVE_CONFIRM_PHRASE) == "paper"

    def test_live_without_confirmation_is_paper(self):
        assert resolve_mode("live", None) == "paper"
        assert resolve_mode("live", "") == "paper"
        assert resolve_mode("live", "yes") == "paper"
        assert resolve_mode("live", "yes_i_understand_real_money") == "paper"

    def test_live_requires_exact_double_opt_in(self):
        assert resolve_mode("live", LIVE_CONFIRM_PHRASE) == "live"
        assert resolve_mode(" LIVE ", LIVE_CONFIRM_PHRASE) == "live"

    def test_non_string_types_are_paper(self):
        assert resolve_mode(1, LIVE_CONFIRM_PHRASE) == "paper"  # type: ignore


# ---------------------------------------------------------------------------
# Daily loss kill switch
# ---------------------------------------------------------------------------

class TestDailyLossCap:
    def test_not_breached_when_flat(self):
        assert not daily_loss_breached(100_000, 100_000, 0.02)

    def test_not_breached_just_above_cap(self):
        assert not daily_loss_breached(100_000, 98_000.01, 0.02)

    def test_breached_exactly_at_cap(self):
        assert daily_loss_breached(100_000, 98_000, 0.02)

    def test_breached_below_cap(self):
        assert daily_loss_breached(100_000, 97_000, 0.02)

    def test_gains_never_breach(self):
        assert not daily_loss_breached(100_000, 105_000, 0.02)

    def test_negative_cap_sign_is_ignored(self):
        # someone writes -0.02 in config: still trips at -2%
        assert daily_loss_breached(100_000, 97_000, -0.02)

    @pytest.mark.parametrize("bad", [0.0, -5.0, math.nan, math.inf])
    def test_bad_day_start_fails_closed(self, bad):
        assert daily_loss_breached(bad, 100_000, 0.02)

    @pytest.mark.parametrize("bad", [0.0, -5.0, math.nan, math.inf])
    def test_bad_current_equity_fails_closed(self, bad):
        assert daily_loss_breached(100_000, bad, 0.02)

    def test_entry_blocked_when_day_halted_flag_set(self, rm):
        d = rm.evaluate_entry("SPY", 500.0, make_ctx(day_halted=True), False)
        assert not d.allowed and "daily loss cap" in d.reason

    def test_entry_blocked_when_equity_below_cap(self, rm):
        ctx = make_ctx(equity=97_500.0, day_start_equity=100_000.0)
        d = rm.evaluate_entry("SPY", 500.0, ctx, False)
        assert not d.allowed and "daily loss" in d.reason


# ---------------------------------------------------------------------------
# Halt flag
# ---------------------------------------------------------------------------

class TestHaltFlag:
    def test_halt_flag_blocks_everything(self, rm):
        d = rm.evaluate_entry("SPY", 500.0, make_ctx(halt_flag_active=True), False)
        assert not d.allowed and "halt.flag" in d.reason

    def test_halt_beats_all_other_checks(self, rm):
        # perfect conditions otherwise — halt still wins
        ctx = make_ctx(halt_flag_active=True, equity=1_000_000.0)
        assert not rm.evaluate_entry("SPY", 500.0, ctx, False).allowed


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

class TestPositionSizing:
    def test_stock_size_capped_at_max_pct(self, rm):
        qty, notional = rm.position_size(100_000, 500.0, 1.0, is_crypto=False)
        assert qty == 10  # floor(5000/500)
        assert notional <= 100_000 * 0.05

    def test_stock_whole_shares_floored(self, rm):
        qty, _ = rm.position_size(100_000, 499.0, 1.0, is_crypto=False)
        assert qty == math.floor(5000 / 499.0)
        assert qty == int(qty)

    def test_stock_too_expensive_returns_zero(self, rm):
        # 5% of 1000 = 50 < one share at 500
        qty, notional = rm.position_size(1_000, 500.0, 1.0, is_crypto=False)
        assert qty == 0 and notional == 0

    def test_crypto_fractional(self, rm):
        qty, notional = rm.position_size(100_000, 60_000.0, 1.0, is_crypto=True)
        assert 0 < qty < 1
        assert abs(notional - 5000) < 60_000 * 1e-6 * 2

    def test_crypto_below_min_notional_returns_zero(self, rm):
        qty, _ = rm.position_size(100.0, 60_000.0, 1.0, is_crypto=True)
        assert qty == 0  # 5% of 100 = $5 < $10 min

    def test_regime_multiplier_halves_size(self, rm):
        full, _ = rm.position_size(100_000, 100.0, 1.0, False)
        half, _ = rm.position_size(100_000, 100.0, 0.5, False)
        assert half == full / 2

    def test_regime_multiplier_cannot_amplify(self, rm):
        boosted, _ = rm.position_size(100_000, 100.0, 5.0, False)
        normal, _ = rm.position_size(100_000, 100.0, 1.0, False)
        assert boosted == normal

    def test_zero_multiplier_zero_size(self, rm):
        qty, _ = rm.position_size(100_000, 100.0, 0.0, False)
        assert qty == 0

    @pytest.mark.parametrize("equity,price", [
        (0, 100), (-5, 100), (math.nan, 100), (100_000, 0),
        (100_000, -1), (100_000, math.nan), (math.inf, 100),
    ])
    def test_bad_inputs_size_zero(self, rm, equity, price):
        qty, notional = rm.position_size(equity, price, 1.0, False)
        assert qty == 0 and notional == 0


# ---------------------------------------------------------------------------
# Protective prices
# ---------------------------------------------------------------------------

class TestProtectivePrices:
    def test_stop_and_tp_bracket_the_entry(self, rm):
        stop, tp = rm.protective_prices(100.0)
        assert stop == 98.0 and tp == 104.0

    def test_stop_below_tp_above(self, rm):
        for price in (0.5, 3.17, 99.99, 60_123.45):
            stop, tp = rm.protective_prices(price)
            assert stop < price < tp


# ---------------------------------------------------------------------------
# The full entry gate
# ---------------------------------------------------------------------------

class TestEvaluateEntry:
    def test_happy_path(self, rm):
        d = rm.evaluate_entry("SPY", 500.0, make_ctx(), False)
        assert d.allowed
        assert d.qty == 10
        assert d.stop_price == 490.0
        assert d.tp_price == 520.0

    def test_max_open_positions_blocks(self, rm):
        ctx = make_ctx(open_position_count=4,
                       open_symbols={"A", "B", "C", "D"})
        d = rm.evaluate_entry("SPY", 500.0, ctx, False)
        assert not d.allowed and "max open positions" in d.reason

    def test_below_max_positions_allows(self, rm):
        ctx = make_ctx(open_position_count=3, open_symbols={"A", "B", "C"})
        assert rm.evaluate_entry("SPY", 500.0, ctx, False).allowed

    def test_duplicate_position_blocked(self, rm):
        ctx = make_ctx(open_position_count=1, open_symbols={"SPY"})
        d = rm.evaluate_entry("SPY", 500.0, ctx, False)
        assert not d.allowed and "already holding" in d.reason

    def test_avoid_list_blocks(self, rm):
        ctx = make_ctx(avoid_symbols={"NVDA"})
        d = rm.evaluate_entry("NVDA", 800.0, ctx, False)
        assert not d.allowed and "avoid list" in d.reason

    def test_avoid_list_only_blocks_listed_symbol(self, rm):
        ctx = make_ctx(avoid_symbols={"NVDA"})
        assert rm.evaluate_entry("SPY", 500.0, ctx, False).allowed

    def test_zero_regime_blocks(self, rm):
        d = rm.evaluate_entry("SPY", 500.0, make_ctx(regime_multiplier=0.0), False)
        assert not d.allowed and "regime" in d.reason

    def test_bad_price_blocks(self, rm):
        for bad in (0.0, -10.0, math.nan, math.inf):
            assert not rm.evaluate_entry("SPY", bad, make_ctx(), False).allowed

    def test_crypto_happy_path_fractional(self, rm):
        d = rm.evaluate_entry("BTC-USD", 60_000.0, make_ctx(), True)
        assert d.allowed and 0 < d.qty < 1
        assert d.stop_price < 60_000 < d.tp_price

    def test_check_order_halt_first(self, rm):
        """halt.flag must short-circuit before anything else is consulted."""
        ctx = make_ctx(halt_flag_active=True, day_halted=True,
                       regime_multiplier=0.0, avoid_symbols={"SPY"})
        d = rm.evaluate_entry("SPY", 500.0, ctx, False)
        assert "halt.flag" in d.reason


# ---------------------------------------------------------------------------
# Watchdog-side breach math
# ---------------------------------------------------------------------------

class TestWatchdogChecks:
    def test_position_within_limit_ok(self, rm):
        # 5% cap, 1.25 tolerance -> 6.25% allowed
        assert not rm.position_breaches_limit(6_000, 100_000, 1.25)

    def test_position_over_tolerance_breaches(self, rm):
        assert rm.position_breaches_limit(6_300, 100_000, 1.25)

    def test_short_position_absolute_value(self, rm):
        assert rm.position_breaches_limit(-10_000, 100_000, 1.25)

    @pytest.mark.parametrize("bad_equity", [0.0, -1.0, math.nan])
    def test_bad_equity_fails_closed(self, rm, bad_equity):
        assert rm.position_breaches_limit(1.0, bad_equity, 1.25)


# ---------------------------------------------------------------------------
# Config sanity — catches fat-fingered config edits at test time
# ---------------------------------------------------------------------------

class TestConfigSanity:
    def test_limits_are_sane(self):
        assert 0 < config.MAX_POSITION_PCT <= 0.25
        assert 1 <= config.MAX_OPEN_POSITIONS <= 20
        assert 0 < config.STOP_LOSS_PCT < config.TAKE_PROFIT_PCT
        assert 0 < config.DAILY_LOSS_CAP_PCT <= 0.10

    def test_default_limits_match_config(self):
        limits = RiskLimits()
        assert limits.max_position_pct == config.MAX_POSITION_PCT
        assert limits.daily_loss_cap_pct == config.DAILY_LOSS_CAP_PCT
