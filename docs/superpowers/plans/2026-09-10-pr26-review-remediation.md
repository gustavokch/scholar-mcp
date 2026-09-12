# PR #26 Review Remediation — Expected-404 Log Suppression

## Goal

PR #26 stops `AsyncHttpClient.get` from emitting `WARNING` records when an upstream
scholarly registry answers `404` for a DOI it does not hold. The fix works, but it
reaches that outcome by passing `ok_statuses={404}` at five call sites. This
remediation keeps the behavior and repairs three problems the review surfaced:

1. Expected 404s now produce **no** log record at all, so a genuine defect that
   yields a 404 (broken DOI quoting, wrong base URL, bad query param) is
   indistinguishable from a real upstream miss.
2. `ok_statuses` conflates "hand this response back to the caller" with "do not
   warn". All five new sites discard the 404 response immediately, so only the
   second meaning is used — while the parameter's docstring still describes the
   first.
3. The new tests have no negative control, assert against every logger rather
   than the HTTP one, and never assert their respx routes were called.

## Architecture

`AsyncHttpClient.get` gains a second, narrower parameter:

| Parameter        | Meaning                                                              | Return for a matching status |
| ---------------- | -------------------------------------------------------------------- | ---------------------------- |
| `ok_statuses`    | Caller must inspect this response (e.g. `api.fda.gov` 404 = no match) | the `httpx.Response`         |
| `quiet_statuses` | Status is an expected miss; log at DEBUG instead of WARNING           | `None`                       |

Both paths log at `DEBUG` when the status is `>= 400`, so expected traffic stays
recoverable with `LOG_LEVEL=DEBUG`. Providers that only want silence use
`quiet_statuses` and keep their existing `resp is None` handling unchanged.

Separately, the Unpaywall lookup duplicated between `UnpaywallProvider.fetch_full_text`
and `WaterfallResolver.fetch_pdf_bytes` collapses into one provider method, so the
404 handling lives in exactly one place.

## Tech Stack

Python 3.10, httpx, respx, pytest, pytest-asyncio.

## Spec Reference

PR #26 review comment: https://github.com/gustavokch/scholar-mcp/pull/26#issuecomment-5623325358

---

## Task 1 — Add `quiet_statuses` and DEBUG logging for expected statuses

**Target files**
- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_expected_status_logging.py` (create)

**Consumes**: `httpx.Response.status_code`
**Produces**: `AsyncHttpClient.get(..., quiet_statuses=...)` returning `None` with a DEBUG record

### Step 1: Write failing test

```python
# tests/test_http_expected_status_logging.py
import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient

HTTP_LOGGER = "scholar_mcp.utils.http"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


def _http_records(caplog, level=None):
    return [
        r
        for r in caplog.records
        if r.name == HTTP_LOGGER and (level is None or r.levelname == level)
    ]


@respx.mock
async def test_quiet_status_returns_none_and_logs_debug(client, caplog):
    route = respx.get("https://example.org/missing").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await client.get("https://example.org/missing", quiet_statuses={404})

    assert route.called
    assert resp is None
    assert _http_records(caplog, "WARNING") == []
    assert len(_http_records(caplog, "DEBUG")) == 1


