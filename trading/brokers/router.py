"""Symbol -> broker routing.

paper mode: EVERYTHING (stocks + crypto) trades on Alpaca's paper venue.
live mode:  stocks on Alpaca live; crypto on Coinbase if CDP keys are
            configured, otherwise Alpaca live crypto.
"""
from __future__ import annotations

from functools import lru_cache

from core.settings import coinbase_configured, is_paper

from .alpaca import AlpacaBroker, is_crypto
from .base import Broker
from .coinbase import CoinbaseBroker


@lru_cache(maxsize=1)
def _alpaca() -> AlpacaBroker:
    return AlpacaBroker()


@lru_cache(maxsize=1)
def _coinbase() -> CoinbaseBroker:
    return CoinbaseBroker()


def broker_for(symbol: str) -> Broker:
    if is_crypto(symbol) and not is_paper() and coinbase_configured():
        return _coinbase()
    return _alpaca()


def all_brokers() -> list[Broker]:
    """Every broker that can hold state in the current mode (watchdog sweeps
    all of them)."""
    brokers: list[Broker] = [_alpaca()]
    if not is_paper() and coinbase_configured():
        brokers.append(_coinbase())
    return brokers


def equity_broker() -> Broker:
    """The broker whose account equity anchors risk math (Alpaca — it holds
    the stock account; Coinbase equity is added on top by the watchdog)."""
    return _alpaca()
