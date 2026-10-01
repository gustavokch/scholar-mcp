"""Shared helpers for Camoufox/Playwright pages behind bot-shield interstitials.

Cloudflare and Bunny CDN interstitials navigate the page themselves while a
goto is in flight, which aborts the navigation (NS_BINDING_ABORTED), and they
make content() raise while their JS is mid-redirect. Both conditions mean
"interstitial in progress", not failure. Message matching instead of
playwright's Error type keeps this module importable without playwright — the
test suite fakes the playwright.async_api module.
"""

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_ABORT_ERROR = "NS_BINDING_ABORTED"
# Playwright rewords only the retriable navigation race to this text:
# Frame.content() rethrows closed-target and evaluation errors unchanged.
# Matching it, not every Exception, keeps a dead page from being polled as
# "still busy". Like the abort match, it fails closed if the wording changes.
_NAVIGATING_ERROR = "page is navigating"

# Cloudflare interstitial markers. The title text is localised; the body
# carries stable platform divs.
_CLOUDFLARE_CHALLENGE_MARKERS = (
    "just a moment",
    "challenge-platform",
    "cf-browser-verification",
)


def looks_like_cloudflare_challenge(content: str) -> bool:
    """True when ``content`` is a Cloudflare interstitial, not the real page."""
    lowered = content.lower()
    return any(marker in lowered for marker in _CLOUDFLARE_CHALLENGE_MARKERS)


async def goto_tolerant(page, target: str, timeout_ms: int) -> bool:
    """Navigate, tolerating an interstitial redirect racing the goto.

    Returns True when the navigation was aborted (the page is on, or heading
    to, an interstitial) and False for a clean one. The abort is not a
    failure, but it does not mean the page is ready either: callers that can
    recover decide how long to keep reading (see ``read_content_settled``).
    """
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if _ABORT_ERROR not in str(exc):
            raise
        logger.debug("goto aborted by an interstitial redirect; continuing")
        return True
    return False


async def read_content_settled(
    page,
    timeout_ms: int,
    poll_ms: int = 500,
    *,
    ready: Callable[[str], bool] | None = None,
) -> str:
    """Return page content, polling while the page is not yet readable.

    Polls while content() raises Playwright's mid-navigation error and, when
    ``ready`` is given, while it rejects the content (typically the
    interstitial itself). Any other error propagates: a closed page is a
    fault, not a race. ``timeout_ms=0`` is one tolerant read.

    The budget is virtual accumulated ms, not wall clock, so tests with
    instant fake sleeps stay fast; callers' outer wait_for bounds real time.
    When it runs out the last readable content is returned ("" if there was
    none), so the caller can still classify what it saw.
    """
    waited = 0
    content = ""
    while True:
        try:
            content = await page.content()
        except Exception as exc:
            if _NAVIGATING_ERROR not in str(exc):
                raise
            logger.debug("content unreadable mid-navigation (%s); retrying", exc)
        else:
            if ready is None or ready(content):
                return content
        if waited >= timeout_ms:
            return content
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms
