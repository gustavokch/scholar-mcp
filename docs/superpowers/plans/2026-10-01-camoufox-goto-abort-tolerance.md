# Camoufox Interstitial-Abort Tolerance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Camoufox browser fallback in scholar-mcp survive the `NS_BINDING_ABORTED` goto race and mid-redirect `content()` reads that bot-shield interstitials (Cloudflare, Bunny CDN) cause.

**Architecture:** Extract the two primitives already proven in `scholar_mcp/medical/pediatrics.py` (tolerant goto, settled content read) into a shared `scholar_mcp/utils/browser.py`, then adopt them in `medical/brazil_moh.py` (`_camoufox_search`) and `providers/scihub.py` (`_fetch_via_camoufox`), and cut pediatrics over to the shared helper (deleting its private `_goto`). Site-specific challenge-marker logic stays in each caller; only the generic race tolerance is shared.

**Tech Stack:** Python 3.10+, Camoufox/Playwright async API, pytest (asyncio_mode=auto), respx.

**Spec:** None — bug fix. Root cause (from the 2026-10-01 systematic-debugging session on the AAP 403 failure):

> Cloudflare/Bunny interstitials navigate the page *themselves* (`__cf_chl_rt_tk` → `__cf_chl_tk` → `?autologincheck=redirected`). When one of those navigations races an in-flight `page.goto`, Playwright raises `Error: Page.goto: NS_BINDING_ABORTED; maybe frame was detached?`. While the interstitial JS is mid-redirect, `page.content()` raises `Error: Page.content: Unable to retrieve content because the page is navigating and changing the content`. Observed live against `publications.aap.org`: the race is intermittent (same goto returned `403 OK` in one run, aborted in another), and the challenge took ~10.5 s to clear. The pediatrics fix is commit-ready in the working tree and serves as the reference implementation.

## Global Constraints

- NEVER import `playwright` at module scope or depend on its `Error` type: tests fake the `playwright.async_api` module (`_install_fake_playwright` in `tests/medical/test_pediatrics.py`), so any import of it breaks the suite. Match aborts by message substring `"NS_BINDING_ABORTED"` against `str(exc)` on a caught `Exception`.
- Poll budgets are **virtual accumulated ms** (`waited += poll_ms` around `page.wait_for_timeout`), never `time.monotonic()` deadlines: the fake pages' `wait_for_timeout` returns instantly, so wall-clock deadlines would make tests take real seconds. The callers' outer `asyncio.wait_for` still bounds real time.
- Repo venv is Python 3.10 (`~/Git/scholar-mcp/.venv`); no 3.11+ syntax.
- Tests need no `@pytest.mark.asyncio` (`asyncio_mode = "auto"` in pyproject.toml).
- Commit style: conventional commits with scope, e.g. `feat(utils): ...`, `fix(medical): ...` (see `git log --oneline`).
- Run tests with `~/Git/scholar-mcp/.venv/bin/python -m pytest` from `~/Git/scholar-mcp`.

---

### Task 1: Shared `utils/browser.py` helpers

**Files:**
- Create: `src/scholar_mcp/utils/browser.py`
- Test: `tests/utils/test_browser.py` (create; `tests/utils/` already exists)

**Interfaces:**
- Produces (all later tasks consume these exact signatures):
  - `async def goto_tolerant(page, target: str, timeout_ms: int) -> None` — swallows only aborts whose message contains `NS_BINDING_ABORTED`; re-raises everything else.
  - `async def read_content_settled(page, timeout_ms: int, poll_ms: int = 500) -> str` — retries `page.content()` while it raises; returns `""` when the virtual budget is exhausted.

- [ ] **Step 1: Write the failing tests**

Create `tests/utils/test_browser.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/utils/test_browser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scholar_mcp.utils.browser'`

- [ ] **Step 3: Implement `utils/browser.py`**

Create `src/scholar_mcp/utils/browser.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/utils/test_browser.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/utils/browser.py tests/utils/test_browser.py
git commit -m "feat(utils): add interstitial-tolerant camoufox goto/content helpers"
```

---

