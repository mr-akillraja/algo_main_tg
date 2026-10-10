"""Deterministic Part B strategy; no broker, model, or market-data side effects.

Candles have ``ts`` (UTC epoch opening time), OHLC and genuine ``ticks`` counts.
Only closed, aligned, contiguous candles are used. Forming candles are ignored.
VWAP volume mode: configurable `vwap_volume_mode` controls how VWAP is computed:
- "equal": treat every minute as unit volume (VWAP becomes a rolling mean of typical price).
- "ticks": use genuine per-minute `ticks` counts; if any tick counts are missing the VWAP gate fails.
This file's evaluator logs the active VWAP mode and reports which later gates fail after Gate 1 passes.

``indicators`` optionally supplies positive ``vwap``, ``ema8``, ``atr1m`` and
``atr15s`` scalars for the latest closed candle. Timestamp-keyed mappings named
``vwap_history``, ``ema8_history`` and ``atr1m_history`` supply historical values.
Historical comparisons use their own bar's indicators, never today's VWAP.
Missing values are computed where the requisite genuine history is available.
EMA uses the first close as its seed; Wilder ATR seeds with the first 14 true
ranges (the first true range is high minus low). EMA needs eight observations.

Explicit interpretations of underspecified rules:
* The retest is the latest closed minute. Method 2 is an alternative to a
  breakout; its pullback starts at the last close peak/trough before the final
  rejection, including the contiguous retreat leading into that rejection.
* Breakout levels must already be valid before the breakout. Its previous close
  must be on the unbroken side. Historical breakout/cancellation distances use
  ATR at those bars, avoiding retrospective qualification.
* Pivot clusters have a maximum span of 0.25 ATR, use their mean price and count
  distinct pivot touches. Equal highs/lows are not strict n=2 pivots.
* Session extrema are accepted only from explicit session_high/session_low.
  A current session extremum is available to a historical breakout only if a
  preceding bar actually attained it. Missing opposing levels cannot prove room.
* VWAP equality is neutral; side changes ignore intervening equalities.
* Entry timing is the inclusive 5..15 second interval. Signal price defaults to
  the latest closed minute close. All price distances are ATR1m-normalized.

Fixed confidence anchors (linear, clipped to 0..1; never adaptive): VWAP uses
  the minimum of |dV| 0.5..1.5, directed slope 0.10..0.30, same-side count 6..8;
  EMA slope 0.10..0.30; structure's smaller progression 0.10..0.50;
  breakout excess 0.15..0.50 with retest proximity; VWAP retest proximity to
  VWAP 0.25..0 and rejection close distance 0..0.50; confluence count/proximity;
  rejection wick 0.40..0.70, engulfing body ratio 1..2, or favorable close
  0.60..0.90 and body 0.40..0.70; votes 3..5 and larger-timeframe alignment 2..3.
  Each passed component scores weight*(0.6+0.4*strength); failed/unvisited
  components score zero. Whole-number confidence is floored, with a hard 75
  threshold. first_failed_gate is 1..7, 'confidence', or None on success.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone


log = logging.getLogger(__name__)
TIMEFRAMES = (5, 10, 15, 20, 30)
WEIGHTS = {"vwap": 20, "ema": 15, "structure": 15, "setup": 20,
           "confluence": 10, "candle": 10, "subminute": 10}
REPORT_FIELDS = (
    "Asset", "Timestamp", "Current Price", "1-Minute Trend", "VWAP",
    "Price vs VWAP", "8 EMA", "Price vs 8 EMA", "Market Structure",
    "Important Support", "Important Resistance", "Breakout Detected",
    "Retest Detected", "Candlestick Confirmation", "5s Direction",
    "10s Direction", "15s Direction", "20s Direction", "30s Direction",
    "Lower-Timeframe Consensus", "Signal", "Confidence", "Expiry",
    "Entry Reason", "Invalidation Reason",
)
STRATEGY_MODE = "rsi_cross"
CONFIDENCE_THRESHOLD = 75


class _Unavailable(ValueError):
    pass


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value):
    return _finite(value) and value > 0


def _clip(value):
    return min(1.0, max(0.0, value))


def _strength(value, threshold, strong):
    return _clip((value - threshold) / (strong - threshold))


def _closed(raw, period, now):
    if raw is None:
        raise _Unavailable(f"missing {period}s candles")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise _Unavailable(f"invalid {period}s candles")
    result = []
    for item in raw:
        if not isinstance(item, Mapping) or not _finite(item.get("ts")):
            raise _Unavailable(f"invalid {period}s candle timestamp")
        ts = item["ts"]
        if item.get("closed") is False or ts + period > now:
            continue
        if ts % period != 0:
            raise _Unavailable(f"unaligned {period}s candle timestamp")
        if not all(_positive(item.get(name)) for name in ("open", "high", "low", "close")):
            raise _Unavailable(f"invalid {period}s candle prices")
        bar = {name: float(item[name]) for name in ("ts", "open", "high", "low", "close")}
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]):
            raise _Unavailable(f"invalid {period}s OHLC bounds")
        if result and ts != result[-1]["ts"] + period:
            raise _Unavailable(f"non-contiguous {period}s closed candles")
        if "ticks" in item:
            bar["ticks"] = item["ticks"]
        result.append(bar)
    if not result:
        raise _Unavailable(f"missing {period}s closed candles")
    if result[-1]["ts"] != math.floor(now / period) * period - period:
        raise _Unavailable(f"stale {period}s closed candles")
    return result


def ema_series(closes, period=8):
    """First-observation-seeded EMA with None during its minimum warm-up."""
    if not closes:
        return []
    current = float(closes[0])
    values = []
    for index, close in enumerate(closes):
        if index:
            current += 2.0 / (period + 1) * (float(close) - current)
        values.append(current if index >= period - 1 else None)
    return values


def wilder_atr_series(candles, period=14):
    """Wilder ATR, seeded by the arithmetic mean of the first period ranges."""
    result, ranges = [], []
    current = None
    for index, bar in enumerate(candles):
        previous = candles[index - 1]["close"] if index else bar["close"]
        tr = max(bar["high"] - bar["low"], abs(bar["high"] - previous), abs(bar["low"] - previous))
        ranges.append(tr)
        if index == period - 1:
            current = sum(ranges) / period
        elif index >= period:
            current = (current * (period - 1) + tr) / period
        result.append(current)
    return result


def confirmed_pivots(candles):
    """Strict n=2 fractals; index+2 is the bar that confirms each pivot."""
    result = []
    for index in range(2, len(candles) - 2):
        neighbours = candles[index - 2:index] + candles[index + 1:index + 3]
        for kind, field, comparison in (("high", "high", max), ("low", "low", min)):
            price = candles[index][field]
            edge = comparison(bar[field] for bar in neighbours)
            if (kind == "high" and price > edge) or (kind == "low" and price < edge):
                result.append({"kind": kind, "price": price, "index": index,
                               "ts": candles[index]["ts"], "confirmed_index": index + 2})
    return result


class _Indicators:
    def __init__(self, candles, supplied):
        self.bars = candles
        self.supplied = supplied
        self.ema = ema_series([bar["close"] for bar in candles])
        self.atr = wilder_atr_series(candles)
        self.cache = {}
        # vwap mode: default to 'ticks' unless caller provided a mode via supplied
        # indicators under key 'vwap_volume_mode'. The orchestrator/global
        # settings may also supply this via config.
        self.vwap_volume_mode = (supplied.get("vwap_volume_mode") or
                                 __import__("config.settings", fromlist=["VWAP_VOLUME_MODE"]).VWAP_VOLUME_MODE)

    def value(self, name, index):
        if index < 0 or index >= len(self.bars):
            raise _Unavailable(f"insufficient {name} history")
        key = (name, index)
        if key in self.cache:
            return self.cache[key]
        history = self.supplied.get(name + "_history", {})
        if not isinstance(history, Mapping):
            raise _Unavailable(f"invalid supplied {name} history")
        timestamp = self.bars[index]["ts"]
        value = history.get(timestamp, history.get(str(timestamp), history.get(str(int(timestamp)))))
        supplied = value is not None
        if index == len(self.bars) - 1 and name in self.supplied:
            value, supplied = self.supplied[name], True
        if supplied:
            if not _positive(value):
                raise _Unavailable(f"invalid supplied {name}")
        elif name == "ema8":
            value = self.ema[index]
        elif name == "atr1m":
            value = self.atr[index]
        elif name == "vwap":
            if index < 59:
                raise _Unavailable("VWAP requires 60 closed minute candles at each historical comparison")
            window = self.bars[index - 59:index + 1]
            # Volume mode handling: 'equal' uses unit weights; 'ticks' uses genuine counts and fails if missing.
            if str(self.vwap_volume_mode).lower() == "equal":
                total = len(window)
                value = sum((bar["high"] + bar["low"] + bar["close"]) / 3 for bar in window) / total
            else:
                for bar in window:
                    ticks = bar.get("ticks")
                    if not _finite(ticks) or ticks < 0 or int(ticks) != ticks:
                        raise _Unavailable("VWAP requires genuine tick counts on all 60 minute candles")
                total = sum(bar["ticks"] for bar in window)
                if total <= 0:
                    raise _Unavailable("VWAP tick-count total is zero")
                value = sum((bar["high"] + bar["low"] + bar["close"]) / 3 * bar["ticks"]
                            for bar in window) / total
        if not _positive(value):
            raise _Unavailable(f"insufficient or zero {name}")
        self.cache[key] = float(value)
        return float(value)


def _levels(pivots, asof, atr, bars, session_high, session_low, *, historical=False):
    candidates = [point for point in pivots
                  if point["confirmed_index"] <= asof and point["index"] >= max(0, asof - 59)]
    candidates.sort(key=lambda point: (point["price"], point["index"], point["kind"]))
    clusters = []
    for point in candidates:
        if not clusters or point["price"] - clusters[-1][0]["price"] > 0.25 * atr:
            clusters.append([point])
        else:
            clusters[-1].append(point)
    result = [{"price": sum(point["price"] for point in cluster) / len(cluster),
               "touches": len(cluster), "source": "pivots"}
              for cluster in clusters if len(cluster) >= 2]
    for price, field in ((session_high, "high"), (session_low, "low")):
        if price is None:
            continue
        # Present-day extrema cannot be assigned to a past breakout unless they
        # had already been observed. Do not infer full-session bounds from a deque.
        if historical and not any(math.isclose(bar[field], price, rel_tol=1e-12, abs_tol=0.0)
                                  for bar in bars[:asof + 1]):
            continue
        result.append({"price": float(price), "touches": 1, "source": "session_" + field})
    return sorted(result, key=lambda level: level["price"])


def _rejection(bar, previous, side):
    span = bar["high"] - bar["low"]
    if span <= 0:
        return None
    body = abs(bar["close"] - bar["open"])
    wick = (min(bar["open"], bar["close"]) - bar["low"] if side == 1
            else bar["high"] - max(bar["open"], bar["close"])) / span
    favorable = ((bar["close"] - bar["low"]) if side == 1 else (bar["high"] - bar["close"])) / span
    choices = []
    if wick >= 0.4:
        choices.append(("rejecting wick", _strength(wick, 0.4, 0.7)))
    if previous is not None and side * (bar["close"] - bar["open"]) > 0:
        previous_body = abs(previous["close"] - previous["open"])
        if (side * (previous["close"] - previous["open"]) < 0
                and min(bar["open"], bar["close"]) <= min(previous["open"], previous["close"])
                and max(bar["open"], bar["close"]) >= max(previous["open"], previous["close"])
                and previous_body > 0):
            choices.append(("directional engulfing", _strength(body / previous_body, 1, 2)))
    if favorable >= 0.6 and body / span >= 0.4:
        choices.append(("favorable close and body", min(_strength(favorable, 0.6, 0.9),
                                                        _strength(body / span, 0.4, 0.7))))
    return max(choices, key=lambda item: item[1]) if choices else None


def _setup(bars, values, pivots, side, atr, vwap, ema, session_high, session_low):
    last = len(bars) - 1
    retest = bars[-1]
    extreme = retest["low"] if side == 1 else retest["high"]
    unavailable = None
    # Most recent completed breakout first; every candidate has its own history.
    for index in range(last - 1, max(-1, last - 7), -1):
        if index < 1:
            continue
        try:
            breakout_atr = values.value("atr1m", index)
            prior_atr = values.value("atr1m", index - 1)
            levels = _levels(pivots, index - 1, prior_atr, bars, session_high, session_low, historical=True)
            candle = bars[index]
            span = candle["high"] - candle["low"]
            if span <= 0 or span / breakout_atr > 2 or abs(candle["close"] - candle["open"]) / span < 0.5:
                continue
            for level in levels:
                price = level["price"]
                excess = side * (candle["close"] - price) / breakout_atr
                if side * (bars[index - 1]["close"] - price) > 0 or excess < 0.15:
                    continue
                offset = side * (extreme - price) / atr
                if not -0.35 <= offset <= 0.25 or side * (retest["close"] - price) <= 0:
                    continue
                if any(side * (bars[i]["close"] - price) / values.value("atr1m", i) < -0.20
                       for i in range(index + 1, last + 1)):
                    continue
                distances = [abs(extreme - point) / atr for point in (price, vwap, ema)]
                near = [distance for distance in distances if distance <= 0.30]
                if len(near) < 2:
                    continue
                return {"method": "breakout and retest", "breakout_ts": candle["ts"],
                        "level": price, "retest_ts": retest["ts"], "extreme": extreme,
                        "confluence_count": len(near),
                        "setup_strength": min(_strength(excess, 0.15, 0.50), _clip(1 - abs(offset) / 0.35)),
                        "confluence_strength": _clip(((len(near) - 2) + 1 - max(near) / 0.30) / 2)}, None
        except _Unavailable as exc:
            unavailable = str(exc)
    if side * (extreme - vwap) / atr > 0.25 or side * (retest["close"] - vwap) <= 0:
        return None, unavailable or "no completed breakout/retest or VWAP retest"
    start = last - 1
    if start < 0:
        return None, "insufficient pullback history"
    while start > 0 and side * (bars[start]["close"] - bars[start - 1]["close"]) <= 0:
        start -= 1
    try:
        if any(side * (bars[i]["close"] - values.value("vwap", i)) / values.value("atr1m", i) < -0.10
               for i in range(start, last + 1)):
            return None, "VWAP pullback canceled by a closed minute candle"
    except _Unavailable as exc:
        return None, str(exc)
    swing_kind = "low" if side == 1 else "high"
    swings = [point for point in pivots if point["kind"] == swing_kind]
    candidates = [ema] + ([swings[-1]["price"]] if swings else [])
    near = [abs(extreme - price) / atr for price in candidates if abs(extreme - price) / atr <= 0.30]
    if not near:
        return None, "VWAP retest lacks EMA or confirmed swing confluence"
    return {"method": "VWAP retest", "breakout_ts": None, "level": vwap,
            "retest_ts": retest["ts"], "extreme": extreme, "pullback_start_ts": bars[start]["ts"],
            "confluence_count": len(near),
            "setup_strength": min(_clip(1 - abs(extreme - vwap) / (0.25 * atr)),
                                  _strength(side * (retest["close"] - vwap) / atr, 0, 0.50)),
            "confluence_strength": _clip(((len(near) - 1) + 1 - min(near) / 0.30) / 2)}, None


def _safe_text(value):
    text = str(value).replace("\n", " ").replace("\r", " ")
    return re.sub(r"\b(?:CALL|PUT)\b", lambda match: "bullish" if match[0].upper() == "CALL" else "bearish",
                  text, flags=re.IGNORECASE)


def evaluate_strategy(candles_by_timeframe: Mapping[int, Sequence[dict]], *, timestamp: float,
                      current_price: float | None, asset: str, signal_price: float | None = None,
                      indicators: dict | None = None, session_high: float | None = None,
                      session_low: float | None = None) -> dict:
    """Evaluate the active strategy and return a signal, diagnostics and exact report.

    The legacy seven-gate Part B path remains below; strategy mode can switch to
    the RSI crossover route while preserving the same return contract.
    """
    if STRATEGY_MODE == "rsi_cross":
        if not getattr(log, "_rsi_cross_startup_logged", False):
            log.info("[Strategy] active=rsi_cross")
            log._rsi_cross_startup_logged = True
        import strategies.rsi_cross as rsi_cross_module
        return rsi_cross_module.rsi_cross_signal(
            candles_by_timeframe=candles_by_timeframe,
            timestamp=timestamp,
            current_price=current_price,
            asset=asset,
            signal_price=signal_price,
            indicators=indicators,
            session_high=session_high,
            session_low=session_low,
        )
    fields = dict.fromkeys(REPORT_FIELDS, "unavailable")
    fields.update({"Asset": _safe_text(asset), "Current Price": current_price if _positive(current_price) else "unavailable",
                   "Expiry": "1 MINUTE", "Breakout Detected": "no", "Retest Detected": "no",
                   "Candlestick Confirmation": "not confirmed"})
    if _finite(timestamp):
        try:
            fields["Timestamp"] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    metrics = {"passed_gates": [], "component_strengths": {}, "component_scores": {key: 0.0 for key in WEIGHTS}}

    def score(component, strength):
        strength = _clip(strength)
        metrics["component_strengths"][component] = strength
        metrics["component_scores"][component] = WEIGHTS[component] * (0.6 + 0.4 * strength)

    def finish(gate=None, detail=None, side=None):
        confidence = min(100, math.floor(sum(metrics["component_scores"].values()) + 1e-9))
        if gate is None and confidence < CONFIDENCE_THRESHOLD:
            gate, detail = "confidence", f"score {confidence} is below {CONFIDENCE_THRESHOLD}"
        signal = "NO TRADE" if gate is not None else ("CALL" if side == 1 else "PUT")
        reason = (f"Gate {gate} failed: {detail}" if isinstance(gate, int)
                  else f"Confidence failed: {detail}" if gate == "confidence"
                  else "All seven gates passed; confirmed " + ("bullish" if side == 1 else "bearish") + " entry")
        fields.update({"Signal": signal, "Confidence": confidence, "Entry Reason": reason,
                       "Invalidation Reason": reason if gate is not None else "Entry expires after second 15 or a price drift above 0.15 ATR"})
        rendered = {name: value if name == "Signal" else _safe_text(value) for name, value in fields.items()}
        report = "\n".join(f"{name}: {rendered[name]}" for name in REPORT_FIELDS)
        # Include vwap mode in the log for Gate 1 diagnostics
        try:
            from config.settings import VWAP_VOLUME_MODE
            vmode = VWAP_VOLUME_MODE
        except Exception:
            vmode = "unknown"
        # Emit PartB info but only include full report when the signal or first_failed_gate changed.
        previous = getattr(log, "_last_partb_state", None)
        current_state = (gate, rendered["Signal"])
        if previous != current_state:
            log.info("[PartB] VWAP_MODE=%s first_failed_gate=%s\n%s", vmode, gate, report)
            setattr(log, "_last_partb_state", current_state)
        else:
            log.info("[PartB] VWAP_MODE=%s first_failed_gate=%s", vmode, gate)
        return {"signal": signal, "confidence": confidence, "first_failed_gate": gate,
                "reason": reason, "report": report, "fields": rendered, "metrics": metrics}

    if not _finite(timestamp) or fields["Timestamp"] == "unavailable":
        return finish(1, "invalid evaluation timestamp")
    if not isinstance(candles_by_timeframe, Mapping) or (indicators is not None and not isinstance(indicators, Mapping)):
        return finish(1, "invalid candle or indicator input")
    try:
        minutes = _closed(candles_by_timeframe.get(60), 60, timestamp)
        if len(minutes) < 10:
            raise _Unavailable("at least 10 closed minute candles required for VWAP side changes")
        last = len(minutes) - 1
        values = _Indicators(minutes, indicators or {})
        atr = values.value("atr1m", last)
        # record the currently active VWAP mode
        try:
            from config.settings import VWAP_VOLUME_MODE
            values.vwap_volume_mode = VWAP_VOLUME_MODE
        except Exception:
            pass
        vwap = values.value("vwap", last)
        historical_vwaps = [values.value("vwap", i) for i in range(last - 9, last + 1)]
        close = minutes[-1]["close"]
        dv = (close - vwap) / atr
        slope = (vwap - values.value("vwap", last - 5)) / atr
        sides = [1 if bar["close"] > point else -1 if bar["close"] < point else 0
                 for bar, point in zip(minutes[-10:], historical_vwaps)]
        nonzero = [side for side in sides if side]
        changes = sum(left != right for left, right in zip(nonzero, nonzero[1:]))
        metrics.update(atr1m=atr, vwap=vwap, dV=dv, vwap_slope=slope, vwap_sides=sides, whipsaw_changes=changes)
        fields.update({"VWAP": vwap, "Price vs VWAP": f"{dv:.6f} ATR"})
    except _Unavailable as exc:
        return finish(1, str(exc))
    side = 1 if dv >= 0.5 and slope >= 0.10 else -1 if dv <= -0.5 and slope <= -0.10 else 0
    if not side:
        return finish(1, "VWAP distance or slope does not confirm bias")
    aligned = sides[-8:].count(side)
    if aligned < 6:
        return finish(1, "fewer than six of the last eight closes on the bias side of their VWAP")
    if changes >= 3:
        return finish(1, "three or more VWAP side changes")
    fields["1-Minute Trend"] = "bullish" if side == 1 else "bearish"
    score("vwap", min(_strength(abs(dv), 0.5, 1.5), _strength(side * slope, 0.1, 0.3), _strength(aligned, 6, 8)))
    metrics["passed_gates"].append(1)
    # If Gate 1 passed, record which later gates would fail (for diagnostics).
    later_gate_failures = []
    def probe_gate(func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except _Unavailable as e:
            return str(e)
    # We'll run lightweight probes to surface which gate would fail next without raising.
    # Probe Gate 2..7 in sequence; stop at first failure recorded.
    try:
        # Probe Gate 2: EMA confirmation
        try:
            _ = values.value("ema8", last)
            _ = values.value("ema8", last - 3)
        except _Unavailable as e:
            later_gate_failures.append((2, str(e)))
        # Probe Gate 3: pivots
        try:
            pivots = confirmed_pivots(minutes)
            highs = [p for p in pivots if p["kind"] == "high"]
            lows = [p for p in pivots if p["kind"] == "low"]
            if len(highs) < 2 or len(lows) < 2:
                later_gate_failures.append((3, "two confirmed pivot highs and lows are required"))
        except Exception as e:
            later_gate_failures.append((3, str(e)))
    except Exception:
        pass
    metrics["later_gate_failures"] = later_gate_failures
    try:
        ema = values.value("ema8", last)
        ema_slope = (ema - values.value("ema8", last - 3)) / atr
    except _Unavailable as exc:
        return finish(2, str(exc))
    ema_distance = (close - ema) / atr
    metrics.update(ema8=ema, ema_slope=ema_slope, ema_distance=ema_distance)
    fields.update({"8 EMA": ema, "Price vs 8 EMA": f"{ema_distance:.6f} ATR"})
    if side * ema_distance < -0.3:
        return finish(2, "price is more than 0.30 ATR across EMA against the VWAP bias")
    if side * ema_distance < 0 or side * ema_slope < 0.10:
        return finish(2, "price or EMA slope does not confirm the bias")
    score("ema", _strength(side * ema_slope, 0.1, 0.3))
    metrics["passed_gates"].append(2)
    pivots = confirmed_pivots(minutes)
    highs = [point for point in pivots if point["kind"] == "high"]
    lows = [point for point in pivots if point["kind"] == "low"]
    metrics["pivots"] = pivots
    if len(highs) < 2 or len(lows) < 2:
        return finish(3, "two confirmed pivot highs and lows are required")
    progression = min(side * (highs[-1]["price"] - highs[-2]["price"]) / atr,
                      side * (lows[-1]["price"] - lows[-2]["price"]) / atr)
    if progression < 0.1:
        fields["Market Structure"] = "range or conflicting progression"
        return finish(3, "pivot highs and lows do not progress by at least 0.10 ATR")
    fields["Market Structure"] = "rising highs and lows" if side == 1 else "falling highs and lows"
    if (session_high is not None and not _positive(session_high)) or (session_low is not None and not _positive(session_low)):
        return finish(3, "invalid supplied session levels")
    if session_high is not None and session_low is not None and session_high < session_low:
        return finish(3, "session high is below session low")
    levels = _levels(pivots, last, atr, minutes, session_high, session_low)
    support = max((level["price"] for level in levels if level["price"] < close), default=None)
    resistance = min((level["price"] for level in levels if level["price"] > close), default=None)
    fields.update({"Important Support": support if support is not None else "unavailable",
                   "Important Resistance": resistance if resistance is not None else "unavailable"})
    opposing = resistance if side == 1 else support
    metrics.update(levels=levels, support=support, resistance=resistance, structure_progression=progression)
    if opposing is None:
        return finish(3, "no known opposing level to establish available room")
    room = side * (opposing - close) / atr
    metrics["room"] = room
    if room < 1.0:
        return finish(3, "nearest opposing level is less than 1.0 ATR away")
    score("structure", _strength(progression, 0.1, 0.5))
    metrics["passed_gates"].append(3)
    setup, setup_error = _setup(minutes, values, pivots, side, atr, vwap, ema, session_high, session_low)
    if setup is None:
        return finish(4, setup_error)
    metrics["setup"] = setup
    fields["Breakout Detected"] = "yes" if setup["breakout_ts"] is not None else "not required for VWAP retest"
    fields["Retest Detected"] = setup["method"]
    rejection = _rejection(minutes[-1], minutes[-2], side)
    rejection_source = "1m"
    if rejection is None:
        try:
            halves = _closed(candles_by_timeframe.get(30), 30, timestamp)
            if halves[-1]["ts"] >= minutes[-1]["ts"]:
                rejection = _rejection(halves[-1], halves[-2] if len(halves) >= 2 else None, side)
                rejection_source = "30s"
        except _Unavailable:
            pass
    if rejection is None:
        return finish(4, "no confirmed rejection on the latest minute or 30s candle")
    fields["Candlestick Confirmation"] = rejection_source + " " + rejection[0]
    score("setup", setup["setup_strength"])
    score("confluence", setup["confluence_strength"])
    score("candle", rejection[1])
    metrics["passed_gates"].append(4)
    if abs(ema_distance) > 1.2:
        return finish(5, "price is extended more than 1.2 ATR from EMA")
    if abs(dv) > 2 and all(side * (bar["close"] - bar["open"]) > 0 and (bar["high"] - bar["low"]) / atr >= 1.2
                          for bar in minutes[-3:]):
        return finish(5, "three large directional minute candles indicate chasing")
    if any((bar["high"] - bar["low"]) / atr > 2.5 for bar in minutes[-3:]):
        return finish(5, "a recent minute candle exceeds the 2.5 ATR spike limit")
    metrics["passed_gates"].append(5)
    votes, shorter = {}, {}
    for period in TIMEFRAMES:
        try:
            shorter[period] = _closed(candles_by_timeframe.get(period), period, timestamp)
        except _Unavailable as exc:
            return finish(6, str(exc))
        bar = shorter[period][-1]
        span = bar["high"] - bar["low"]
        clv = (2 * bar["close"] - bar["low"] - bar["high"]) / span if span else 0.0
        vote = 1 if bar["close"] > bar["open"] and clv >= 0.30 else -1 if bar["close"] < bar["open"] and clv <= -0.30 else 0
        votes[period] = vote
        fields[f"{period}s Direction"] = "bullish" if vote == 1 else "bearish" if vote == -1 else "neutral"
    aligned_votes = sum(vote == side for vote in votes.values())
    larger_aligned = sum(votes[period] == side for period in (15, 20, 30))
    fields["Lower-Timeframe Consensus"] = f"{aligned_votes}/5 aligned; {larger_aligned}/3 larger timeframes aligned"
    metrics["votes"] = votes
    if aligned_votes < 3 or larger_aligned < 2 or any(votes[period] == -side for period in (15, 20, 30)):
        return finish(6, "sub-minute votes lack the required alignment or include larger-timeframe opposition")
    atr15 = (indicators or {}).get("atr15s")
    if atr15 is None:
        atr15 = wilder_atr_series(shorter[15])[-1]
    if not _positive(atr15) or len(shorter[15]) < 3:
        return finish(6, "insufficient or invalid 15s ATR/burst history")
    metrics["atr15s"] = float(atr15)
    if any(side * (bar["close"] - bar["open"]) < 0 and abs(bar["close"] - bar["open"]) >= 1.5 * atr15
           for bar in shorter[15][-3:]):
        return finish(6, "opposing 15s burst reaches 1.5 ATR15s")
    score("subminute", (_strength(aligned_votes, 3, 5) + _strength(larger_aligned, 2, 3)) / 2)
    metrics["passed_gates"].append(6)
    seconds = timestamp - math.floor(timestamp / 60) * 60
    metrics["seconds_into_minute"] = seconds
    if not 5 <= seconds <= 15:
        return finish(7, "entry is outside seconds 5 through 15 of the new minute")
    if not _positive(current_price):
        return finish(7, "current price is unavailable or invalid")
    signal_price = close if signal_price is None else signal_price
    if not _positive(signal_price):
        return finish(7, "signal price is invalid")
    drift = abs(current_price - signal_price) / atr
    metrics.update(signal_price=signal_price, price_drift=drift)
    if drift > 0.15:
        return finish(7, "current price differs from signal price by more than 0.15 ATR")
    metrics["passed_gates"].append(7)
    return finish(side=side)
