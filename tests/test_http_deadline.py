"""Deadline-aware retry ladder in ``AsyncHttpClient.get``.

The ladder is bounded in attempt count (``max_retries``) and, before this,
in nothing else. A caller that wraps the call in its own ``asyncio.wait_for``
-- every BrazilMoHEngine stage, and ``get_full_text`` -- therefore watches the
ladder overrun the budget and get cancelled mid-attempt, which throws away the
HTTP status the host actually answered with. The ``deadline`` kwarg lets the
ladder stop itself in time and return that status.

``deadline`` is an absolute ``time.monotonic()`` value. ``None`` keeps the
pre-existing behaviour exactly, which is what every other caller passes.

These tests use ``httpx.MockTransport``: the handler sees the real
``httpx.Request``, so the per-attempt timeout the clamp installs is readable
from ``request.extensions["timeout"]``.
"""

import time

import httpx

from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.rate_limit import AsyncRateLimiter


def _client(handler, max_retries: int = 4, request_timeout: int = 30, backoff_base: float = 0.01):
    """Client wired to a counting MockTransport and a private fast limiter.

    The limiter registry is process-global; a private bucket keeps the attempt
    count independent of whatever another test owes for this host.
    """
    client = AsyncHttpClient(
        settings=Settings(request_timeout=request_timeout),
        max_retries=max_retries,
        backoff_base=backoff_base,
        min_429_wait=0.0,
    )
    calls: dict = {"count": 0, "timeouts": []}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        calls["timeouts"].append(request.extensions.get("timeout"))
        return handler(request)

    client.client = httpx.AsyncClient(
        timeout=float(client.settings.request_timeout),
        follow_redirects=True,
        transport=httpx.MockTransport(counting_handler),
    )
    limiter = AsyncRateLimiter(rate_per_sec=1000.0)
    client._limiter_for_url = lambda url: limiter
    return client, calls


def _always_503(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="Service Unavailable")


async def test_ladder_stops_when_the_next_retry_cannot_fit_the_deadline():
    """A retry whose backoff lands past the deadline is not taken, and the
    real status survives.

    Without this the loop sleeps and retries until ``max_retries`` runs out,
    the caller's outer ``wait_for`` fires, and the 503 is replaced by a
    statusless cancellation. The 1.0 s backoff against a 0.5 s deadline makes
    the refusal independent of how fast the transport answers.
    """
    client, calls = _client(_always_503, backoff_base=1.0)
    try:
        resp = await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 0.5
        )
        assert resp is None
        assert calls["count"] == 1
        failure = client.last_failure
        assert failure is not None
        assert failure.kind == "http"
        assert failure.status == 503
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_retry_is_refused_when_the_attempt_itself_will_not_fit():
    """"Fits" means the backoff plus another attempt, not the backoff alone.

    A gate that weighs only the backoff still starts an attempt it cannot
    finish. That attempt is cut off -- by the clamp, or by the caller's own
    ceiling -- and the completed response the ladder was already holding is
    replaced by a statusless timeout. The cost of an attempt is measured from
    the previous one rather than guessed: in a degraded window the host's fast
    failures are consistent, which is what makes the previous attempt a fair
    predictor of the next.

    Here one attempt costs ~0.1 s and the backoff is negligible, against 0.15 s
    of budget: the first attempt fits, a second does not.
    """
    slept = {"n": 0}

    def _slow_503(request: httpx.Request) -> httpx.Response:
        slept["n"] += 1
        time.sleep(0.1)
        return httpx.Response(503, text="Service Unavailable")

    client, calls = _client(_slow_503, backoff_base=0.001)
    try:
        resp = await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 0.15
        )
        assert resp is None
        assert calls["count"] == 1
        failure = client.last_failure
        assert failure is not None
        assert failure.status == 503
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_spent_deadline_issues_no_request_at_all():
    """A budget already gone when the call starts buys nothing by opening a
    socket; the failure reports as transport, which callers map to a timeout."""
    client, calls = _client(_always_503)
    try:
        assert (
            await client.get(
                "https://example.org/degraded", deadline=time.monotonic()
            )
            is None
        )
        assert calls["count"] == 0
        failure = client.last_failure
        assert failure is not None
        assert failure.kind == "transport"
        assert failure.status is None
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_deadline_none_keeps_the_full_ladder():
    """The default must not change behaviour for the callers that do not opt in."""
    client, calls = _client(_always_503, max_retries=3)
    try:
        assert await client.get("https://example.org/degraded") is None
        assert calls["count"] == 3
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_attempt_timeout_is_clamped_to_the_remaining_budget():
    """The in-flight attempt must end at the deadline, not at request_timeout.

    Otherwise an attempt started just before the deadline still runs for the
    full ``request_timeout`` and the caller's ``wait_for`` -- not the ladder --
    is what ends the call.
    """
    client, calls = _client(_always_503, request_timeout=30, max_retries=1)
    try:
        await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 2.0
        )
        installed = calls["timeouts"][0]
        assert installed is not None
        # httpx carries the per-request timeout as a four-key dict.
        assert 0 < installed["read"] <= 2.0
        assert 0 < installed["connect"] <= 2.0
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_request_timeout_applies_when_it_is_under_the_remaining_budget():
    """The deadline is a ceiling, never a floor: a short request_timeout wins."""
    client, calls = _client(_always_503, request_timeout=5, max_retries=1)
    try:
        await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 100.0
        )
        assert calls["timeouts"][0]["read"] == 5.0
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_no_request_is_issued_when_the_limiter_parks_past_the_deadline():
    """A throttled bucket can hold the call past the deadline on its own.

    The 1 req/s BVS bucket, plus the throttle a 429 or a shielded 403 installs,
    can outlast the whole budget. Issuing the request anyway would send it with
    a non-positive timeout.
    """
    client, calls = _client(_always_503)
    limiter = AsyncRateLimiter(rate_per_sec=1000.0)
    limiter.throttle(0.2)
    client._limiter_for_url = lambda url: limiter
    try:
        resp = await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 0.05
        )
        assert resp is None
        assert calls["count"] == 0
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()
