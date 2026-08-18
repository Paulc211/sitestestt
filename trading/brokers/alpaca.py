"""Alpaca adapter (REST, no SDK — keeps memory small and behavior explicit).

Stocks:  bracket orders => stop-loss AND take-profit live broker-side.
Crypto:  Alpaca does not support bracket/OCO on crypto, so the entry is
         followed immediately by a broker-side STOP-LIMIT sell (the stop
         survives our server dying). Take-profit for crypto is enforced in
         software by the trading agent each cycle.

Data: free tier = IEX feed for stocks (real-time IEX, 15-min delayed SIP).
We only ever use CLOSED bars and never assume SIP-quality fills.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config import ALPACA_STOCK_FEED, CRYPTO_SYMBOLS
from core.http import ApiError, retry_request
from core.settings import ALPACA_DATA_HOST, alpaca_keys, alpaca_trading_host

from .base import Broker

log = logging.getLogger("alpaca")


def is_crypto(symbol: str) -> bool:
    return symbol in CRYPTO_SYMBOLS or "-USD" in symbol or "/" in symbol


def to_alpaca(symbol: str) -> str:
    """BTC-USD -> BTC/USD ; stocks unchanged."""
    return symbol.replace("-", "/") if is_crypto(symbol) else symbol


def from_alpaca(symbol: str) -> str:
    """BTCUSD or BTC/USD -> BTC-USD ; stocks unchanged."""
    if "/" in symbol:
        return symbol.replace("/", "-")
    for c in CRYPTO_SYMBOLS:
        if symbol == c.replace("-", ""):
            return c
    return symbol


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self) -> None:
        key, secret = alpaca_keys()
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }
        self._trading = alpaca_trading_host()
        self._data = ALPACA_DATA_HOST

    # -- plumbing -------------------------------------------------------

    def _get(self, host: str, path: str, params: dict | None = None) -> Any:
        r = retry_request("GET", f"{host}{path}", headers=self._headers,
                          params=params)
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        r = retry_request("POST", f"{self._trading}{path}",
                          headers=self._headers, json_body=body)
        return r.json()

    def _delete(self, path: str, params: dict | None = None) -> Any:
        r = retry_request("DELETE", f"{self._trading}{path}",
                          headers=self._headers, params=params)
        return r.json() if r.text else None

    # -- account / positions -------------------------------------------

    def get_account(self) -> dict[str, Any]:
        a = self._get(self._trading, "/v2/account")
        return {"equity": float(a["equity"]), "cash": float(a["cash"])}

    def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for p in self._get(self._trading, "/v2/positions"):
            out.append({
                "symbol": from_alpaca(p["symbol"]),
                "qty": float(p["qty"]),
                "entry_price": float(p["avg_entry_price"]),
                "current_price": float(p.get("current_price") or 0),
                "market_value": float(p.get("market_value") or 0),
                "unrealized_pl": float(p.get("unrealized_pl") or 0),
            })
        return out

    def get_open_orders(self) -> list[dict[str, Any]]:
        orders = self._get(self._trading, "/v2/orders",
                           {"status": "open", "limit": 200, "nested": "true"})
        return [{
            "id": o["id"],
            "symbol": from_alpaca(o["symbol"]),
            "side": o["side"],
            "type": o["type"],
            "qty": float(o.get("qty") or 0),
        } for o in orders]

    # -- data -----------------------------------------------------------

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        # generous start window: limit 15-min bars only land during market
        # hours for stocks, so reach back far enough across weekends
        start = end - timedelta(minutes=15 * limit * 6 + 3 * 1440)
        if is_crypto(symbol):
            data = self._get(
                self._data, "/v1beta3/crypto/us/bars",
                {"symbols": to_alpaca(symbol), "timeframe": timeframe,
                 "start": start.isoformat(), "limit": limit * 2, "sort": "desc"},
            )
            raw = data.get("bars", {}).get(to_alpaca(symbol), [])
        else:
            data = self._get(
                self._data, f"/v2/stocks/{symbol}/bars",
                {"timeframe": timeframe, "start": start.isoformat(),
                 "limit": limit * 2, "feed": ALPACA_STOCK_FEED, "sort": "desc",
                 "adjustment": "split"},
            )
            raw = data.get("bars") or []
        bars = [{
            "t": datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp(),
            "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
            "c": float(b["c"]), "v": float(b["v"]),
        } for b in raw]
        bars.sort(key=lambda b: b["t"])
        # drop the still-forming bar: a 15Min bar starting < 15 min ago is open
        cutoff = end.timestamp() - 15 * 60
        bars = [b for b in bars if b["t"] <= cutoff]
        return bars[-limit:]

    # -- orders ---------------------------------------------------------

    def place_protected_buy(self, symbol: str, qty: float, stop_price: float,
                            tp_price: float) -> dict[str, Any]:
        if is_crypto(symbol):
            return self._crypto_protected_buy(symbol, qty, stop_price)
        body = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "take_profit": {"limit_price": f"{tp_price:.2f}"},
        }
        o = self._post("/v2/orders", body)
        log.info("Bracket buy %s x%s (stop %.2f / tp %.2f) id=%s",
                 symbol, qty, stop_price, tp_price, o["id"])
        return o

    def _crypto_protected_buy(self, symbol: str, qty: float,
                              stop_price: float) -> dict[str, Any]:
        a_sym = to_alpaca(symbol)
        entry = self._post("/v2/orders", {
            "symbol": a_sym, "qty": f"{qty:.6f}", "side": "buy",
            "type": "market", "time_in_force": "gtc",
        })
        # broker-side stop so it fires even if this server is dead.
        # IMPORTANT: Alpaca takes its crypto fee out of the base asset, so the
        # filled quantity is slightly LESS than requested — size the stop from
        # the actual sellable balance, never the requested qty.
        try:
            stop_qty = self._crypto_sellable_qty(symbol, qty)
            if stop_qty <= 0:
                raise ApiError(0, "no sellable quantity after entry fill",
                               "alpaca:crypto-stop")
            stop = self._post("/v2/orders", {
                "symbol": a_sym, "qty": f"{stop_qty:.9f}", "side": "sell",
                "type": "stop_limit", "time_in_force": "gtc",
                "stop_price": f"{stop_price:.2f}",
                # limit slightly through the stop so it actually fills
                "limit_price": f"{stop_price * 0.995:.2f}",
            })
            entry["protective_stop_id"] = stop["id"]
        except ApiError:
            # entry filled but stop rejected -> unprotected position. Bail out.
            log.exception("Protective stop REJECTED for %s — closing entry", symbol)
            self.close_position(symbol)
            raise
        log.info("Crypto buy %s x%.6f with broker-side stop %.2f on %.9f",
                 symbol, qty, stop_price, stop_qty)
        return entry

    def _crypto_sellable_qty(self, symbol: str, requested: float) -> float:
        """Actual quantity we can attach a sell order to, post-fees. Polls the
        position briefly because market-buy settlement isn't instantaneous."""
        pos_sym = to_alpaca(symbol).replace("/", "")
        avail = 0.0
        for _ in range(6):
            try:
                p = self._get(self._trading, f"/v2/positions/{pos_sym}")
                avail = float(p.get("qty_available") or p.get("qty") or 0)
            except ApiError as e:
                if e.status != 404:
                    raise
            if avail > 0:
                break
            time.sleep(1)
        # round DOWN so we never ask for a hair more than we hold
        qty = math.floor(min(avail, requested) * 1e9) / 1e9
        return qty

    def close_position(self, symbol: str) -> dict[str, Any] | None:
        # cancel this symbol's working orders first (bracket children / stops)
        for o in self.get_open_orders():
            if o["symbol"] == symbol:
                try:
                    self._delete(f"/v2/orders/{o['id']}")
                except ApiError as e:
                    if e.status != 404:
                        raise
        try:
            r = self._delete(f"/v2/positions/{to_alpaca(symbol).replace('/', '')}"
                             if is_crypto(symbol) else f"/v2/positions/{symbol}")
            log.info("Closed position %s", symbol)
            return r
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def cancel_all_orders(self) -> int:
        r = self._delete("/v2/orders")
        n = len(r) if isinstance(r, list) else 0
        log.info("Cancelled %d open orders", n)
        return n

    def close_all_positions(self) -> int:
        r = self._delete("/v2/positions", {"cancel_orders": "true"})
        n = len(r) if isinstance(r, list) else 0
        log.warning("FLATTENED %d positions", n)
        return n

    def market_open(self, symbol: str) -> bool:
        if is_crypto(symbol):
            return True
        clock = self._get(self._trading, "/v2/clock")
        return bool(clock.get("is_open"))
