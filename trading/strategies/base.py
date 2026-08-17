"""Strategy contract. A strategy module exposes build() -> Strategy.
Swap strategies by changing config.STRATEGY_MODULE — nothing else moves.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Signal:
    action: str          # "buy" | "sell" | "hold"
    reason: str          # human-readable, goes into the reasoning log verbatim

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Strategy(ABC):
    name: str = "strategy"
    min_bars: int = 30

    @abstractmethod
    def generate(self, symbol: str, bars: list[dict[str, Any]]) -> Signal:
        """bars: closed bars only, oldest first (see brokers.base for shape)."""
