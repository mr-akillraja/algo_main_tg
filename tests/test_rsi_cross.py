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


def _signal(closes, *, entry_window="open", quote_status="fresh", current_price=_UNSET):
    price = closes[-1] if current_price is _UNSET else current_price
    return rsi_cross_signal(
        candles_by_timeframe={60: _make_bars(closes)},
        timestamp=60 * len(closes),
        current_price=price,
        asset="TEST",
        indicators={"entry_window": entry_window, "quote_status": quote_status},
    )


def test_rsi_cross_buy_signal_on_60_closed_candles():
    result = _signal(BUY_CLOSES, quote_status="stale")

    assert result["signal"] == "CALL"
    assert result["reason"] == (
        f"RSI14 crossed up through 35 (prev={result['metrics']['rsi_prev']:.2f} "
        f"now={result['metrics']['rsi_now']:.2f})"
    )
    assert result["reason"] != "stale_quote"
    assert result["metrics"]["rsi_prev"] < 35.0
    assert result["metrics"]["rsi_now"] >= 35.0


def test_rsi_cross_sell_signal_on_60_closed_candles():
    result = _signal(SELL_CLOSES, quote_status="stale")

    assert result["signal"] == "PUT"
    assert result["reason"] == (
        f"RSI14 crossed down through 65 (prev={result['metrics']['rsi_prev']:.2f} "
        f"now={result['metrics']['rsi_now']:.2f})"
    )
    assert result["reason"] != "stale_quote"
    assert result["metrics"]["rsi_prev"] > 65.0
    assert result["metrics"]["rsi_now"] <= 65.0


def test_rsi_cross_warmup_never_uses_50_default():
    result = _signal([100.0] * 20, quote_status="fresh")

    assert result["signal"] == "NO TRADE"
    assert result["reason"] == "warmup have=20 need=40"
    assert result["metrics"]["rsi_prev"] not in (50.0, 50)
    assert result["metrics"]["rsi_now"] not in (50.0, 50)
    assert "50.00" not in result["report"]


def test_rsi_cross_window_closed_gate_and_no_price_gate():
    window_closed = _signal(BUY_CLOSES, entry_window="closed")
    assert window_closed["signal"] == "NO TRADE"
    assert window_closed["reason"] == "window_closed"

    no_price = _signal(BUY_CLOSES, current_price=None)
    assert no_price["signal"] == "NO TRADE"
    assert no_price["reason"] == "no_price"