### Task 2: brazil_moh `_camoufox_search` adopts the helpers

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` (imports near line 1-30; `_camoufox_search._run` at lines 1638-1650; constants near line 140)
- Test: `tests/medical/test_brazil_moh.py` (`_install_fake_camoufox` at line 2288; add test after `test_search_guidelines_falls_back_to_camoufox_on_persistent_403`)

**Interfaces:**
- Consumes: `goto_tolerant(page, target, timeout_ms)`, `read_content_settled(page, timeout_ms)` from Task 1.
- Produces: nothing new; `_camoufox_search(self, composed: str, count: int, ceiling: float | None) -> list[dict[str, Any]]` signature unchanged.

Current failure shape: `page.goto` (brazil_moh.py:1647) raises `NS_BINDING_ABORTED` when the Bunny shield redirect races it → escapes `_run` → outer `except Exception` logs and returns `[]` — the whole browser tier spuriously fails.

- [ ] **Step 1: Extend the fake harness with `abort_goto`**

In `tests/medical/test_brazil_moh.py`, change `_install_fake_camoufox` (line 2288) — signature and `goto`:

```python
def _install_fake_camoufox(monkeypatch, rendered_html="", abort_goto=False):
    """Fake camoufox.async_api; returns (attempts, captured_urls, exits, sleeps).

    Copied from tests/medical/test_pediatrics.py — no conftest exists to
    share it through. ``rendered_html`` is what page.content() returns.
    ``abort_goto`` makes goto raise NS_BINDING_ABORTED, the race the Bunny
    shield interstitial causes when its own redirect aborts the in-flight
    navigation. The production helper matches on the message, not the
    playwright Error type: that module is faked out in tests.
    """
```

and inside `_FakePage`:

```python
        async def goto(self, url, *a, **k):
            captured_urls.append(url)
            if abort_goto:
                raise RuntimeError(
                    "Page.goto: NS_BINDING_ABORTED; maybe frame was detached?"
                )
            self.url = url
            return None
```

(Keep the rest of the harness byte-identical; it has no `hang_s`/`first_landing_url` params — do not port those.)

- [ ] **Step 2: Write the failing test**

Add after `test_search_guidelines_falls_back_to_camoufox_on_persistent_403` in `tests/medical/test_brazil_moh.py`:

```python
async def test_camoufox_search_tolerates_aborted_goto(tmp_path, monkeypatch):
    """A Bunny-shield redirect racing the goto aborts it with
    NS_BINDING_ABORTED; the JSON payload must still be read once the page
    settles instead of the whole browser tier returning []."""
    settings = Settings(
        cache_ttl_seconds=3600,
        enable_browser_fallback=True,
        brazil_browser_fallback=True,
        request_timeout=5,
    )
    http_client = AsyncHttpClient(
        settings, max_retries=2, backoff_base=0.01, min_429_wait=0.0
    )
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client, cache, settings)
    payload = {
        "diaServerResponse": [
            {
                "response": {
                    "docs": [
                        {"id": "1", "ti": "Manejo da dengue",
                         "pais_publicacao": "^eBrasil", "da": "202401",
                         "ur": ["https://bvsms.saude.gov.br/x.pdf"]},
                    ]
                }
            }
        ]
    }
    attempts, urls, _exits, _sleeps = _install_fake_camoufox(
        monkeypatch, json.dumps(payload), abort_goto=True
    )
    try:
        docs = await engine._camoufox_search("dengue", 10, None)
        assert [d["ti"] for d in docs] == ["Manejo da dengue"]
        assert attempts == [True]
    finally:
        await cache.close()
```

(`json`, `Settings`, `AsyncHttpClient`, `SQLiteCacheManager`, `BrazilMoHEngine` are all already imported by this test file — verify at the top before adding imports.)

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py::test_camoufox_search_tolerates_aborted_goto -v`
Expected: FAIL — `docs == []` (abort propagated out of `_run`, caught by the outer `except Exception`)

- [ ] **Step 4: Implement the adoption**

In `src/scholar_mcp/medical/brazil_moh.py`:

a) Add the import with the other `scholar_mcp` imports at the top:

```python
from scholar_mcp.utils.browser import goto_tolerant, read_content_settled
```

b) Add a constant next to `_CAMOUFOX_NAV_TIMEOUT_MS` (line 140):

```python
# After an aborted goto the shield page settles within seconds; the tier's
# outer wait_for(effective_ceiling) still bounds the whole attempt.
_CAMOUFOX_CONTENT_SETTLE_MS = 5000
```

c) In `_camoufox_search._run` (lines 1646-1650), replace:

```python
                page = await browser.new_page()
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=nav_timeout_ms
                )
                content = await page.content()
```

with:

```python
                page = await browser.new_page()
                await goto_tolerant(page, target, nav_timeout_ms)
                content = await read_content_settled(
                    page, _CAMOUFOX_CONTENT_SETTLE_MS
                )
```

