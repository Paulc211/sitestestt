"""SQLite shared state for all agents.

WAL mode so five processes can read while one writes. Every write is a short
transaction; connections are opened per-call (cheap on a 1-vCPU box, and it
sidesteps cross-process locking bugs).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from config import DB_PATH
from core.settings import resolve_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    agent  TEXT PRIMARY KEY,
    ts     REAL NOT NULL,
    pid    INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS heartbeat_history (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    pid   INTEGER NOT NULL,
    ts    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    agent   TEXT NOT NULL,
    symbol  TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_ts ON agent_logs (ts);
CREATE TABLE IF NOT EXISTS trades (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    symbol   TEXT NOT NULL,
    side     TEXT NOT NULL,
    qty      REAL NOT NULL,
    price    REAL,
    order_id TEXT,
    reason   TEXT,
    mode     TEXT NOT NULL,
    pnl      REAL
);
CREATE TABLE IF NOT EXISTS equity_history (
    ts     REAL NOT NULL,
    equity REAL NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_history (ts);
CREATE TABLE IF NOT EXISTS positions_snapshot (
    symbol        TEXT PRIMARY KEY,
    qty           REAL NOT NULL,
    entry_price   REAL,
    current_price REAL,
    market_value  REAL,
    unrealized_pl REAL,
    ts            REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS position_targets (
    symbol     TEXT PRIMARY KEY,
    stop_price REAL NOT NULL,
    tp_price   REAL NOT NULL,
    qty        REAL NOT NULL,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS avoid_list (
    symbol     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    expires_ts REAL NOT NULL,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS news_seen (
    news_id TEXT PRIMARY KEY,
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT NOT NULL,
    created_ts    REAL NOT NULL,
    lookback_days INTEGER,
    trades        INTEGER,
    win_rate      REAL,
    expectancy    REAL,
    max_drawdown  REAL,
    total_return  REAL,
    notes         TEXT
);
"""


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    path = resolve_path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(_SCHEMA)


# --- kv ---------------------------------------------------------------------

def kv_set(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO kv (key, value, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, str(value), time.time()),
        )


def kv_get(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# --- heartbeats -------------------------------------------------------------

def heartbeat(agent: str, pid: int, status: str = "ok") -> None:
    now = time.time()
    with conn() as c:
        prev = c.execute(
            "SELECT pid FROM heartbeats WHERE agent = ?", (agent,)
        ).fetchone()
        c.execute(
            "INSERT INTO heartbeats (agent, ts, pid, status) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts, pid=excluded.pid, "
            "status=excluded.status",
            (agent, now, pid, status),
        )
        if prev is None or prev["pid"] != pid:
            c.execute(
                "INSERT INTO heartbeat_history (agent, pid, ts) VALUES (?, ?, ?)",
                (agent, pid, now),
            )


def get_heartbeats() -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute("SELECT * FROM heartbeats ORDER BY agent").fetchall()
    return [dict(r) for r in rows]


def recent_pids(agent: str, window_sec: float) -> list[int]:
    cutoff = time.time() - window_sec
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT pid FROM heartbeat_history WHERE agent = ? AND ts > ?",
            (agent, cutoff),
        ).fetchall()
    return [r["pid"] for r in rows]


# --- reasoning log ----------------------------------------------------------

def log_reasoning(agent: str, message: str, symbol: str | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO agent_logs (ts, agent, symbol, message) VALUES (?, ?, ?, ?)",
            (time.time(), agent, symbol, message),
        )
        # keep the table bounded on a 1GB box
        c.execute(
            "DELETE FROM agent_logs WHERE id < "
            "(SELECT COALESCE(MAX(id), 0) - 20000 FROM agent_logs)"
        )


