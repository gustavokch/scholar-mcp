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
    """Records emitted by the HTTP client only, optionally filtered by level.

    `caplog.records` collects every logger that propagates to root, so an
    unfiltered assertion couples these tests to unrelated modules staying quiet.
    """
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
    assert resp is not None
    assert resp.status_code == 404
    assert _http_records(caplog, "WARNING") == []
    assert len(_http_records(caplog, "DEBUG")) == 1


@respx.mock
async def test_ok_status_below_400_logs_nothing(client, caplog):
    """A 2xx listed in ok_statuses is an ordinary success, not an expected miss."""
    route = respx.get("https://example.org/fine").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await client.get("https://example.org/fine", ok_statuses={200})

    assert route.called
    assert resp is not None
    assert resp.status_code == 200
    assert _http_records(caplog) == []


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


@respx.mock
async def test_no_quiet_statuses_keeps_warning_on_404(client, caplog):
    """Opting out must be explicit: an unannotated 404 is still a warning."""
    route = respx.get("https://example.org/plain").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await client.get("https://example.org/plain")

    assert route.called
    assert resp is None
    assert len(_http_records(caplog, "WARNING")) == 1
