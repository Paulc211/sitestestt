"""Central configuration — every tunable lives here, none of the logic does.

Secrets do NOT live here. They come from .env via core.settings.
"""

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
STOCK_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
CRYPTO_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD"]
ALL_SYMBOLS = STOCK_SYMBOLS + CRYPTO_SYMBOLS

# ---------------------------------------------------------------------------
# Risk limits  (the risk manager and the watchdog BOTH read these,
# but each instantiates its own copy — the watchdog never trusts
# values the trading agent hands it at runtime)
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.05        # max 5% of equity per position
MAX_OPEN_POSITIONS = 4
STOP_LOSS_PCT = 0.02           # 2% stop loss
TAKE_PROFIT_PCT = 0.04         # 4% take profit
DAILY_LOSS_CAP_PCT = 0.02      # kill switch: halt new trades at -2% on the day

# Watchdog tolerances — reality is allowed to drift slightly past the limits
# (fills happen at real prices) before the watchdog nukes everything.
WATCHDOG_POSITION_TOLERANCE = 1.25   # breach if position > 125% of allowed size
WATCHDOG_INTERVAL_SEC = 60
WATCHDOG_STALE_HEARTBEAT_SEC = 900   # 3x the trading loop interval
WATCHDOG_CRASH_LOOP_WINDOW_SEC = 900
WATCHDOG_CRASH_LOOP_RESTARTS = 3     # >=3 distinct PIDs in the window = crash loop
WATCHDOG_FLATTEN_ON_SILENCE = False  # silence -> halt + alert; positions are
                                     # already protected by broker-side stops

# ---------------------------------------------------------------------------
# Strategy / loop
# ---------------------------------------------------------------------------
STRATEGY_MODULE = "strategies.ema_rsi"   # swappable: any module exposing build()
TIMEFRAME = "15Min"
BARS_LOOKBACK = 80                       # bars fetched per symbol per cycle
LOOP_INTERVAL_SEC = 300                  # 5 minutes

EMA_FAST = 12
EMA_SLOW = 26
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70                      # don't buy above this
RSI_OVERSOLD = 30                        # don't sell-signal below this

# Alpaca free tier = IEX feed + 15-min delayed SIP. We use IEX and never
# assume real-time SIP. Signals are computed on CLOSED bars only.
ALPACA_STOCK_FEED = "iex"

MIN_CRYPTO_NOTIONAL_USD = 10.0
STOCK_WHOLE_SHARES = True                # bracket orders need whole shares

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
NEWS_INTERVAL_SEC = 600
NEWS_BATCH_LIMIT = 40                    # headlines per poll
NEWS_DEFAULT_AVOID_HOURS = 4.0           # if the classifier omits a duration

REGIME_INTERVAL_SEC = 900
# Annualized realized-vol thresholds computed from 15-min bars.
REGIME_EQUITY_VOL_ELEVATED = 0.25        # SPY realized vol -> size down
REGIME_EQUITY_VOL_EXTREME = 0.40         # -> halt equities entirely
REGIME_CRYPTO_VOL_ELEVATED = 0.60
REGIME_CRYPTO_VOL_EXTREME = 1.00
VIX_ELEVATED = 25.0
VIX_EXTREME = 35.0
REGIME_REDUCED_MULT = 0.5                # position size multiplier when elevated

REALITY_INTERVAL_SEC = 3600
REALITY_MIN_TRADES = 10                  # need this many live trades to compare
REALITY_LOOKBACK_DAYS = 14
REALITY_WIN_RATE_DELTA = 0.15            # alert if live win rate drops this far
                                         # below backtest
REALITY_DRAWDOWN_FACTOR = 1.5            # alert if live DD > 1.5x backtest DD

# ---------------------------------------------------------------------------
# Files / paths (relative to the project root unless absolute)
# ---------------------------------------------------------------------------
DB_PATH = "state/trading.db"
HALT_FLAG_PATH = "state/halt.flag"
LOG_DIR = "logs"

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HOST = "127.0.0.1"   # keep behind nginx/caddy or an SSH tunnel;
                               # set 0.0.0.0 only if you accept direct exposure
DASHBOARD_PORT = 8899
EQUITY_CURVE_POINTS = 500

# ---------------------------------------------------------------------------
# LLM (news classification)
# ---------------------------------------------------------------------------
ANTHROPIC_MODEL_DEFAULT = "claude-opus-5"
NEWS_MAX_TOKENS = 1500
