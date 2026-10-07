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
    s._feed_watchdog_active = False

    async def run_watchdog():
        # call the method under test in isolation
        await runtime.MultiAgentTradingSystem._feed_watchdog_loop(s)

    # run for a short time using timeout
    try:
        asyncio.get_event_loop().run_until_complete(asyncio.wait_for(run_watchdog(), timeout=0.5))
    except asyncio.TimeoutError:
        pass

    # Ensure guards were invoked at least once during the loop
    assert s.bot._ensure_chart_visible_and_close_panels.await_count >= 1
    assert s.bot.resubscribe_quote_stream.await_count >= 1
