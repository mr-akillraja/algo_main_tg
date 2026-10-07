"""config/settings.py — v3"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env', override=True)

EMAIL    = os.getenv("OLYMPTRADE_EMAIL", "")
PASSWORD = os.getenv("OLYMPTRADE_PASSWORD", "")
ASSET    = os.getenv("ASSET", "EUR/USD OTC")
_ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000.0"))
_TRADE_STAKE_PCT = float(os.getenv("TRADE_STAKE_PCT", "0.01"))
TRADE_AMOUNT  = float(os.getenv("TRADE_AMOUNT", str(_ACCOUNT_BALANCE * _TRADE_STAKE_PCT)))
TRADE_MODE    = os.getenv("TRADE_MODE", "ftt").lower()
FTT_DURATION  = int(os.getenv("FTT_DURATION", "60"))
ASSET_SCAN_INTERVAL = int(os.getenv("ASSET_SCAN_INTERVAL_SECONDS", "99999"))

# Compatibility for unchanged execution guards: the Part B threshold is fixed.
CONFIDENCE_THRESHOLD = 0.75

MAX_DAILY_TRADES   = int(os.getenv("MAX_DAILY_TRADES", "50"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "10"))
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT", "0.02"))
# Monitoring cadence is independent of candle periods and broker expiry.
POLL_INTERVAL      = float(os.getenv("POLL_INTERVAL_SECONDS", "1"))
ACTIVE_CANDLE_SECONDS = (30, 60)
# Part B signals always use one-minute candles. Feed periods remain unchanged.
STRATEGY_ENTRY_SECONDS = 60
# VWAP volume handling: 'equal' treats every minute as volume=1; 'ticks' requires genuine tick counts.
VWAP_VOLUME_MODE = os.getenv("VWAP_VOLUME_MODE", "equal")
QUOTE_MAX_AGE_SECONDS = 2.0
HEADLESS           = os.getenv("HEADLESS", "false").lower() in ("1","true","yes")
LOG_DIR            = os.getenv("LOG_DIR",   "logs")
LOG_DATE_FORMAT            = os.getenv("LOG_DATE_FORMAT", "%Y%m%d")
LOG_ARCHIVE_DIR            = os.getenv("LOG_ARCHIVE_DIR", os.path.join(LOG_DIR, "archive"))
JOURNAL_ARCHIVE_DIR        = os.getenv("JOURNAL_ARCHIVE_DIR", os.path.join(LOG_DIR, "archive"))
RESET_TODAY_LOGS_ON_STARTUP = os.getenv("RESET_TODAY_LOGS_ON_STARTUP", "false").lower() in ("1", "true", "yes")
MAX_TRADE_LOSS_USD = float(os.getenv("MAX_TRADE_LOSS_USD", "500.0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TRAINING_MODE = os.getenv("TRAINING_MODE", "false").lower() in ("1", "true", "yes")
LOGIN_WAIT_SECONDS = int(os.getenv("LOGIN_WAIT_SECONDS", "300"))

ALLOW_HEDGING = os.getenv("ALLOW_HEDGING", "true").lower() in ("1","true","yes")
CLOSE_OPPOSITE_ON_STRONG_SIGNAL = os.getenv("CLOSE_OPPOSITE_ON_STRONG_SIGNAL", "false").lower() in ("1","true","yes")
