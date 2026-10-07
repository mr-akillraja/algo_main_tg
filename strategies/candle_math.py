"""Candle validation and descriptive calculations without trading decisions."""

import math
from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float]:
    """Return an EMA seeded with the first observed value."""
    if not len(values):
        return []
    result = [float(values[0])]
    weight = 2.0 / (period + 1.0)
    for value in values[1:]:
        result.append(float(value) * weight + result[-1] * (1.0 - weight))
    return result


def confirmed_swings(candles: Sequence[dict]) -> tuple[list, list]:
    """Return strict pivots with two closed candles on each side."""
    highs, lows = [], []
    for index in range(2, len(candles) - 2):
        candle = candles[index]
        neighbours = list(candles[index - 2:index]) + list(candles[index + 1:index + 3])
        point = {"ts": candle["ts"], "index": index}
        if candle["high"] > max(other["high"] for other in neighbours):
            highs.append({**point, "price": float(candle["high"])})
        if candle["low"] < min(other["low"] for other in neighbours):
            lows.append({**point, "price": float(candle["low"])})
    return highs, lows


def valid_candles(candles: Sequence[dict]) -> bool:
    """Require positive finite OHLC values and ordered candle timestamps."""
    previous_ts = None
    for candle in candles:
        try:
            values = [float(candle[field]) for field in ("open", "high", "low", "close", "ts")]
        except (KeyError, TypeError, ValueError):
            return False
        opening, high, low, close, timestamp = values
        if not all(math.isfinite(value) for value in values):
            return False
        if low <= 0 or high < max(opening, close) or low > min(opening, close):
            return False
        if previous_ts is not None and timestamp <= previous_ts:
            return False
        previous_ts = timestamp
    return True
