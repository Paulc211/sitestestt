"""Dashboard: FastAPI + single-page HUD frontend.

Security posture: HTTP Basic (constant-time compare) on every route, bound to
127.0.0.1 by default — put nginx/caddy with TLS in front, or reach it through
an SSH tunnel (`ssh -L 8899:127.0.0.1:8899 root@droplet`). Basic auth over
plain HTTP on a public IP is not acceptable with real money behind it.

Run: uvicorn dashboard.app:app --host 127.0.0.1 --port 8899
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
from core import db
from core.alerts import send_alert
from core.halt import clear_halt, halt_active, halt_reason, set_halt
from core.logging_setup import setup_logging
from core.settings import dashboard_creds, get_mode

setup_logging("dashboard")
db.init_db()

app = FastAPI(title="tradebot", docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic()

STATIC_DIR = Path(__file__).parent / "static"


def authed(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    user, password = dashboard_creds()
    ok_user = secrets.compare_digest(credentials.username.encode(), user.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Basic realm=tradebot"},
        )
    return credentials.username


@app.get("/")
def index(_: str = Depends(authed)) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/overview")
def overview(_: str = Depends(authed)) -> JSONResponse:
    now = time.time()
    heartbeats = []
    for h in db.get_heartbeats():
        age = now - h["ts"]
        heartbeats.append({
            **h,
            "age_sec": round(age),
            "alive": age < 3 * _expected_interval(h["agent"]),
        })
    day_key = time.strftime("day_start_equity:%Y-%m-%d")
    return JSONResponse({
        "mode": get_mode(),
        "ts": now,
        "halt": {"active": halt_active(), "reason": halt_reason()},
        "day_halt": db.kv_get(time.strftime("day_halt:%Y-%m-%d")),
        "day_start_equity": db.kv_get(day_key),
        "regime": {
            "label": db.kv_get("regime_label", "UNKNOWN"),
            "multiplier": db.kv_get("regime_multiplier", "1.0"),
        },
        "equity_curve": db.get_equity_curve(config.EQUITY_CURVE_POINTS),
        "positions": db.get_positions_snapshot(),
        "trades": db.get_trades(limit=30),
        "heartbeats": heartbeats,
        "avoid_list": db.get_avoid_list(active_only=True),
    })


def _expected_interval(agent: str) -> float:
    return {
        "trading": config.LOOP_INTERVAL_SEC,
        "watchdog": config.WATCHDOG_INTERVAL_SEC,
        "news": config.NEWS_INTERVAL_SEC,
        "regime": config.REGIME_INTERVAL_SEC,
        "reality": config.REALITY_INTERVAL_SEC,
    }.get(agent, 300)


@app.get("/api/logs")
def logs(after_id: int = 0, _: str = Depends(authed)) -> JSONResponse:
    return JSONResponse({"logs": db.get_logs(after_id=after_id, limit=200)})


@app.post("/api/kill")
def kill_switch(user: str = Depends(authed)) -> JSONResponse:
    set_halt(f"manual kill switch from dashboard (user {user})", "dashboard")
    db.log_reasoning("dashboard", f"🛑 MANUAL KILL SWITCH by {user}")
    send_alert(f"🛑 Manual KILL SWITCH pressed on dashboard by {user}. "
               f"halt.flag written — all trading stopped.",
               dedup_key="manual_kill")
    return JSONResponse({"ok": True, "halt": True})


@app.post("/api/resume")
def resume(user: str = Depends(authed)) -> JSONResponse:
    cleared = clear_halt()
    if cleared:
        db.log_reasoning("dashboard", f"halt.flag cleared by {user}")
        send_alert(f"▶️ halt.flag cleared from dashboard by {user}. "
                   f"Trading may resume next cycle.", dedup_key="manual_resume")
    return JSONResponse({"ok": True, "cleared": cleared})
