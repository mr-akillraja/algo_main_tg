"""
bot/browser.py
Playwright-based browser controller for Olymptrade.

Handles:
  • Login with session persistence (avoids re-login every run)
  • Asset selection
  • Fixed Time Trade (FTT) — click UP/DOWN + set duration
  • Forex/CFD trade        — click BUY/SELL + set stop-loss
  • Screenshot on every action for audit trail
"""
import asyncio
import json
import uuid
import logging
import os
import re
import time
import math
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PWTimeout

from config import settings
from models.predictor import Signal

log = logging.getLogger("browser")

ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = os.path.join(ROOT, "data", "session.json")
SCREENSHOT_DIR = os.path.join(settings.LOG_DIR, "screenshots")
PLATFORM_URL = getattr(settings, "PLATFORM_URL", "https://olymptrade.com/platform")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)


def _asset_to_url_slug(asset: Optional[str]) -> str:
    """Normalize an asset name to a simple URL slug used by the UI."""
    if not asset:
        return ""
    s = str(asset).strip()
    s = s.replace("/", "-")
    s = s.replace(" ", "-")
    return re.sub(r"[^0-9A-Za-z\-]", "", s).lower()


def _asset_display_name(asset: Optional[str]) -> str:
    """Return the user-facing asset name used by the Olymptrade asset search."""
    if not asset:
        return ""
    asset_str = str(asset).strip()
    has_otc = asset_str.upper().endswith("OTC")
    base = re.sub(r"[\s_-]*OTC$", "", asset_str, flags=re.IGNORECASE) if has_otc else asset_str
    cleaned = re.sub(r"[^A-Za-z0-9/]+", "", base).upper()

    if "/" in cleaned:
        display = cleaned.replace("/", "/")
    else:
        if len(cleaned) == 6:
            display = f"{cleaned[:3]}/{cleaned[3:6]}"
        else:
            display = cleaned

    if has_otc and not display.endswith("OTC"):
        display = f"{display} OTC"

    return display


def _normalized_asset(asset: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(asset).upper())


class OrderUncertainError(RuntimeError):
    """A click may have submitted an order; never retry it automatically."""


