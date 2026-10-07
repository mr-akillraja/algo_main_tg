# Part B: VWAP and EMA8 strategy

Part B is the sole source of directional trade decisions. It evaluates closed
5s, 10s, 15s, 20s, 30s and 1m candles in Python, returns `CALL`, `PUT` or
`NO TRADE`, and uses a fixed confidence threshold of 75 for a one-minute expiry.
No LLM, trained model, reinforcement learning or alternative entry gate is used.

## Current live-data limitation

The existing protected data path builds only 30s, 60s and 300s OHLC candles and
does not retain real tick counts. It cannot supply Part B's complete inputs.
The strategy returns `NO TRADE` when required inputs are missing. It does not
manufacture smaller candles, treat price polling as tick volume, or substitute
equal weights for tick-weighted VWAP. Authoritative 5s, 10s, 15s and 20s candles
and either actual tick counts or the required precomputed indicator values are
needed before live Part B entries are possible. Browser automation, live price
ingestion and candle-building behavior remain protected.

The browser retains its existing `Signal.BUY`, `Signal.SELL`, `Signal.HOLD`
interface. A strategy-side adapter maps Part B's three outputs to that enum.
Account risk limits, settlement, order deduplication, data validity and browser
execution safeguards remain operational controls; they do not create signals.

## Indicator definitions

Only closed candles contribute to indicators and structure. The forming minute
supplies entry time and current price. Supplied authoritative indicators take
precedence over local calculations; historical indicator values are required
where slopes or historical VWAP comparisons depend on them.

All price distances below are divided by `ATR1m`, except the explicitly named
`ATR15s` burst threshold.

| Value | Definition |
| --- | --- |
| ATR1m | Wilder ATR(14) of 1m candles |
| ATR15s | ATR(14) of 15s candles |
| Typical price | `(high + low + close) / 3` |
| VWAP | `sum(typical_price * ticks) / sum(ticks)` over the last 60 1m candles |
| EMA8 | EMA of 1m closes, period 8 |
| dV | `(last_close1m - VWAP) / ATR1m` |
| VWAPslope | `(VWAP - VWAP_5_bars_ago) / ATR1m` |
| EMAslope | `(EMA8 - EMA8_3_bars_ago) / ATR1m` |
| CLV | `((close - low) - (high - close)) / (high - low)` |

Zero-range candles cannot establish a directional CLV, rejection or breakout.
Missing, invalid or insufficient inputs produce `NO TRADE` rather than a guess.

## Ordered hard gates

Evaluation stops at the first failed gate. Every evaluation, including a
`NO TRADE` result, is logged with that gate and its reason.

1. **VWAP bias.** Bullish requires `dV >= 0.5` and `VWAPslope >= 0.10`;
   bearish requires `dV <= -0.5` and `VWAPslope <= -0.10`. At least six of the
   last eight 1m closes must be on the bias side of VWAP. A pullback does not
   cancel bias when its close remains on that side. Three or more VWAP side
   changes among the last ten closes veto entry.
2. **EMA8.** Bullish requires close at or above EMA8 and slope at least `0.10`;
   bearish requires close at or below EMA8 and slope at most `-0.10`. A flat
   slope fails. Price on the opposing EMA8 side more than `0.30` away is a
   conflict.
3. **Structure and levels.** Confirmed 1m fractal pivots use two neighbors on
   each side. The last two highs and last two lows must both rise by at least
   `0.10` for bullish structure, or both fall by at least `0.10` for bearish
   structure. Use pivots from the last 60 bars, merge pivots within `0.25`,
   and require two touches or a session high/low. The nearest opposing level
   must leave at least `1.0` of room.
4. **Setup and rejection.** One of the two setups below must complete, with a
   rejecting last closed 1m candle or latest closed 30s candle. Confirmation
   requires a rejecting wick at least 40% of range, directional engulfing,
   or a close in the favorable 40% of range with a body at least 40% of range.
5. **Filters.** Reject an EMA8 distance greater than `1.2`. Reject chasing
   when the last three 1m candles all follow the trade direction, each has
   range at least `1.2`, and `abs(dV) > 2.0`. Reject any of the last three
   1m candles with range greater than `2.5`.
6. **Sub-minute votes.** For each of 5s, 10s, 15s, 20s and 30s, the latest
   closed candle votes bullish when close exceeds open and CLV is at least
   `0.30`; bearish when close is below open and CLV is at most `-0.30`;
   otherwise neutral. Require three of five votes aligned, at least two
   aligned among 15s/20s/30s, and none opposing among those three. An opposing
   15s candle in the latest three with body at least `1.5 * ATR15s` vetoes.
7. **Entry timing.** Entry is valid from 5 through 15 seconds after the new
   minute opens. All other times fail, including after 20 seconds. Current
   price may differ from the signal-price anchor by no more than `0.15`.

### Setup 1: breakout and retest

A closed 1m candle must break an already known level L by at least `0.15`,
with body/range at least 0.5 and range at most `2.0`. Retest must occur one
through six bars later. For bullish entry the retest low must be between
`L - 0.35` and `L + 0.25`, with close above L. For bearish entry, mirror those
bounds around L and require close below L. A 1m close more than `0.20` back
through L cancels the setup. At least two of L, VWAP and EMA8 must be within
`0.30` of the retest extreme.

The ordering is breakout, close, retest, rejection, sub-minute confirmation,
then entry. A level or pivot must already be confirmed when used; later
candles cannot retroactively establish a breakout.

### Setup 2: VWAP retest

After bias is established, a bullish retest requires low at or below
`VWAP + 0.25`, close above VWAP and no pullback close below `VWAP - 0.10`.
The bearish method mirrors these comparisons. At least one of EMA8 and the
confirmed swing low/high must lie within `0.30` of the retest extreme.
Rejection and subsequent confirmation are still required.

## Confidence

Weights are VWAP 20, EMA 15, structure 15, breakout/retest 20, level confluence
10, candle 10 and sub-minute 10. For each passed component the contribution
is `weight * (0.6 + 0.4 * strength)`, with strength clamped to `[0, 1]`.
A failed gate contributes zero. Confidence is reported as a whole number.
All seven hard gates must pass and confidence must be at least 75; waiting
longer never lowers that threshold.

The numerical strength anchors, session boundaries, pullback boundaries and
signal-price anchor are implementation conventions where the supplied Part B
does not prescribe a value. Their exact deterministic definitions are
documented with the strategy implementation and must remain consistent with
its tests; they must not be adjusted dynamically to increase trade frequency.

## Output contract

The report contains the following fields in this order and no additional fields:

```text
Asset:
Timestamp:
Current Price:
1-Minute Trend:
VWAP:
Price vs VWAP:
8 EMA:
Price vs 8 EMA:
Market Structure:
Important Support:
Important Resistance:
Breakout Detected:
Retest Detected:
Candlestick Confirmation:
5s Direction:
10s Direction:
15s Direction:
20s Direction:
30s Direction:
Lower-Timeframe Consensus:
Signal:
Confidence:
Expiry:
Entry Reason:
Invalidation Reason:
```

`Signal` is exactly `CALL`, `PUT` or `NO TRADE`. Other fields use bullish and
bearish language, never the directional signal words. `Expiry` is always
`1 MINUTE`. `Entry Reason` names the first failed gate for `NO TRADE`.

## Development environment

```bash
conda activate tg_algo
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

Validation is offline and does not require placing orders.
