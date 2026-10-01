"""Interstitial-race tolerance for the shared Camoufox page helpers."""

import pytest

from scholar_mcp.utils.browser import goto_tolerant, read_content_settled


class _AbortGotoPage:
    """goto raises the exact Playwright abort an interstitial redirect causes."""

    def __init__(self):
        self.goto_calls = 0

    async def goto(self, url, *args, **kwargs):
        self.goto_calls += 1
        raise RuntimeError(
            "Page.goto: NS_BINDING_ABORTED; maybe frame was detached?"
        )


class _TimeoutGotoPage:
    async def goto(self, url, *args, **kwargs):
        raise TimeoutError("Page.goto: Timeout 15000ms exceeded")


class _FlakyContentPage:
    """content() raises `failures` times (mid-redirect), then returns html."""

    def __init__(self, failures: int, html: str = "<html>ok</html>"):
        self._failures = failures
        self._html = html
        self.sleeps: list[int] = []

    async def content(self):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page "
                "is navigating and changing the content."
            )
        return self._html

    async def wait_for_timeout(self, ms):
        self.sleeps.append(ms)


async def test_goto_tolerant_swallows_interstitial_abort():
    page = _AbortGotoPage()
    await goto_tolerant(page, "https://example.org/x", 15000)
    assert page.goto_calls == 1


async def test_goto_tolerant_reraises_other_navigation_errors():
    with pytest.raises(TimeoutError):
        await goto_tolerant(_TimeoutGotoPage(), "https://example.org/x", 15000)


async def test_read_content_settled_retries_while_mid_redirect():
    page = _FlakyContentPage(failures=2)
    assert await read_content_settled(page, timeout_ms=5000) == "<html>ok</html>"
    assert page.sleeps == [500, 500]


async def test_read_content_settled_gives_up_within_budget():
    page = _FlakyContentPage(failures=999)
    assert await read_content_settled(page, timeout_ms=1000) == ""
    # 1s budget at 500ms polls: exactly two sleeps, no unbounded looping.
    assert page.sleeps == [500, 500]
