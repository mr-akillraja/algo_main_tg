"""Provisional readings of forming bars; never authorize an entry or a swing.

The closed-candle strategy remains the authority for entry confirmation. Live
indicator values include the current observed close, while support/resistance
and confirmed structure always use completed candles only.
"""

import math

from strategies.candle_math import valid_candles, confirmed_swings, ema


def _progression(points, higher, lower):
    if len(points) < 2:
        return None
    difference = points[-1]["price"] - points[-2]["price"]
    return higher if difference > 0 else lower if difference < 0 else "equal"


def _candidate_swings(closed, forming, highs, lows):
    """Inspect pending pivots without counting a forming right bar as closed."""
    bars = closed + [forming]
    candidates = {"highs": [], "lows": []}
    for index in range(max(2, len(closed) - 2), len(bars)):
        bar = bars[index]
        neighbours = bars[index - 2:index] + bars[index + 1:index + 3]
        closed_right = min(2, max(0, len(closed) - index - 1))
        for field, points, key, higher, lower in (
                ("high", highs, "highs", "HH", "LH"),
                ("low", lows, "lows", "HL", "LL")):
            extreme = (bar[field] > max(other[field] for other in neighbours)
                       if field == "high" else
                       bar[field] < min(other[field] for other in neighbours))
            if not extreme or closed_right >= 2:
                continue
            label = None
            if points:
                change = bar[field] - points[-1]["price"]
                label = higher if change > 0 else lower if change < 0 else "equal"
            candidates[key].append({
                "ts": bar["ts"], "price": float(bar[field]), "label": label,
                "confirmed": False, "closed_right_bars": closed_right,
                "required_more_closed_right_bars": 2 - closed_right,
            })
    return candidates


def analyze_forming(closed, forming, period, now, *, max_age_seconds=2.0, role=None):
    """Return JSON-safe live diagnostics for a 30s or 60s forming candle.

    ``forming`` supplies OHLC, candle-opening ``ts``, and quote ``observed_at``
    (``updated_at`` is accepted too). ``sampled_at`` may separately prove that a
    still-fresh quote was sampled after the current candle opened. Partial
    observations remain explicitly partial. Outdated observations expose no
    current values; a history gap retains fresh candle geometry but suppresses
    indicators and structure. This function never mutates inputs.

    ``role`` labels the monitored candle without changing its calculations.
    EMA8 is descriptive; every role remains provisional and cannot confirm an
    entry or apply strategy gates.
    """
    if period not in (30, 60):
        raise ValueError("Active monitoring requires 30-second or 60-second candles")
    if role is None:
        role = "trend" if period == 30 else "entry"
    if role not in ("trend", "entry"):
        raise ValueError("Monitoring role must be 'trend' or 'entry'")
    if not math.isfinite(now) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("Monitoring time and quote age limit must be finite and valid")
    result = {
        "timeframe_seconds": period, "role": role, "evaluated_at": now, "provisional": True,
        "can_confirm_entry": False, "current": False, "status": "unavailable",
        "reason": "forming_candle_unavailable", "quote_age_seconds": None,
        "live_price": None, "last_observed_price": None, "candle": None,
        "indicators": {}, "confirmed_structure": {}, "swing_candidates": {},
        "support_resistance": {}, "provisional_conditions": {},
    }
    if forming is None:
        return result
    bars = [dict(candle) for candle in closed]
    current = dict(forming)
    if not valid_candles(bars + [current]):
        return dict(result, status="invalid", reason="invalid_ohlc_or_timestamps")
    start = float(current["ts"])
    observed = current.get("observed_at", current.get("updated_at"))
    result["last_observed_price"] = float(current["close"])
    if observed is None:
        return dict(result, reason="quote_timestamp_unavailable")
    try:
        observed = float(observed)
    except (TypeError, ValueError):
        return dict(result, status="invalid", reason="invalid_quote_timestamp")
    if not math.isfinite(observed):
        return dict(result, status="invalid", reason="invalid_quote_timestamp")
    age = now - observed
    result["quote_age_seconds"] = age
    if current.get("stale", False):
        return dict(result, status="stale", reason="stale_quote")
    if not 0 <= age <= max_age_seconds:
        return dict(result, status="stale", reason="stale_quote" if age >= 0 else "future_quote")
    sampled = current.get("sampled_at", observed)
    try:
        sampled = float(sampled)
    except (TypeError, ValueError):
        return dict(result, status="invalid", reason="invalid_sample_timestamp")
    if not math.isfinite(sampled):
        return dict(result, status="invalid", reason="invalid_sample_timestamp")
    if not start <= sampled <= now < start + period or sampled < observed or start % period:
        return dict(result, status="stale", reason="forming_candle_not_current")
    if any(candle["ts"] % period for candle in bars):
        return dict(result, status="invalid", reason="unaligned_history_timestamps")
    if any(candle["ts"] + period > start for candle in bars):
        return dict(result, status="invalid", reason="history_contains_unclosed_candle")

    opening, high, low, price = (float(current[field]) for field in ("open", "high", "low", "close"))
    partial = bool(current.get("partial", not current.get("complete_from_open", True)))
    candle = {
        "ts": start, "open": opening, "high": high, "low": low,
        "close": price, "live_price": price, "body": abs(price - opening),
        "upper_wick": high - max(opening, price),
        "lower_wick": min(opening, price) - low,
        "direction": "bullish" if price > opening else "bearish" if price < opening else "doji",
        "remaining_seconds": start + period - now, "closed": False,
        "partial": partial, "source": current.get("source", "unspecified"),
        "native_observed_at": current.get("native_observed_at"),
        "native_source": current.get("native_source"),
        "observed_at": observed, "sampled_at": sampled,
    }
    if bars and (bars[-1]["ts"] + period != start or
                 any(right["ts"] - left["ts"] != period
                     for left, right in zip(bars, bars[1:]))):
        return dict(result, current=True, status="history_gap", reason="closed_history_gap",
                    live_price=price, candle=candle)
    closes = [float(bar["close"]) for bar in bars] + [price]
    ema8 = ema(closes, 8)
    indicators = {
        "ema8": ema8[-1] if len(closes) >= 8 else None,
        "includes_forming_close": True,
    }
    highs, lows = confirmed_swings(bars)
    high_label, low_label = _progression(highs, "HH", "LH"), _progression(lows, "HL", "LL")
    structure = {
        "high": high_label, "low": low_label,
        "label": (f"{high_label}+{low_label}" if high_label and low_label else "insufficient"),
        "highs": highs[-2:], "lows": lows[-2:], "closed_only": True,
    }
    resistances = [point["price"] for point in highs if point["price"] > price]
    supports = [point["price"] for point in lows if point["price"] < price]
    resistance = min(resistances) if resistances else None
    support = max(supports) if supports else None
    levels = {
        "nearest_resistance": resistance, "nearest_support": support,
        "resistance_distance": resistance - price if resistance is not None else None,
        "support_distance": price - support if support is not None else None,
        "levels_from_confirmed_swings_only": True,
    }
    return dict(result, current=True, status="partial" if partial else "provisional",
                reason="partial_forming_candle" if partial else "awaiting_candle_close",
                live_price=price, candle=candle, indicators=indicators,
                confirmed_structure=structure,
                swing_candidates=_candidate_swings(bars, current, highs, lows),
                support_resistance=levels)
