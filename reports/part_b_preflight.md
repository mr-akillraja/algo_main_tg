# Part B replacement: pre-deletion inventory

Date: 2026-10-08 (Asia/Kolkata)
Branch: `strategy/part-b-vwap-ema8`
Status: stopped before deletion because implementation crosses explicitly protected boundaries.

No existing source, tests, configuration, model artifacts, or browser files have
been changed or removed by this task. This report is the only added file.
The `tg_algo` conda environment exists; implementation checks will use it.

## Blocking conflicts found in the actual code

1. **Missing candle intervals.** `MultiTimeframeAgent.reset()` in
   `agents/agent_multi_timeframe.py:355` constructs only 30s, 60s, and 300s
   builders. `config/settings.py:32` enables only 30s and 60s active candles.
   Part B requires additional 5s, 10s, 15s, and 20s histories. These cannot
   be reconstructed faithfully from larger OHLC candles. Adding their builders
   changes candle building, which Part A expressly protects.
2. **Missing tick volumes.** `CandleBuilder.add_tick()` at
   `agents/agent_multi_timeframe.py:121` stores only OHLC, timestamp and source.
   `MultiTimeframeAgent.seed_from_dataframe()` at line 406 retains only OHLC
   values. `OlymtradeBot._receive_market_frame()` in `bot/browser.py:707`
   likewise retains only OHLC and timestamp. The available strategy data
   therefore cannot supply the required tick-weighted VWAP. Counting periodic
   price polls or assuming equal volume would change the specified strategy.
3. **Protected browser data path.** `OlymtradeBot._receive_market_frame()`
   at `bot/browser.py:702` and `get_native_candles()` at line 741 reject
   intervals other than 30s/60s. Extending this path would edit a protected
   browser file. Whether the upstream broker actually provides smaller native
   intervals or tick-volume fields has not been established. An alternative
   authorized source of real ticks or candles with tick counts is needed if
   this file must remain untouched.
4. **Different entry deadline.** `MultiAgentTradingSystem.CONFIRMATION_WINDOW`
   in `multi_agent_orchestrator.py:175` is five seconds. `_evaluate_and_trade()`
   enforces that window and passes its deadline to `safe_execute_trade()`
   at line 1171. Part B requires entry 5–15 seconds after the minute opens.
   The existing timing checks and deadline calculation must change, while
   preserving the browser's deadline enforcement and order mechanics.
5. **Different existing signal contract.** `models.predictor.Signal` defines
   `BUY`, `SELL`, `HOLD`, and `bot/browser.py` imports and consumes that enum.
   It does not currently receive literal `CALL`, `PUT`, `NO TRADE` strings.
   A nonbrowser adapter can map those three Part B signals to the existing
   enum without changing browser interfaces.
6. **Rules embedded in protected execution.** Besides the old evaluators,
   `_evaluate_and_trade()` has a five-minute trend veto; both
   `PipelineSupervisorAgent.safe_execute_trade()` and `RiskManager.approve()`
   enforce the old configurable confidence threshold. Removing those strategy
   vetoes requires narrow edits in functions that also handle execution.
   Account loss limits, unresolved-order protection and circuit breakers need
   an explicit boundary from the requested removal of all decision gates.

## Proposed removal/replacement inventory

This is an inventory for review, not a record of completed deletions.
Shared functions and files below must retain their protected responsibilities.

| File(s) | Proposed removal or replacement |
| --- | --- |
| `strategies/closed_candle.py` | Replace `evaluate_signal`, `evaluate_demo_continuation`, `evaluate_both`, RSI/EMA rules, old `_atr14`, and frozen-rule constants/hash. Retain reusable validation, EMA/pivot primitives where appropriate, and the logging helper `append_signal_log_row`. |
| `strategies/trend_filter.py` | Remove `evaluate_five_minute_trend` and its old veto. |
| `agents/agent_multi_timeframe.py` | Remove old indicator helpers `_ema`, `_rsi`, `_macd`, `_bollinger`, `_stoch`, `_ema_cross`, `SREngine`, `MultiTimeframeAgent.analyze`, `_analyze_tf`, `evaluate_chart_strength`, and strategy-only state/calls. Preserve public interfaces required by ingestion and browser consumers. Calls embedded in reset/seeding/tick handling require careful shared-code edits. |
| `agents/chart_analyzer.py`, `agents/multi_timeframe_consensus.py`, `agents/agent_regime_detector.py`, `agents/agent_rl_learning.py`, `agents/agent_strategic_framework.py` | Remove obsolete chart/indicator decisions, consensus scoring, regime selection, RL decisions and strategy agents after removing their consumers. |
| `models/predictor.py` | Remove `_build_features`, `_build_label`, `RandomForestPredictor`, `LSTMPredictor`, `EnsemblePredictor`, `get_predictor` and their imports. Retain `Signal` at its existing import path. |
| `utils/technical.py` | Remove legacy strategy indicators and `strategy_signal` after checking remaining monitoring consumers. |
| `multi_agent_orchestrator.py` | Replace strategy imports/initialization, `select_entry_evaluator`, `_calculate_consensus_score`, `_combine_confidences`, `_five_minute_filter`, obsolete Q-table tasks, and strategy portions of `_monitor_context`/`_evaluate_and_trade`. Preserve main-loop cadence, browser operations, accounting, logging infrastructure and settlement. Shared timing/data changes require confirmation. |
| `strategies/live_monitor.py` | Replace old strategy-specific diagnostics inside `analyze_forming`; retain data provenance/freshness and monitoring interfaces. |
| `config/settings.py` | Remove strategy-only `MODEL_TYPE`, `LOOKBACK_CANDLES`, `CANDLE_TIMEFRAME`, `RL_USE_IN_LIVE_TRADING`, `MODEL_DIR`, obsolete profit target, `DEMO_*`, `FROZEN_SIGNAL_RULES`, `FROZEN_SIGNAL_CONFIG_HASH` once dependencies are removed. Review shared confidence, training, timeframe and filter settings separately. |
| `tests/test_closed_strategy.py`, `tests/test_trend_filter.py` | Replace old rule assertions with Part B tests. |
| `tests/test_structure_gate.py`, `tests/test_short_entry_runtime.py`, `tests/test_monitoring_runtime.py`, `tests/test_entry_deadline.py`, `tests/test_live_monitor.py`, `tests/test_strategy_performance.py` | Remove obsolete strategy assertions while preserving execution, data, monitoring and accounting coverage. |

