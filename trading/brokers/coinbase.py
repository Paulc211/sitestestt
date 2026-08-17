"""Coinbase Advanced Trade adapter — LIVE crypto only (Coinbase has no paper
environment; in paper mode crypto routes to Alpaca's paper venue instead).

Auth: CDP API keys, ES256 JWT per request (PyJWT + cryptography).
Protection: entry is a market IOC buy immediately followed by a broker-side
STOP-LIMIT sell (trigger bracket where available), so the stop fires even if
our server dies.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import jwt

from core.http import ApiError, retry_request
from core.settings import coinbase_keys

from .base import Broker

log = logging.getLogger("coinbase")

HOST = "api.coinbase.com"
BASE = f"https://{HOST}"

GRANULARITY = {"15Min": ("FIFTEEN_MINUTE", 900)}


def to_product(symbol: str) -> str:
    return symbol  # config already uses Coinbase product ids (BTC-USD)


class CoinbaseBroker(Broker):
    name = "coinbase"

    def __init__(self) -> None:
        self._key_name, self._pem = coinbase_keys()

    def _jwt(self, method: str, path: str) -> str:
        uri = f"{method} {HOST}{path}"
        now = int(time.time())
        return jwt.encode(
            {"sub": self._key_name, "iss": "cdp", "nbf": now, "exp": now + 110,
             "uri": uri},
            self._pem,
            algorithm="ES256",
            headers={"kid": self._key_name, "nonce": secrets.token_hex(16)},
        )

    def _req(self, method: str, path: str, params: dict | None = None,
             body: dict | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._jwt(method, path)}",
            "Content-Type": "application/json",
        }
        r = retry_request(method, f"{BASE}{path}", headers=headers,
                          params=params, json_body=body)
        return r.json() if r.text else None

    # -- account / positions -------------------------------------------

    def get_account(self) -> dict[str, Any]:
        """Equity = USD cash + market value of crypto balances."""
        accounts = self._all_accounts()
        cash = 0.0
        equity = 0.0
        for a in accounts:
            avail = float(a["available_balance"]["value"]) + \
                float(a.get("hold", {}).get("value") or 0)
            cur = a["currency"]
            if cur in ("USD", "USDC"):
                cash += avail
                equity += avail
            elif avail > 0:
                price = self._spot_price(f"{cur}-USD")
                if price:
                    equity += avail * price
        return {"equity": equity, "cash": cash}

    def _all_accounts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            data = self._req("GET", "/api/v3/brokerage/accounts", params)
            out.extend(data.get("accounts", []))
            if not data.get("has_next"):
                return out
            cursor = data.get("cursor")

    def _spot_price(self, product: str) -> float | None:
        try:
            d = self._req("GET", f"/api/v3/brokerage/products/{product}")
            return float(d["price"])
        except (ApiError, KeyError, ValueError):
            return None

    def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for a in self._all_accounts():
            cur = a["currency"]
            if cur in ("USD", "USDC"):
                continue
            qty = float(a["available_balance"]["value"]) + \
                float(a.get("hold", {}).get("value") or 0)
            if qty <= 0:
                continue
            symbol = f"{cur}-USD"
            price = self._spot_price(symbol) or 0.0
            out.append({
                "symbol": symbol, "qty": qty, "entry_price": None,
                "current_price": price, "market_value": qty * price,
                "unrealized_pl": None,
            })
        return out

    def get_open_orders(self) -> list[dict[str, Any]]:
        data = self._req("GET", "/api/v3/brokerage/orders/historical/batch",
                         {"order_status": "OPEN", "limit": 100})
        return [{
            "id": o["order_id"], "symbol": o["product_id"], "side": o["side"].lower(),
            "type": o.get("order_type", "").lower(),
            "qty": float(o.get("order_configuration", {})
                         .get("stop_limit_stop_limit_gtc", {})
                         .get("base_size") or 0),
        } for o in data.get("orders", [])]

    # -- data -----------------------------------------------------------

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        gran, secs = GRANULARITY.get(timeframe, ("FIFTEEN_MINUTE", 900))
        end = int(time.time())
        start = end - secs * (limit + 2)
        d = self._req("GET", f"/api/v3/brokerage/products/{to_product(symbol)}/candles",
                      {"start": str(start), "end": str(end), "granularity": gran})
        bars = [{
            "t": float(c["start"]), "o": float(c["open"]), "h": float(c["high"]),
            "l": float(c["low"]), "c": float(c["close"]), "v": float(c["volume"]),
        } for c in d.get("candles", [])]
        bars.sort(key=lambda b: b["t"])
        cutoff = time.time() - secs
        return [b for b in bars if b["t"] <= cutoff][-limit:]

    # -- orders ---------------------------------------------------------

    def _order(self, body: dict) -> dict[str, Any]:
        r = self._req("POST", "/api/v3/brokerage/orders", body=body)
        if not r.get("success"):
            raise ApiError(400, str(r.get("error_response", r))[:300],
                           "coinbase:/orders")
        return r["success_response"]

    def place_protected_buy(self, symbol: str, qty: float, stop_price: float,
                            tp_price: float) -> dict[str, Any]:
        product = to_product(symbol)
        entry = self._order({
            "client_order_id": secrets.token_hex(12),
            "product_id": product,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {"base_size": f"{qty:.8f}"}
            },
        })
        try:
            stop = self._order({
                "client_order_id": secrets.token_hex(12),
                "product_id": product,
                "side": "SELL",
                "order_configuration": {
                    "stop_limit_stop_limit_gtc": {
                        "base_size": f"{qty:.8f}",
                        "stop_price": f"{stop_price:.2f}",
                        "limit_price": f"{stop_price * 0.995:.2f}",
                        "stop_direction": "STOP_DIRECTION_STOP_DOWN",
                    }
                },
            })
            entry["protective_stop_id"] = stop["order_id"]
        except ApiError:
            log.exception("Protective stop REJECTED for %s — closing entry", symbol)
            self.close_position(symbol)
            raise
        log.info("Coinbase buy %s x%.8f with broker-side stop %.2f",
                 symbol, qty, stop_price)
        return entry

    def close_position(self, symbol: str) -> dict[str, Any] | None:
        product = to_product(symbol)
        open_ids = [o["id"] for o in self.get_open_orders() if o["symbol"] == product]
        if open_ids:
            self._req("POST", "/api/v3/brokerage/orders/batch_cancel",
                      body={"order_ids": open_ids})
        qty = 0.0
        for p in self.get_positions():
            if p["symbol"] == symbol:
                qty = p["qty"]
        if qty <= 0:
            return None
        r = self._order({
            "client_order_id": secrets.token_hex(12),
            "product_id": product,
            "side": "SELL",
            "order_configuration": {
                "market_market_ioc": {"base_size": f"{qty:.8f}"}
            },
        })
        log.info("Closed Coinbase position %s", symbol)
        return r

    def cancel_all_orders(self) -> int:
        ids = [o["id"] for o in self.get_open_orders()]
        if ids:
            self._req("POST", "/api/v3/brokerage/orders/batch_cancel",
                      body={"order_ids": ids})
        log.info("Cancelled %d Coinbase orders", len(ids))
        return len(ids)

    def close_all_positions(self) -> int:
        n = 0
        for p in self.get_positions():
            if self.close_position(p["symbol"]):
                n += 1
        log.warning("FLATTENED %d Coinbase positions", n)
        return n

    def market_open(self, symbol: str) -> bool:
        return True
