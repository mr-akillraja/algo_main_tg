#!/usr/bin/env python3
"""Summarize the frozen signal log for demo-trade quality checks."""

from __future__ import annotations

import csv
import math
from pathlib import Path


LOG_PATH = Path(__file__).resolve().parents[1] / "data" / "signals_log.csv"


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 0.0
    p = successes / trials
    denominator = 1.0 + (z ** 2) / trials
    center = (p + (z ** 2) / (2 * trials)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) / trials) + ((z ** 2) / (4 * trials ** 2)))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _trade_rows(rows):
    return [row for row in rows if str(row.get("taken", "")).lower() == "true" and str(row.get("outcome", "")).lower() in {"win", "loss", "draw"}]


def _baseline_rows(rows):
    return [row for row in rows if str(row.get("baseline", "")).lower() == "true" and str(row.get("outcome", "")).lower() in {"win", "loss", "draw"}]


def _winning_outcomes(rows):
    return [row for row in rows if str(row.get("outcome", "")).lower() == "win"]


def main():
    if not LOG_PATH.exists():
        print(f"No signal log found at {LOG_PATH}")
        return 0
    with LOG_PATH.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    actual = _trade_rows(rows)
    baseline = _baseline_rows(rows)
    if not actual:
        print("No executed trades in the signal log.")
        return 0

    wins = len(_winning_outcomes(actual))
    total = len(actual)
    payout = 0.8
    if actual:
        payout_values = [_safe_float(row.get("payout_pct")) for row in actual if row.get("payout_pct") not in (None, "")]
        if payout_values:
            payout = max(0.0, sum(payout_values) / len(payout_values))
    win_rate = wins / total if total else 0.0
    break_even = 1.0 / (1.0 + payout)
    lower, upper = _wilson_interval(wins, total)
    net_pnl = sum(_safe_float(row.get("pnl")) for row in actual)

    losses = [row for row in actual if str(row.get("outcome", "")).lower() == "loss"]
    streak = 0
    longest = 0
    for row in actual:
        if str(row.get("outcome", "")).lower() == "loss":
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    by_hour = {}
    for row in actual:
        hour = int(row.get("hour_of_day", 0) or 0)
        bucket = by_hour.setdefault(hour, {"wins": 0, "trades": 0})
        bucket["trades"] += 1
        if str(row.get("outcome", "")).lower() == "win":
            bucket["wins"] += 1

    random_rows = _baseline_rows(rows)
    random_win_total = len([row for row in random_rows if str(row.get("outcome", "")).lower() == "win"])
    random_total = len(random_rows)
    random_win_rate = (random_win_total / random_total) if random_total else 0.0

    print(f"total trades: {total}")
    print(f"win rate: {win_rate:.2%}")
    print(f"break-even win rate: {break_even:.2%}")
    print(f"95% CI (Wilson): {lower:.2%} to {upper:.2%}")
    print(f"net P&L: {net_pnl:.2f}")
    print(f"longest losing streak: {longest}")
    print("win rate by hour of day:")
    for hour in sorted(by_hour):
        bucket = by_hour[hour]
        rate = bucket["wins"] / bucket["trades"] if bucket["trades"] else 0.0
        print(f"  {hour:02d}:00 -> {rate:.2%} ({bucket['wins']}/{bucket['trades']})")
    print(f"random baseline win rate: {random_win_rate:.2%} ({random_win_total}/{random_total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
