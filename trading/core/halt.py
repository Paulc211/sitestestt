"""The halt flag: a file on disk. Any agent (or a human with `touch`) can set
it; the trading agent checks it before EVERY order and refuses to trade while
it exists. File-on-disk beats a DB row here: it works even if SQLite is
corrupted, and a human can clear it over SSH with `rm`.
"""
from __future__ import annotations

import time
from pathlib import Path

from config import HALT_FLAG_PATH
from core.settings import resolve_path


def _flag_path() -> Path:
    return resolve_path(HALT_FLAG_PATH)


def halt_active() -> bool:
    return _flag_path().exists()


def halt_reason() -> str | None:
    p = _flag_path()
    if not p.exists():
        return None
    try:
        return p.read_text(errors="replace").strip() or "(no reason recorded)"
    except OSError:
        return "(unreadable halt.flag)"


def set_halt(reason: str, source: str) -> None:
    p = _flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    # append so multiple trips are all recorded
    with p.open("a") as f:
        f.write(f"[{stamp}] {source}: {reason}\n")


def clear_halt() -> bool:
    p = _flag_path()
    if p.exists():
        p.unlink()
        return True
    return False
