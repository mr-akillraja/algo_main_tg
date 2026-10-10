import asyncio
from unittest.mock import AsyncMock

import pytest

import multi_agent_orchestrator as runtime


def test_chart_guard_closes_panels_and_resubscribes(monkeypatch, tmp_path):
    s = runtime.MultiAgentTradingSystem.__new__(runtime.MultiAgentTradingSystem)
    # minimal wiring
    s.bot = AsyncMock()
    s.bot._ensure_chart_visible_and_close_panels = AsyncMock(return_value=True)
    s.bot.resubscribe_quote_stream = AsyncMock(return_value=True)
    s._feed_watchdog_active = True
    s._feed_outage_start = 1000.0
    s._quote = {"price": 1.0, "observed_at": 1000.0}
    s._feed_recovery_attempts = 0

    clock = {"now": 1010.0}
    monkeypatch.setattr(runtime.time, "time", lambda: clock["now"])

    async def fake_sleep(seconds):
        clock["now"] += max(0.0, seconds)

    monkeypatch.setattr(runtime.asyncio, "sleep", fake_sleep)

    async def run_watchdog():
        # call the method under test in isolation
        await runtime.MultiAgentTradingSystem._feed_watchdog_loop(s)

    # advance simulated time so the watchdog reaches the 10s recovery branch immediately
    try:
        asyncio.run(asyncio.wait_for(run_watchdog(), timeout=1.0))
    except asyncio.TimeoutError:
        pass

    # Ensure guards were invoked at least once during the loop
    assert s.bot._ensure_chart_visible_and_close_panels.await_count >= 1
    assert s.bot.resubscribe_quote_stream.await_count >= 1
