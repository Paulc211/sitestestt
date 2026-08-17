"""Environment / secrets loading with fail-safe mode resolution.

THE PRIME DIRECTIVE: it must be impossible to trade live by accident.
MODE resolves to "paper" unless BOTH of these hold:
  * MODE is exactly "live" (case-insensitive, whitespace-stripped)
  * LIVE_TRADING_CONFIRM is exactly "YES_I_UNDERSTAND_REAL_MONEY"
Anything missing, blank, misspelled, or malformed -> paper.

Secrets are only ever read through this module and are never logged.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# load .env sitting next to the project root; real env vars win
load_dotenv(PROJECT_ROOT / ".env", override=False)

LIVE_CONFIRM_PHRASE = "YES_I_UNDERSTAND_REAL_MONEY"


def resolve_mode(raw_mode: str | None, raw_confirm: str | None) -> str:
    """Pure function so the safety rule is unit-testable.

    Returns "live" only for an exact, confirmed opt-in. Everything else,
    including None, "", "Live ", "LIVE!", "prod", garbage bytes -> "paper".
    """
    if raw_mode is None or raw_confirm is None:
        return "paper"
    try:
        mode = raw_mode.strip().lower()
        confirm = raw_confirm.strip()
    except AttributeError:
        return "paper"
    if mode == "live" and confirm == LIVE_CONFIRM_PHRASE:
        return "live"
    return "paper"


def get_mode() -> str:
    return resolve_mode(os.getenv("MODE"), os.getenv("LIVE_TRADING_CONFIRM"))


def is_paper() -> bool:
    return get_mode() == "paper"


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required env var {name} — check your .env "
            f"(see .env.example). Value is never logged."
        )
    return val


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# --- Alpaca ----------------------------------------------------------------

def alpaca_keys() -> tuple[str, str]:
    """(key_id, secret). Paper and live use different keys."""
    if is_paper():
        return _require("ALPACA_PAPER_KEY_ID"), _require("ALPACA_PAPER_SECRET")
    return _require("ALPACA_LIVE_KEY_ID"), _require("ALPACA_LIVE_SECRET")


def alpaca_trading_host() -> str:
    if is_paper():
        return "https://paper-api.alpaca.markets"
    return "https://api.alpaca.markets"


ALPACA_DATA_HOST = "https://data.alpaca.markets"


# --- Coinbase (live crypto only) -------------------------------------------

def coinbase_configured() -> bool:
    return bool(_optional("COINBASE_API_KEY_NAME") and _optional("COINBASE_API_PRIVATE_KEY"))


def coinbase_keys() -> tuple[str, str]:
    """(key_name, ec_private_key_pem). CDP keys ship the PEM with literal \\n."""
    key_name = _require("COINBASE_API_KEY_NAME")
    pem = _require("COINBASE_API_PRIVATE_KEY").replace("\\n", "\n")
    return key_name, pem


# --- Telegram ---------------------------------------------------------------

def telegram_configured() -> bool:
    return bool(_optional("TELEGRAM_BOT_TOKEN") and _optional("TELEGRAM_CHAT_ID"))


def telegram_creds() -> tuple[str, str]:
    return _require("TELEGRAM_BOT_TOKEN"), _require("TELEGRAM_CHAT_ID")


# --- Anthropic --------------------------------------------------------------

def anthropic_api_key() -> str:
    return _require("ANTHROPIC_API_KEY")


def anthropic_model() -> str:
    from config import ANTHROPIC_MODEL_DEFAULT
    return _optional("ANTHROPIC_MODEL", ANTHROPIC_MODEL_DEFAULT)


# --- Dashboard --------------------------------------------------------------

def dashboard_creds() -> tuple[str, str]:
    return _require("DASHBOARD_USER"), _require("DASHBOARD_PASSWORD")


# --- Paths ------------------------------------------------------------------

def resolve_path(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else PROJECT_ROOT / p
