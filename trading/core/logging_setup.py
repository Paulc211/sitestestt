"""Structured logging: rotating file per agent + console. Never logs secrets —
nothing in this codebase passes keys into log calls, and the formatter is
plain (no env dumping).
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import LOG_DIR
from core.settings import resolve_path

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(agent_name: str, level: int = logging.INFO) -> logging.Logger:
    log_dir = resolve_path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    # avoid duplicate handlers when called twice (tests, reload)
    if getattr(root, "_tradebot_configured", None) == agent_name:
        return logging.getLogger(agent_name)
    root.handlers.clear()

    fh = RotatingFileHandler(
        log_dir / f"{agent_name}.log", maxBytes=5_000_000, backupCount=3
    )
    fh.setFormatter(logging.Formatter(_FMT))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(_FMT))
    root.addHandler(fh)
    root.addHandler(ch)
    root._tradebot_configured = agent_name  # type: ignore[attr-defined]

    # third-party noise down
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(agent_name)
