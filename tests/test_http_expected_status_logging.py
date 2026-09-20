import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient, RETRYABLE_STATUS_CODES
from log_helpers import HTTP_LOGGER, http_records as _http_records


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


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


@respx.mock
async def test_get_bytes_forwards_quiet_statuses(client, caplog):
    """A stale open-access URL 404s routinely; the caller must be able to say so."""
    route = respx.get("https://example.org/gone.pdf").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        data = await client.get_bytes("https://example.org/gone.pdf", quiet_statuses={404})

    assert route.called
    assert data is None
    assert _http_records(caplog, "WARNING") == []
    assert len(_http_records(caplog, "DEBUG")) == 1


@respx.mock
async def test_get_bytes_without_quiet_statuses_still_warns(client, caplog):
    route = respx.get("https://example.org/gone2.pdf").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        data = await client.get_bytes("https://example.org/gone2.pdf")

    assert route.called
    assert data is None
    assert len(_http_records(caplog, "WARNING")) == 1


@respx.mock
async def test_retryable_statuses_default_retries_four_times(retrying_client, caplog):
    """Control: default retryable_statuses retries a 500 up to max_retries."""
    route = respx.get("https://example.org/internal-error-retried").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await retrying_client.get("https://example.org/internal-error-retried")

    assert route.call_count == 4
    assert resp is None
    assert len(_http_records(caplog, "WARNING")) == 1


@respx.mock
async def test_custom_retryable_statuses_fast_fails_and_respects_quiet(retrying_client, caplog):
    route = respx.get("https://example.org/internal-error-fast-fail").mock(
        return_value=httpx.Response(500, text="No XML available")
    )
    with caplog.at_level("DEBUG", logger=HTTP_LOGGER):
        resp = await retrying_client.get(
            "https://example.org/internal-error-fast-fail",
            retryable_statuses=RETRYABLE_STATUS_CODES - {500},
            quiet_statuses={500},
        )

    assert route.call_count == 1
    assert resp is None
    assert _http_records(caplog, "WARNING") == []
    assert len(_http_records(caplog, "DEBUG")) == 1


@respx.mock
async def test_empty_set_retryable_statuses_disables_all_retries(retrying_client):
    """retryable_statuses=set() is falsy; `is not None` must be used so it disables retries."""
    route = respx.get("https://example.org/falsy-empty-set").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    resp = await retrying_client.get(
        "https://example.org/falsy-empty-set",
        retryable_statuses=set(),
    )
    assert route.call_count == 1
    assert resp is None