def get_logs(after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM agent_logs WHERE id > ? ORDER BY id DESC LIMIT ?",
            (after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows][::-1]


# --- trades / equity / positions -------------------------------------------

def record_trade(symbol: str, side: str, qty: float, price: float | None,
                 order_id: str | None, reason: str, mode: str,
                 pnl: float | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, order_id, reason, mode, pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), symbol, side, qty, price, order_id, reason, mode, pnl),
        )


def get_trades(limit: int = 50, since_ts: float = 0) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE ts > ? ORDER BY ts DESC LIMIT ?",
            (since_ts, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def record_equity(equity: float, source: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO equity_history (ts, equity, source) VALUES (?, ?, ?)",
            (time.time(), equity, source),
        )
        c.execute(
            "DELETE FROM equity_history WHERE ts < ?", (time.time() - 90 * 86400,)
        )


def get_equity_curve(points: int = 500) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT ts, equity FROM equity_history ORDER BY ts DESC LIMIT ?",
            (points,),
        ).fetchall()
    return [dict(r) for r in rows][::-1]


def snapshot_positions(positions: list[dict[str, Any]]) -> None:
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM positions_snapshot")
        for p in positions:
            c.execute(
                "INSERT INTO positions_snapshot "
                "(symbol, qty, entry_price, current_price, market_value, unrealized_pl, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (p["symbol"], p["qty"], p.get("entry_price"),
                 p.get("current_price"), p.get("market_value"),
                 p.get("unrealized_pl"), now),
            )


def get_positions_snapshot() -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute("SELECT * FROM positions_snapshot ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


# --- position targets (software TP for crypto) ------------------------------

def set_position_targets(symbol: str, stop: float, tp: float, qty: float) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO position_targets (symbol, stop_price, tp_price, qty, ts) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
            "stop_price=excluded.stop_price, tp_price=excluded.tp_price, "
            "qty=excluded.qty, ts=excluded.ts",
            (symbol, stop, tp, qty, time.time()),
        )


def get_position_targets(symbol: str) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM position_targets WHERE symbol = ?", (symbol,)
        ).fetchone()
    return dict(row) if row else None


def clear_position_targets(symbol: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM position_targets WHERE symbol = ?", (symbol,))


# --- avoid list -------------------------------------------------------------

def set_avoid(symbol: str, reason: str, expires_ts: float) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO avoid_list (symbol, reason, expires_ts, ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
            "reason=excluded.reason, expires_ts=excluded.expires_ts, ts=excluded.ts",
            (symbol, reason, expires_ts, time.time()),
        )


def get_avoid_list(active_only: bool = True) -> list[dict[str, Any]]:
    with conn() as c:
        if active_only:
            rows = c.execute(
                "SELECT * FROM avoid_list WHERE expires_ts > ?", (time.time(),)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM avoid_list").fetchall()
    return [dict(r) for r in rows]


def avoided_symbols() -> set[str]:
    return {r["symbol"] for r in get_avoid_list(active_only=True)}


def news_already_seen(news_id: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM news_seen WHERE news_id = ?", (news_id,)
        ).fetchone()
        if row:
            return True
        c.execute(
            "INSERT INTO news_seen (news_id, ts) VALUES (?, ?)",
            (news_id, time.time()),
        )
        c.execute("DELETE FROM news_seen WHERE ts < ?", (time.time() - 7 * 86400,))
    return False


# --- backtest stats ---------------------------------------------------------

def save_backtest_stats(strategy: str, lookback_days: int, trades: int,
                        win_rate: float, expectancy: float, max_drawdown: float,
                        total_return: float, notes: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO backtest_stats (strategy, created_ts, lookback_days, trades, "
            "win_rate, expectancy, max_drawdown, total_return, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy, time.time(), lookback_days, trades, win_rate, expectancy,
             max_drawdown, total_return, notes),
        )


def latest_backtest_stats(strategy: str) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM backtest_stats WHERE strategy = ? "
            "ORDER BY created_ts DESC LIMIT 1",
            (strategy,),
        ).fetchone()
    return dict(row) if row else None
