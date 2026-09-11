import httpx
import pytest
import respx
from log_helpers import HTTP_LOGGER, assert_no_http_warnings, http_records

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.unpaywall import UnpaywallProvider
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.utils.http import AsyncHttpClient

MISSING_DOI = "10.1093/humupd/dmab061"
UNPAYWALL_URL = f"https://api.unpaywall.org/v2/{MISSING_DOI}"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_unpaywall_fetch_full_text_404_no_warning(client, caplog):
    route = respx.get(UNPAYWALL_URL).mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    provider = UnpaywallProvider(client, email="test@example.com")
    with caplog.at_level("WARNING", logger=HTTP_LOGGER):
        res = await provider.fetch_full_text(IdentifierMap(doi=MISSING_DOI))

    assert route.called
    assert res is None
    assert_no_http_warnings(caplog)


@respx.mock
async def test_unpaywall_fetch_full_text_500_still_warns(client, caplog):
    """Only 404 is an expected miss; a server error must stay loud."""
    route = respx.get(UNPAYWALL_URL).mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    provider = UnpaywallProvider(client, email="test@example.com")
    with caplog.at_level("WARNING", logger=HTTP_LOGGER):
        res = await provider.fetch_full_text(IdentifierMap(doi=MISSING_DOI))

    assert route.called
    assert res is None
    assert len(http_records(caplog, "WARNING")) == 1


@respx.mock
async def test_resolver_fetch_pdf_bytes_unpaywall_404_no_warning(client, caplog):
    settings = Settings(unpaywall_email="test@example.com", enable_scihub=False)
    resolver = WaterfallResolver(settings=settings, http_client=client)

    route = respx.get(UNPAYWALL_URL).mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("WARNING", logger=HTTP_LOGGER):
        bytes_data, source = await resolver.fetch_pdf_bytes(IdentifierMap(doi=MISSING_DOI))

    assert route.called
    assert bytes_data is None
    assert source is None
    assert_no_http_warnings(caplog)


@respx.mock
async def test_resolver_fetch_pdf_bytes_unpaywall_500_still_warns(client, caplog):
    settings = Settings(unpaywall_email="test@example.com", enable_scihub=False)
    resolver = WaterfallResolver(settings=settings, http_client=client)

    route = respx.get(UNPAYWALL_URL).mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with caplog.at_level("WARNING", logger=HTTP_LOGGER):
        bytes_data, source = await resolver.fetch_pdf_bytes(IdentifierMap(doi=MISSING_DOI))

    assert route.called
    assert bytes_data is None
    assert source is None
    assert len(http_records(caplog, "WARNING")) == 1


@respx.mock
async def test_fetch_oa_pdf_url_prefers_url_for_pdf(client):
    route = respx.get("https://api.unpaywall.org/v2/10.1000/x").mock(
        return_value=httpx.Response(
            200,
            json={
                "is_oa": True,
                "best_oa_location": {
                    "url_for_pdf": "https://example.org/paper.pdf",
                    "url": "https://example.org/landing",
                },
            },
        )
    )
    provider = UnpaywallProvider(client, email="test@example.com")

    assert route  # bound so a URL typo fails here rather than silently
    assert (
        await provider.fetch_oa_pdf_url(IdentifierMap(doi="10.1000/x"))
        == "https://example.org/paper.pdf"
    )
    assert route.called


@respx.mock
async def test_fetch_oa_pdf_url_falls_back_to_landing_url(client):
    respx.get("https://api.unpaywall.org/v2/10.1000/z").mock(
        return_value=httpx.Response(
            200,
            json={"is_oa": True, "best_oa_location": {"url": "https://example.org/landing"}},
        )
    )
    provider = UnpaywallProvider(client, email="test@example.com")

    assert (
        await provider.fetch_oa_pdf_url(IdentifierMap(doi="10.1000/z"))
        == "https://example.org/landing"
    )


@respx.mock
async def test_fetch_oa_pdf_url_none_when_closed_access(client):
    respx.get("https://api.unpaywall.org/v2/10.1000/y").mock(
        return_value=httpx.Response(200, json={"is_oa": False})
    )
    provider = UnpaywallProvider(client, email="test@example.com")

    assert await provider.fetch_oa_pdf_url(IdentifierMap(doi="10.1000/y")) is None


async def test_fetch_oa_pdf_url_none_without_email(client):
    """No email means no Unpaywall request at all; respx is not even engaged."""
    provider = UnpaywallProvider(client, email=None)

    assert await provider.fetch_oa_pdf_url(IdentifierMap(doi="10.1000/x")) is None


@respx.mock
async def test_resolver_fetch_pdf_bytes_uses_unpaywall_provider(client):
    """The resolver must go through the provider, not a second inline lookup."""
    settings = Settings(unpaywall_email="test@example.com", enable_scihub=False)
    resolver = WaterfallResolver(settings=settings, http_client=client)

    lookup = respx.get("https://api.unpaywall.org/v2/10.1000/x").mock(
        return_value=httpx.Response(
            200,
            json={
                "is_oa": True,
                "best_oa_location": {"url_for_pdf": "https://example.org/paper.pdf"},
            },
        )
    )
    pdf = respx.get("https://example.org/paper.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 body")
    )

    bytes_data, source = await resolver.fetch_pdf_bytes(IdentifierMap(doi="10.1000/x"))

    assert lookup.called
    assert pdf.called
    assert bytes_data == b"%PDF-1.4 body"
    assert source == "unpaywall"
