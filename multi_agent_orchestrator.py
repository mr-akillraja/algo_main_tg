"""Part B deterministic one-minute strategy with existing browser execution."""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import argparse
import json
import math
from contextlib import nullcontext
from bot.process_lock import ProcessLock
from utils.safe_io import safe_write_csv_row
from strategies.part_b import evaluate_strategy
from strategies.live_monitor import analyze_forming

sys.path.insert(0, os.path.dirname(__file__))

from config import settings
from bot.risk import RiskManager, TradeRecord
from bot.browser import OlymtradeBot
from bot.chart_indicators import configure_strategy_chart, reconcile_selected_strategy_chart
from datetime import datetime
import os as _os
from models.predictor import Signal
from agents.agent_multi_timeframe import MultiTimeframeAgent
from agents.agent_pipeline_supervisor import PipelineSupervisorAgent
from agents.agent_asset_selector import AssetSelectorAgent

# ── Risk constants (from your strategy plan) ──────────────────────────────────
TRADE_AMOUNT        = 200.0    # $200 fixed per trade
DAILY_LOSS_LIMIT    = 2000.0   # $2,000 circuit breaker
MIN_CONFIDENCE      = 0.75     # Fixed Part B confidence threshold
MAX_TRADES_PER_DAY  = 20       # hard cap
PAYOUT_MINIMUM      = 0.80     # skip assets below 80% payout
FTT_DURATION_S      = 60       # 1 minute trades


def _rotate_daily_log(log_path: str):
    if not os.path.exists(log_path):
        return
    archive_dir = settings.LOG_ARCHIVE_DIR
    os.makedirs(archive_dir, exist_ok=True)
    today     = datetime.now().date()
    file_date = datetime.fromtimestamp(os.path.getmtime(log_path)).date()
    if file_date < today:
        name = (f"multi_agent_system_"
                f"{file_date.strftime(settings.LOG_DATE_FORMAT)}.log")
        path = os.path.join(archive_dir, name)
        if os.path.exists(path):
            name = (f"multi_agent_system_"
                    f"{file_date.strftime(settings.LOG_DATE_FORMAT)}"
                    f"_{int(time.time())}.log")
            path = os.path.join(archive_dir, name)
        os.rename(log_path, path)



log = logging.getLogger("multi_agent_orchestrator")


class MonitorConsoleFormatter(logging.Formatter):
    """Keep the terminal readable; file handlers retain the full JSON evidence."""

    def format(self, record):
        message = record.getMessage()
        if message.startswith("[Decision] {"):
            try:
                data = json.loads(message[len("[Decision] "):])
                gate = data.get("entry_evaluation", data.get("buy_evaluation", {}))
                entry_seconds = data.get("entry_timeframe_seconds", gate.get("entry_timeframe_seconds", 60))
                entry_label = "30s" if entry_seconds == 30 else "1m"
                live_entry = data.get("entry_candle", data.get("candles", {}).get(str(entry_seconds), {}))
                live_rsi = live_entry.get("indicators", {}).get("rsi14")
                number = lambda value: f"{value:.5f}" if isinstance(value, (int, float)) else "?"
                seconds = lambda value: f"{value:.3f}" if isinstance(value, (int, float)) else "?"
                timing = data.get("entry_timing", {})
                window = {True: "open", False: "closed"}.get(timing.get("within_window"), "unknown")
                stale_timeframes = ",".join(f"{period}s" for period in data.get("stale_closed_timeframes", [])) or "none"
                # Represent failed rules fully for easier debugging.
                failed_rules = gate.get('failed_rules') or []
                failed_rules_str = json.dumps(failed_rules)
                # Prefer explicit top-level then gate trend/entry close fields; fall back to closed_candles
                # Ensure we print CLOSE PRICES for the closed candles, not timestamps.
                trend_close = data.get('trend_close') or gate.get('trend_close')
                if trend_close is None:
                    trend_close = data.get('closed_candles', {}).get('30', {}).get('close')
                entry_close = data.get('entry_close') or gate.get('entry_close')
                if entry_close is None:
                    entry_close = data.get('closed_candles', {}).get(str(entry_seconds), {}).get('close')
                message = (
                    f"[Decision] {data['decision']} stage={data['blocking_stage']} "
                    f"direction={data.get('direction') or 'none'} "
                    f"evaluating={data.get('diagnostic_direction', 'BUY')} "
                    f"reason={data['reason']} quote={number(data.get('live_price'))} "
                    f"quote_status={data.get('quote_status', 'unknown')} "
                    f"quote_age_seconds={seconds(data.get('quote_age_seconds'))} "
                    f"closed_history={data.get('closed_candle_status', 'unknown')} "
                    f"stale_timeframes={stale_timeframes} "
                        f"trend.30s.closed={number(trend_close)} "
                        f"entry.{entry_label}.closed={number(entry_close)} "
                        f"trend_closed_ts={data.get('trend_closed_ts') or gate.get('trend_closed_ts') or data.get('closed_candles', {}).get('30', {}).get('open_ts')} "
                        f"entry_closed_ts={data.get('entry_closed_ts') or gate.get('entry_closed_ts') or data.get('closed_candles', {}).get(str(entry_seconds), {}).get('open_ts')} "
                    f"entry_seconds={entry_seconds} "
                    f"entry_age_seconds={seconds(timing.get('age_seconds'))} entry_window={window} "
                    f"{entry_label}.RSI.closed={number(gate.get('rsi'))} "
                    f"{entry_label}.RSI.live={number(live_rsi)} "
                    f"5m={data.get('trend_filter_5m', {}).get('reason', 'unavailable')} "
                    f"failed_count={len(failed_rules)} failed_rules={failed_rules_str} "
                    f"accepted={data.get('accepted_orders_this_run', 0)}")
            except (ValueError, TypeError, KeyError):
                pass
        # Do not mutate the shared record: the file handlers need the original.
        copy = logging.makeLogRecord(record.__dict__)
        copy.msg, copy.args = message, ()
        return super().format(copy)


