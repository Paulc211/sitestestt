"""News agent: polls Alpaca's news API (Benzinga; covers stocks and crypto),
classifies headlines with Claude, and maintains a "symbols to avoid" list
with expiry timestamps.

This is a PAUSE signal only. It never generates buy/sell signals and no
sentiment score ever reaches the strategy.
"""
from __future__ import annotations

import json
import logging
import os
import time

import anthropic

import config
from core import db
from core.alerts import send_alert
from core.http import retry_request
from core.logging_setup import setup_logging
from core.settings import (ALPACA_DATA_HOST, alpaca_keys, anthropic_api_key,
                           anthropic_model)

AGENT = "news"
log = logging.getLogger(AGENT)

SYSTEM_PROMPT = """You are a risk filter for an automated trading system.
You receive news headlines about specific ticker symbols. Your ONLY job is to
flag symbols that a small automated strategy should PAUSE trading, because a
high-impact event makes short-term price action unpredictable.

High-impact categories: earnings reports/guidance, Fed or major macro
announcements, regulatory action (SEC/DOJ/lawsuits), delistings or halts,
exchange hacks or protocol exploits, bankruptcy, M&A, large token unlocks,
CEO departure, major product recalls.

NOT high-impact: routine analyst ratings, price-movement commentary,
listicles, general market color, minor partnerships.

Respond ONLY with a JSON array, no prose. One entry per symbol that should be
paused (empty array if none):
[{"symbol": "NVDA", "category": "earnings", "avoid_hours": 6,
  "reason": "short plain-english reason"}]
"avoid_hours" is how long to pause (1-24)."""


class NewsAgent:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=anthropic_api_key())
        self.model = anthropic_model()
        key, secret = alpaca_keys()
        self._alpaca_headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }

    def run_forever(self) -> None:
        log.info("News agent starting (model=%s)", self.model)
        db.log_reasoning(AGENT, f"News agent online — classifier {self.model}")
        while True:
            started = time.time()
            try:
                self.cycle()
                db.heartbeat(AGENT, os.getpid(), "ok")
            except Exception as e:
                log.exception("News cycle failed")
                db.heartbeat(AGENT, os.getpid(), f"error: {type(e).__name__}")
                db.log_reasoning(AGENT, f"cycle failed: {type(e).__name__}: {e}")
            time.sleep(max(10.0, config.NEWS_INTERVAL_SEC - (time.time() - started)))

    # ------------------------------------------------------------------

    def cycle(self) -> None:
        headlines = self.fetch_headlines()
        fresh = [h for h in headlines if not db.news_already_seen(h["id"])]
        if not fresh:
            db.log_reasoning(AGENT, f"{len(headlines)} headlines polled, "
                                    f"none new")
            return
        db.log_reasoning(AGENT, f"{len(fresh)} new headlines — classifying")
        flags = self.classify(fresh)
        for f in flags:
            symbol = str(f.get("symbol", "")).upper().replace("/", "-")
            if symbol not in config.ALL_SYMBOLS:
                # Alpaca news uses BTCUSD-style ids for crypto sometimes
                matches = [s for s in config.ALL_SYMBOLS
                           if s.replace("-", "") == symbol]
                if not matches:
                    continue
                symbol = matches[0]
            hours = f.get("avoid_hours", config.NEWS_DEFAULT_AVOID_HOURS)
            try:
                hours = min(max(float(hours), 0.5), 24.0)
            except (TypeError, ValueError):
                hours = config.NEWS_DEFAULT_AVOID_HOURS
            reason = f"{f.get('category', 'event')}: {f.get('reason', '')}"[:200]
            expires = time.time() + hours * 3600
            db.set_avoid(symbol, reason, expires)
            msg = f"⏸️ PAUSE {symbol} for {hours:.0f}h — {reason}"
            db.log_reasoning(AGENT, msg, symbol)
            send_alert(msg, dedup_key=f"avoid:{symbol}")
        if not flags:
            db.log_reasoning(AGENT, "no high-impact events in new headlines")

    def fetch_headlines(self) -> list[dict]:
        # Alpaca news wants crypto as BTCUSD
        symbols = ",".join(
            s.replace("-", "") if s in config.CRYPTO_SYMBOLS else s
            for s in config.ALL_SYMBOLS
        )
        r = retry_request(
            "GET", f"{ALPACA_DATA_HOST}/v1beta1/news",
            headers=self._alpaca_headers,
            params={"symbols": symbols, "limit": config.NEWS_BATCH_LIMIT,
                    "sort": "desc"},
        )
        out = []
        for n in r.json().get("news", []):
            out.append({
                "id": str(n["id"]),
                "headline": n.get("headline", ""),
                "symbols": n.get("symbols", []),
            })
        return out

    def classify(self, headlines: list[dict]) -> list[dict]:
        lines = "\n".join(
            f"- [{', '.join(h['symbols'][:5])}] {h['headline']}"
            for h in headlines
        )
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=config.NEWS_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": lines}],
        )
        if response.stop_reason == "refusal":
            log.warning("Classifier refused; treating batch as no-flags")
            return []
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            start, end = text.find("["), text.rfind("]")
            if start == -1 or end == -1:
                return []
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            log.warning("Classifier returned unparseable JSON; ignoring batch")
            return []


def main() -> None:
    setup_logging(AGENT)
    db.init_db()
    NewsAgent().run_forever()


if __name__ == "__main__":
    main()