class OlymtradeBot:
    """
    Async Playwright controller.

    Use as an async context manager:

        async with OlymtradeBot() as bot:
            await bot.place_ftt_trade(Signal.BUY, 60)
    """

    def __init__(self):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in = False
        self.last_order = None
        self._selected_asset = None
        self._order_lock = asyncio.Lock()
        self._native_candles = {}
        self._native_forming = {}
        self._native_requests = {}
        self._native_responses = {}
        self._quote_observations = {}

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    def _page_origin(self, page=None):
        page = page or self._page
        if page is None:
            return ""
        try:
            if page.is_closed():
                return "about:blank"
            return page.url or "about:blank"
        except Exception:
            return "about:blank"

    def _attach_page_handlers(self, page: Optional[Page]) -> None:
        if page is None:
            return

        def _on_ws(socket):
            socket.on("framereceived", self._receive_market_frame)
            socket.on("close", lambda ws: log.warning(
                "[FeedWS] drop ts=%s url=%s",
                datetime.utcnow().isoformat(timespec="seconds"),
                getattr(ws, "url", "unknown"),
            ))
            socket.on("socketerror", lambda exc: log.warning(
                "[FeedWS] drop ts=%s url=%s",
                datetime.utcnow().isoformat(timespec="seconds"),
                getattr(socket, "url", "unknown"),
            ))

        def _on_response(response):
            return None

        def _on_console(message):
            return None

        page.on("websocket", _on_ws)
        page.on("response", _on_response)
        page.on("console", _on_console)
        page.on("pageerror", lambda exc: log.warning("[Feed] page_error %s", exc))

    async def _add_init_scripts(self, page: Optional[Page]) -> None:
        if page is None:
            return
        await page.add_init_script("""(() => {
            const nativeSocket = window.WebSocket;
            window.__tgHistorySockets = Array.isArray(window.__tgHistorySockets) ? window.__tgHistorySockets : [];
            window.__tgWsCloses = Array.isArray(window.__tgWsCloses) ? window.__tgWsCloses : [];
            const pushClose = (socket, event) => {
                const entry = {
                    url: socket && socket.url ? socket.url : "",
                    code: event && Object.prototype.hasOwnProperty.call(event, 'code') ? event.code : null,
                    reason: event && event.reason ? event.reason : "",
                    wasClean: !!(event && event.wasClean),
                    ts: Date.now(),
                };
                window.__tgWsCloses.push(entry);
                if (window.__tgWsCloses.length > 50) {
                    window.__tgWsCloses.splice(0, window.__tgWsCloses.length - 50);
                }
            };
            window.WebSocket = class extends nativeSocket {
                constructor(...args) {
                    super(...args);
                    window.__tgHistorySockets.push(this);
                    this.addEventListener('close', (event) => pushClose(this, event));
                }
                send(data) {
                    try {
                        const packets = JSON.parse(data);
                        if (Array.isArray(packets) && packets.some(p => p.e === 10 && p.t === 2))
                            this.__tgHistory = true;
                    } catch (_) {}
                    return super.send(data);
                }
            };
        })();""")
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    async def drain_ws_closes(self):
        page = self._page
        if page is None:
            return []
        try:
            rows = await page.evaluate("""() => {
                const rows = Array.isArray(window.__tgWsCloses) ? window.__tgWsCloses.slice() : [];
                window.__tgWsCloses = [];
                return rows;
            }""")
        except Exception:
            return []
        if not rows:
            return []
        for row in rows:
            log.warning(
                "[FeedWS] close code=%s reason=%s clean=%s url=%s",
                row.get("code"), row.get("reason"), row.get("wasClean"), row.get("url"),
            )
        return rows

    async def _init_platform_page(self, asset: Optional[str] = None, timeout: float = 30.0) -> bool:
        asset = asset or settings.ASSET
        platform_scheme = urlparse(PLATFORM_URL).scheme
        if platform_scheme == "file":
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            page = self._page
            if page is None or page.is_closed():
                return False
            url = self._page_origin(page)
            page_netloc = urlparse(url).netloc if url and url != "about:blank" else ""
            platform_netloc = urlparse(PLATFORM_URL).netloc
            if url == "about:blank" or page_netloc != platform_netloc:
                try:
                    await page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    return False
            if await self.login():
                try:
                    if await self.select_asset(asset):
                        await self._ensure_chart_visible_and_close_panels()
                        quote = await self.get_current_quote(asset)
                        if quote is not None and self._quote_is_fresh(quote):
                            return True
                except Exception:
                    pass
            await asyncio.sleep(1)
        return False

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=settings.HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        if os.path.exists(SESSION_FILE):
            self._ctx = await self._browser.new_context(
                storage_state=SESSION_FILE,
                viewport={"width": 1366, "height": 768},
            )
            log.info("Loaded saved browser session")
        else:
            self._ctx = await self._browser.new_context(viewport={"width": 1366, "height": 768})

        self._page = await self._ctx.new_page()
        self._attach_page_handlers(self._page)
        await self._add_init_scripts(self._page)
        await self._page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30_000)
        started = await self._init_platform_page(timeout=60.0)
        if not started or self._page_origin(self._page) == "about:blank":
            await self._page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30_000)

    async def stop(self):
        if self._ctx:
            try:
                try:
                    # Prefer direct write via Playwright API (may open a new target internally)
                    await self._ctx.storage_state(path=SESSION_FILE)
                    log.info("Browser session saved")
                except Exception:
                    # Fallback: obtain state dict and write it without asking Playwright to open targets
                    state = await self._ctx.storage_state()
                    try:
                        import json

                        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
                            json.dump(state, fh)
                        log.info("Browser session saved (fallback) to %s", SESSION_FILE)
                    except Exception as e:
                        log.warning(f"Could not write browser session fallback: {e}")
            except Exception as e:
                log.warning(f"Could not save browser session: {e}")
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                log.warning(f"Browser close failed during shutdown: {e}")
        if self._pw:
            try:
                await self._pw.stop()
            except Exception as e:
                log.warning(f"Playwright stop failed during shutdown: {e}")

    async def is_alive(self) -> bool:
        """Health check used by the supervisor: True if the page is responsive."""
        try:
            if not self._page or self._page.is_closed():
                return False
            state = await self._page.evaluate("() => document.readyState")
            return state in ("interactive", "complete")
        except Exception:
            return False

    async def _ensure_chart_visible_and_close_panels(self, timeout: float = 5.0) -> dict:
        """Ensure chart is visible: close trades/tech panels and verify timeframe.

        Returns a dict with keys: verified(bool), closed_panels(list), reason(str).
        """
        page = self._page
        closed = []
        try:
            # Close trades panel if visible
            trades_close = page.locator('[data-test="cor-w-panel-trades"] [data-test="cor-w-panel-close"]')
            if await trades_close.count() and await trades_close.is_visible():
                try:
                    await trades_close.click(timeout=1500)
                    closed.append('trades_panel')
                except Exception:
                    pass
            # Close tech analysis panel if visible
            tech_close = page.locator('[data-test="cor-w-panel-techAnalysisList"] [data-test="cor-w-panel-close"]')
            if await tech_close.count() and await tech_close.is_visible():
                try:
                    await tech_close.click(timeout=1500)
                    closed.append('tech_panel')
                except Exception:
                    pass
            # After closing, verify timeframe selector is visible
            tf = page.locator('[data-test="TimeFrameSelector.selected"]')
            if not await tf.count() or not await tf.is_visible():
                return {"verified": False, "closed_panels": closed, "reason": "timeframe_unavailable"}
            return {"verified": True, "closed_panels": closed, "reason": "ok"}
        except Exception as exc:
            return {"verified": False, "closed_panels": closed, "reason": f"exception:{type(exc).__name__}"}

    async def login(self) -> bool:
        """Log in with email/password, or fallback to manual Google Sign-In."""
        page = self._page
        if page is None:
            return False

        try:
            if page.url and page.url.startswith("file://"):
                self._logged_in = True
                log.info("Using mock platform page")
                return True
        except Exception:
            pass

        try:
            if await page.locator(".platform-chart, .trade-panel, .chart-wrapper, [data-test='account-balance-title']").count() > 0:
                log.info("Already logged in (session reused)")
                self._logged_in = True
                return True
        except Exception:
            pass

        if os.path.exists(SESSION_FILE):
            try:
                await page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30_000)
                if await page.locator('[data-test="account-balance-title"]').count() > 0:
                    self._logged_in = True
                    log.info("Verified saved platform session")
                    return True
            except Exception:
                log.info("Saved session requires login")
        try:
            try:
                await page.goto("https://olymptrade.com", wait_until="load", timeout=30_000)
            except PWTimeout:
                log.warning("Page load timed out; falling back to commit.")
                await page.goto("https://olymptrade.com", wait_until="commit", timeout=30_000)
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Navigation failed: {e}")
            return False

        try:
            if await page.locator(".platform-chart, .trade-panel, .chart-wrapper").count() > 0:
                log.info("Already logged in (session reused)")
                self._logged_in = True
                return True
        except Exception:
            pass

        log.info("Logging in to Olymptrade...")

        has_credentials = bool(settings.EMAIL and settings.PASSWORD and "@" in settings.EMAIL)

        if has_credentials:
            opened = False
            for selector in [
                'button[data-test="action-sign-in"]',
                'button:has-text("Sign in")',
                'text=Sign in',
                'button:has-text("Log in")',
            ]:
                try:
                    await page.click(selector, timeout=2000, force=True)
                    opened = True
                    await asyncio.sleep(0.6)
                    break
                except Exception:
                    continue

            email_selectors = ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email"]', 'input[type="text"]']
            found_email = None
            for sel in email_selectors:
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=4000)
                    found_email = sel
                    break
                except Exception:
                    continue

            if not found_email:
                log.warning("Auto-login: email field not found after opening modal")
            else:
                try:
                    await page.fill(found_email, settings.EMAIL, timeout=4000)
                    pass_selectors = ['input[type="password"]', 'input[name="password"]']
                    found_pass = None
                    for ps in pass_selectors:
                        try:
                            await page.wait_for_selector(ps, state="visible", timeout=2000)
                            found_pass = ps
                            break
                        except Exception:
                            continue
                    if found_pass:
                        await page.fill(found_pass, settings.PASSWORD, timeout=3000)
                    await self._screenshot("before_login")
                    for btn in ['button[type="submit"]', '.login-btn', '.sign-in-btn', 'button:has-text("Sign in")', 'button:has-text("Log in")']:
                        try:
                            await page.click(btn, timeout=3000)
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(3)
                    if await page.locator(".platform-chart, .trade-panel, .chart-wrapper").count() > 0:
                        log.info("Automatic login successful!")
                        self._logged_in = True
                        try:
                            await self._ctx.storage_state(path=SESSION_FILE)
                        except Exception:
                            pass
                        return True
                except Exception as e:
                    log.warning(f"Auto-login failed: {e}")

        log.info("MANUAL SIGN-IN if required — waiting up to 180s")
        for i in range(180):
            try:
                url = page.url.lower()
                logged = (
                    await page.locator(".platform-chart, .trade-panel, .chart-wrapper, .sidebar, .user-avatar, [data-test='deposit-button']").count() > 0
                ) or any(k in url for k in ("platform", "cabinet", "dashboard"))
                if logged:
                    log.info(f"Login detected! URL={page.url}")
                    self._logged_in = True
                    try:
                        await self._ctx.storage_state(path=SESSION_FILE)
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
            if i % 15 == 0 and i > 0:
                log.info(f"Waiting for manual sign-in... ({180-i}s left) | {page.url}")

        log.error("Login timed out (180s)")
        await self._screenshot("login_timeout")
        return False

    async def select_asset(self, asset: str) -> bool:
        """Select the exact broker instrument and verify its active chart title."""
        if await self.get_current_quote(asset):
            return True
        code = _normalized_asset(asset)
        code = code[:-3] + "_OTC" if code.endswith("OTC") else code
        try:
            existing = self._page.locator(f'[data-test="asset-select-button-{code}/ftt"]')
            if await existing.count():
                await existing.click(timeout=3000)
            else:
                await self._page.locator('[data-test="assets-tabs-add-button"]').click(timeout=3000)
                search = self._page.locator('[data-test="asset-search-field"] input')
                await search.fill(_asset_display_name(asset).replace(" OTC", ""), timeout=3000)
                await self._page.locator(f'[data-test="asset-item-title-{code}"]').click(timeout=5000)
            for _ in range(50):
                if await self.get_current_quote(asset):
                    await self._page.keyboard.press("Escape")
                    log.info("[Browser] asset_verified=%s", asset)
                    return True
                await asyncio.sleep(0.1)
        except Exception as exc:
            log.error("[Browser] asset_selection_failed=%s reason=%s", asset, exc)
        return False

    async def _verify_chart_loaded(self, asset: str) -> bool:
        return await self.get_current_quote(asset) is not None

    async def set_amount(self, amount: float) -> bool:
        try:
            field = self._page.locator('[data-test="amount-input"] input')
            await field.fill(format(float(amount), "g"), timeout=2000)
            await field.press("Tab")
            actual = float((await field.input_value()).replace(",", "").strip())
            return math.isclose(actual, float(amount), rel_tol=0, abs_tol=0.00001)
        except Exception as exc:
            log.warning("[Order] amount_not_verified reason=%s", exc)
            return False

    async def _set_ftt_duration(self, seconds: int) -> bool:
        try:
            field = self._page.locator('[data-test="trade-time-duration-input"] input')
            actual = (await field.input_value()).strip().lower()
            if actual in self._ftt_duration_labels(seconds):
                return True
            label = f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"
            await field.fill(label, timeout=2000)
            await field.press("Tab")
            return (await field.input_value()).strip().lower() in self._ftt_duration_labels(seconds)
        except Exception as exc:
            log.warning("[Order] duration_not_verified reason=%s", exc)
            return False

    def _ftt_duration_labels(self, seconds: int) -> list[str]:
        labels = [f"{seconds}s", f"{seconds} sec"]
        if seconds % 60 == 0:
            labels += [f"{seconds // 60}m", f"{seconds // 60} min"]
        return labels

    @staticmethod
    def _money(text):
        normalized = text.replace(",", "").replace("−", "-")
        match = re.search(r"([+-]?)[^0-9]*([0-9]+(?:\.[0-9]+)?)([KM]?)", normalized)
        if not match:
            return None
        value = float(match.group(2)) * ({"K": 1000, "M": 1000000}.get(match.group(3), 1))
        return -value if match.group(1) == "-" else value

    async def _order_rows(self, include_history=False, wanted_id=None):
        """Read IDs and results from the broker's expanded trade cards."""
        panel = self._page.locator('[data-test="cor-w-panel-trades"]')
        if not await panel.is_visible():
            await self._page.locator('[data-test="sidebar-btn-trading-bar"]').click(timeout=2000)
        await panel.wait_for(state="visible", timeout=3000)
        # Active and settled cards share the misleading "deal-history-card-ftt"
        # data-test on the broker UI. Membership in active-deal-list identifies
        # open positions; the card's name cannot identify its lifecycle state.
        for _ in range(30):
            if await panel.locator('[data-test="active-deal-list"], [data-test="deal-list"]').count():
                break
            if "No active" in " ".join((await panel.inner_text()).split()):
                break
            await asyncio.sleep(0.1)
        scope = panel if include_history else panel.locator('[data-test="active-deal-list"]')
        headers = scope.locator('[data-test="deal-header"]')
        rows = []
        for index in range(await headers.count()):
            header = headers.nth(index)
            card = header.locator('xpath=ancestor::*[contains(@data-test, "card")][1]')
            if not await card.count():
                continue
            text = await card.inner_text()
            match = re.search(r"Trade ID\s+(\d+)", text)
            if not match:
                await header.click(timeout=1500)
                try:
                    await card.get_by_text("Trade ID", exact=True).wait_for(timeout=1500)
                except Exception:
                    continue
                text = await card.inner_text()
                match = re.search(r"Trade ID\s+(\d+)", text)
            if not match:
                continue
            trade_id = match.group(1)
            if wanted_id is not None and trade_id != str(wanted_id):
                continue
            asset_node = card.locator('[data-test^="asset-name-"]').first
            asset = (await asset_node.inner_text()).strip()
            amount_match = re.search(r"\bAmount\s+([^\n]+)", text)
            amount = self._money(amount_match.group(1)) if amount_match else None
            icon = await card.locator('[data-test="direction-icon"]').get_attribute("data-icon") or ""
            side = "BUY" if "up" in icon else "SELL" if "down" in icon else None
            duration_match = re.search(r"\bDuration\s+(\d+)\s*(min|sec|s|m)\b", text)
            duration = None
            if duration_match:
                duration = int(duration_match.group(1)) * (60 if duration_match.group(2) in {"min", "m"} else 1)
            closed = bool(re.search(r"\bClosed\b", text))
            pnl_node = card.locator('[data-test="pnl"]').first
            pnl = self._money(await pnl_node.inner_text()) if closed and await pnl_node.count() else None
            status = "OPEN"
            if closed and pnl is not None:
                status = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "DRAW"
            rows.append({"id": trade_id, "asset": asset, "amount": amount,
                         "direction": side, "duration": duration,
                         "status": status, "pnl": pnl})
            if wanted_id is not None:
                break
        return rows

    async def get_flat_ftt_account_evidence(self):
        """Verify the broker explicitly reports no active FTT trades/orders."""
        if not await self.is_demo_account():
            return None
        panel = self._page.locator('[data-test="cor-w-panel-trades"]')
        if not await panel.is_visible():
            await self._page.locator('[data-test="sidebar-btn-trading-bar"]').click(timeout=2000)
        await panel.wait_for(state="visible", timeout=3000)
        for _ in range(30):
            text = " ".join((await panel.inner_text()).split())
            active = panel.locator('[data-test="active-deal-list"] [data-test="deal-header"]')
            if await active.count():
                return None
            if ("No active Fixed Time trades or orders on this account" in text
                    and await self.is_demo_account()):
                return {"source": "platform_active_order_list", "demo": True,
                        "no_active_ftt": True, "observed_at": time.time()}
            await asyncio.sleep(0.1)
        return None

    async def place_ftt_trade(self, signal: Signal, duration_seconds: int, asset: str = None,
                              *, entry_deadline: Optional[float] = None) -> bool:
        """Submit only while an optional UTC entry deadline remains valid.

        The deadline limits preflight and the click, not acknowledgement of a
        click already attempted. A click timeout still leaves its outcome unknown.
        """
        asset = asset or settings.ASSET
        async with self._order_lock:
            self.last_order = None
            if signal not in (Signal.BUY, Signal.SELL):
                return False
            if entry_deadline is not None:
                try:
                    if isinstance(entry_deadline, bool):
                        raise ValueError("Boolean deadline")
                    entry_deadline = float(entry_deadline)
                    if not math.isfinite(entry_deadline):
                        raise ValueError("Nonfinite deadline")
                except (TypeError, ValueError, OverflowError):
                    log.info("[Order] rejected=invalid_entry_deadline")
                    return False
                if time.time() >= entry_deadline:
                    log.info("[Order] rejected=entry_deadline_expired")
                    return False
            quote = await self.get_current_quote(asset)
            if not await self.is_demo_account() or not self._quote_is_fresh(quote):
                log.error("[Order] rejected=demo_or_asset_not_verified")
                return False
            if not await self._page.locator('[data-test="trade-form-ftt"]').is_visible():
                log.error("[Order] rejected=ftt_mode_not_verified")
                return False
            if not await self.set_amount(settings.TRADE_AMOUNT):
                return False
            if not await self._set_ftt_duration(duration_seconds):
                return False
            payout = await self.get_asset_payout(asset)
            if payout is None:
                return False
            button = self._page.locator('[data-test="deal-submit-button-' +
                ("up" if signal == Signal.BUY else "down") + '"]')
            if not await button.is_enabled():
                return False
            before = {row["id"] for row in await self._order_rows()}
            if before:
                log.info("[Order] rejected=existing_active_demo_order")
                return False
            # Revalidate the account and active instrument immediately before clicking.
            quote = await self.get_current_quote(asset)
            if not await self.is_demo_account() or not self._quote_is_fresh(quote):
                log.info("[Order] rejected=stale_quote_or_demo_not_verified")
                return False
            click_timeout_ms = 3000
            if entry_deadline is not None:
                remaining = entry_deadline - time.time()
                if remaining <= 0:
                    log.info("[Order] rejected=entry_deadline_expired")
                    return False
                # Playwright's actionability wait must fit the same entry window.
                # A zero timeout disables its limit, so reject expiry above first.
                click_timeout_ms = min(click_timeout_ms, remaining * 1000)
            try:
                await button.click(timeout=click_timeout_ms)
                for _ in range(50):
                    rows = [row for row in await self._order_rows() if
                            row["id"] not in before and
                            _normalized_asset(row.get("asset", "")) == _normalized_asset(asset) and
                            row.get("direction") == signal.value and
                            row.get("amount") == float(settings.TRADE_AMOUNT) and
                            row.get("duration") == duration_seconds]
                    if len(rows) == 1:
                        row = rows[0]
                        self.last_order = {"id": row["id"], "asset": asset,
                            "amount": float(settings.TRADE_AMOUNT), "duration": duration_seconds,
                            "payout": payout, "accepted_at": time.time(), "demo": True,
                            "confirmation_source": "platform_order_id"}
                        log.info("[Order] accepted_id=%s side=%s", row["id"], signal.value)
                        return True
                    await asyncio.sleep(0.1)
            except Exception as exc:
                raise OrderUncertainError("Click outcome requires broker reconciliation") from exc
            raise OrderUncertainError("No unique broker order acknowledgement after click")

    async def get_order_result(self, order):
        for row in await self._order_rows(include_history=True, wanted_id=order.get("id")):
            if row["id"] != order.get("id"):
                continue
            state = (row.get("status") or "").upper()
            if state not in {"WIN", "LOSS", "DRAW"} or row.get("pnl") is None:
                return None
            try:
                pnl = float(row["pnl"])
            except (ValueError, TypeError):
                return None
            if math.isfinite(pnl):
                return {"result": state, "pnl": pnl, "source": "platform"}
        return None

    async def place_forex_trade(self, signal: Signal, asset: str = None) -> bool:
        page = self._page
        try:
            await self._switch_trade_mode('forex')
            await self.set_amount(settings.TRADE_AMOUNT)
            await self._screenshot(f'forex_before_{signal.value.lower()}')
            if signal == Signal.BUY:
                await page.locator('button[data-test="buy-btn"], button:has-text("Buy")').first.click(timeout=5000)
                log.info(f"[Trade] Forex BUY placed | Asset: {asset}")
            else:
                await page.locator('button[data-test="sell-btn"], button:has-text("Sell")').first.click(timeout=5000)
                log.info(f"[Trade] Forex SELL placed | Asset: {asset}")
            await asyncio.sleep(0.8)
            await self._screenshot(f'forex_after_{signal.value.lower()}')
            try:
                if await page.locator('button:has-text("Confirm"), .confirm-btn').count() > 0:
                    await page.locator('button:has-text("Confirm"), .confirm-btn').first.click()
            except Exception:
                pass
            return True
        except Exception as e:
            log.error(f"[Trade] Forex trade failed: {e}")
            await self._screenshot('forex_error')
            return False

    async def _set_forex_stop_loss(self):
        page = self._page
        try:
            sl = page.locator('input[data-test="stop-loss"], .stop-loss-input')
            if await sl.count() > 0:
                await sl.first.fill(str(settings.STOP_LOSS_PCT))
        except Exception:
            pass

    async def _switch_trade_mode(self, mode: str):
        page = self._page
        try:
            if mode == 'ftt':
                sel = 'button:has-text("Fixed Time"), [data-test="ftt-tab"]'
            else:
                sel = 'button:has-text("Forex"), [data-test="forex-tab"]'
            try:
                await page.click(sel, timeout=1500)
                await asyncio.sleep(0.6)
            except Exception:
                pass
        except Exception:
            pass

    async def _screenshot(self, label: str):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOT_DIR, f"{ts}_{label}.png")
            if self._page:
                await self._page.screenshot(path=path, timeout=5000)
        except Exception:
            pass

    async def close_active_position(self, signal: Signal, entry_price: float) -> bool:
        page = self._page
        try:
            rows = ['.position-row', '.open-position', '.deal-row']
            for r in rows:
                try:
                    nodes = await page.locator(r).all()
                    for n in nodes:
                        try:
                            txt = await n.inner_text()
                            if (signal.value in txt or signal.name in txt) and str(entry_price)[:6] in txt:
                                try:
                                    btn = n.locator('button:has-text("Close"), button.close-btn')
                                    if await btn.count() > 0:
                                        await btn.first.click()
                                        await asyncio.sleep(0.5)
                                        await self._screenshot('early_close')
                                        return True
                                except Exception:
                                    continue
                        except Exception:
                            continue
                except Exception:
                    continue

            for sel in ['button:has-text("Close"), .close-position, .close-deal, button.close']:
                try:
                    await page.click(sel, timeout=1200)
                    await asyncio.sleep(0.4)
                    await self._screenshot('early_close_fallback')
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _receive_market_frame(self, data):
        """Capture only broker candle replies, never authentication/account frames."""
        try:
            packets = json.loads(data)
            if not isinstance(packets, list):
                return
            for packet in packets:
                if not isinstance(packet, dict) or not isinstance(packet.get("d"), list):
                    continue
                if packet.get("e", 10) != 10:
                    continue
                for item in packet["d"]:
                    if not isinstance(item, dict) or not isinstance(item.get("candles"), list):
                        continue
                    period = item.get("tf")
                    pair = item.get("p")
                    if period not in (30, 60) or not isinstance(pair, str):
                        continue
                    rows = []
                    for candle in item["candles"]:
                        try:
                            row = {key: float(candle[key]) for key in ("open", "high", "low", "close")}
                            row["ts"] = float(candle["t"])
                        except (ValueError, TypeError, KeyError):
                            continue
                        if not all(math.isfinite(value) for value in row.values()):
                            continue
                        if row["ts"] % period != 0 or row["low"] <= 0:
                            continue
                        if row["high"] < max(row["open"], row["close"]) or row["low"] > min(row["open"], row["close"]):
                            continue
                        rows.append(row)
                    key = (_normalized_asset(pair), period)
                    received_at = time.time()
                    self._native_candles[key] = (received_at, rows)
                    for row in rows:
                        if row["ts"] <= received_at < row["ts"] + period:
                            self._native_forming[key] = (received_at, dict(row))
                    request_id = packet.get("uuid")
                    if (isinstance(request_id, str)
                            and self._native_requests.get(request_id) == key):
                        # Keep the exact reply: a concurrent small refresh must
                        # not replace the payload of a bulk history request.
                        self._native_responses[request_id] = (key, received_at, rows)
        except (ValueError, TypeError, KeyError):
            return

    async def get_native_candles(self, asset: str, period: int, count: int = 180,
                                 *, response_timeout: float = 3.0, purpose: str = "closed_history"):
        """Request native OHLC on the already-authenticated chart connection.

        Event 10 and pair/size/to/limit are observed chart history messages.
        The explicit broker tf must match the requested interval. M1 is never
        converted to 30s; only bars closed when requested are returned.
        """
        if (period not in (30, 60) or count <= 0
                or not math.isfinite(response_timeout) or response_timeout <= 0):
            return None
        try:
            return await asyncio.wait_for(self._request_native_candles(
                asset, period, count, response_timeout=response_timeout, purpose=purpose),
                timeout=response_timeout + 0.2)
        except asyncio.TimeoutError:
            # Quote verification and the browser send must also be bounded:
            # otherwise a stalled page can suspend closed-history recovery.
            emit = log.debug if purpose == "live_quote" else log.warning
            emit("[NativeHistory] request=unavailable timeframe=%s purpose=%s timeout_seconds=%s",
                 period, purpose, response_timeout + 0.2)
            return None

    async def resubscribe_quote_stream(self) -> bool:
        """Attempt to re-establish the broker's WS quote stream by toggling asset selection."""
        try:
            # Try cancel/reopen the asset tab to force ws reconnection
            btn = self._page.locator('[data-test="assets-tabs-tab-selected"]')
            if await btn.count():
                await btn.click(timeout=1500)
                await asyncio.sleep(0.2)
            # Press escape to dismiss modals
            await self._page.keyboard.press('Escape')
            await asyncio.sleep(0.2)
            return True
        except Exception:
            return False

    async def _request_native_candles(self, asset: str, period: int, count: int,
                                      *, response_timeout: float, purpose: str):
        if await self.get_current_quote(asset) is None:
            return None
        import pandas as pd
        code = _normalized_asset(asset)
        pair = code[:-3] + "_OTC" if code.endswith("OTC") else code
        request_id = "tg-history-" + uuid.uuid4().hex
        requested_at = time.time()
        packet = {"t": 2, "e": 10, "uuid": request_id,
                  "d": [{"pair": pair, "size": period,
                         "to": int(requested_at), "limit": int(count) + 2}]}
        key = (code, period)
        self._native_requests[request_id] = key
        try:
            sent = await self._page.evaluate("""packet => {
                const socket = (window.__tgHistorySockets || []).find(s => s.__tgHistory && s.readyState === 1);
                if (!socket) return false;
                socket.send(JSON.stringify([packet]));
                return true;
            }""", packet)
            if not sent:
                log.warning("[NativeHistory] transport=unavailable timeframe=%s", period)
                return None
            deadline = time.monotonic() + response_timeout
            while True:
                response = self._native_responses.get(request_id)
                if response is not None and response[0] == key and response[1] >= requested_at:
                    # A request sent before a candle closes can contain its
                    # partial OHLC even if the reply arrives after the close.
                    rows = [row for row in response[2] if row["ts"] + period <= requested_at]
                    if not rows:
                        return None
                    frame = pd.DataFrame(rows).sort_values("ts").drop_duplicates("ts").tail(count)
                    frame.index = pd.to_datetime(frame.pop("ts"), unit="s", utc=True)
                    frame.index.name = "timestamp"
                    frame.attrs.update(source="broker_native_ws", asset=asset,
                                       timeframe_seconds=period, closed_only=True)
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # A lightweight live-quote request is not a loss of the
                    # completed history. Its caller reports feed state changes.
                    emit = log.debug if purpose == "live_quote" else log.warning
                    emit("[NativeHistory] response=unavailable timeframe=%s purpose=%s timeout_seconds=%s",
                         period, purpose, response_timeout)
                    return None
                await asyncio.sleep(min(0.05, remaining))
        finally:
            self._native_requests.pop(request_id, None)
            self._native_responses.pop(request_id, None)

    async def refresh_native_market_data(self, asset: str, *, response_timeout: float = 1.5):
        """Refresh both live native buckets with bounded, small history requests.

        This updates only browser caches. The caller's separate bulk history
        refresh remains responsible for confirmed strategy candles.
        """
        if not math.isfinite(response_timeout) or response_timeout <= 0:
            raise ValueError("Native refresh timeout must be finite and positive")

        async def refresh(period):
            try:
                await asyncio.wait_for(self.get_native_candles(
                    asset, period, count=2, response_timeout=response_timeout, purpose="live_quote"),
                    timeout=response_timeout + 0.2)
            except (asyncio.TimeoutError, ConnectionError):
                return period, False
            candle = self.get_native_forming_candle(asset, period)
            return period, bool(candle and 0 <= time.time() - candle["observed_at"] <= 2.0)

        return dict(await asyncio.gather(*(refresh(period) for period in (30, 60))))

    def get_native_forming_candle(self, asset: str, period: int):
        """Return a detached, provisional native row for the current bucket.

        Reading the cache does not refresh its receipt time. This partial OHLC
        can seed live monitoring, but must never be inserted as a closed bar.
        """
        if period not in (30, 60):
            return None
        cached = self._native_forming.get((_normalized_asset(asset), period))
        if cached is None:
            return None
        received_at, row = cached
        now = time.time()
        if not math.isfinite(received_at) or received_at > now:
            return None
        start = math.floor(now / period) * period
        if row["ts"] == start and start <= received_at < start + period:
            return {**row, "observed_at": received_at,
                    "source": "broker_native_ws", "provisional": True,
                    "timeframe_seconds": period}
        return None

    @staticmethod
    def _quote_is_fresh(quote, max_age=2.0):
        if not quote or quote.get("stale"):
            return False
        try:
            price = float(quote["price"])
            age = time.time() - float(quote["observed_at"])
        except (KeyError, TypeError, ValueError):
            return False
        return math.isfinite(price) and price > 0 and 0 <= age <= max_age

    async def get_current_quote(self, asset: str):
        if not await self.is_alive():
            return None
        try:
            title = await self._page.title()
            match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s+(.+?)(?:\s*\||$)", title)
            if not match or _normalized_asset(match.group(2)) != _normalized_asset(asset):
                return None
            price = float(match.group(1).replace(",", "."))
            if not math.isfinite(price) or price <= 0:
                return None
            read_at = time.time()
            code = _normalized_asset(asset)
            previous = self._quote_observations.get(code)
            # A responsive browser can keep showing a frozen title. Re-reading
            # the same value is not evidence of a newly received market price.
            observed = previous[1] if previous and previous[0] == price else read_at
            verified_update = bool(previous and (previous[0] != price or previous[2]))
            freshness = ("initial_observation_unverified" if not verified_update
                         else "unchanged_since_observation" if previous and previous[0] == price
                         else "price_change_observed")
            unchanged = previous is not None and previous[0] == price
            timestamp_kind = previous[3] if unchanged else "observation_time"
            evidence_source = (previous[4] if unchanged else
                               "title_price_change" if verified_update else None)
            for period in (30, 60):
                native = self.get_native_forming_candle(asset, period)
                if (native and native["close"] == price
                        and 0 <= read_at - native["observed_at"] <= 2.0
                        and (not verified_update or native["observed_at"] >= observed)):
                    observed = native["observed_at"]
                    verified_update = True
                    freshness = "native_forming_candle_received"
                    timestamp_kind = "broker_receipt_time"
                    evidence_source = "broker_native_ws"
            self._quote_observations[code] = (price, observed, verified_update,
                                              timestamp_kind, evidence_source)
            age = read_at - observed
            self._selected_asset = asset
            return {"price": price, "asset": asset, "ts": observed,
                    "observed_at": observed, "timestamp_kind": timestamp_kind,
                    "read_at": read_at, "age_seconds": age,
                    "stale": not verified_update or not 0 <= age <= 2.0,
                    "freshness": freshness, "freshness_source": evidence_source,
                    "source": "broker_browser_sample"}
        except Exception:
            return None

    async def get_current_price(self):
        quote = await self.get_current_quote(self._selected_asset or settings.ASSET)
        return quote["price"] if self._quote_is_fresh(quote) else None

    async def is_demo_account(self) -> bool:
        try:
            label = self._page.locator('[data-test="account-balance-title"]')
            return await label.is_visible() and (await label.inner_text()).strip().lower() == "demo account"
        except Exception:
            return False

    async def get_asset_payout(self, asset: str):
        if await self.get_current_quote(asset) is None:
            return None
        try:
            selected = self._page.locator('[data-test="assets-tabs-tab-selected"]')
            text = await selected.inner_text(timeout=2000)
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
            if not match:
                return None
            payout = float(match.group(1)) / 100.0
            return payout if 0 <= payout <= 1 else None
        except Exception:
            return None