Strategy-only tooling proposed for removal, after checking shared dependencies:

- `scripts/strategy_and_analysis.py`, `scripts/train_predictors.py`,
  `scripts/scan_assets.py`, `train_rl.py`, `ab_test_qtables.py`,
  `analyze_rl_fixes.py`, `fast_track_config.py`,
  `integration_strategic_trading.py`, `monitor_strategy_dashboard.py`.
- Obsolete `models/*/rf_model.pkl`, `models/rl_qtable.json`,
  `models/backups/rl_qtable*.json`, and bytecode for removed strategy modules.
- Strategy reports `reports/train_report.json`,
  `reports/abtest_20260526_174933.json`, `reports/scan_*.json`.

Old strategy documentation proposed for removal or replacement of its strategy
content, retaining any applicable operational instructions:

- `STRATEGY_GUIDE.md`, `TRADING_STRATEGY_SUMMARY.md`,
  `FAST_TRACK_60_PERCENT_GUIDE.md`, `IMPLEMENTATION_PLAN.md`, `QUICK_START.md`,
  `ROOT_CAUSE_ANALYSIS.md`, `FIXES_SUMMARY.md`, `QUICK_REFERENCE.md`,
  `COMPLETE_CODE_GUIDE.md`, `COMPLETE_FIXED_CODE.md`, `FIXED_CODE_REFERENCE.md`,
  `COMPLETE_ANALYSIS_REPORT.md`, `FIX_SUMMARY_2026-05-31.md`.

Ambiguous/protected items requiring a boundary decision before deletion:

- `agents/agent_asset_selector.py`: legacy scoring is part of asset selection,
  which the request protects; preserve asset-selection behavior unless clarified.
- `agents/agent_pipeline_supervisor.py`, `bot/risk.py`: remove only obsolete
  strategy confidence vetoes if authorized; proposed default is to retain
  account-risk limits, circuit breakers and execution safeguards.
- `COMPLETE_UPDATED_multi_agent_orchestrator.py` and
  `backups/pre_unification_sell.py.txt`: archived strategy also contains browser
  calls/main-loop code; confirm whether the protected-code rule covers archives.
- Preserve operational reports, `MONITORING.md`, `ASSET_RECORDING_FIX.md`,
  `trade_analysis_visualization.py`, and `scripts/report.py` except for obsolete
  strategy-specific references that can be removed without changing operations.

## Planned addition after resolving the conflicts

Implement Part B as deterministic Python: Wilder ATR, tick-weighted rolling
VWAP, EMA8, confirmed fractal pivots/levels, seven ordered hard gates, integer
confidence with a fixed 75 threshold, and the exact requested report fields.
Log each evaluation and its first failed gate through existing logging
infrastructure. Map the final signal to the existing browser enum outside
browser code. No runtime LLM is necessary.

Document and confirm the strategy details that Part B leaves unspecified:
the strength=1 anchors for confidence components, session boundaries for
session high/low, pullback start/end rules, and the signal-price anchor.
These choices affect decisions and should not be hidden defaults.

Validation will cover indicator reference calculations, both trade directions,
threshold boundaries, gate order, setup sequencing and invalidation, absence of
look-ahead, all five sub-minute votes, exact entry timing, confidence, output
format and every-evaluation logging. Retain and run relevant existing browser,
feed, order and accounting tests using `conda run -n tg_algo`; no live orders are
needed for validation.

## Existing work and verification

The worktree was already dirty before this task, including `bot/browser.py`,
`bot/risk.py`, `config/settings.py`, `multi_agent_orchestrator.py`, old strategy
files, tests, bytecode and runtime data. Do not restore or overwrite those edits.
The initial tracked diff was **20 files changed, 665 insertions, 31 deletions**.
That diff is pre-existing work, not this task's implementation.

Browser source SHA-256 at audit:
`6ba2af221af90e8903e065e602ab20b37e7d2e68dee2db0996c67c98046cafda`.
Tracked-file hashes and the tracked diff fingerprint are saved in
`/tmp/part_b_preflight_baseline.json` for comparison.

No implementation tests were run because no implementation was changed.

## Confirmation needed

Please approve the removal/replacement inventory and resolve the protected-code
conflicts: authorize the required market-data/candle-building extensions and
strategy timing/threshold wiring (including browser market-data changes if
necessary), or identify an existing external source that already supplies all
six candle intervals and actual tick counts. Proposed preserved behavior is
login, asset selection, stake/expiry entry, button clicks, result reading,
browser setup/environment, order/account safeguards and main-loop cadence.
Data-source capability must be verified before claiming that Part B can run.
