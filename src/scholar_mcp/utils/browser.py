"""Shared helpers for Camoufox/Playwright pages behind bot-shield interstitials.

Cloudflare and Bunny CDN interstitials navigate the page themselves while a
goto is in flight, which aborts the navigation (NS_BINDING_ABORTED), and they
make content() raise while their JS is mid-redirect. Both conditions mean
"interstitial in progress", not failure. Message matching instead of
playwright's Error type keeps this module importable without playwright — the
test suite fakes the playwright.async_api module.
"""

import logging

logger = logging.getLogger(__name__)

_ABORT_ERROR = "NS_BINDING_ABORTED"


async def goto_tolerant(page, target: str, timeout_ms: int) -> None:
    """Navigate, tolerating an interstitial redirect racing the goto.

    The abort means the interstitial is working, not that the navigation
    failed: the page lands on the interstitial and the caller's settle /
    re-navigate logic takes it from there.
    """
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if _ABORT_ERROR not in str(exc):
            raise
        logger.debug("goto aborted by an interstitial redirect; continuing")


async def read_content_settled(page, timeout_ms: int, poll_ms: int = 500) -> str:
    """Return page content, retrying while the page is mid-redirect.

    The budget is virtual accumulated ms, not wall clock, so tests with
    instant fake sleeps stay fast; callers' outer wait_for bounds real time.
    Returns "" when the budget runs out — callers treat that like any other
    unreadable page.
    """
    waited = 0
    while True:
        try:
            return await page.content()
        except Exception:
            logger.debug("content unreadable mid-navigation; retrying")
        if waited >= timeout_ms:
            return ""
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms
