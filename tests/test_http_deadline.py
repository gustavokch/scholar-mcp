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

import asyncio
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
    def _slow_503(request: httpx.Request) -> httpx.Response:
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


async def test_gate_costs_the_limiter_spacing_before_retrying():
    """The next attempt's price includes the bucket it has to wait for.

    Backoff plus the measured attempt cost fits the budget; the 1 req/s spacing
    on top of them does not. A gate blind to the limiter approves the retry and
    parks in ``acquire`` past the deadline -- and because a real caller holds
    the same deadline as an ``asyncio.wait_for``, the cancellation lands inside
    that park. ``get`` never reaches its own bailout, so the 503 it was already
    holding is replaced by a bare ``TimeoutError``: the exact failure mode this
    ladder exists to remove. Production is this shape -- the BVS bucket is
    1 req/s, and the 429 branch installs a throttle of its own two lines above
    the gate.
    """
    client, calls = _client(_always_503, backoff_base=0.01)
    # A fresh 1 req/s bucket: the first acquire spends the burst token for
    # free, the second pays a full second.
    limiter = AsyncRateLimiter(rate_per_sec=1.0)
    client._limiter_for_url = lambda url: limiter
    budget = 0.3
    try:

        async def _call() -> tuple[httpx.Response | None, object]:
            # ``last_failure`` is ContextScoped, and ``wait_for`` runs the
            # coroutine in a child task with its own context. Read it here, in
            # the same task -- which is also where every real caller reads it
            # (``_fetch_records`` is itself the coroutine the stage wraps).
            resp = await client.get(
                "https://example.org/degraded",
                deadline=time.monotonic() + budget,
            )
            return resp, client.last_failure

        resp, failure = await asyncio.wait_for(_call(), timeout=budget)
        assert resp is None
        assert calls["count"] == 1
        assert failure is not None
        assert failure.kind == "http"
        assert failure.status == 503
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_spent_deadline_does_not_inherit_an_earlier_calls_failure():
    """A call that issues no request must not report the previous call's status.

    ``last_failure`` is cleared only on success, and its ``ContextScoped`` dict
    is shared with child tasks, so a stale ``FetchFailure`` outlives the call
    that produced it. Reporting it from the bailout hands the caller an HTTP
    status this call never received -- and the §2 classification is made from
    exactly that status, so a stage that opened no socket would be recorded as
    an ``origin_outage`` against the host.
    """
    client, calls = _client(_always_503, max_retries=1)
    try:
        await client.get("https://example.org/degraded")
        assert client.last_failure is not None
        assert client.last_failure.status == 503

        assert (
            await client.get(
                "https://example.org/degraded", deadline=time.monotonic()
            )
            is None
        )
        # The second call issued nothing, so the first call's attempt is still
        # the only one on the transport.
        assert calls["count"] == 1
        failure = client.last_failure
        assert failure is not None
        assert failure.kind == "transport"
        assert failure.status is None
        assert failure.detail == "DeadlineExceeded"
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


async def test_client_default_timeout_bounds_connect_not_read():
    """D9 client-wide: connect gets ``connect_timeout_s``; the phases that
    legitimately need the full ``request_timeout`` -- read, write, pool --
    keep it. A 30 s connect is never the desired behaviour anywhere; the
    read phase is untouched either way."""
    client = AsyncHttpClient(settings=Settings(request_timeout=30))
    try:
        timeout = client.client.timeout
        assert timeout.connect == 5.0  # settings.connect_timeout_s default
        assert timeout.read == 30.0
        assert timeout.write == 30.0
        assert timeout.pool == 30.0
    finally:
        await client.aclose()


async def test_deadline_clamp_preserves_the_connect_bound():
    """The clamp must not re-raise connect to ``min(request_timeout, remaining)``.

    A scalar per-attempt timeout bounds connect at the whole remaining budget,
    so one hopeless connect consumes the entire ceiling -- the exact failure
    D9 exists to stop (a dead INCA host burned 30 s of a 30 s budget in one
    connect). Read keeps the budget-clamped value; connect keeps its own bound.
    """
    client, calls = _client(_always_503, request_timeout=30, max_retries=1)
    try:
        await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 20.0
        )
        installed = calls["timeouts"][0]
        assert installed["connect"] <= 5.0
        assert 19.0 < installed["read"] <= 20.0
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()


async def test_ladder_retries_after_a_connect_failure():
    """The point of bounding connect rather than lowering the ceiling: a
    transient connect failure is still retried when the budget allows."""
    attempts = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectTimeout("Connection timed out")
        return httpx.Response(200, text="ok")

    client, calls = _client(flaky, max_retries=4, backoff_base=0.01)
    try:
        resp = await client.get(
            "https://example.org/flaky", deadline=time.monotonic() + 30.0
        )
        assert resp is not None
        assert resp.status_code == 200
        assert calls["count"] == 3
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()
