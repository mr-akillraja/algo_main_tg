import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from bot.browser import OlymtradeBot
from multi_agent_orchestrator import MultiAgentTradingSystem


def test_page_recovery_detects_blank_and_drains_ws_closes(tmp_path, monkeypatch, caplog):
    platform = tmp_path / "platform.html"
    platform.write_text("<html><body>mock platform</body></html>", encoding="utf-8")
    platform_url = platform.as_uri()
    monkeypatch.setattr("bot.browser.PLATFORM_URL", platform_url)
    monkeypatch.setattr("multi_agent_orchestrator.PLATFORM_URL", platform_url)

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            bot = OlymtradeBot()
            bot._browser = browser
            bot._ctx = ctx
            bot._page = page
            await bot._add_init_scripts(page)

            await page.evaluate("""() => {
                window.__tgWsCloses = [{
                    url: 'wss://example/ws',
                    code: 1006,
                    reason: 'drop',
                    wasClean: false,
                    ts: Date.now(),
                }];
            }""")
            drained = await bot.drain_ws_closes()
            assert drained and len(drained) == 1
            assert await page.evaluate("() => window.__tgWsCloses.length") == 0

            await page.goto("about:blank")
            assert bot._page_origin(page) == "about:blank"

            system = MultiAgentTradingSystem.__new__(MultiAgentTradingSystem)
            system.running = True
            system.bot = bot

            async def fake_sleep(_):
                raise asyncio.CancelledError

            monkeypatch.setattr("multi_agent_orchestrator.asyncio.sleep", fake_sleep)
            try:
                await system._feed_watchdog_loop()
            except asyncio.CancelledError:
                pass

            assert "[Feed] page_lost url=about:blank" in caplog.text
            assert "[Feed] recovery attempt=" in caplog.text
            assert bot._page is not None
            assert bot._page.url != "about:blank"
            assert bot._page.url.startswith("file://") or bot._page.url == platform_url
            await browser.close()

    asyncio.run(run())