def configure_logging():
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    log_file = os.path.join(settings.LOG_DIR, "multi_agent_system.log")
    _rotate_daily_log(log_file)
    run_dir = Path(settings.LOG_DIR) / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_log = run_dir / ("unified_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    format_string = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(MonitorConsoleFormatter(format_string))
    logging.basicConfig(level=logging.INFO, format=format_string,
        handlers=[console,
                  logging.FileHandler(log_file, encoding="utf-8"),
                  logging.FileHandler(run_log, encoding="utf-8")])
    log.info("[RunLog] path=%s", run_log.resolve())


# ── OHLC helper (fixes RSI=2.9 seed noise bug) ───────────────────────────────

def _get_ohlc_from_builder(builder):
    """Read actual closed OHLC only; never invent highs, lows, or opens."""
    return (builder.get_opens(), builder.get_highs(),
            builder.get_lows(), builder.get_closes())


# ── Session window helpers ────────────────────────────────────────────────────

# ── Main system ───────────────────────────────────────────────────────────────

class MultiAgentTradingSystem:

    TICK_INTERVAL     = 1.0
    DECISION_INTERVAL = 1.0
    CONFIRMATION_WINDOW = 15.0  # Part B: 5–15 seconds after the minute opens.
    ASSET_CHECK_EVERY = settings.ASSET_SCAN_INTERVAL

    def __init__(self, force_trades: bool = False,
                 training_mode: bool = False,
                 disable_circuit_breaker: bool = False,
                 min_confidence: float = MIN_CONFIDENCE,
                 trade_amount: float = TRADE_AMOUNT,
                 payout_minimum: float = PAYOUT_MINIMUM,
                 max_trades_per_day: int = MAX_TRADES_PER_DAY,
                 daily_loss_limit: float = DAILY_LOSS_LIMIT,
                 observe_only: bool = False, max_candle_closes: int = 0,
                 max_runtime: float = 0, chart_view: str = "entry",
                 entry_seconds: Optional[int] = None):
        self.entry_seconds = settings.STRATEGY_ENTRY_SECONDS if entry_seconds is None else entry_seconds
        if self.entry_seconds != 60:
            raise ValueError("Part B requires one-minute signal candles")
        if float(min_confidence) != MIN_CONFIDENCE:
            raise ValueError("Part B confidence threshold is fixed at 75")
        if settings.RESET_TODAY_LOGS_ON_STARTUP:
            log.warning("[Risk] startup_reset_ignored=true reason=preserve_trade_accounting")
            settings.RESET_TODAY_LOGS_ON_STARTUP = False
        if not math.isfinite(float(trade_amount)) or float(trade_amount) <= 0:
            raise ValueError("Trade amount must be finite and positive")
        if chart_view not in {"entry", "trend", "filter5m"}:
            raise ValueError("Chart view must be entry, trend or filter5m")
        self.chart_view = chart_view
        self.bot            = OlymtradeBot()
        self.risk           = RiskManager(max_daily_trades=max_trades_per_day,
                                          max_daily_loss=daily_loss_limit)
        self.asset_selector = AssetSelectorAgent()
        self.agent1         = MultiTimeframeAgent(settings.ASSET)
        # Debug / test overrides (set before supervisor creation)
        self.force_trades: bool = bool(force_trades)
        self.training_mode: bool = bool(training_mode)
        self.disable_circuit_breaker: bool = bool(disable_circuit_breaker)
        self.min_confidence: float = float(min_confidence)
        self.trade_amount: float = float(trade_amount)
        settings.TRADE_AMOUNT = self.trade_amount
        self.payout_minimum: float = float(payout_minimum)
        self.max_trades_per_day = self.risk.max_daily_trades
        self.daily_loss_limit = self.risk.max_daily_loss

        self.supervisor     = PipelineSupervisorAgent(
            self.bot, self.risk, self.agent1, None,
            asset_selector=self.asset_selector,
            force_trades=self.force_trades,
            disable_circuit_breaker=self.disable_circuit_breaker,
        )

        self.observe_only = observe_only
        self.max_candle_closes = max_candle_closes
        self.max_runtime = max_runtime
        self.closed_events = 0
        self._last_evaluation_key = None
        self._last_attempted_entry = None
        self._last_attempted_entry_seconds = None
        self._attempted_entries = {}
        self._execution_lock = asyncio.Lock()
        self._feed_missing = False
        self._process_lock_held = False
        self._execution_uncertain = False
        self._quote = None
        self.latest_decision = None
        self._chart_sync_active = False
        self._last_chart_report = None
        self._decision_tasks = set()
        self._accepted_count = 0
        self._seed_sources = {}
        self.active_trades: List[Dict] = []
        self._order_state_path = Path(settings.LOG_DIR) / "execution_state.json"
        self._attempted_setup_path = Path(settings.LOG_DIR) / "last_entry_setup.json"
        if self._attempted_setup_path.exists():
            try:
                attempted = json.loads(self._attempted_setup_path.read_text())
                if not isinstance(attempted, dict):
                    raise ValueError("Invalid attempted setup state")
                if attempted.get("asset") == settings.ASSET:
                    entry_ts = float(attempted["entry_ts"])
                    if not math.isfinite(entry_ts):
                        raise ValueError("Invalid setup timestamp")
                    entry_seconds = attempted.get("entry_timeframe_seconds", 60)
                    if entry_seconds not in (30, 60):
                        raise ValueError("Invalid setup timeframe")
                    entries = attempted.get("entries_by_timeframe", {str(entry_seconds): entry_ts})
                    if (not isinstance(entries, dict) or
                            any(period not in {"30", "60"} or not math.isfinite(float(timestamp))
                                for period, timestamp in entries.items())):
                        raise ValueError("Invalid attempted setups")
                    self._attempted_entries = {int(period): float(timestamp) for period, timestamp in entries.items()}
                    self._attempted_entries[entry_seconds] = entry_ts
                    self._last_attempted_entry = entry_ts
                    self._last_attempted_entry_seconds = entry_seconds
            except (ValueError, KeyError, TypeError, OSError):
                self._execution_uncertain = True
        if self._order_state_path.exists():
            try:
                state = json.loads(self._order_state_path.read_text())
                if state and state.get("status") == "accepted":
                    self.active_trades = [state["order"]]
                    order = state["order"]
                    self.risk.record_trade(TradeRecord(
                        timestamp=order["timestamp"], asset=order.get("asset", settings.ASSET),
                        mode="ftt", signal=order["signal"], confidence=0.95,
                        amount=order["amount"], state_key=order.get("state_key", "")))
                elif state:
                    self._execution_uncertain = True
            except (ValueError, KeyError, OSError):
                self._execution_uncertain = True
            if self.active_trades or self._execution_uncertain:
                log.warning("[Execution] pending_state_restored=true entries=paused_until_reconciled")
        self.running = False
        self._last_decision_ts:    float = 0.0
        self._last_asset_check_ts: float = 0.0
        self._current_asset: str   = settings.ASSET
        self._bg_tasks: List[asyncio.Task] = []

        self._loss_halt_duration_sec = 300.0
        self._quote_was_stale = True
        self._last_quote_recovery_ts = 0.0
        # Feed watchdog state
        self._feed_watchdog_active = False
        self._feed_outage_start = None
        self._feed_outage_cause = None
        self._feed_recovery_attempts = 0
        snapshot = self._sync_daily_accounting()
        self.supervisor.cb.restore_from_outcomes(snapshot["strategy"]["recent_outcomes"])
        self._log_daily_accounting("restored")
        # Reset accepted counter for fresh run as requested by user; durable journal remains authoritative.
        self._accepted_count = 0

    def _sync_daily_accounting(self):
        """Daily controls derive from durable records, including pending orders."""
        snapshot = self.risk.daily_snapshot()
        self._daily_loss = snapshot["gross_loss"]
        self._daily_profit = snapshot["gross_profit"]
        self._trades_today = snapshot["accepted_count"]
        self._settled_today = snapshot["settled_count"]
        self._wins_today = snapshot["wins"]
        self._consecutive_losses = snapshot["strategy"]["consecutive_losses"]
        last_loss = snapshot["strategy"]["last_loss_at"]
        self._loss_halt_time = datetime.fromisoformat(last_loss).timestamp() if last_loss else 0.0
        self._account_snapshot = snapshot
        return snapshot

    def _log_daily_accounting(self, event):
        snapshot = self._sync_daily_accounting()
        strategy = snapshot["strategy"]
        log.info("[DailyRisk] %s", json.dumps({
            "event": event, "date": snapshot["date"], "source": "trade_journal",
            "max_daily_trades": self.max_trades_per_day,
            "max_daily_loss": self.daily_loss_limit, "loss_basis": "gross_loss_plus_pending",
            "accepted": snapshot["accepted_count"], "settled": snapshot["settled_count"],
            "gross_loss": snapshot["gross_loss"], "net_pnl": snapshot["net_pnl"],
            "pending_exposure": snapshot["pending_exposure"],
            "historical_unconfirmed_ftt_count": snapshot["historical_unconfirmed_ftt_count"],
            "strategy_settled": strategy["settled_count"],
            "strategy_wins": strategy["wins"], "demo_tests_in_strategy_stats": False,
        }, sort_keys=True))

    def _check_consecutive_loss_halt(self) -> Tuple[bool, str]:
        self._sync_daily_accounting()
        remaining = self._loss_halt_duration_sec - (time.time() - self._loss_halt_time)
        if self._consecutive_losses >= 2 and remaining > 0:
            reason = f"Loss halt active ({self._consecutive_losses} losses), {remaining:.0f}s left"
            log.warning("[DailyRisk] %s", reason)
            return True, reason
        return False, ""

    def _trade_capacity(self) -> Tuple[int, int, float]:
        snapshot = self._sync_daily_accounting()
        loss_budget = snapshot["loss_budget_remaining"]
        return snapshot["trades_left"], int(loss_budget // self.trade_amount), loss_budget

    def _check_circuit_breaker(self) -> bool:
        snapshot = self._sync_daily_accounting()
        blocked = snapshot["halted"] or snapshot["trades_left"] <= 0
        if blocked:
            log.warning("[DailyRisk] status=blocked accepted=%s gross_loss=%s pending_exposure=%s",
                        snapshot["accepted_count"], snapshot["gross_loss"], snapshot["pending_exposure"])
        return blocked

    def _reset_daily_counters(self):
        # RiskManager detects rollover; never discard a pending order or reset the journal.
        snapshot = self._sync_daily_accounting()
        self.supervisor.cb.restore_from_outcomes(snapshot["strategy"]["recent_outcomes"])
        self._log_daily_accounting("rollover")

    def _record_trade_outcome(self, won: bool, pnl: float):
        # Settlement was already committed by RiskManager. Do not count it twice.
        self._log_daily_accounting("settled")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_price(self):
        """Return only a current observation of the selected broker instrument."""
        try:
            self._quote = await asyncio.wait_for(
                self.bot.get_current_quote(self._current_asset), timeout=0.8)
        except (asyncio.TimeoutError, ConnectionError):
            self._quote = None
        if self._quote is None:
            return None
        price = self._quote.get("price")
        if not self._quote_is_fresh(self._quote):
            return None
        return float(price)

    @staticmethod
    def _quote_is_fresh(quote):
        try:
            price, observed = float(quote["price"]), float(quote["observed_at"])
            return (not quote.get("stale", False) and math.isfinite(price) and price > 0
                    and math.isfinite(observed)
                    and 0 <= time.time() - observed <= settings.QUOTE_MAX_AGE_SECONDS)
        except (TypeError, ValueError, KeyError):
            return False

    async def setup(self) -> bool:
        log.info("[Setup] strategy=part_b_vwap_ema8 asset=%s monitor=1s expiry=60s "
                 "required_candles=5s,10s,15s,20s,30s,1m tick_counts_required=true "
                 "confidence_min=75 entry_seconds=%s observe_only=%s",
                 self._current_asset, self.entry_seconds, self.observe_only)
        if settings.FTT_DURATION != FTT_DURATION_S:
            log.error("[Setup] status=blocked reason=duration_mismatch configured=%s required=%s", settings.FTT_DURATION, FTT_DURATION_S)
            return False
        if settings.POLL_INTERVAL != self.TICK_INTERVAL:
            log.error("[Setup] status=blocked reason=monitor_interval_must_be_one_second")
            return False
        await self.bot.start()
        if not await self.bot.login():
            log.error("[Setup] status=blocked reason=login_failed")
            return False
        self.supervisor._current_asset = self._current_asset
        # Avoid repeated asset switches when the active title already proves identity.
        quote = await self.bot.get_current_quote(self._current_asset)
        if quote is None:
            if not await self.bot.select_asset(self._current_asset):
                log.error("[Setup] status=blocked reason=asset_selection_failed")
                return False
            quote = await self.bot.get_current_quote(self._current_asset)
        if quote is None:
            log.error("[Setup] status=blocked reason=selected_asset_quote_unavailable")
            return False
        if not self.observe_only and not await self.bot.is_demo_account():
            log.error("[Setup] status=blocked reason=demo_account_not_verified")
            return False
        flat_proof = await self.bot.get_flat_ftt_account_evidence()
        if flat_proof is not None:
            self.risk.confirm_flat_ftt_account(flat_proof)
            self._log_daily_accounting("broker_reconciled")
        display = await configure_strategy_chart(self.bot._page, self._entry_chart_view())
        log.info("[ChartDisplay] %s", json.dumps(display, sort_keys=True))
        # Perform startup self-test of writable CSVs to avoid runtime failures.
        try:
            ok = self._startup_selftest()
            if not ok:
                log.error("[Setup] startup_selftest_failed; aborting startup")
                return False
        except Exception as exc:
            log.error("[Setup] startup_selftest_exception %s", exc)
            return False
        log.info("[StrategyData] source=broker_native_ws strategy=part_b entry_seconds=%s "
                 "display_view=%s missing_required_inputs=fail_closed", self.entry_seconds, self._entry_chart_view())
        native_periods = set()
        # No Yahoo OTC mapping, fabricated 30s bars, or randomized bootstrap.
        for period in (30, 60):
            history = await self.bot.get_native_candles(self._current_asset, period, 180)
            if history is not None and not history.empty:
                native_periods.add(period)
                source = history.attrs.get("source", "broker_native")
                self.agent1.seed_from_dataframe(history, timeframe=period, source=source, log_seed=False)
                self._seed_sources[period] = source
            else:
                self._seed_sources[period] = "broker_browser_sample"
            log.info("[Seed] %s", json.dumps({
                "asset": self._current_asset, "timeframe_seconds": period,
                "source": self._seed_sources[period],
                "native_history_available": history is not None and not history.empty,
                "received_closed_candles": len(history) if history is not None else 0,
                "retained_closed_candles": len(self.agent1.builders[period]),
                "minimum_history_closed": 2 if period == 30 else 69,
                "entry_timeframe_seconds": self.entry_seconds,
                "m1_upsampling": False,
            }, sort_keys=True))
        if native_periods != {30, 60}:
            log.info("[DataConstraint] native_history_missing_for=%s; live browser "
                     "samples are observed quotes, not broker-native tick-complete OHLC; "
                     "Part B additionally requires 5s/10s/15s/20s history and actual tick counts; "
                     "missing inputs produce NO TRADE; entry_seconds=%s",
                     sorted({30, 60} - native_periods), self.entry_seconds)
        self._last_evaluation_key = (self.agent1.builders[30].latest_closed_ts,
                                     self.agent1.builders[self.entry_seconds].latest_closed_ts)
        self._bg_tasks.append(asyncio.create_task(self._daily_reset_monitor()))
        self._bg_tasks.append(asyncio.create_task(self._settlement_loop()))
        self._bg_tasks.append(asyncio.create_task(self._native_refresh_loop()))
        self._bg_tasks.append(asyncio.create_task(self._market_refresh_loop()))
        self._bg_tasks.append(asyncio.create_task(self._chart_display_loop()))
        self._bg_tasks.append(asyncio.create_task(self._feed_watchdog_loop()))
        return True

    def _startup_selftest(self) -> bool:
        """Write and read a row from key CSVs to detect permission or path problems.

        Returns True when all checks pass; False otherwise.
        """
        from pathlib import Path
        import csv
        from utils.safe_io import safe_write_csv_row

        base = Path(__file__).resolve().parent / "data"
        tests = [
            (base / "signals_log.csv", ["timestamp", "direction"], {"timestamp": "1970-01-01T00:00:00", "direction": "TEST"}),
            (base / "feed_outages.csv", ["start", "end"], {"start": "1970-01-01T00:00:00", "end": ""}),
            (base / "guard_events.csv", ["ts", "event"], {"ts": "1970-01-01T00:00:00", "event": "TEST"}),
        ]
        for path, fields, row in tests:
            try:
                ok = safe_write_csv_row(path, fields, row, write_header=True)
                if not ok:
                    log.error("[StartupSelfTest] write_failed path=%s", path)
                    return False
                # verify read
                with path.open("r", newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    found = False
                    for r in reader:
                        if r.get(list(row.keys())[0]) == list(row.values())[0]:
                            found = True
                            break
                    if not found:
                        log.error("[StartupSelfTest] read_back_failed path=%s", path)
                        return False
            except Exception as exc:
                log.error("[StartupSelfTest] exception path=%s reason=%s", path, exc)
                return False
        return True

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _daily_reset_monitor(self):
        """Reset circuit breaker and daily counters at midnight."""
        while True:
            try:
                now   = datetime.now()
                # Sleep until next midnight
                secs  = (86400 - (now.hour*3600 + now.minute*60 + now.second))
                await asyncio.sleep(secs)
                self._reset_daily_counters()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"[DailyReset] {e}")
                await asyncio.sleep(3600)

    # ── Shared data and decision loop ────────────────────────────────────────

    async def run(self, lock_acquired=False):
        with (nullcontext() if lock_acquired else ProcessLock()):
            self._process_lock_held = True
            try:
                if not await self.setup():
                    return
                self.running = True
                started = time.monotonic()
                log.info("[Run] poll_seconds=%s decisions=every_second signals=provisional_until_confirmed pid=%s",
                         self.TICK_INTERVAL, os.getpid())
                while self.running:
                    tick_start = time.monotonic()
                    if self.max_runtime and tick_start - started >= self.max_runtime:
                        log.info("[Run] status=finished reason=max_runtime closed_events=%s", self.closed_events)
                        break
                    price = await self._get_price()
                    if price is None:
                        if not self._feed_missing:
                            self.agent1.suspend_live_observations("quote_unavailable")
                            log.warning("[Feed] status=unavailable entries=paused closed_history=preserved")
                        self._feed_missing = True
                    elif self._feed_missing:
                        log.info("[Feed] status=recovered closed_history=preserved forming_coverage=rechecked")
                        self._feed_missing = False
                    if price is not None:
                        self._update_forming_candles(price)
                    key = tuple(self.agent1.builders[p].latest_closed_ts for p in (30, self.entry_seconds))
                    if key != self._last_evaluation_key and key != (None, None):
                        self.closed_events += 1
                    task = asyncio.create_task(self._evaluate_and_trade(price))
                    self._decision_tasks.add(task)
                    task.add_done_callback(self._decision_finished)
                    if self.max_candle_closes and self.closed_events >= self.max_candle_closes:
                        log.info("[Run] status=finished reason=candle_close_target closed_events=%s", self.closed_events)
                        break
                    await asyncio.sleep(max(0.01, self.TICK_INTERVAL - (time.monotonic() - tick_start)))
            finally:
                await self._shutdown()
                self._process_lock_held = False

    def _update_forming_candles(self, price):
        """Observe both active bars without promoting native samples to history."""
        observed = self._quote.get("read_at", self._quote["observed_at"])
        for period in settings.ACTIVE_CANDLE_SECONDS:
            builder = self.agent1.builders[period]
            if self._seed_sources.get(period) == "broker_native_ws":
                native = self.bot.get_native_forming_candle(self._current_asset, period)
                builder.update_forming(price, observed, native=native)
            elif builder.add_tick(price, observed) and period == 60:
                self.agent1.aggregate_five_minute()

    async def _native_refresh_loop(self):
        """Slow broker history responses cannot suspend the one-second monitor."""
        while True:
            started = time.monotonic()
            try:
                await self._update_closed_candles(None, native_only=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[NativeHistory] status=unavailable reason=%s", exc)
            await asyncio.sleep(max(0.01, self.TICK_INTERVAL - (time.monotonic() - started)))

    def _entry_chart_view(self):
        view = getattr(self, "chart_view", "entry")
        return "entry30s" if view == "entry" and self.entry_seconds == 30 else view

    async def _sync_chart_display(self):
        # Indicator dialogs share the order page. Reserve the close/entry window
        # for orders. Leave room for the 8s update plus bounded cleanup/readback.
        second = time.time() % self.entry_seconds
        if (not 10 <= second <= min(40, self.entry_seconds - 14) or self.active_trades or self._execution_uncertain
                or self._execution_lock.locked()):
            return
        async with self._execution_lock:
            self._chart_sync_active = True
            try:
                result = await reconcile_selected_strategy_chart(
                    self.bot._page, timeout_seconds=8, preferred_view=self._entry_chart_view())
                report = json.dumps(result, sort_keys=True)
                if report != getattr(self, "_last_chart_report", None):
                    log.info("[ChartDisplay] %s", report)
                    self._last_chart_report = report
                return result
            finally:
                self._chart_sync_active = False

    async def _chart_display_loop(self):
        while True:
            try:
                await self._sync_chart_display()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[ChartDisplay] verified=false reason=%s", exc)
            await asyncio.sleep(3)

    async def _write_feed_outage(self, start_ts, end_ts, cause, recovery_step):
        from pathlib import Path
        from utils.safe_io import safe_write_csv_row
        path = Path(__file__).resolve().parent / "data" / "feed_outages.csv"
        header = ["start", "end", "duration_seconds", "cause", "recovery_step"]
        row = {"start": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds") if start_ts else "",
               "end": datetime.fromtimestamp(end_ts).isoformat(timespec="seconds") if end_ts else "",
               "duration_seconds": (end_ts - start_ts) if start_ts and end_ts else "",
               "cause": cause, "recovery_step": recovery_step}
        # Use safe_io helper; do not let logging failures stop recovery.
        try:
            safe_write_csv_row(path, header, row, write_header=True)
        except Exception:
            # In the unlikely case safe_write_csv_row raises, swallow and log once.
            try:
                log.warning("[FeedWatchdog] failed_to_write_outage path=%s", path)
            except Exception:
                pass

    async def _feed_watchdog_loop(self):
        """Monitor quote freshness and run escalating recovery procedures when stale."""
        while True:
            try:
                now = time.time()
                quote = getattr(self, "_quote", None)
                quote_age = None
                if quote and isinstance(quote.get("observed_at"), (int, float)):
                    quote_age = now - quote.get("observed_at")
                is_stale = quote is None or not self._quote_is_fresh(quote)
                # Start outage
                if is_stale and not getattr(self, '_feed_watchdog_active', False):
                    self._feed_watchdog_active = True
                    self._feed_outage_start = now
                    self._feed_recovery_attempts = 0
                    # Determine likely cause: trades panel open check
                    page = getattr(self.bot, '_page', None)
                    try:
                        panel_open = await page.locator('[data-test="cor-w-panel-trades"]').is_visible() if page is not None else False
                    except Exception:
                        panel_open = False
                    cause = 'panel_open' if panel_open else 'ws_drop'
                    self._feed_outage_cause = cause
                    log.warning("[FeedWatchdog] outage_started cause=%s", cause)
                # Active recovery attempts
                if getattr(self, '_feed_watchdog_active', False):
                    elapsed = now - (getattr(self, '_feed_outage_start', None) or now)
                    worked = False
                    step = None
                    # Step 1: after 10s
                    if elapsed >= 10 and self._feed_recovery_attempts < 1:
                        self._feed_recovery_attempts = 1
                        step = 'close_panels_resubscribe'
                        # Close panels and verify chart
                        res = await self.bot._ensure_chart_visible_and_close_panels()
                        log.info("[FeedWatchdog] step=%s closed=%s result=%s", step, res.get('closed_panels'), res.get('reason'))
                        # Try re-subscribe
                        sub = await self.bot.resubscribe_quote_stream()
                        log.info("[FeedWatchdog] resubscribe=%s", sub)
                        # Check quote freshness soon after
                        await asyncio.sleep(1)
                        if self._quote_is_fresh(getattr(self, '_quote', None)):
                            worked = True
                    # Step 2: after 30s
                    if not worked and elapsed >= 30 and self._feed_recovery_attempts < 2:
                        self._feed_recovery_attempts = 2
                        step = 'reload_page'
                        try:
                            await self.bot._page.reload()
                            await asyncio.sleep(2)
                            # wait for chart to be verified
                            ok = await reconcile_selected_strategy_chart(self.bot._page, preferred_view=self._entry_chart_view())
                            log.info("[FeedWatchdog] step=%s result=%s", step, ok.get('reason'))
                            await asyncio.sleep(1)
                            if self._quote_is_fresh(getattr(self, '_quote', None)):
                                worked = True
                        except Exception as exc:
                            log.warning("[FeedWatchdog] reload_failed %s", exc)
                    # Step 3: after 90s
                    if not worked and elapsed >= 90 and self._feed_recovery_attempts < 3:
                        self._feed_recovery_attempts = 3
                        step = 'restart_browser_context'
                        try:
                            await self.bot.stop()
                            await asyncio.sleep(1)
                            await self.bot.start()
                            await asyncio.sleep(2)
                            if self._quote_is_fresh(getattr(self, '_quote', None)):
                                worked = True
                        except Exception as exc:
                            log.warning("[FeedWatchdog] restart_failed %s", exc)
                    if worked:
                        # Backfill closed history for 30s and 60s
                        await self._update_closed_candles(None, native_only=False)
                        # require one fresh closed candle after reload
                        await asyncio.sleep(2)
                        self._feed_watchdog_active = False
                        end_ts = time.time()
                        await self._write_feed_outage(self._feed_outage_start, end_ts, self._feed_outage_cause, step)
                        log.info("[FeedWatchdog] recovered step=%s duration=%.1fs", step, end_ts - self._feed_outage_start)
                    else:
                        # Escalation failed; if attempts exceeded stop trading
                        if getattr(self, '_feed_recovery_attempts', 0) >= 3 and elapsed >= 95:
                            end_ts = time.time()
                            await self._write_feed_outage(self._feed_outage_start, end_ts, self._feed_outage_cause, 'failed')
                            log.error("[FeedWatchdog] unrecoverable_feed_outage duration=%.1fs cause=%s", end_ts - self._feed_outage_start, self._feed_outage_cause)
                            # Stop new trading and wait for any active trades to settle
                            self.running = False
                            # Wait up to 120s for active trades to be settled via settlement loop
                            wait_deadline = time.time() + 120
                            while self.active_trades and time.time() < wait_deadline:
                                log.info("[FeedWatchdog] waiting_for_settlement active_trades=%s", len(self.active_trades))
                                await asyncio.sleep(2)
                            # Proceed to shutdown after waiting
                            try:
                                await self._shutdown()
                            except Exception:
                                pass
                            return
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[FeedWatchdog] error=%s", exc)
                await asyncio.sleep(1)

    async def _market_refresh_loop(self):
        """Request source updates even when the displayed price stays unchanged."""
        previous = None
        while True:
            started = time.monotonic()
            try:
                refreshed = await self.bot.refresh_native_market_data(self._current_asset)
                if refreshed != previous:
                    # Log feed drops and recoveries with timestamps and durations.
                    now_ts = time.time()
                    log_entry = {
                        "native_forming_fresh_by_timeframe": refreshed,
                        "scope": "native_forming_refresh",
                        "closed_history_managed_separately": True,
                        "timestamp": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
                    }
                    # Detect which timeframes dropped compared to previous
                    if previous is not None:
                        dropped = [p for p, ok in refreshed.items() if not ok and previous.get(p, True)]
                        recovered = [p for p, ok in refreshed.items() if ok and not previous.get(p, False)]
                        log_entry.update({"dropped_timeframes": dropped, "recovered_timeframes": recovered})
                    log.info("[MarketData] %s", json.dumps(log_entry, sort_keys=True))
                    previous = refreshed
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[MarketData] refresh=unavailable reason=%s", exc)
            await asyncio.sleep(max(0.01, self.TICK_INTERVAL - (time.monotonic() - started)))

    async def _update_closed_candles(self, price, native_only=False):
        """Refresh each native interval after its close, independently of monitoring."""
        closed = set()
        now = time.time()

        async def refresh(period):
            builder = self.agent1.builders[period]
            if self._seed_sources.get(period) == "broker_native_ws":
                expected = math.floor(now / period) * period - period
                previous = builder.latest_closed_ts
                if previous is not None and previous >= expected:
                    return
                history = await self.bot.get_native_candles(self._current_asset, period, 180)
                if history is None or history.empty:
                    log.warning("[NativeHistory] status=unavailable timeframe=%s entries=waiting", period)
                    return
                self.agent1.seed_from_dataframe(history, timeframe=period,
                                                source="broker_native_ws", log_seed=False)
                if builder.latest_closed_ts is not None and builder.latest_closed_ts != previous:
                    closed.add(period)
            elif not native_only and builder.add_tick(price, self._quote["observed_at"]):
                closed.add(period)
                if period == 60:
                    self.agent1.aggregate_five_minute()
        await asyncio.gather(*(refresh(period) for period in (30, 60)))
        return closed

    def _decision_finished(self, task):
        self._decision_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("[Decision] status=error reason=%s", task.exception())
            self._execution_uncertain = True
            self.running = False

    def _monitor_context(self, trend, entry, current_price, direction="BUY"):
        now = time.time()
        quote = getattr(self, "_quote", None)
        fresh = current_price is not None and self._quote_is_fresh(quote)
        observed_at = quote.get("observed_at") if quote else None
        if not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at):
            observed_at = None
        quote_age = now - observed_at if observed_at is not None else None
        if quote_age is not None and not math.isfinite(quote_age):
            quote_age = None
        candles, closed_status, forming_bars = {}, {}, {}
        monitored = {30: trend, 60: (entry if self.entry_seconds == 60 else
                                    self.agent1.builders[60].closed_snapshot())}
        for period, closed in monitored.items():
            expected = math.floor(now / period) * period - period
            opening = closed[-1]["ts"] if closed else None
            contiguous = all(right["ts"] - left["ts"] == period
                             for left, right in zip(closed, closed[1:]))
            closed_status[str(period)] = {
                "expected_open_ts": expected, "open_ts": opening,
                "close_at": opening + period if opening is not None else None,
                "fresh": opening == expected and contiguous,
                "contiguous": contiguous, "count": len(closed),
            }
            builder = self.agent1.builders[period]
            forming = builder.forming_snapshot(now) if hasattr(builder, "forming_snapshot") else None
            if forming and quote:
                # Source update time is distinct from our successful poll time.
                forming["sampled_at"] = forming["observed_at"]
                forming["observed_at"] = quote.get("observed_at")
                forming["stale"] = forming.get("stale", False) or not fresh
            forming_bars[period] = forming
            candles[str(period)] = analyze_forming(closed, forming, period, now)
        live_entry = analyze_forming(entry, forming_bars[self.entry_seconds],
                                     self.entry_seconds, now, role="entry")
        history_fresh = all(item["fresh"] for item in closed_status.values())
        entry_close_at = closed_status[str(self.entry_seconds)]["close_at"]
        age = now - entry_close_at if entry_close_at is not None else None
        return {"evaluated_at": now, "asset": self._current_asset,
                "monitor_interval_seconds": self.DECISION_INTERVAL,
                "active_candle_seconds": list(settings.ACTIVE_CANDLE_SECONDS),
                "trend_timeframe_seconds": 60, "entry_timeframe_seconds": self.entry_seconds,
                "trade_expiry_seconds": FTT_DURATION_S, "expiry_basis": "actual_broker_entry",
                "data_status": "fresh" if fresh and history_fresh else "stale_or_unavailable",
                "quote_status": "fresh" if fresh else "stale_or_unavailable",
                "closed_candle_status": "fresh" if history_fresh else "stale_or_unavailable",
                "closed_candles": closed_status,
                "stale_closed_timeframes": [int(period) for period, status in closed_status.items()
                                            if not status["fresh"]],
                "confirmation_window_seconds": self.CONFIRMATION_WINDOW,
                "entry_timing": {"timeframe_seconds": self.entry_seconds,
                                 "entry_close_at": entry_close_at, "age_seconds": age,
                                 "within_window": age is not None and 5 <= age <= self.CONFIRMATION_WINDOW},
                "live_price": current_price if fresh else None,
                "quote_observed_at": observed_at,
                "quote_age_seconds": quote_age,
                "quote_freshness": quote.get("freshness") if quote else None,
                "quote_freshness_source": quote.get("freshness_source") if quote else None,
                "quote_timestamp_kind": quote.get("timestamp_kind") if quote else None,
                "entry_candle": live_entry,
                "candles": candles}

    def _publish_decision(self, context, decision, reason):
        if reason in {"stale_or_unavailable_quote", "stale_candle_snapshot", "quote_expired_during_preflight"}:
            stage = "data"
        elif reason == "awaiting_next_closed_confirmation":
            stage = "timing"
        elif reason in {"risk_limit_or_loss_halt", "capacity", "payout_unavailable_or_below_minimum"}:
            stage = "risk"
        elif reason in {"order_unresolved", "chart_update_in_progress", "checking_demo_order_conditions",
                        "execution_rejected", "execution_uncertain", "order_accepted", "setup_already_attempted"}:
            stage = "execution"
        elif decision in {"BUY", "SELL"}:
            stage = "confirmation"
        else:
            stage = "entry_rules"
        payload = dict(context, decision=decision, reason=reason, blocking_stage=stage,
                       accepted_orders_this_run=self._accepted_count)
        self.latest_decision = payload
        log.info("[Decision] %s", json.dumps(payload, sort_keys=True, allow_nan=False))
        return payload

    def _evaluate_part_b(self, current_price):
        """Evaluate supplied history without altering or synthesizing market data."""
        histories = {period: builder.closed_snapshot()
                     for period, builder in self.agent1.builders.items()}
        evaluation = evaluate_strategy(
            histories, timestamp=time.time(), current_price=current_price,
            asset=self._current_asset)
        # All evaluations, including missing histories, reach the existing logger.
        log.info("%s", evaluation["report"])
        # Log evaluation as a durable CSV row using safe_write_csv_row; don't block trading on I/O failures.
        try:
            from pathlib import Path
            path = Path(__file__).resolve().parent / "data" / "signals_log.csv"
            fields = ["timestamp", "direction", "taken", "skip_reason", "entry_price",
                      "outcome", "confidence", "first_failed_gate", "vwap", "ema8", "stake", "payout_pct", "baseline", "pnl"]
            row = {
                "timestamp": datetime.fromtimestamp(time.time()).isoformat(timespec="seconds"),
                "direction": evaluation["signal"], "taken": False,
                "skip_reason": evaluation["reason"] if evaluation["signal"] == "NO TRADE" else "",
                "entry_price": current_price, "outcome": "not_taken",
                "confidence": evaluation["confidence"],
                "first_failed_gate": evaluation["first_failed_gate"],
                "vwap": evaluation["fields"].get("VWAP"),
                "ema8": evaluation["fields"].get("8 EMA"),
                "stake": float(self.trade_amount), "payout_pct": self.payout_minimum,
                "baseline": False, "pnl": 0.0,
            }
            safe_write_csv_row(path, fields, row, write_header=True)
        except Exception:
            pass
        return histories, evaluation

    async def _evaluate_and_trade(self, current_price: Optional[float]):
        histories, evaluation = self._evaluate_part_b(current_price)
        trend, entry = histories.get(30, []), histories.get(60, [])
        key = (trend[-1]["ts"] if trend else None,
               entry[-1]["ts"] if entry else None)
        self._last_evaluation_key = key
        direction = {"CALL": "BUY", "PUT": "SELL"}.get(evaluation["signal"])

        def decision_context(price):
            context = self._monitor_context(trend, entry, price, direction)
            gate = dict(evaluation, passed=evaluation["signal"] != "NO TRADE",
                        failed_rules=[evaluation["reason"]] if evaluation["signal"] == "NO TRADE" else [])
            context.update(strategy_evaluation=evaluation, entry_evaluation=gate,
                           signal=evaluation["signal"], confidence=evaluation["confidence"],
                           first_failed_gate=evaluation["first_failed_gate"],
                           direction=direction, diagnostic_direction=direction,
                           trend_close=trend[-1]["close"] if trend else None,
                           entry_close=entry[-1]["close"] if entry else None)
            return context

        quote_fresh = bool(current_price is not None and self._quote_is_fresh(getattr(self, "_quote", None)))
        if quote_fresh and getattr(self, "_quote_was_stale", False):
            self._last_quote_recovery_ts = time.time()
        self._quote_was_stale = not quote_fresh
        context = decision_context(current_price)
        if direction is None:
            return self._publish_decision(context, "NO TRADE", evaluation["reason"])
        # Logging a snapshot does not consume an entry. Revisit delayed history
        # within the confirmation window; only an attempted setup is consumed.
        if context["quote_status"] != "fresh":
            return self._publish_decision(context, "NO TRADE", "stale_or_unavailable_quote")
        if getattr(self, '_feed_watchdog_active', False):
            return self._publish_decision(context, "NO TRADE", "feed_recovery_in_progress")
        if time.time() - getattr(self, '_last_quote_recovery_ts', 0.0) < 2.0:
            return self._publish_decision(context, "NO TRADE", "feed_recovery_cooldown")
        if context["closed_candle_status"] != "fresh":
            return self._publish_decision(context, "NO TRADE", "stale_candle_snapshot")
        chart_busy = getattr(self, "_chart_sync_active", False)
        if (self.active_trades or self._execution_uncertain
                or (self._execution_lock.locked() and not chart_busy)):
            return self._publish_decision(context, "NO TRADE", "order_unresolved")
        now = time.time()
        if any(not candles or candles[-1]["ts"] != math.floor(now / period) * period - period
               for candles, period in ((trend, 30), (entry, self.entry_seconds))):
            return self._publish_decision(context, "NO TRADE", "stale_candle_snapshot")
        if self._setup_already_attempted(key[1]):
            return self._publish_decision(context, "NO TRADE", "setup_already_attempted")
        if not 5 <= time.time() - (key[1] + self.entry_seconds) <= self.CONFIRMATION_WINDOW:
            log.info("[Execution] status=blocked reason=stale_confirmation entry_ts=%s", key[1])
            return self._publish_decision(context, "NO TRADE", "awaiting_next_closed_confirmation")
        if chart_busy:
            return self._publish_decision(context, "NO TRADE", "chart_update_in_progress")
        if self.observe_only:
            log.info("[Execution] status=observe_only direction=%s entry_ts=%s", direction, key[1])
            return self._publish_decision(context, direction, f"confirmed_{direction.lower()}_observe_only")
        if not self._process_lock_held:
            raise RuntimeError("Execution requires the shared process lock")
        async with self._execution_lock:
            if self._setup_already_attempted(key[1]):
                return self._publish_decision(context, "NO TRADE", "setup_already_attempted")
            if self.active_trades or self._execution_uncertain:
                log.info("[Execution] status=blocked reason=order_unresolved")
                return self._publish_decision(context, "NO TRADE", "order_unresolved")
            now = time.time()
            if any(candles[-1]["ts"] != math.floor(now / period) * period - period
                   for candles, period in ((trend, 30), (entry, self.entry_seconds))):
                log.info("[Execution] status=blocked reason=stale_candle_snapshot")
                return self._publish_decision(context, "NO TRADE", "stale_candle_snapshot")
            if self._check_circuit_breaker() or self._check_consecutive_loss_halt()[0]:
                return self._publish_decision(context, "NO TRADE", "risk_limit_or_loss_halt")
            remaining, budget, _ = self._trade_capacity()
            if remaining <= 0 or budget <= 0:
                log.info("[Execution] status=blocked reason=capacity")
                return self._publish_decision(context, "NO TRADE", "capacity")
            self._publish_decision(context, "NO TRADE", "checking_demo_order_conditions")
            payout = await self.bot.get_asset_payout(self._current_asset)
            context = decision_context((self._quote or {}).get("price"))
            if payout is None or not self.payout_minimum <= payout <= 1.0:
                log.info("[Execution] status=blocked reason=payout payout=%s minimum=%s", payout, self.payout_minimum)
                return self._publish_decision(context, "NO TRADE", "payout_unavailable_or_below_minimum")
            if not 5 <= time.time() - (key[1] + self.entry_seconds) <= self.CONFIRMATION_WINDOW:
                log.info("[Execution] status=blocked reason=stale_confirmation entry_ts=%s", key[1])
                return self._publish_decision(context, "NO TRADE", "awaiting_next_closed_confirmation")
            if not self._quote_is_fresh(getattr(self, "_quote", None)):
                return self._publish_decision(context, "NO TRADE", "quote_expired_during_preflight")
            if context["closed_candle_status"] != "fresh":
                return self._publish_decision(context, "NO TRADE", "stale_candle_snapshot")
            _, latest = self._evaluate_part_b((self._quote or {}).get("price"))
            if latest["signal"] != evaluation["signal"]:
                evaluation = latest
                context = decision_context((self._quote or {}).get("price"))
                return self._publish_decision(context, "NO TRADE", latest["reason"])
            evaluation = latest
            self._last_attempted_entry = key[1]
            self._last_attempted_entry_seconds = self.entry_seconds
            self._persist_attempted_entry(key[1])
            signal = Signal.BUY if direction == "BUY" else Signal.SELL
            self._publish_decision(context, direction, evaluation["reason"])
            # Part B confidence is a rule score, not a calibrated win probability.
            self._persist_order_state({"status": "submitting", "asset": self._current_asset,
                                       "direction": direction, "entry_ts": key[1],
                                       "entry_timeframe_seconds": self.entry_seconds,
                                       "amount": self.trade_amount, "duration": FTT_DURATION_S,
                                       "submitted_at": datetime.now().isoformat(timespec="microseconds")})
            submission_evaluated_at = context["evaluated_at"]
            try:
                # Chart guard: ensure no panels are open before attempting a click.
                try:
                    guard = await self.bot._ensure_chart_visible_and_close_panels()
                    log.info("[ChartGuard] preflight_closed=%s reason=%s", guard.get('closed_panels'), guard.get('reason'))
                    if not guard.get('verified'):
                        context = decision_context((self._quote or {}).get("price"))
                        return self._publish_decision(context, "NO TRADE", "chart_not_ready")
                except Exception as _:
                    log.warning("[ChartGuard] preflight_failed")
                _, latest = self._evaluate_part_b((self._quote or {}).get("price"))
                if latest["signal"] != evaluation["signal"]:
                    evaluation = latest
                    context = decision_context((self._quote or {}).get("price"))
                    self._persist_order_state(None)
                    return self._publish_decision(context, "NO TRADE", latest["reason"])
                evaluation = latest
                success = await self.supervisor.safe_execute_trade(
                    signal, evaluation["confidence"] / 100.0, "ftt",
                    entry_deadline=key[1] + self.entry_seconds + self.CONFIRMATION_WINDOW)
            except asyncio.CancelledError:
                # Keep the durable submitting record and block later setups even
                # when this decision task is canceled while the process continues.
                self._execution_uncertain = True
                self.supervisor.execution_uncertain = True
                self.supervisor.last_execution_reason = "execution_uncertain: CancelledError"
                log.error("[Execution] status=uncertain retry=false reason=CancelledError direction=%s entry_ts=%s",
                          direction, key[1])
                context = decision_context((self._quote or {}).get("price"))
                context.update(submission_evaluated_at=submission_evaluated_at,
                               execution_detail=self.supervisor.last_execution_reason)
                self._publish_decision(context, "NO TRADE", "execution_uncertain")
                raise
            self._execution_uncertain = getattr(self.supervisor, "execution_uncertain", False)
            context = decision_context((self._quote or {}).get("price"))
            context.update(submission_evaluated_at=submission_evaluated_at)
            if not success:
                if not self._execution_uncertain:
                    self._persist_order_state(None)
                log.warning("[Execution] status=%s direction=%s entry_ts=%s",
                            "uncertain" if self._execution_uncertain else "rejected", direction, key[1])
                context["execution_detail"] = getattr(self.supervisor, "last_execution_reason", None)
                return self._publish_decision(context, "NO TRADE",
                                              "execution_uncertain" if self._execution_uncertain else "execution_rejected")
            order = dict(self.bot.last_order or {})
            timestamp = datetime.now().isoformat(timespec="microseconds")
            order.update(timestamp=timestamp, signal=direction, asset=self._current_asset,
                         state_key="PART_B_VWAP_EMA8", entry_ts=key[1],
                         entry_timeframe_seconds=self.entry_seconds)
            self._persist_order_state({"status": "accepted", "order": order})
            self.risk.record_trade(TradeRecord(
                timestamp=timestamp, asset=self._current_asset, mode="ftt",
                signal=direction, confidence=evaluation["confidence"] / 100.0, amount=order.get("amount", self.trade_amount),
                state_key=order["state_key"]))
            self.active_trades.append(order)
            self._accepted_count += 1
            # Chart guard: ensure panels closed after acceptance and log
            try:
                post = await self.bot._ensure_chart_visible_and_close_panels()
                log.info("[ChartGuard] post_accept_closed=%s reason=%s", post.get('closed_panels'), post.get('reason'))
            except Exception:
                log.warning("[ChartGuard] post_accept_failed")
            log.info("[Execution] %s", json.dumps(dict(order, status="accepted", direction=direction), default=str))
            # Do not manufacture outcomes from a later quote. Until the platform
            # settlement adapter confirms this order, further entries stay blocked.
            log.info("[Settlement] status=pending source=platform required_order_id=%s", order.get("id"))
            context["order_id"] = order.get("id")
            return self._publish_decision(context, direction, "order_accepted")

    def _setup_already_attempted(self, entry_ts):
        return (getattr(self, "_attempted_entries", {}).get(self.entry_seconds) == entry_ts or
                (self._last_attempted_entry == entry_ts and
                 getattr(self, "_last_attempted_entry_seconds", 60) == self.entry_seconds))

    def _persist_attempted_entry(self, entry_ts):
        """Keep rejected or settled setups consumed across process restarts too."""
        if not hasattr(self, "_attempted_entries"):
            self._attempted_entries = {}
        self._attempted_entries[self.entry_seconds] = entry_ts
        path = getattr(self, "_attempted_setup_path", None)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("w") as handle:
            json.dump({"asset": self._current_asset, "entry_ts": entry_ts,
                       "entry_timeframe_seconds": self.entry_seconds,
                       "entries_by_timeframe": self._attempted_entries}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def _persist_order_state(self, state):
        """Persist before clicking so restart never silently resubmits an order."""
        path = getattr(self, "_order_state_path", None)
        if path is None:  # Dependency-injected unit tests have no execution store.
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    async def _settlement_loop(self):
        while True:
            try:
                for order in list(self.active_trades):
                    result = await self.bot.get_order_result(order)
                    if result is None:
                        continue
                    if result.get("result") not in {"WIN", "LOSS", "DRAW"}:
                        continue
                    pnl = float(result["pnl"])
                    if not math.isfinite(pnl):
                        continue
                    committed = self.risk.update_pnl_and_journal(order["timestamp"], result["result"], pnl)
                    if committed is False:
                        self.active_trades.remove(order)
                        self._persist_order_state(None)
                        log.info("[Settlement] status=already_recorded order_id=%s", order.get("id"))
                        continue
                    won = result["result"] == "WIN"
                    self._record_trade_outcome(won, pnl)
                    if (result["result"] != "DRAW"
                            and order.get("state_key") != "DEMO_EXECUTION_TEST"
                            and order["timestamp"].startswith(self._account_snapshot["date"])):
                        self.supervisor.record_outcome(won)
                    self.active_trades.remove(order)
                    self._persist_order_state(None)
                    log.info("[Settlement] %s", json.dumps(dict(result, order_id=order.get("id"))))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[Settlement] status=unconfirmed reason=%s", exc)
            await asyncio.sleep(5)

    # ── Shutdown ───────────────────────────────────────────────────────────────

    async def _shutdown(self):
        self.running = False
        if self._decision_tasks:
            await asyncio.gather(*list(self._decision_tasks), return_exceptions=True)
        for t in list(self._bg_tasks):
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        await self.bot.stop()
        snapshot = self._sync_daily_accounting()
        strategy = snapshot["strategy"]
        log.info("[Session] accepted_this_run=%s account_accepted_today=%s "
                 "account_settled_today=%s account_net_pnl=%s strategy_settled_today=%s "
                 "strategy_wins_today=%s",
                 self._accepted_count, snapshot["accepted_count"], snapshot["settled_count"],
                 snapshot["net_pnl"], strategy["settled_count"], strategy["wins"])



if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Part B VWAP/EMA8 one-minute strategy with one-second monitoring")
    parser.add_argument("--chart-view", choices=("entry", "trend", "filter5m"), default="entry",
                        help="Initial chart: selected entry period EMA9/21, 30s trend EMA20/50, or 5m EMA9/21")
    parser.add_argument("--entry-seconds", type=int, choices=(60,), default=settings.STRATEGY_ENTRY_SECONDS,
                        help="Part B uses completed one-minute signal candles")
    parser.add_argument("--observe-only", action="store_true", help="Collect actual quotes and evaluate without clicks")
    parser.add_argument("--max-candle-closes", type=int, default=0, help="Stop after this many real close events; 0 runs continuously")
    parser.add_argument("--max-runtime", type=float, default=0, help="Bound runtime after login in seconds")
    parser.add_argument('--trade-amount', type=float, default=TRADE_AMOUNT,
                        help='Trade amount to use for this run')
    parser.add_argument('--max-daily-trades', type=int, default=None,
                        help='Override maximum daily trades')
    parser.add_argument('--max-daily-loss', type=float, default=None,
                        help='Override the daily loss limit for the local circuit breaker')
    parser.add_argument('--payout-minimum', type=float, default=PAYOUT_MINIMUM,
                        help='Minimum payout percentage required to place a trade')

    args = parser.parse_args()
    if args.max_daily_trades is not None:
        settings.MAX_DAILY_TRADES = args.max_daily_trades
    if args.max_daily_loss is not None:
        settings.MAX_DAILY_LOSS_USD = args.max_daily_loss
    if args.trade_amount is not None:
        settings.TRADE_AMOUNT = args.trade_amount

    try:
        with ProcessLock():
            asyncio.run(
                MultiAgentTradingSystem(
                    observe_only=args.observe_only,
                    chart_view=args.chart_view,
                    entry_seconds=args.entry_seconds,
                    max_candle_closes=args.max_candle_closes,
                    max_runtime=args.max_runtime,
                    trade_amount=args.trade_amount,
                    payout_minimum=args.payout_minimum,
                    max_trades_per_day=(args.max_daily_trades
                                         if args.max_daily_trades is not None
                                         else MAX_TRADES_PER_DAY),
                    daily_loss_limit=(args.max_daily_loss
                                      if args.max_daily_loss is not None
                                      else DAILY_LOSS_LIMIT),
                ).run(lock_acquired=True)
            )
    except KeyboardInterrupt:
        pass
