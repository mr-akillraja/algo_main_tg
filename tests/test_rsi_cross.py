from __future__ import annotations

from strategies.rsi_cross import rsi_cross_signal


BUY_CLOSES = [
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    92.0, 90.0, 98.0, 87.0, 63.0, 56.0, 60.0, 57.0, 43.0, 61.0,
    46.0, 23.0, 29.0, 39.0, 26.0, 43.0, 29.0, 41.0, 19.0, 30.0,
]

SELL_CLOSES = [
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
    119.0, 110.0, 129.0, 145.0, 159.0, 139.0, 146.0, 156.0, 173.0, 189.0,
    191.0, 183.0, 200.0, 212.0, 194.0, 203.0, 192.0, 187.0, 204.0, 194.0,
]


def _make_bars(closes):
    return [
        {"ts": index * 60, "open": close, "high": close + 1.0, "low": close - 1.0, "close": close}
        for index, close in enumerate(closes)
    ]


_UNSET = object()


def _signal(closes, *, entry_window="open", quote_status="fresh", current_price=_UNSET, extra_indicators=None):
    price = closes[-1] if current_price is _UNSET else current_price
    indicators = {"quote_status": quote_status}
    if entry_window is not None:
        indicators["entry_window"] = entry_window
    if extra_indicators:
        indicators.update(extra_indicators)
    return rsi_cross_signal(
        candles_by_timeframe={60: _make_bars(closes)},
        timestamp=60 * len(closes),
        current_price=price,
        asset="TEST",
        indicators=indicators,
    )


def _assert_rsi_values(result, *, cross):
    assert result["metrics"]["cross"] == cross
    if result["metrics"]["rsi_prev"] is not None:
        assert isinstance(result["metrics"]["rsi_prev"], float)
        assert f"{result['metrics']['rsi_prev']:.2f}" in result["report"]
    if result["metrics"]["rsi_now"] is not None:
        assert isinstance(result["metrics"]["rsi_now"], float)
        assert f"{result['metrics']['rsi_now']:.2f}" in result["report"]


def test_rsi_cross_buy_signal_on_60_closed_candles():
    result = _signal(BUY_CLOSES, quote_status="fresh")

    assert result["signal"] == "CALL"
    assert result["reason"] == (
        f"RSI14 crossed up through 35 (prev={result['metrics']['rsi_prev']:.2f} "
        f"now={result['metrics']['rsi_now']:.2f})"
    )
    assert result["metrics"]["cross"] == "BUY"
    assert result["reason"] != "stale_quote"
    assert result["metrics"]["rsi_prev"] < 35.0
    assert result["metrics"]["rsi_now"] >= 35.0


def test_rsi_cross_sell_signal_on_60_closed_candles():
    result = _signal(SELL_CLOSES, quote_status="fresh")

    assert result["signal"] == "PUT"
    assert result["reason"] == (
        f"RSI14 crossed down through 65 (prev={result['metrics']['rsi_prev']:.2f} "
        f"now={result['metrics']['rsi_now']:.2f})"
    )
    assert result["metrics"]["cross"] == "SELL"
    assert result["reason"] != "stale_quote"
    assert result["metrics"]["rsi_prev"] > 65.0
    assert result["metrics"]["rsi_now"] <= 65.0


def test_rsi_cross_warmup_never_uses_50_default():
    result = _signal([100.0] * 20, quote_status="fresh")

    assert result["signal"] == "NO TRADE"
    assert result["reason"] == "warmup have=20 need=40"
    assert result["metrics"]["rsi_prev"] == "n/a(warmup)"
    assert result["metrics"]["rsi_now"] == "n/a(warmup)"
    assert result["metrics"]["cross"] == "none"
    assert "50.00" not in result["report"]


def test_rsi_cross_window_closed_and_no_cross_and_missing_window_and_already_signalled():
    window_closed = _signal(BUY_CLOSES, entry_window="closed")
    assert window_closed["signal"] == "NO TRADE"
    assert window_closed["reason"] == "window_closed"
    assert window_closed["metrics"]["cross"] == "BUY"
    assert window_closed["metrics"]["rsi_prev"] is not None
    assert window_closed["metrics"]["rsi_now"] is not None

    no_cross = _signal([100.0] * 60, entry_window="open")
    assert no_cross["signal"] == "NO TRADE"
    assert no_cross["reason"] == "no_cross"
    assert no_cross["metrics"]["cross"] == "none"
    assert no_cross["metrics"]["rsi_prev"] is not None
    assert no_cross["metrics"]["rsi_now"] is not None

    missing_window = _signal(BUY_CLOSES, entry_window=None)
    assert missing_window["signal"] == "NO TRADE"
    assert missing_window["reason"] == "no_window_info"

    no_price = _signal(BUY_CLOSES, current_price=None)
    assert no_price["signal"] == "NO TRADE"
    assert no_price["reason"] == "no_price"

    first = _signal(BUY_CLOSES, entry_window="open", extra_indicators={"entry_closed_ts": 10_000})
    second = _signal(BUY_CLOSES, entry_window="open", extra_indicators={"entry_closed_ts": 10_000, "already_signalled": True})
    assert first["signal"] == "CALL"
    assert second["signal"] == "NO TRADE"
    assert second["reason"] == "already_signalled"
