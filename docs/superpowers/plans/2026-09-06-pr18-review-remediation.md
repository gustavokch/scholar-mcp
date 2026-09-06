# PR #18 Review Remediation — fonttools dependency and HTTP error logging

**PR:** https://github.com/gustavokch/scholar-mcp/pull/18
**Branch:** `fix/fonttools-and-http-logging`
**Date:** 2026-09-06

## Goal

Resolve the findings raised in the review of PR #18. Two are blocking:

1. NCBI credentials injected by `AsyncHttpClient._inject_credentials` are silently
   discarded for every caller that passes `params=`, because httpx replaces the URL
   query wholesale when `params` is supplied. This is the root cause of the
   `FetchError: esearch request failed` that PR #18 set out to diagnose.
2. The four warning log statements added by PR #18 print the post-injection URL,
   which contains `api_key` and `email`. Secrets reach the log stream.

Three non-blocking findings (body decoding, retry log level, test rigor) are fixed
in the same pass.

## Architecture

`src/scholar_mcp/utils/http.py` — `AsyncHttpClient`.

Current request path:

```
url --_inject_credentials--> target_url --httpx.get(target_url, params=params)--> query REPLACED by params
```

Target request path:

```
url + params --_merge_params--> merged --_inject_credentials--> target_url --httpx.get(target_url)--> query preserved
```

Logging path gains a `_redact_url` step that masks sensitive query parameters.

No caller currently passes both a query-bearing URL and `params`, so folding `params`
into the URL query changes no existing behaviour beyond the fix itself.

## Tech Stack

Python 3.10+, httpx, respx, pytest (asyncio_mode = auto), uv.

## Tasks

### Task 1 — Merge `params` into the URL before credential injection

**Modify:** `src/scholar_mcp/utils/http.py`
**Test:** `tests/test_http_cache.py`

**Consumes:** `url: str`, `params: dict[str, Any] | None`
**Produces:** a single request URL carrying both caller params and injected credentials.

Step 1 — failing test:

```python
@respx.mock
async def test_ncbi_credentials_survive_explicit_params():
    """httpx replaces the URL query when params= is given; credentials must survive."""
    route = respx.get(url__regex=r"https://eutils\.ncbi\.nlm\.nih\.gov/.*").mock(
        return_value=httpx.Response(200, text="ok")
    )
    client = AsyncHttpClient(
        settings=Settings(
            pubmed_api_key="secret-key", pubmed_email="e@example.com", pubmed_tool="TestApp"
        )
    )
    await client.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": "q"},
    )
    sent = str(route.calls[0].request.url)
    assert "api_key=secret-key" in sent
    assert "tool=TestApp" in sent
    assert "db=pubmed" in sent and "term=q" in sent
    await client.aclose()
```

Step 2 — `uv run pytest tests/test_http_cache.py -k credentials_survive -v` (expect fail).

Step 3 — add `_merge_params(url, params)` and call it before `_inject_credentials`;
drop `params=` from the inner `client.get`.

Step 4 — re-run, expect pass.

Step 5 — `git commit -m "fix(http): keep NCBI credentials when caller passes params"`

### Task 2 — Redact credentials from log output

**Modify:** `src/scholar_mcp/utils/http.py`
**Test:** `tests/test_http_cache.py`

Step 1 — failing test asserting `secret-key` never appears in `caplog.text` while the
redaction marker and the host do.

Step 2 — run, expect fail.

Step 3 — add `SENSITIVE_QUERY_PARAMS` and `_redact_url`; log `_redact_url(target_url)`
at all four sites.

Step 4 — re-run, expect pass.

Step 5 — `git commit -m "fix(http): redact api_key and email from request logs"`

### Task 3 — Bound the error-body decode and quiet retry logs

**Modify:** `src/scholar_mcp/utils/http.py`
**Test:** `tests/test_http_cache.py`

Step 1 — failing test: a 4xx response with a large binary body logs at most ~500
characters and does not raise; a retryable 503 logs at INFO, not WARNING.

Step 2 — run, expect fail.

Step 3 — slice `resp.content[:500]` and decode with `errors="replace"`; demote the two
retry statements to `logger.info`.

Step 4 — re-run, expect pass.

Step 5 — `git commit -m "fix(http): bound error body decode, demote retry logs to info"`

### Task 4 — Tighten the tests added by PR #18

**Modify:** `tests/test_http_cache.py`

- Hoist `import logging` to module scope.
- Replace `assert any(A or B)` with a direct assertion on the terminal failure line.
- Make `test_fonttools_installed` import `fontTools.cffLib.CFFFontSet`, the symbol
  `pypdf._cmap` actually needs.

Step 5 — `git commit -m "test(http): tighten logging and fonttools assertions"`

## Verification

`uv run pytest` must be fully green before pushing to `fix/fonttools-and-http-logging`.
