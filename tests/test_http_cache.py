import asyncio
import logging
import time

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.rate_limit import AsyncRateLimiter


async def test_ttl_cache_lru_eviction():
    cache = TTLCache(maxsize=2, ttl_seconds=60)
    await cache.set("a", 1)
    await cache.set("b", 2)
    assert await cache.get("a") == 1  # refreshes recency of "a"
    await cache.set("c", 3)
    assert await cache.get("b") is None  # "b" was least recently used
    assert await cache.get("a") == 1
    assert await cache.get("c") == 3


async def test_ttl_cache_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("scholar_mcp.utils.cache.time.monotonic", lambda: clock[0])
    cache = TTLCache(maxsize=10, ttl_seconds=30)
    await cache.set("k", "v")
    assert await cache.get("k") == "v"
    clock[0] += 31
    assert await cache.get("k") is None


async def test_rate_limiter_throttles():
    limiter = AsyncRateLimiter(rate_per_sec=10.0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    # 5 tokens at 10/s cannot complete faster than ~0.4s after the initial token
    assert time.monotonic() - start >= 0.3


def test_ncbi_credential_injection():
    settings = Settings(
        pubmed_api_key="secret-key", pubmed_email="test@example.com", pubmed_tool="TestApp"
    )
    client = AsyncHttpClient(settings=settings)
    url = client._inject_credentials(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?id=123"
    )
    assert "api_key=secret-key" in url
    assert "tool=TestApp" in url
    assert "test%40example.com" in url or "test@example.com" in url

    other = client._inject_credentials("https://api.unpaywall.org/v2/10.1038/abc")
    assert "api_key" not in other


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
    assert "db=pubmed" in sent
    assert "term=q" in sent
    await client.aclose()


@respx.mock
async def test_non_ncbi_params_are_still_sent():
    """Folding params into the URL must not drop them for hosts without injection."""
    route = respx.get(url__regex=r"https://api\.unpaywall\.org/.*").mock(
        return_value=httpx.Response(200, text="ok")
    )
    client = AsyncHttpClient(settings=Settings())
    await client.get(
        "https://api.unpaywall.org/v2/10.1038/abc", params={"email": "e@example.com"}
    )
    sent = str(route.calls[0].request.url)
    assert "email=e%40example.com" in sent or "email=e@example.com" in sent
    assert "/v2/10.1038/abc" in sent
    await client.aclose()


@respx.mock
async def test_retries_then_succeeds():
    route = respx.get("https://example.org/data").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, text="ok"),
        ]
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), backoff_base=0.01)
    resp = await client.get("https://example.org/data")
    assert resp is not None and resp.text == "ok"
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_returns_none_after_exhausting_retries():
    respx.get("https://example.org/down").mock(return_value=httpx.Response(500))
    client = AsyncHttpClient(settings=Settings(request_timeout=5), max_retries=2, backoff_base=0.01)
    assert await client.get("https://example.org/down") is None
    await client.aclose()


@respx.mock
async def test_ncbi_requests_are_rate_limited(monkeypatch):
    """Without an API key the NCBI host bucket must be safe 2.8 rps, not unlimited."""
    respx.get(url__regex=r"https://eutils\.ncbi\.nlm\.nih\.gov/.*").mock(
        return_value=httpx.Response(200, text="ok")
    )
    client = AsyncHttpClient(settings=Settings(pubmed_api_key=None))
    assert client._limiter_for("eutils.ncbi.nlm.nih.gov").rate_per_sec == 2.8
    # Host grouping: subdomains share the ncbi.nlm.nih.gov limiter bucket
    assert client._limiter_for("www.ncbi.nlm.nih.gov") is client._limiter_for("eutils.ncbi.nlm.nih.gov")
    await client.aclose()


def test_http_client_user_agent_version():
    client = AsyncHttpClient(settings=Settings(pubmed_email="author@example.com"))
    ua = client.client.headers.get("User-Agent", "")
    assert "ScholarMCP/1.0.0" in ua
    assert "mailto:author@example.com" in ua


async def test_limiters_concurrent_access():
    client = AsyncHttpClient(settings=Settings())
    limiters = await asyncio.gather(
        *(asyncio.to_thread(client._limiter_for, "api.crossref.org") for _ in range(20))
    )
    assert len(set(id(lim) for lim in limiters)) == 1
    await client.aclose()


