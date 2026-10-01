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


class _CleanGotoPage:
    async def goto(self, url, *args, **kwargs):
        return None


_MID_REDIRECT = (
    "Page.content: Unable to retrieve content because the page is navigating "
    "and changing the content."
)


class _ScriptedContentPage:
    """content() replays `reads`, one entry per call: an Exception is raised,
    a string returned; the last entry repeats."""

    def __init__(self, *reads):
        self._reads = list(reads)
        self.sleeps: list[int] = []

    async def content(self):
        entry = self._reads.pop(0) if len(self._reads) > 1 else self._reads[0]
        if isinstance(entry, Exception):
            raise entry
        return entry

    async def wait_for_timeout(self, ms):
        self.sleeps.append(ms)


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
    assert await goto_tolerant(page, "https://example.org/x", 15000) is True
    assert page.goto_calls == 1


async def test_goto_tolerant_reports_a_clean_navigation():
    assert await goto_tolerant(_CleanGotoPage(), "https://example.org/x", 15000) is False


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


async def test_read_content_settled_reraises_faults_that_are_not_the_navigation_race():
    page = _ScriptedContentPage(
        RuntimeError("Target page, context or browser has been closed")
    )
    with pytest.raises(RuntimeError, match="closed"):
        await read_content_settled(page, timeout_ms=5000)
    # A dead page is a fault, not a race: no budget is burned polling it.
    assert page.sleeps == []


async def test_read_content_settled_waits_until_ready():
    interstitial = "<html><title>Just a moment...</title></html>"
    page = _ScriptedContentPage(
        RuntimeError(_MID_REDIRECT), interstitial, interstitial, "<html>real</html>"
    )
    html = await read_content_settled(
        page, timeout_ms=5000, ready=lambda c: "Just a moment" not in c
    )
    assert html == "<html>real</html>"
    assert page.sleeps == [500, 500, 500]


async def test_read_content_settled_returns_last_readable_content_when_never_ready():
    interstitial = "<html><title>Just a moment...</title></html>"
    page = _ScriptedContentPage(interstitial)
    html = await read_content_settled(page, timeout_ms=1000, ready=lambda c: False)
    # The caller still gets to classify what it saw (challenge vs. blank).
    assert html == interstitial
    assert page.sleeps == [500, 500]


async def test_read_content_settled_without_a_budget_is_one_tolerant_read():
    page = _ScriptedContentPage(RuntimeError(_MID_REDIRECT))
    assert await read_content_settled(page, timeout_ms=0) == ""
    assert page.sleeps == []
