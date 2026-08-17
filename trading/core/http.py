"""HTTP helper: every external call goes through retry_request, which retries
connection errors, 429 and 5xx with exponential backoff + jitter, and NEVER
puts credentials in exception messages or logs.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger("http")

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class ApiError(Exception):
    """Non-retryable API failure. Message contains status + trimmed body only."""

    def __init__(self, status: int, body: str, url_hint: str):
        self.status = status
        super().__init__(f"HTTP {status} from {url_hint}: {body[:300]}")


def retry_request(method: str, url: str, *, headers: dict[str, str] | None = None,
                  params: dict[str, Any] | None = None, json_body: Any = None,
                  timeout: float = 15.0, max_retries: int = 4,
                  base_delay: float = 2.0) -> requests.Response:
    """Returns the successful Response, or raises ApiError / the last
    connection error. url_hint in errors strips the query string so signed
    params or tokens never leak into logs."""
    url_hint = url.split("?")[0]
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, json=json_body,
                timeout=timeout,
            )
            if resp.status_code < 400:
                return resp
            if resp.status_code in RETRYABLE_STATUS and attempt < max_retries:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else \
                    min(base_delay * (2 ** attempt) + random.uniform(0, 1), 60)
                log.warning("HTTP %s from %s, retry %d/%d in %.1fs",
                            resp.status_code, url_hint, attempt + 1,
                            max_retries, delay)
                time.sleep(delay)
                continue
            raise ApiError(resp.status_code, resp.text, url_hint)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), 60)
                log.warning("Connection error to %s (%s), retry %d/%d in %.1fs",
                            url_hint, type(e).__name__, attempt + 1,
                            max_retries, delay)
                time.sleep(delay)
                continue
            raise
    raise last_exc  # pragma: no cover — loop always returns or raises