@respx.mock
async def test_http_client_logs_warning_on_4xx_5xx(caplog):
    respx.get("https://example.org/bad").mock(
        return_value=httpx.Response(400, text="Bad Request error detail")
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5))
    with caplog.at_level(logging.WARNING):
        resp = await client.get("https://example.org/bad")
    assert resp is None
    assert any("failed with status 400" in rec.message for rec in caplog.records)
    assert any("Bad Request error detail" in rec.message for rec in caplog.records)
    await client.aclose()


@respx.mock
async def test_http_client_logs_warning_on_transport_error(caplog):
    respx.get("https://example.org/timeout").mock(
        side_effect=httpx.ConnectTimeout("Connection timed out")
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), max_retries=2, backoff_base=0.01)
    with caplog.at_level(logging.WARNING):
        resp = await client.get("https://example.org/timeout")
    assert resp is None
    terminal = [rec for rec in caplog.records if "failed after 2 attempts" in rec.message]
    assert len(terminal) == 1
    assert terminal[0].levelno == logging.WARNING
    assert "Connection timed out" in terminal[0].message
    await client.aclose()


@respx.mock
async def test_http_logs_never_leak_credentials(caplog):
    """Injected api_key/email must not reach the log stream on failure paths."""
    respx.get(url__regex=r"https://eutils\.ncbi\.nlm\.nih\.gov/.*").mock(
        return_value=httpx.Response(400, text="Bad Request")
    )
    client = AsyncHttpClient(
        settings=Settings(pubmed_api_key="secret-key", pubmed_email="e@example.com")
    )
    with caplog.at_level(logging.WARNING):
        assert await client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": "q"},
        ) is None
    assert "secret-key" not in caplog.text
    assert "e@example.com" not in caplog.text
    assert "e%40example.com" not in caplog.text
    # The diagnostic itself must survive redaction.
    assert "api_key=REDACTED" in caplog.text
    assert "term=q" in caplog.text
    await client.aclose()


@respx.mock
async def test_error_body_log_is_bounded_and_binary_safe(caplog, monkeypatch):
    """The error excerpt must come from bounded bytes, never a whole-body decode."""

    def _forbidden(self):
        raise AssertionError("resp.text decodes the whole body; slice resp.content instead")

    monkeypatch.setattr(httpx.Response, "text", property(_forbidden))
    body = b"\xff\xfe" + b"A" * 200_000
    respx.get("https://example.org/binary-error").mock(
        return_value=httpx.Response(400, content=body)
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5))
    with caplog.at_level(logging.WARNING):
        assert await client.get("https://example.org/binary-error") is None
    record = next(r for r in caplog.records if "failed with status 400" in r.message)
    assert len(record.message) < 1000
    await client.aclose()


@respx.mock
async def test_retry_is_logged_below_warning(caplog):
    """NCBI 429 backoff is routine; only the terminal failure deserves WARNING."""
    respx.get("https://example.org/flaky").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="ok")]
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), backoff_base=0.01)
    with caplog.at_level(logging.DEBUG):
        resp = await client.get("https://example.org/flaky")
    assert resp is not None
    retry_records = [r for r in caplog.records if "retryable status 503" in r.message]
    assert retry_records, "the retry must still be reported"
    assert all(r.levelno == logging.INFO for r in retry_records)
    await client.aclose()


def test_pypdf_sees_fonttools():
    """pypdf gates its fontTools code paths behind this flag at import time.

    Note: in pypdf 6.16 those paths are `Font.from_truetype_font_file` and
    `_get_typographic_maps`, both on the *writer* side. Text extraction
    (`pypdf._cmap`) does not use fontTools at all.
    """
    from pypdf._font import HAS_FONTTOOLS

    assert HAS_FONTTOOLS is True




@respx.mock
async def test_merge_params_skips_none_values():
    """httpx drops None-valued params; folding into URL must match, not send 'None'."""
    route = respx.get(url__regex=r"https://api\.unpaywall\.org/.*").mock(
        return_value=httpx.Response(200, text="ok")
    )
    client = AsyncHttpClient(settings=Settings())
    await client.get(
        "https://api.unpaywall.org/v2/10.1038/abc",
        params={"email": "e@example.com", "unused": None},
    )
    sent = str(route.calls[0].request.url)
    assert "unused" not in sent
    assert "email=" in sent
    await client.aclose()