Do NOT change the surrounding budget/ceiling logic or the JSON-parse challenge-marker handling — an empty `content` falls into the existing non-JSON branch and returns `[]`, which is the correct degradation.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -v -k camoufox`
Expected: all PASS, including the new test and `test_search_guidelines_falls_back_to_camoufox_on_persistent_403`

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "fix(medical): tolerate interstitial-aborted goto in brazil_moh camoufox tier"
```

---

### Task 3: scihub `_fetch_via_camoufox` adopts the helpers

**Files:**
- Modify: `src/scholar_mcp/providers/scihub.py` (constants at lines 14-15; `_fetch_via_camoufox._try_mirrors` at lines 145-186)
- Test: `tests/test_search_scihub_providers.py` (`_install_fake_camoufox` at line 409; add test after `test_scihub_camoufox_uses_final_page_url_as_referer`, ~line 646)

**Interfaces:**
- Consumes: `goto_tolerant(page, target, timeout_ms)`, `read_content_settled(page, timeout_ms)` from Task 1.
- Produces: nothing new; `_fetch_via_camoufox(self, clean_doi: str) -> tuple[bytes | None, str | None]` signature unchanged.

Current failure shape: the per-mirror `except Exception: continue` (scihub.py:184) already swallows the abort, but it **spuriously skips the mirror** — an aborted goto means the interstitial is working and the landing page is readable moments later. Same for a `content()` read that races the redirect.

- [ ] **Step 1: Extend the fake harness with `abort_goto`**

In `tests/test_search_scihub_providers.py`, change `_install_fake_camoufox` (line 409) — signature:

```python
def _install_fake_camoufox(
    monkeypatch,
    rendered_html="",
    pdf_bytes=b"%PDF-1.5-fake-data",
    final_url=None,
    browser_status=200,
    abort_goto=False,
):
```

and `_FakePage.goto` (line 439):

```python
        async def goto(self, url, *a, **k):
            captured_urls.append(url)
            if abort_goto:
                # The production helper matches on the message, not the
                # playwright Error type: that module is faked out in tests.
                raise RuntimeError(
                    "Page.goto: NS_BINDING_ABORTED; maybe frame was detached?"
                )
            # A real page reports the URL it landed on after redirects.
            self.url = final_url or url
            return None
```

- [ ] **Step 2: Write the failing test**

Add after `test_scihub_camoufox_uses_final_page_url_as_referer`:

```python
@respx.mock
async def test_scihub_camoufox_tolerates_aborted_goto(client, monkeypatch):
    """A bot-shield interstitial navigates the page itself; when that
    redirect races the goto, Playwright raises NS_BINDING_ABORTED. The abort
    means the interstitial is working, not that the mirror failed: the
    landing page is still read once it settles and the mirror is not
    skipped."""
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(403))
    rendered_html = '<html><embed src="https://sci-pdf.org/paper.pdf" type="application/pdf"/></html>'
    fake = _install_fake_camoufox(
        monkeypatch, rendered_html=rendered_html, abort_goto=True
    )
    settings = Settings(enable_browser_fallback=True)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)

    pdf_bytes, pdf_url = await provider._fetch_via_camoufox("10.1038/test")

    assert pdf_bytes == b"%PDF-1.5-fake-data"
    assert pdf_url == "https://sci-pdf.org/paper.pdf"
    assert fake.urls == ["https://mirror1.org/10.1038/test"]
```

