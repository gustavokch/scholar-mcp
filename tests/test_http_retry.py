"""Retry-policy tests for the permanent-transport-error fast fail.

Task 2 of the BVS stage chain / dead mirrors plan: name resolution failure
(``socket.gaierror``) and a TLS certificate verification failure
(``ssl.SSLCertVerificationError``) are permanent for the life of the process,
so retrying them with exponential backoff buys nothing. Genuinely transient
transport errors (ReadTimeout, connection resets) keep the existing loop.

These tests use ``httpx.MockTransport`` rather than respx: respx re-raises a
side-effect exception with ``raise error.origin from error``, which overwrites
the ``__cause__`` the permanent-error classifier walks, so the chain being
tested never reaches it. MockTransport re-raises the handler exception bare,
matching what httpx's real transports do with the OS error.
"""

import socket
import ssl
import time

import httpx

from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient, FetchFailure


def _client_with_handler(handler, max_retries: int = 4) -> tuple[AsyncHttpClient, dict]:
    """AsyncHttpClient wired to a counting MockTransport.

    Returns the client and a ``{"count": int}`` dict the handler bumps on
    every transport attempt.
    """
    client = AsyncHttpClient(
        settings=Settings(request_timeout=5), max_retries=max_retries, backoff_base=0.01
    )
    calls = {"count": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return handler(request)

    client.client = httpx.AsyncClient(
        timeout=float(client.settings.request_timeout),
        follow_redirects=True,
        transport=httpx.MockTransport(counting_handler),
    )
    return client, calls


def _raise_permanent(exc: Exception):
    """Handler that wraps ``exc`` the way httpx does on the wire: the
    underlying OS error becomes the ``__cause__`` of the httpx wrapper."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(str(exc)) from exc

    return _handler


async def test_dns_resolution_failure_returns_none_on_first_attempt():
    """A gaierror-caused ConnectError is permanent: exactly one transport
    attempt, no retries."""
    client, calls = _client_with_handler(
        _raise_permanent(socket.gaierror(8, "nodename nor servname provided, or not known"))
    )
    try:
        assert await client.get("https://example.org/dead-dns") is None
        assert calls["count"] == 1
        failure = client.last_failure
        assert failure is not None
        assert failure.kind == "transport"
        assert failure.status is None
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_ssl_certificate_verification_failure_returns_none_on_first_attempt():
    """A self-signed certificate is permanent for the life of the process."""
    client, calls = _client_with_handler(
        _raise_permanent(
            ssl.SSLCertVerificationError(
                "certificate verify failed: self-signed certificate"
            )
        )
    )
    try:
        assert await client.get("https://example.org/bad-cert") is None
        assert calls["count"] == 1
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


def _raise_transient(exc: Exception):
    def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


async def test_read_timeout_still_retries_max_retries_times():
    """The guard must not quietly disable retries for transient errors."""
    client, calls = _client_with_handler(
        _raise_transient(httpx.ReadTimeout("timed out")), max_retries=3
    )
    try:
        assert await client.get("https://example.org/slow") is None
        assert calls["count"] == 3
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_connection_refused_still_retries():
    """A refused connection to a host that is merely down is transient; it
    must keep the retry loop."""
    client, calls = _client_with_handler(
        _raise_permanent(ConnectionRefusedError("Connection refused")), max_retries=3
    )
    try:
        assert await client.get("https://example.org/refused") is None
        assert calls["count"] == 3
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_dead_host_cache_short_circuits_the_second_call():
    """Two gets to the same DNS-dead host make exactly one transport attempt
    in total: the dead-host verdict lives for the life of the process."""
    client, calls = _client_with_handler(
        _raise_permanent(socket.gaierror(8, "nodename nor servname provided, or not known"))
    )
    try:
        assert await client.get("https://example.org/dead-host") is None
        assert await client.get("https://example.org/dead-host") is None
        assert await client.get("https://example.org/dead-host?q=2") is None
        assert calls["count"] == 1
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_dead_host_cache_is_per_host_not_per_client():
    """The cache is class-level (one process, one DNS view); a second client
    instance must see the verdict too."""
    calls = {"count": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _raise_permanent(
            socket.gaierror(8, "nodename nor servname provided, or not known")
        )(request)

    client = AsyncHttpClient(
        settings=Settings(request_timeout=5), max_retries=4, backoff_base=0.01
    )
    client.client = httpx.AsyncClient(
        timeout=5.0, follow_redirects=True, transport=httpx.MockTransport(counting_handler)
    )
    try:
        assert await client.get("https://example.org/dead-dns") is None
    finally:
        await client.aclose()
    # No reset here: the next client must still see the verdict.
    client2 = AsyncHttpClient(
        settings=Settings(request_timeout=5), max_retries=4, backoff_base=0.01
    )
    client2.client = httpx.AsyncClient(
        timeout=5.0, follow_redirects=True, transport=httpx.MockTransport(counting_handler)
    )
    try:
        assert await client2.get("https://example.org/dead-dns") is None
        # Both clients hit the transport exactly once; the second client's
        # call short-circuited on the cached verdict.
        assert calls["count"] == 1
    finally:
        await client2.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_transient_failure_does_not_poison_the_host_cache():
    """A ReadTimeout must not mark the host dead; a later call still opens a
    socket."""
    client, calls = _client_with_handler(
        _raise_transient(httpx.ReadTimeout("timed out")), max_retries=2
    )
    try:
        assert await client.get("https://example.org/flaky-host") is None
        first = calls["count"]
        assert await client.get("https://example.org/flaky-host") is None
        assert calls["count"] > first
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_retry_after_beyond_cap_fails_fast_and_short_circuits_the_host():
    """A 429 whose Retry-After exceeds MAX_RETRY_AFTER (OpenAlex's exhausted
    daily budget answers 660) cannot succeed within this call: one attempt,
    no sleep, the real status reported, and later calls to the host skip the
    network until the server's stated time instead of re-paying the ladder."""
    client, calls = _client_with_handler(
        lambda request: httpx.Response(429, headers={"Retry-After": "660"})
    )
    try:
        start = time.monotonic()
        assert await client.get("https://api.openalex.org/works/a") is None
        assert await client.get("https://api.openalex.org/works/b") is None
        assert time.monotonic() - start < 1.0
        assert calls["count"] == 1
        assert client.last_failure == FetchFailure("http", 429, "RateLimitedCached")
        # Other hosts are unaffected.
        assert await client.get("https://api.crossref.org/works/c") is None
        assert calls["count"] == 2
    finally:
        await client.aclose()


async def test_retry_after_within_cap_still_retries():
    """A Retry-After the cap tolerates keeps the normal wait-and-retry path."""
    responses = iter([httpx.Response(429, headers={"Retry-After": "0.05"}), httpx.Response(200)])
    client, calls = _client_with_handler(lambda request: next(responses))
    client.min_429_wait = 0.0
    try:
        resp = await client.get("https://api.openalex.org/works/a")
        assert resp is not None and resp.status_code == 200
        assert calls["count"] == 2
    finally:
        await client.aclose()


async def test_long_retry_after_marks_the_host_throttled():
    """Callers that gate on is_throttled (BrazilMoHEngine._is_bvs_shielded)
    must see a host short-circuited by a long Retry-After as throttled, just
    as they see one parked by a limiter throttle. A sibling host sharing no
    bucket stays unthrottled."""
    client, _ = _client_with_handler(
        lambda request: httpx.Response(429, headers={"Retry-After": "660"})
    )
    try:
        assert await client.get("https://pesquisa.bvsalud.org/portal/") is None
        assert client.is_throttled("pesquisa.bvsalud.org")
        assert not client.is_throttled("api.openalex.org")
    finally:
        await client.aclose()


async def test_rate_limited_host_is_reprobed_once_the_capped_window_passes(monkeypatch):
    """The short-circuit lasts min(Retry-After, MAX_RATE_LIMITED_HOST_S) and
    then expires: the next call goes back to the network. Without the cap a
    hostile Retry-After would blackhole the host for its full stated time."""
    import asyncio

    import scholar_mcp.utils.http as http_mod

    monkeypatch.setattr(http_mod, "MAX_RATE_LIMITED_HOST_S", 0.05)
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "660"}), httpx.Response(200)]
    )
    client, calls = _client_with_handler(lambda request: next(responses))
    try:
        assert await client.get("https://api.openalex.org/works/a") is None
        assert await client.get("https://api.openalex.org/works/a") is None
        assert calls["count"] == 1  # second call short-circuited
        await asyncio.sleep(0.1)
        resp = await client.get("https://api.openalex.org/works/a")
        assert resp is not None and resp.status_code == 200
        assert calls["count"] == 2
        assert client.last_failure is None
    finally:
        await client.aclose()


async def test_http_date_retry_after_beyond_cap_fails_fast():
    """The HTTP-date form of Retry-After takes the same fail-fast path as the
    delta-seconds form: one attempt, then the host short-circuits."""
    from datetime import datetime, timedelta, timezone

    far = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    client, calls = _client_with_handler(
        lambda request: httpx.Response(429, headers={"Retry-After": far})
    )
    try:
        assert await client.get("https://api.openalex.org/works/a") is None
        assert calls["count"] == 1
        assert await client.get("https://api.openalex.org/works/b") is None
        assert calls["count"] == 1
        assert client.last_failure == FetchFailure("http", 429, "RateLimitedCached")
    finally:
        await client.aclose()
