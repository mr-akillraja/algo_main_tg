from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

RSI_PERIOD = 14
BUY_LEVEL = 35
SELL_LEVEL = 65
MIN_CANDLES = 40

log = logging.getLogger(__name__)


def _wilder_rsi(closes: Sequence[float]) -> list[float]:
    if not closes:
        return []
    result = [50.0] * len(closes)
    if len(closes) < RSI_PERIOD + 1:
        return result
    deltas = [float(curr) - float(prev) for prev, curr in zip(closes, closes[1:])]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    if avg_loss == 0:
        current = 100.0 if avg_gain > 0 else 50.0
    else:
        current = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    result[RSI_PERIOD] = current
    for index in range(RSI_PERIOD + 1, len(closes)):
        avg_gain = ((avg_gain * (RSI_PERIOD - 1)) + gains[index - 1]) / RSI_PERIOD
        avg_loss = ((avg_loss * (RSI_PERIOD - 1)) + losses[index - 1]) / RSI_PERIOD
        if avg_loss == 0:
            current = 100.0 if avg_gain > 0 else 50.0
        else:
            current = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
        result[index] = current
    return result


def _mapping_value(source: object, *keys: str):
    if not isinstance(source, Mapping):
        return None
    for key in keys:
        if key in source:
            return source[key]
    return None


def rsi_cross_signal(candles_by_timeframe: Mapping[int, Sequence[dict]], *, timestamp: float,
                    current_price: float | None, asset: str, signal_price: float | None = None,
                    indicators: dict | None = None, session_high: float | None = None,
                    session_low: float | None = None) -> dict:
    """Evaluate the RSI crossover strategy and return the same contract as Part B."""
    import strategies.part_b as part_b

    legacy_mode = getattr(part_b, "STRATEGY_MODE", "gates")
    logger = getattr(part_b, "log", None)
    previously_disabled = logger.disabled if logger is not None else False
    if logger is not None:
        logger.disabled = True
    try:
        part_b.STRATEGY_MODE = "gates"
        legacy = part_b.evaluate_strategy(
            candles_by_timeframe=candles_by_timeframe,
            timestamp=timestamp,
            current_price=current_price,
            asset=asset,
            signal_price=signal_price,
            indicators=indicators,
            session_high=session_high,
            session_low=session_low,
        )
    finally:
        part_b.STRATEGY_MODE = legacy_mode
        if logger is not None:
            logger.disabled = previously_disabled

    fields = dict(legacy.get("fields") or {})
    base_confidence = getattr(part_b, "CONFIDENCE_THRESHOLD", 75)
    signal = "NO TRADE"
    confidence = 0
    reason = "no_cross"
    entry_reason = reason
    invalidation_reason = legacy.get("fields", {}).get("Invalidation Reason", "Entry expires after second 15 or a price drift above 0.15 ATR")

    quote_status = _mapping_value(indicators, "quote_status")
    entry_window = _mapping_value(indicators, "entry_window")
    entry_closed_ts = _mapping_value(indicators, "entry_closed_ts")
    already_signalled = bool(_mapping_value(indicators, "already_signalled"))
    last_signal_ts = _mapping_value(indicators, "last_signal_ts", "signal_ts")
    rsi_prev = 50.0
    rsi_now = 50.0

    try:
        minutes = part_b._closed(candles_by_timeframe.get(60), 60, timestamp)
    except Exception:
        minutes = []
    have = len(minutes)

    if have < MIN_CANDLES:
        reason = f"warmup have={have} need={MIN_CANDLES}"
        signal = "NO TRADE"
        confidence = 0
        entry_reason = reason
        invalidation_reason = reason
    elif quote_status != "fresh":
        reason = "stale_quote"
        signal = "NO TRADE"
        confidence = 0
        entry_reason = reason
        invalidation_reason = reason
    elif entry_window != "open":
        reason = "window_closed"
        signal = "NO TRADE"
        confidence = 0
        entry_reason = reason
        invalidation_reason = reason
    elif entry_closed_ts is not None and (already_signalled or last_signal_ts == entry_closed_ts):
        reason = "already_signalled"
        signal = "NO TRADE"
        confidence = 0
        entry_reason = reason
        invalidation_reason = reason
    else:
        closes = [float(bar["close"]) for bar in minutes]
        values = _wilder_rsi(closes)
        rsi_prev = float(values[-2]) if len(values) >= 2 else 50.0
        rsi_now = float(values[-1]) if values else 50.0
        if rsi_prev < BUY_LEVEL and rsi_now >= BUY_LEVEL:
            signal = "CALL"
            confidence = min(100, base_confidence + 5)
            reason = f"RSI14 crossed up through 35 (prev={rsi_prev:.2f} now={rsi_now:.2f})"
            entry_reason = reason
            invalidation_reason = legacy.get("fields", {}).get("Invalidation Reason", "Entry expires after second 15 or a price drift above 0.15 ATR")
        elif rsi_prev > SELL_LEVEL and rsi_now <= SELL_LEVEL:
            signal = "PUT"
            confidence = min(100, base_confidence + 5)
            reason = f"RSI14 crossed down through 65 (prev={rsi_prev:.2f} now={rsi_now:.2f})"
            entry_reason = reason
            invalidation_reason = legacy.get("fields", {}).get("Invalidation Reason", "Entry expires after second 15 or a price drift above 0.15 ATR")
        else:
            reason = "no_cross"
            signal = "NO TRADE"
            confidence = 0
            entry_reason = reason
            invalidation_reason = reason

    fields.update({
        "Signal": signal,
        "Confidence": confidence,
        "Entry Reason": entry_reason,
        "Invalidation Reason": invalidation_reason,
    })
    report = "\n".join(f"{name}: {fields.get(name, 'unavailable')}" for name in part_b.REPORT_FIELDS)
    report += (
        f"\nDecision: 1m.RSI.closed={rsi_now:.2f} strategy=rsi_cross "
        f"rsi_prev={rsi_prev:.2f} rsi_now={rsi_now:.2f}"
    )

    return {
        "signal": signal,
        "confidence": confidence,
        "first_failed_gate": None,
        "reason": reason,
        "report": report,
        "fields": fields,
        "metrics": {"rsi_prev": float(rsi_prev) if 'rsi_prev' in locals() else 50.0,
                     "rsi_now": float(rsi_now) if 'rsi_now' in locals() else 50.0,
                     "quote_status": quote_status,
                     "entry_window": entry_window,
                     "entry_closed_ts": entry_closed_ts},
    }