Note for the implementer: after the abort the fake's `page.url` stays `""`; `_landing_url("", mirror_url)` falls back to `mirror_url` (same falsy path as the existing `None` case at line 472-485), so the absolute `embed src` still resolves to `https://sci-pdf.org/paper.pdf`.

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_search_scihub_providers.py::test_scihub_camoufox_tolerates_aborted_goto -v`
Expected: FAIL — `(None, None)` because the abort made the per-mirror `except` skip the only mirror

- [ ] **Step 4: Implement the adoption**

In `src/scholar_mcp/providers/scihub.py`:

a) Add the import with the other `scholar_mcp` imports:

```python
from scholar_mcp.utils.browser import goto_tolerant, read_content_settled
```

b) Replace the magic `15000` with named constants next to `_CAMOUFOX_MAX_MIRRORS` (lines 14-15):

```python
_CAMOUFOX_GOTO_TIMEOUT_MS = 15000
# After an aborted goto the interstitial page settles within seconds; the
# tier's outer wait_for(_CAMOUFOX_TOTAL_TIMEOUT) still bounds everything.
_CAMOUFOX_CONTENT_SETTLE_MS = 5000
```

c) In `_try_mirrors` (lines 151-156), replace:

```python
                        await page.goto(
                            mirror_url,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        content = await page.content()
```

with:

```python
                        await goto_tolerant(
                            page, mirror_url, _CAMOUFOX_GOTO_TIMEOUT_MS
                        )
                        content = await read_content_settled(
                            page, _CAMOUFOX_CONTENT_SETTLE_MS
                        )
```

Do NOT remove the per-mirror `except Exception: continue` — it still handles dead mirrors; the helpers only convert the spurious skips into successes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_search_scihub_providers.py -v -k camoufox`
Expected: all PASS, including the new test and the four pre-existing camoufox tests

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/providers/scihub.py tests/test_search_scihub_providers.py
git commit -m "fix(providers): tolerate interstitial-aborted goto in scihub camoufox tier"
```

---

### Task 4: Cut pediatrics over to the shared helper

**Files:**
- Modify: `src/scholar_mcp/medical/pediatrics.py` (imports at lines 8-12; `_goto` static method ~lines 311-330; two call sites in `_camoufox_scrape._run` at ~lines 374 and 387)
- Test: `tests/medical/test_pediatrics.py` (no changes — existing `test_camoufox_tolerates_aborted_goto_during_challenge` covers the behavior)

**Interfaces:**
- Consumes: `goto_tolerant(page, target, timeout_ms)` from Task 1.
- Produces: nothing. Removes `PediatricsEngine._goto`. Keeps `_await_challenge_clear` unchanged — its per-poll marker check (`_looks_like_challenge` + `autologincheck` URL) is AAP-specific and does not belong in the shared module.

Clean cutover: one tolerant-goto implementation in the package, not two.

- [ ] **Step 1: Replace the call sites**

In `src/scholar_mcp/medical/pediatrics.py`:

a) Add to the imports at the top:

```python
from scholar_mcp.utils.browser import goto_tolerant
```

b) In `_camoufox_scrape._run`, replace both occurrences of:

```python
                await self._goto(page, target)
```

with:

```python
                await goto_tolerant(page, target, _NAV_TIMEOUT_MS)
```

c) Delete the now-dead `_goto` static method (the `~20`-line `@staticmethod` block directly after `_settle`).

Leave `_await_challenge_clear` and the guarded post-renavigation `content()` read exactly as they are.

- [ ] **Step 2: Run the pediatrics suite**

Run: `.venv/bin/python -m pytest tests/medical/test_pediatrics.py -v`
Expected: all 29 PASS — in particular `test_camoufox_tolerates_aborted_goto_during_challenge` (the fake raises `RuntimeError` with the `NS_BINDING_ABORTED` message, which `goto_tolerant` matches) and `test_camoufox_waits_on_results_not_the_clock` (zero sleeps on the happy path)

- [ ] **Step 3: Commit**

```bash
git add src/scholar_mcp/medical/pediatrics.py
git commit -m "refactor(medical): cut pediatrics over to shared goto_tolerant helper"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (baseline before this work: `tests/medical` alone was 608 passed; the full suite must show zero new failures)

- [ ] **Step 2: Live smoke against the real Cloudflare host**

The AAP host is the one live site known to trigger the race. From `~/Git/zimqa` (its venv has camoufox's browser binary installed):

```bash
cd ~/Git/zimqa && .venv/bin/python - <<'EOF'
import asyncio, sys, tempfile, pathlib
sys.path.insert(0, "/Users/gus/Git/scholar-mcp/src")
from scholar_mcp.config import Settings
from scholar_mcp.medical.pediatrics import PediatricsEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

async def main():
    settings = Settings.load()
    http = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=pathlib.Path(tempfile.mktemp(suffix=".db")), settings=settings)
    eng = PediatricsEngine(http_client=http, cache=cache, settings=settings, jitter_range=None)
    items, meta = await eng.search_aap_guidelines(
        "roseola infantum exanthem subitum HHV-6 etiology diagnosis"
    )
    print("n:", len(items), "error:", meta.error)
    assert items and not meta.error
    await cache.close(); await http.aclose()

asyncio.run(main())
EOF
```

Expected: `n: 1 error: False` (or more results; key is no exception and `error: False`). There is no equivalent live smoke for BVS/Sci-Hub — do not hit Sci-Hub mirrors from tests; their unit fakes are the coverage.
