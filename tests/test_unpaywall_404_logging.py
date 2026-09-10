import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.unpaywall import UnpaywallProvider
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.utils.http import AsyncHttpClient


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_unpaywall_fetch_full_text_404_no_warning(client, caplog):
    respx.get("https://api.unpaywall.org/v2/10.1093/humupd/dmab061").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    provider = UnpaywallProvider(client, email="test@example.com")
    with caplog.at_level("WARNING", logger="scholar_mcp.utils.http"):
        res = await provider.fetch_full_text(IdentifierMap(doi="10.1093/humupd/dmab061"))
    assert res is None
    assert len(caplog.records) == 0


@respx.mock
async def test_resolver_fetch_pdf_bytes_unpaywall_404_no_warning(client, caplog):
    settings = Settings(unpaywall_email="test@example.com", enable_scihub=False)
    resolver = WaterfallResolver(settings=settings, http_client=client)

    respx.get("https://api.unpaywall.org/v2/10.1093/humupd/dmab061").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with caplog.at_level("WARNING", logger="scholar_mcp.utils.http"):
        bytes_data, source = await resolver.fetch_pdf_bytes(IdentifierMap(doi="10.1093/humupd/dmab061"))

    assert bytes_data is None
    assert source is None
    assert len(caplog.records) == 0
