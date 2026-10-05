"""config/settings.py — v3"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env', override=True)

EMAIL    = os.getenv("OLYMPTRADE_EMAIL", "")
PASSWORD = os.getenv("OLYMPTRADE_PASSWORD", "")
ASSET    = os.getenv("ASSET", "EUR/USD OTC")
TRADE_AMOUNT  = float(os.getenv("TRADE_AMOUNT", "1000"))
TRADE_MODE    = os.getenv("TRADE_MODE", "ftt").lower()
FTT_DURATION  = int(os.getenv("FTT_DURATION", "60"))
ASSET_SCAN_INTERVAL = int(os.getenv("ASSET_SCAN_INTERVAL_SECONDS", "99999"))

# Confidence gate — raise to 0.65 once profitable
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.50"))
MODEL_TYPE           = os.getenv("MODEL_TYPE", "ensemble")
LOOKBACK_CANDLES     = int(os.getenv("LOOKBACK_CANDLES", "200"))
CANDLE_TIMEFRAME     = os.getenv("CANDLE_TIMEFRAME", "M1")

MAX_DAILY_TRADES   = int(os.getenv("MAX_DAILY_TRADES", "50"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "10"))
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT", "0.02"))
# Monitoring cadence is independent of candle periods and broker expiry.
POLL_INTERVAL      = float(os.getenv("POLL_INTERVAL_SECONDS", "1"))
ACTIVE_CANDLE_SECONDS = (30, 60)
# Apply the existing entry rules to completed 30s candles; 60 retains legacy entries.
STRATEGY_ENTRY_SECONDS = int(os.getenv("STRATEGY_ENTRY_SECONDS", "30"))
TREND_FILTER_SECONDS = 300
QUOTE_MAX_AGE_SECONDS = 2.0
HEADLESS           = os.getenv("HEADLESS", "false").lower() in ("1","true","yes")
LOG_DIR            = os.getenv("LOG_DIR",   "logs")
LOG_DATE_FORMAT            = os.getenv("LOG_DATE_FORMAT", "%Y%m%d")
LOG_ARCHIVE_DIR            = os.getenv("LOG_ARCHIVE_DIR", os.path.join(LOG_DIR, "archive"))
JOURNAL_ARCHIVE_DIR        = os.getenv("JOURNAL_ARCHIVE_DIR", os.path.join(LOG_DIR, "archive"))
RESET_TODAY_LOGS_ON_STARTUP = os.getenv("RESET_TODAY_LOGS_ON_STARTUP", "false").lower() in ("1", "true", "yes")
RL_USE_IN_LIVE_TRADING     = os.getenv("RL_USE_IN_LIVE_TRADING", "false").lower() in ("1", "true", "yes")
MODEL_DIR                  = os.getenv("MODEL_DIR", "models")
TARGET_PROFIT_PER_TRADE_USD = float(os.getenv("TARGET_PROFIT_PER_TRADE_USD", "100.0"))
MAX_TRADE_LOSS_USD = float(os.getenv("MAX_TRADE_LOSS_USD", "500.0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TRAINING_MODE = os.getenv("TRAINING_MODE", "false").lower() in ("1", "true", "yes")
LOGIN_WAIT_SECONDS = int(os.getenv("LOGIN_WAIT_SECONDS", "300"))

ALLOW_HEDGING = os.getenv("ALLOW_HEDGING", "true").lower() in ("1","true","yes")
CLOSE_OPPOSITE_ON_STRONG_SIGNAL = os.getenv("CLOSE_OPPOSITE_ON_STRONG_SIGNAL", "false").lower() in ("1","true","yes")

# Strategy selection; keep the original gate as the default and switch to the demo
# continuation model only when explicitly enabled.
DEMO_STRATEGY_ENABLED = os.getenv("DEMO_STRATEGY_ENABLED", "false").lower() in ("1", "true", "yes")
DEMO_ENTRY_TIMEFRAME_SECONDS = int(os.getenv("DEMO_ENTRY_TIMEFRAME_SECONDS", "30"))
DEMO_EMA_TOUCH_TOLERANCE_MULTIPLIER = float(os.getenv("DEMO_EMA_TOUCH_TOLERANCE_MULTIPLIER", "0.15"))
DEMO_ENTRY_DEADLINE_SECONDS = float(os.getenv("DEMO_ENTRY_DEADLINE_SECONDS", "5.0"))