@respx.mock
async def test_ok_status_returns_response_and_logs_debug(client, caplog):
    route = respx.get("https://example.org/nomatch").mock(
        return_value=httpx.Response(404, text="No matches found")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await client.get("https://example.org/nomatch", ok_statuses={404})

    assert route.called
    assert resp is not None and resp.status_code == 404
    assert _http_records(caplog, "WARNING") == []
    assert len(_http_records(caplog, "DEBUG")) == 1


@respx.mock
async def test_unlisted_error_status_still_warns(client, caplog):
    route = respx.get("https://example.org/boom").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await client.get("https://example.org/boom", quiet_statuses={404})

    assert route.called
    assert resp is None
    assert len(_http_records(caplog, "WARNING")) == 1


@respx.mock
async def test_terminal_retryable_status_still_warns(client, caplog):
    route = respx.get("https://example.org/five").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await client.get("https://example.org/five", quiet_statuses={404})

    assert route.called
    assert resp is None
    assert len(_http_records(caplog, "WARNING")) == 1
```

### Step 2: Confirm failure

```bash
uv run pytest tests/test_http_expected_status_logging.py -v
```

Expect `TypeError: get() got an unexpected keyword argument 'quiet_statuses'`.

### Step 3: Minimal implementation

In `src/scholar_mcp/utils/http.py`, add the `quiet_statuses` parameter to `get`,
update the docstring to describe both parameters, and replace the status-handling
block so that:

- an `ok_statuses` match logs DEBUG when `>= 400`, then returns the response;
- a `quiet_statuses` match logs DEBUG and returns `None`;
- any other `>= 400` keeps the existing WARNING and returns `None`.

### Step 4: Confirm pass

```bash
uv run pytest tests/test_http_expected_status_logging.py -v
```

### Step 5: Commit

```bash
git add src/scholar_mcp/utils/http.py tests/test_http_expected_status_logging.py
git commit -m "feat(http): add quiet_statuses and debug-log expected error statuses"
```

---

## Task 2 — Migrate provider call sites to `quiet_statuses`

**Target files**
- Modify: `src/scholar_mcp/providers/crossref.py`, `src/scholar_mcp/providers/openalex.py`,
  `src/scholar_mcp/providers/semantic_scholar.py`, `src/scholar_mcp/providers/unpaywall.py`,
  `src/scholar_mcp/resolver.py`
- Test: existing PR tests (`tests/test_citations_references.py`, `tests/test_openalex_s2.py`,
  `tests/test_unpaywall_404_logging.py`, `tests/test_invalid_doi_logging.py`)

**Consumes**: `AsyncHttpClient.get(..., quiet_statuses=...)`
**Produces**: providers that return `None`/`[]` on 404 with no WARNING

### Step 1: Write failing test

No new test. Task 1's suite plus the PR's own tests are the contract. The existing
`resp is None or resp.status_code != 200` guards already cover the `None` return.

### Step 2: Confirm current state

```bash
uv run pytest tests/test_citations_references.py tests/test_openalex_s2.py \
  tests/test_unpaywall_404_logging.py tests/test_invalid_doi_logging.py -q
```

Green before and after — this task must not change observable behavior.

### Step 3: Minimal implementation

Replace `ok_statuses={404}` with `quiet_statuses={404}` at all five sites.

### Step 4: Confirm pass

```bash
uv run pytest tests/test_citations_references.py tests/test_openalex_s2.py \
  tests/test_unpaywall_404_logging.py tests/test_invalid_doi_logging.py -q
```

### Step 5: Commit

```bash
git add src/scholar_mcp/providers src/scholar_mcp/resolver.py
git commit -m "refactor(providers): use quiet_statuses for expected 404 misses"
```

---

## Task 3 — Forward `quiet_statuses` through `get_bytes`

**Target files**
- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_expected_status_logging.py`

**Consumes**: `AsyncHttpClient.get`
**Produces**: `get_bytes(..., quiet_statuses=...)`

### Step 1: Write failing test

```python
@respx.mock
async def test_get_bytes_forwards_quiet_statuses(client, caplog):
    route = respx.get("https://example.org/gone.pdf").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        data = await client.get_bytes("https://example.org/gone.pdf", quiet_statuses={404})

    assert route.called
    assert data is None
    assert _http_records(caplog, "WARNING") == []
```

### Step 2: Confirm failure

```bash
uv run pytest tests/test_http_expected_status_logging.py::test_get_bytes_forwards_quiet_statuses -v
```

### Step 3: Minimal implementation

Add `quiet_statuses` to `get_bytes` and pass it through to `self.get`.

### Step 4: Confirm pass

```bash
uv run pytest tests/test_http_expected_status_logging.py -v
```

### Step 5: Commit

```bash
git add src/scholar_mcp/utils/http.py tests/test_http_expected_status_logging.py
git commit -m "feat(http): forward quiet_statuses from get_bytes"
```

---

## Task 4 — Tighten the PR's caplog assertions and assert routes were called

**Target files**
- Modify: `tests/test_citations_references.py`, `tests/test_openalex_s2.py`,
  `tests/test_unpaywall_404_logging.py`, `tests/test_invalid_doi_logging.py`

**Consumes**: `caplog.records`, respx route objects
**Produces**: assertions scoped to `scholar_mcp.utils.http` with explicit route coverage

### Step 1: Rewrite the assertions

Replace every `assert len(caplog.records) == 0` with a logger-scoped check:

```python
assert [r for r in caplog.records if r.name == "scholar_mcp.utils.http"] == []
```

Bind each respx route to a name and assert it was called, so a mistyped mock URL
fails on its own terms instead of surfacing as a swallowed exception:

```python
route = respx.get("https://api.crossref.org/works/10.1093/humupd/dmab061").mock(
    return_value=httpx.Response(404, text="Resource not found.")
)
...
assert route.called
```

### Step 2: Confirm the tests still discriminate

Temporarily revert one provider to a bare `self.http_client.get(url)` and confirm
the corresponding test fails, then restore.

### Step 3: Confirm pass

```bash
uv run pytest tests/test_citations_references.py tests/test_openalex_s2.py \
  tests/test_unpaywall_404_logging.py tests/test_invalid_doi_logging.py -q
```

### Step 4: Commit

```bash
git add tests/
git commit -m "test: scope 404 log assertions to the http logger and assert routes called"
```

---

## Task 5 — Add provider-level negative controls

**Target files**
- Modify: `tests/test_unpaywall_404_logging.py`, `tests/test_openalex_s2.py`

**Consumes**: provider methods under a terminal 500
**Produces**: tests proving non-404 failures still warn

### Step 1: Write failing test

```python
@respx.mock
async def test_unpaywall_fetch_full_text_500_still_warns(client, caplog):
    route = respx.get("https://api.unpaywall.org/v2/10.1093/humupd/dmab061").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    provider = UnpaywallProvider(client, email="test@example.com")
    with caplog.at_level("WARNING", logger="scholar_mcp.utils.http"):
        res = await provider.fetch_full_text(IdentifierMap(doi="10.1093/humupd/dmab061"))

    assert route.called
    assert res is None
    assert [r for r in caplog.records if r.name == "scholar_mcp.utils.http"]
```

Add the OpenAlex equivalent against `_get_work`.

### Step 2: Confirm failure

Run before Task 1 lands and the assertion on a non-empty record list is the guard;
after Task 1 it must pass. Run it now to confirm it exercises the WARNING branch.

```bash
uv run pytest tests/test_unpaywall_404_logging.py tests/test_openalex_s2.py -v
```

### Step 3: Minimal implementation

None — these are pure guard tests over existing behavior.

### Step 4: Confirm pass

```bash
uv run pytest tests/test_unpaywall_404_logging.py tests/test_openalex_s2.py -q
```

### Step 5: Commit

```bash
git add tests/
git commit -m "test: assert non-404 provider failures still emit http warnings"
```

---

## Task 6 — Collapse the duplicated Unpaywall lookup

**Target files**
- Modify: `src/scholar_mcp/providers/unpaywall.py`, `src/scholar_mcp/resolver.py`
- Test: `tests/test_unpaywall_404_logging.py`

**Consumes**: `IdentifierMap`
**Produces**: `UnpaywallProvider.fetch_oa_pdf_url(ids) -> str | None`

### Step 1: Write failing test

```python
@respx.mock
async def test_unpaywall_fetch_oa_pdf_url_returns_best_location(client):
    route = respx.get("https://api.unpaywall.org/v2/10.1000/x").mock(
        return_value=httpx.Response(
            200,
            json={
                "is_oa": True,
                "best_oa_location": {"url_for_pdf": "https://example.org/paper.pdf"},
            },
        )
    )
    provider = UnpaywallProvider(client, email="test@example.com")
    url = await provider.fetch_oa_pdf_url(IdentifierMap(doi="10.1000/x"))

    assert route.called
    assert url == "https://example.org/paper.pdf"


@respx.mock
async def test_unpaywall_fetch_oa_pdf_url_none_when_closed(client):
    respx.get("https://api.unpaywall.org/v2/10.1000/y").mock(
        return_value=httpx.Response(200, json={"is_oa": False})
    )
    provider = UnpaywallProvider(client, email="test@example.com")
    assert await provider.fetch_oa_pdf_url(IdentifierMap(doi="10.1000/y")) is None
```

### Step 2: Confirm failure

```bash
uv run pytest tests/test_unpaywall_404_logging.py -v
```

Expect `AttributeError: 'UnpaywallProvider' object has no attribute 'fetch_oa_pdf_url'`.

### Step 3: Minimal implementation

Extract the lookup — email guard, DOI guard, `quiet_statuses={404}` GET, `is_oa`
check, `best_oa_location` fallback from `url_for_pdf` to `url` — into
`fetch_oa_pdf_url`. Have `fetch_full_text` call it, and replace the inline
`UNPAYWALL_BASE` block in `WaterfallResolver.fetch_pdf_bytes` with
`await self.unpaywall.fetch_oa_pdf_url(ids)`.

`fetch_full_text` still needs `title` from the payload; keep that read inside
`fetch_full_text` by having `fetch_oa_pdf_url` return only the URL and letting
`fetch_full_text` fall back to an empty title, or return a small
`(pdf_url, payload)` tuple from a private `_lookup` helper that both callers share.
Prefer the private-helper form so no payload field is lost.

### Step 4: Confirm pass

```bash
uv run pytest tests/test_unpaywall_404_logging.py tests/test_waterfall_resolver.py \
  tests/test_oa_providers.py -q
```

### Step 5: Commit

```bash
git add src/scholar_mcp/providers/unpaywall.py src/scholar_mcp/resolver.py tests/
git commit -m "refactor(unpaywall): share the OA lookup between provider and resolver"
```

---

## Verification Gate

```bash
uv run pytest
```

Must be fully green (590 tests at `8055298`, plus the tests added here) before push.
