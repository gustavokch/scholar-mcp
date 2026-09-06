import asyncio
import logging
import time

import httpx
import pytest
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
    """Without an API key the NCBI host bucket must be 3 rps, not unlimited."""
    respx.get(url__regex=r"https://eutils\.ncbi\.nlm\.nih\.gov/.*").mock(
        return_value=httpx.Response(200, text="ok")
    )
    client = AsyncHttpClient(settings=Settings(pubmed_api_key=None))
    assert client._limiter_for("eutils.ncbi.nlm.nih.gov").rate_per_sec == 3.0
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
    import logging

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
    import logging

    respx.get("https://example.org/timeout").mock(
        side_effect=httpx.ConnectTimeout("Connection timed out")
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), max_retries=2, backoff_base=0.01)
    with caplog.at_level(logging.WARNING):
        resp = await client.get("https://example.org/timeout")
    assert resp is None
    assert any("ConnectTimeout" in rec.message or "failed after 2 attempts" in rec.message for rec in caplog.records)
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


def test_fonttools_installed():
    """Verify fontTools is installed so pypdf can decode CFF Type1 font encodings."""
    import fontTools
    assert fontTools.__version__ is not None