def test_parse_retry_after_delta_seconds():
    from scholar_mcp.utils.http import _parse_retry_after

    resp = httpx.Response(429, headers={"Retry-After": "30"})
    assert _parse_retry_after(resp) == 30.0

    resp_float = httpx.Response(429, headers={"Retry-After": "2.5"})
    assert _parse_retry_after(resp_float) == 2.5

    resp_none = httpx.Response(429)
    assert _parse_retry_after(resp_none) is None

    resp_invalid = httpx.Response(429, headers={"Retry-After": "invalid"})
    assert _parse_retry_after(resp_invalid) is None


def test_parse_retry_after_http_date():
    from scholar_mcp.utils.http import _parse_retry_after
    from datetime import datetime, timezone, timedelta

    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    resp = httpx.Response(429, headers={"Retry-After": date_str})
    val = _parse_retry_after(resp)
    assert val is not None and 50.0 <= val <= 65.0


def test_host_key_normalization():
    from scholar_mcp.utils.http import _host_key

    assert _host_key("eutils.ncbi.nlm.nih.gov") == "ncbi.nlm.nih.gov"
    # Ports are stripped by _limiter_for_url, not by _host_key.
    assert _host_key("www.ncbi.nlm.nih.gov") == "ncbi.nlm.nih.gov"
    assert _host_key("export.arxiv.org") == "arxiv.org"
    assert _host_key("api.fda.gov") == "api.fda.gov"


@respx.mock
async def test_429_retries_and_throttles_limiter():
    route = respx.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi").mock(
        side_effect=[
            httpx.Response(429, text='{"error":"API rate limit exceeded"}'),
            httpx.Response(200, text="<eSummaryResult>ok</eSummaryResult>"),
        ]
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), backoff_base=0.01)
    resp = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi")
    assert resp is not None and "ok" in resp.text
    assert route.call_count == 2
    limiter = client._limiter_for("eutils.ncbi.nlm.nih.gov")
    # Limiter throttled_until must have been set
    assert limiter.throttled_until > 0.0
    await client.aclose()


@respx.mock
async def test_429_honors_retry_after_header():
    route = respx.get("https://api.semanticscholar.org/graph/v1/paper/123").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0.05"}, text="Rate limit"),
            httpx.Response(200, json={"title": "Paper"}),
        ]
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), backoff_base=0.01)
    start = time.monotonic()
    resp = await client.get("https://api.semanticscholar.org/graph/v1/paper/123")
    elapsed = time.monotonic() - start
    assert resp is not None
    assert route.call_count == 2
    assert elapsed >= 0.04
    await client.aclose()


def test_parse_retry_after_rejects_non_finite():
    from scholar_mcp.utils.http import _parse_retry_after

    for raw in ("inf", "+Inf", "-inf", "1e400", "NaN", "nan"):
        resp = httpx.Response(429, headers={"Retry-After": raw})
        assert _parse_retry_after(resp) is None, raw


def test_parse_retry_after_clamps_to_max():
    from scholar_mcp.utils.http import MAX_RETRY_AFTER, _parse_retry_after

    resp = httpx.Response(429, headers={"Retry-After": "86400"})
    assert _parse_retry_after(resp) == MAX_RETRY_AFTER

    from datetime import datetime, timedelta, timezone

    far = datetime.now(timezone.utc) + timedelta(days=1)
    resp_date = httpx.Response(
        429, headers={"Retry-After": far.strftime("%a, %d %b %Y %H:%M:%S GMT")}
    )
    assert _parse_retry_after(resp_date) == MAX_RETRY_AFTER


def test_host_key_ignores_userinfo_and_ipv6_brackets():
    from scholar_mcp.utils.http import _host_key

    assert _host_key("a.example.com") == "a.example.com"
    assert _host_key("A.Example.COM") == "a.example.com"
    assert _host_key("2001:db8::1") == "2001:db8::1"


async def test_limiter_key_uses_hostname_not_netloc():
    """A userinfo-bearing URL must share the bucket of the bare host, not key on the username."""
    client = AsyncHttpClient(settings=Settings(pubmed_api_key=None))
    plain = client._limiter_for_url("https://api.crossref.org/works")
    with_userinfo = client._limiter_for_url("https://user:pw@api.crossref.org/works")
    assert plain is with_userinfo
    assert plain.rate_per_sec == 10.0

    ipv6_a = client._limiter_for_url("https://[2001:db8::1]:8443/x")
    ipv6_b = client._limiter_for_url("https://[2001:db8::2]:8443/x")
    assert ipv6_a is not ipv6_b
    await client.aclose()
