"""Broker interface. Both adapters normalize to these shapes:

bar:      {"t": epoch_sec, "o": float, "h": float, "l": float, "c": float, "v": float}
position: {"symbol": canonical, "qty": float, "entry_price": float,
           "current_price": float, "market_value": float, "unrealized_pl": float}
order:    {"id": str, "symbol": canonical, "side": str, "type": str, "qty": float}

'canonical' symbols are the config spellings: "NVDA", "BTC-USD".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Broker(ABC):
    name: str = "broker"

    @abstractmethod
    def get_account(self) -> dict[str, Any]:
        """{"equity": float, "cash": float}"""

    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_open_orders(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        """Closed bars only, oldest first."""

    @abstractmethod
    def place_protected_buy(self, symbol: str, qty: float, stop_price: float,
                            tp_price: float) -> dict[str, Any]:
        """Market/near-market entry with a BROKER-SIDE stop attached, plus a
        broker-side take-profit where the venue supports OCO. Returns the
        entry order dict."""

    @abstractmethod
    def close_position(self, symbol: str) -> dict[str, Any] | None:
        """Cancel the symbol's open orders, then market-close the position."""

    @abstractmethod
    def cancel_all_orders(self) -> int: ...

    @abstractmethod
    def close_all_positions(self) -> int: ...

    @abstractmethod
    def market_open(self, symbol: str) -> bool:
        """Is this symbol tradable right now (market hours for stocks)."""
