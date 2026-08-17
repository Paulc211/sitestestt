# tradebot — multi-agent automated day-trading system

Five independent processes + a dashboard, sharing state through SQLite,
designed for a 1 vCPU / 1GB Ubuntu droplet. **Defaults to paper trading and
cannot go live by accident.**

```
┌─────────────┐   signals/orders   ┌────────────────────┐
│ trading      │──────────────────▶│ Alpaca (stocks +    │
│ agent        │   bracket/stop    │ paper crypto)       │
└─────┬───────┘   broker-side ✔    │ Coinbase (live      │
      │ writes state               │ crypto, optional)   │
┌─────▼───────┐                    └─────────▲──────────┘
│   SQLite    │◀── reads/writes ─┐           │ independent queries
└─────▲───────┘                  │     ┌─────┴──────┐
      │                          ├─────│ watchdog   │ cancel/flatten/halt/kill
┌─────┴──────────────────────┐   │     └────────────┘
│ news · regime · reality    │───┘
└────────────────────────────┘        halt.flag ── any agent can write it;
┌────────────────────────────┐        trading agent checks it before EVERY
│ dashboard (FastAPI, HUD)   │        order
└────────────────────────────┘
```

## Safety model (read this first)

1. **Paper by default.** `MODE` is read from `.env`. Live trading requires
   *both* `MODE=live` and `LIVE_TRADING_CONFIRM=YES_I_UNDERSTAND_REAL_MONEY`,
   exactly. Missing, blank, or malformed values resolve to paper
   (`core/settings.py::resolve_mode`, unit-tested in `tests/test_risk.py`).
2. **Broker-side stops.** Stocks enter via Alpaca **bracket orders** (stop +
   take-profit live at the broker). Crypto entries are immediately followed by
   a broker-side **stop-limit sell** (Alpaca paper/live or Coinbase trigger
   stop). If the droplet dies, the stops still fire. If the protective stop is
   *rejected*, the entry is closed immediately rather than held naked, and the
   watchdog additionally alerts on any position with no working sell order.
3. **halt.flag.** A file at `state/halt.flag`. The trading agent checks it at
   the start of every cycle *and again immediately before every order*. Any
   agent — or you, with `touch state/halt.flag` over SSH — can set it.
4. **Daily loss kill switch.** At −2% from the day's starting equity the
   trading agent stops opening new positions for the rest of the day; the
   watchdog *independently* computes the same limit from real broker equity
   and, on breach, cancels all orders, flattens all positions, writes
   halt.flag, kills the trading agent, and alerts you on Telegram.
5. **Secrets.** Keys are only read in `core/settings.py`, never logged, never
   printed; `.env` is git-ignored and `chmod 600`.

Crypto note: in **paper** mode crypto trades on Alpaca's paper venue (Coinbase
has no sandbox for Advanced Trade). In **live** mode crypto routes to Coinbase
if CDP keys are configured, otherwise Alpaca live crypto. Crypto take-profit
is enforced in software (checked every cycle) because neither venue supports a
true OCO alongside our stop — the *stop* leg, the one that matters when the
server is dead, is always broker-side.

Data note: Alpaca's free tier is IEX real-time / 15-min-delayed SIP. All
signals are computed on **closed** 15-minute bars and entries are simulated in
the backtester at next-bar open — nothing assumes real-time SIP fills.

## Layout

```
config.py                 all tunables (symbols, limits, intervals)
core/                     settings, db, risk manager, halt, alerts, http, indicators
brokers/                  alpaca.py, coinbase.py, router.py
strategies/ema_rsi.py     EMA(12/26)+RSI(14) — swap via config.STRATEGY_MODULE
agents/                   trading, watchdog, news, regime, reality_check
dashboard/                FastAPI app + HUD frontend
backtest.py               historical evaluation, --save stores the baseline
deploy/                   systemd units + install.sh
tests/test_risk.py        risk manager + mode-safety tests
```

## Local setup

```bash
cd trading
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # fill in your keys
pytest tests/ -v                          # risk manager tests must pass
python backtest.py --days 60 --save       # evaluate + store baseline
python -m agents.trading_agent            # each agent is its own process
```

## Deploy to the droplet

```bash
# from your machine
rsync -a --exclude .venv --exclude state --exclude logs trading/ root@159.223.129.254:/tmp/tradebot-src/
ssh root@159.223.129.254 "cd /tmp/tradebot-src && bash deploy/install.sh"
ssh root@159.223.129.254 "nano /opt/tradebot/.env"   # keys; leave MODE=paper
ssh root@159.223.129.254 "systemctl start tradebot-watchdog tradebot-trading \
  tradebot-news tradebot-regime tradebot-reality tradebot-dashboard"
```

Units auto-restart on crash (`Restart=always`) and start on boot
(`systemctl enable`, done by install.sh). Per-unit `MemoryMax` keeps the whole
stack well under 1GB. Logs: `journalctl -u tradebot-trading -f` plus rotating
files in `/opt/tradebot/logs/`.

### Dashboard access

The dashboard binds to `127.0.0.1:8899` on purpose — HTTP Basic auth alone on
a public IP with money behind it is not enough. Two good options:

* **SSH tunnel (zero setup):**
  `ssh -L 8899:127.0.0.1:8899 root@159.223.129.254`, then open
  `http://localhost:8899` (browser prompts for the dashboard user/password).
* **Caddy with TLS:** `apt install caddy`, then in `/etc/caddy/Caddyfile`:

  ```
  yourdomain.example.com {
      reverse_proxy 127.0.0.1:8899
  }
  ```

  Caddy fetches certificates automatically; basic auth still applies on top.

The dashboard shows the live equity curve, open positions, recent trades,
per-agent heartbeats, halt/avoid flags, a streaming reasoning feed, and a
manual **KILL SWITCH** (writes halt.flag + Telegram alert). "CLEAR HALT"
appears only while halted.

## Operating it

* **Stop everything now:** press KILL SWITCH, or `touch /opt/tradebot/state/halt.flag`.
* **Resume:** clear the halt from the dashboard or `rm /opt/tradebot/state/halt.flag`
  (read `halt.flag` first — it records who tripped it and why).
* **Daily loss cap** resets at the next New York calendar day automatically.
* **Swap strategy:** implement `strategies/yourstrat.py` with a `build()`
  returning a `Strategy`, point `config.STRATEGY_MODULE` at it, backtest with
  `python backtest.py --save`, restart `tradebot-trading`.
* **Going live** (after weeks of clean paper trading): set the two env vars in
  `.env`, restart services, and you'll get a Telegram alert confirming LIVE
  mode. Start with a small account.

## Telegram alerts fire on

kill switch / halt.flag trips, watchdog emergency stop (limit breach), daily
loss cap, trading agent silent or crash-looping, take-profit/stop events,
regime changes, news pauses, and reality-check divergence.

## What the reality-check agent needs from you

Run `python backtest.py --save` whenever you change the strategy or its
parameters. That stored baseline (win rate, expectancy, max drawdown) is what
live results are compared against; without it the agent idles and tells you so.
